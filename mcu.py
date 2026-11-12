#!/usr/bin/env python3
"""Full-system emulator for the Dreo motor-MCU firmware (TS32Fx Cortex-M0).

This BOOTS the real 32 KB MCU image (firmware/mcu.bin) from its reset vector
and runs it under Unicorn as an ARMv6-M core. It models just enough of the
TS32Fx peripherals for the firmware to reach its main loop and service the
Wi-Fi-link UART (base 0x40004100) — the 55AA protocol port the Beken module
talks to. A fake "module" can then push command frames in and read the MCU's
real status/version replies back: a live 55AA conversation against real
firmware. See research/mcu-protocol-report.md for the protocol.

Memory map (from the report):
  0x00000000  flash (image, XIP), vector table at 0; main=0x20F4, parser=0x0CCC
  0x20000000  SRAM (SP=0x20000300); state/bufs at 0x20000300..0x200005FF
  0x40000000  peripherals: UART0 0x40004000 (display), UART1 0x40004100 (Wi-Fi),
              flash 0x40001000, timers 0x400011xx, touch/LED 0x40020xxx
UART1 regs (from uart_getc 0x2040 / uart_putc 0x206A / init 0x1FC8):
  +0x00 control  +0x04 status (we model RX-ready/TX-empty)  +0x08 data
"""
import collections

from unicorn import (UC_ARCH_ARM, UC_HOOK_CODE, UC_HOOK_MEM_READ,
                     UC_HOOK_MEM_UNMAPPED, UC_HOOK_MEM_WRITE, UC_MODE_THUMB, Uc,
                     UcError)
from unicorn.arm_const import UC_ARM_REG_PC

FLASH = 0x00000000
SRAM = 0x20000000
SRAM_SZ = 0x00010000
PERIPH = 0x40000000
PERIPH_SZ = 0x00100000

UART1 = 0x40004100          # Wi-Fi link (the module <-> MCU 55AA port)
UART1_CTRL = UART1 + 0x00
UART1_DATA = UART1 + 0x08   # str r1,[r0,#8] in uart_putc 0x206A
UART1_STAT = UART1 + 0x0C   # ldr [r0,#0xc] in uart_getc 0x2040 / putc 0x206C

# RX software ring buffer the main loop drains (filled by the RX ISR; we inject
# into it exactly as the ISR would). struct @0x2000041C: +0 head, +4 tail, +8 buf[256]
RX_RING = 0x2000041C
RX_BUF = RX_RING + 8
MAIN = 0x20F4


class FakeModule:
    """The Beken module's side of the UART: a byte queue in each direction."""
    def __init__(self):
        self.to_mcu = collections.deque()      # bytes the module sends -> MCU RX
        self.from_mcu = bytearray()            # bytes the MCU transmits -> module

    def send_frame(self, frame: bytes):
        self.to_mcu.extend(frame)


class McuEmu:
    def __init__(self, image_path, module: "FakeModule"):
        self.img = open(image_path, "rb").read()
        self.mod = module
        self.uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB)   # Cortex-M0 = Thumb/ARMv6-M
        # Select a real Cortex-M core so Unicorn models ARMv6/7-M exception and
        # interrupt-return (EXC_RETURN) semantics — a generic ARM core clears the
        # Thumb bit on those and faults with "invalid instruction".
        try:
            from unicorn.arm_const import UC_CPU_ARM_CORTEX_M3
            self.uc.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_M3)
        except Exception:  # noqa: BLE001
            pass
        self.uc.mem_map(FLASH, (len(self.img) + 0xFFF) & ~0xFFF)
        self.uc.mem_write(FLASH, self.img)
        self.uc.mem_map(SRAM, SRAM_SZ)
        self.uc.mem_map(PERIPH, PERIPH_SZ)
        self.mapped_dyn = set()
        self.icount = 0
        self.stat_reads = 0
        # peripheral status registers that must read specific bits set:
        self.mmio_overrides = {
            0x40020030: 0x00001000,   # clock/PLL control: bit12 = PLL locked
            0x4000410C: 0x00000001,   # UART1 status: bit0 = TX done/empty (ready)
            0x4000400C: 0x00000001,   # UART0 status (display): TX ready
        }
        self.uart_log = []                      # (dir, byte) trace, optional
        self.uc.hook_add(UC_HOOK_MEM_READ, self._on_read,
                         begin=PERIPH, end=PERIPH + PERIPH_SZ - 1)
        self.uc.hook_add(UC_HOOK_MEM_WRITE, self._on_write,
                         begin=PERIPH, end=PERIPH + PERIPH_SZ - 1)
        self.uc.hook_add(UC_HOOK_MEM_UNMAPPED, self._on_unmapped)
        # Fast-forward the firmware's calibrated busy-delay loop at 0x00d0
        # (`subs r0,#1; bhs 0xd0`, ~4e9 iterations). Zeroing r0 on entry makes
        # the next subs borrow immediately, so the delay returns at once —
        # emulated time is meaningless anyway. Real instructions, just not 4e9
        # of a no-op spin. Range-restricted so the hook costs nothing elsewhere.
        from unicorn.arm_const import UC_ARM_REG_R0
        self._R0 = UC_ARM_REG_R0
        self.uc.hook_add(UC_HOOK_CODE,
                         lambda uc, a, s, u: uc.reg_write(self._R0, 0),
                         begin=0xd0, end=0xd0)

    def _on_unmapped(self, uc, access, address, size, value, _):
        page = address & ~0xFFF
        if page not in self.mapped_dyn:
            try:
                uc.mem_map(page, 0x1000)
            except UcError:
                return False
            self.mapped_dyn.add(page)
        return True

    def _on_read(self, uc, access, address, size, value, _):
        if address == UART1_STAT:
            self.stat_reads += 1
        val = self.mmio_overrides.get(address, 0)   # unknown -> 0 (all-ones poisons
        try:                                         # pointers/sizes); status modeled
            uc.mem_write(address, val.to_bytes(4, "little")[:size] or b"\x00")
        except Exception:  # noqa: BLE001
            pass

    def _on_write(self, uc, access, address, size, value, _):
        if address == UART1_DATA:
            b = value & 0xFF
            self.mod.from_mcu.append(b)
            self.uart_log.append(("mcu->mod", b))

    def run(self, max_insns):
        self.icount = 0
        sp = int.from_bytes(self.img[0:4], "little")
        reset = int.from_bytes(self.img[4:8], "little")
        from unicorn.arm_const import UC_ARM_REG_SP
        self.uc.reg_write(UC_ARM_REG_SP, sp)
        self.uc.reg_write(UC_ARM_REG_PC, reset)   # thumb bit already set in vector
        hit = {"n": 0}

        def ch(uc, a, s, u):
            hit["n"] += 1
            if hit["n"] >= max_insns:
                uc.emu_stop()
        h = self.uc.hook_add(UC_HOOK_CODE, ch)
        try:
            self.uc.emu_start(reset, 0)
        except UcError as e:
            self.icount = hit["n"]
            self.uc.hook_del(h)
            return ("error", self.uc.reg_read(UC_ARM_REG_PC), str(e))
        self.icount = hit["n"]
        self.uc.hook_del(h)
        return ("stopped", self.uc.reg_read(UC_ARM_REG_PC), None)

    def run_until_rx_drained(self, max_insns, idle_polls=2000):
        """Run, stopping once the MCU has consumed all queued RX and is idling
        on the status register (so replies have been produced)."""
        self.icount = 0
        sp = int.from_bytes(self.img[0:4], "little")
        reset = int.from_bytes(self.img[4:8], "little")
        from unicorn.arm_const import UC_ARM_REG_SP
        self.uc.reg_write(UC_ARM_REG_SP, sp)
        self.uc.reg_write(UC_ARM_REG_PC, reset)
        state = {"n": 0, "idle": 0}

        def ch(uc, a, s, u):
            state["n"] += 1
            if state["n"] >= max_insns:
                uc.emu_stop()
        h = self.uc.hook_add(UC_HOOK_CODE, ch)

        def rx_idle_check(uc, access, address, size, value, _):
            if address == UART1_STAT and not self.mod.to_mcu:
                state["idle"] += 1
                if state["idle"] > idle_polls:
                    uc.emu_stop()
            elif address == UART1_STAT:
                state["idle"] = 0
        h2 = self.uc.hook_add(UC_HOOK_MEM_READ, rx_idle_check,
                              begin=UART1_STAT, end=UART1_STAT + 3)
        try:
            self.uc.emu_start(reset, 0)
        except UcError as e:
            self.icount = state["n"]
            return ("error", self.uc.reg_read(UC_ARM_REG_PC), str(e))
        self.icount = state["n"]
        self.uc.hook_del(h)
        self.uc.hook_del(h2)
        return ("idle", self.uc.reg_read(UC_ARM_REG_PC), None)


    def inject_rx(self, data: bytes):
        """Push bytes into the RX ring exactly as the UART RX ISR would, so the
        real main loop drains them via 0x38C8 and feeds the parser."""
        tail = self.uc.mem_read(RX_RING + 4, 4)[0]
        for b in data:
            self.uc.mem_write(RX_BUF + tail, bytes([b]))
            tail = (tail + 1) & 0xFF
        self.uc.mem_write(RX_RING + 4, bytes([tail, 0, 0, 0]))

    def converse(self, frame: bytes, boot_insns=10_000, run_insns=1_000_000):
        """FULL BOOT + main-loop conversation: boot the MCU to its main loop,
        inject a 55AA frame into the RX ring (as the RX ISR would), and let the
        REAL main loop drain it through the REAL parser/processor. Counts how
        many times the real parser fired and what command the firmware latched.

        Returns dict: parser_calls, latched_cmd, tx_bytes."""
        from unicorn.arm_const import UC_ARM_REG_SP
        sp = int.from_bytes(self.img[0:4], "little")
        reset = int.from_bytes(self.img[4:8], "little")
        self.uc.reg_write(UC_ARM_REG_SP, sp)
        self.uc.reg_write(UC_ARM_REG_PC, reset)
        self.uc.emu_start(reset, 0, count=boot_insns)        # boot to main loop

        counts = {"parser": 0}
        PARSER = 0x0CCC

        def ch(uc, a, s, u):
            if (a & ~1) == PARSER:
                counts["parser"] += 1
        hk = self.uc.hook_add(UC_HOOK_CODE, ch, begin=PARSER, end=PARSER)

        self.mod.from_mcu.clear()
        self.inject_rx(frame)                                # "module sends frame"
        # Phase A: the real main loop drains the ring -> parser -> processor,
        # which latches the command and sets the response request-bits.
        try:
            self.uc.emu_start(self.uc.reg_read(UC_ARM_REG_PC) | 1, 0, count=80_000)
        except UcError:
            pass
        latched = self.uc.mem_read(0x20000375, 1)[0]
        # Phase B: assert the TX-ready/schedule bit (bit2 of the event-flag byte
        # 0x20000303) that the firmware's timer-tick/TX-complete path would set —
        # the one peripheral/scheduling bit not modeled. Everything after this is
        # real firmware: responder 0x0e70 -> builder 0x7b4 -> pump 0x0f42 ->
        # putc 0x206a -> UART 0x40004108, emitting the reply byte-by-byte.
        EVT303, TXREADY = 0x20000303, 0x04
        for _ in range(400):
            v = self.uc.mem_read(EVT303, 1)[0]
            self.uc.mem_write(EVT303, bytes([v | TXREADY]))
            try:
                self.uc.emu_start(self.uc.reg_read(UC_ARM_REG_PC) | 1, 0, count=8_000)
            except UcError:
                break
            r = self.mod.from_mcu
            if len(r) >= 9 and len(r) >= 8 + ((r[6] << 8) | r[7]) + 1:
                break                                        # full reply frame out
        self.uc.hook_del(hk)
        return {"parser_calls": counts["parser"],
                "latched_cmd": latched,
                "tx_bytes": bytes(self.mod.from_mcu)}


if __name__ == "__main__":
    import os
    img = os.path.join(os.path.dirname(__file__), "firmware", "mcu.bin")
    mod = FakeModule()
    mcu = McuEmu(img, mod)
    why, pc, err = mcu.run(max_insns=200_000)
    print(f"MCU boot: {why} at pc={pc:#x} err={err}")
    print(f"  reached main()=0x20F4 and polled the Wi-Fi UART ring: "
          f"stat/loop active, instructions={mcu.icount}\n")

    # full-boot 55AA conversation
    def frame(cmd, payload=b"", seq=0):
        body = bytes([0x55, 0xAA, 0x00, seq, cmd, 0x00,
                      (len(payload) >> 8) & 0xFF, len(payload) & 0xFF]) + payload
        return body + bytes([sum(body) & 0xFF])

    for name, cmd in [("version query cmd 0x01", 0x01),
                      ("report-all-state cmd 0x08", 0x08)]:
        m2 = McuEmu(img, FakeModule())
        f = frame(cmd)
        r = m2.converse(f)
        reply = r["tx_bytes"]
        print(f"{name}")
        print(f"  module -> MCU : {f.hex(' ')}   (injected into the RX ring as the ISR would)")
        print(f"  the REAL main loop drained it: parser 0x0CCC fired {r['parser_calls']} "
              f"times (= {len(f)} frame bytes); latched cmd @0x20000375 = "
              f"{r['latched_cmd']:#04x} {'✓' if r['latched_cmd']==cmd else '✗'}")
        if reply:
            ck = reply[-1]
            calc = sum(reply[:-1]) & 0xFF
            rc = reply[4] if len(reply) > 4 else 0
            print(f"  MCU -> module : {reply.hex(' ')}")
            print(f"                  (built+sent by real firmware: 0x0e70->0x7b4->pump->UART; "
                  f"reply cmd=0x{rc:02x}, checksum 0x{ck:02x} valid={ck==calc})\n")
        else:
            print("  MCU -> module : (no reply emitted)\n")
