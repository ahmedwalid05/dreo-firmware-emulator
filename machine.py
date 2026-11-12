#!/usr/bin/env python3
"""Full-system bring-up harness for the Dreo BK7231N (ARMv5TE) firmware.

This boots the real image from its reset vector (0x10000, ARM) and runs it under
Unicorn. We don't know the full SoC memory map up front, so this harness
*discovers* it: any access to unmapped memory is auto-mapped as a logged MMIO
page and execution resumes. A coarse page-level trace shows how far boot gets,
and a spin detector flags the tight polling loops that mean "a peripheral ready
bit we must model" (classic bring-up).

Run:  python3 machine.py [image]   (default: firmware/app.bin)
Output: boot page trace, MMIO regions touched, and where (if) it spins.
"""
import collections
import sys

from unicorn import (UC_ARCH_ARM, UC_HOOK_BLOCK, UC_HOOK_CODE,
                     UC_HOOK_MEM_UNMAPPED, UC_MODE_ARM, Uc, UcError)
from unicorn.arm_const import UC_ARM_REG_PC

LOAD_BASE = 0x10000


class Machine:
    def __init__(self, image_path, load_base=LOAD_BASE):
        self.img = open(image_path, "rb").read()
        self.base = load_base
        self.uc = Uc(UC_ARCH_ARM, UC_MODE_ARM)
        isz = (len(self.img) + 0xFFF) & ~0xFFF
        self.uc.mem_map(load_base & ~0xFFF, isz + 0x1000)
        self.uc.mem_write(load_base, self.img)
        self.mmio = collections.OrderedDict()   # region base -> hit count
        self.mapped_dyn = set()
        # MMIO model: anything >= 0x00800000 is a peripheral register (RAM/code
        # all live below). Status polls want "ready" bits, so unknown reads
        # return all-ones by default; per-address overrides handle the rest
        # (e.g. a bit the firmware waits to see CLEARED -> override to 0).
        self.MMIO_BASE = 0x00800000
        self.mmio_default = 0xFFFFFFFF
        # per-register models discovered during bring-up:
        self.mmio_overrides = {
            0x800040: 0x00000000,   # UART/bus busy+FIFO-count status: wait-until-clear (tst #0x7f)
            0x800074: 0x00000000,   # bus/periph busy: wait-until-(reg & mask)==0 (0x25442, post-scheduler thread)
            0x802c00: 0x40000000,   # ack/done reg: bit8 wait-clear (0x24daa), bit30 wait-set (0x252cc, live r1=0x40000000)
        }
        # registers whose bit the firmware waits to see both set AND cleared
        # (TX busy/ready) -> alternate the given mask on each read
        self.mmio_toggle = {0x800010: 0x40}
        self._toggle_state = {}
        # free-running counter/timer registers: return an increasing positive value
        self.mmio_counter = {0x803000}
        self._counter = 0
        self.mmio_read_log = collections.Counter()
        # discovery/trace state
        self.pages = []                           # ordered distinct code pages
        self.seen_pages = set()
        self.icount = 0
        # run-state (also set in run(); defaulted here so the hooks are safe)
        self.stop_reason = None
        self.max_insns = 1 << 62
        self.spin_reads = 20000
        self._last_mmio = -1
        self._mmio_run = 0
        from unicorn import UC_HOOK_MEM_READ
        self.uc.hook_add(UC_HOOK_MEM_UNMAPPED, self._on_unmapped)
        self.uc.hook_add(UC_HOOK_BLOCK, self._on_block)
        # range-restricted: only MMIO reads reach Python (RAM reads stay in C)
        self.uc.hook_add(UC_HOOK_MEM_READ, self._on_read,
                         begin=self.MMIO_BASE, end=0xFFFFFFFF)

    def enable_rtos_and_wifi(self):
        """Install the two enablers that carry the module boot through the
        RT-Thread scheduler and into the Wi-Fi MAC init:

        1. SWI handler — `svc #0` (0x5f2e0) is RT-Thread's context-switch to the
           first thread. Unicorn raises UC_ERR_EXCEPTION on it; this hook emulates
           ARM SWI entry (SVC mode, LR_svc=return, SPSR_svc=CPSR, PC=0x10760) so
           the real handler runs and the scheduler starts threads.
        2. Wi-Fi chip/dev-ID inject — `rwnxl_init` reads the chip/dev ID over an
           indirect MAC register bus and panics "Unsupported chip or dev" on the
           placeholder values. At the ID-reader return (0xa7a44, r4=result struct)
           inject a supported pair (chip 0x7231c, dev 0x20150414) so init proceeds.
        """
        from unicorn import UC_HOOK_CODE, UC_HOOK_INTR
        from unicorn.arm_const import (UC_ARM_REG_CPSR, UC_ARM_REG_LR,
                                       UC_ARM_REG_PC, UC_ARM_REG_R4, UC_ARM_REG_SPSR)
        import struct as _s
        SWI_VEC = 0x10760

        def _on_intr(uc, intno, user):
            pc = uc.reg_read(UC_ARM_REG_PC)
            cpsr = uc.reg_read(UC_ARM_REG_CPSR)
            uc.reg_write(UC_ARM_REG_CPSR, (cpsr & ~0x1F) | 0x13 | 0x80)  # SVC mode, IRQ off
            uc.reg_write(UC_ARM_REG_LR, pc)
            try:
                uc.reg_write(UC_ARM_REG_SPSR, cpsr)
            except Exception:  # noqa: BLE001
                pass
            uc.reg_write(UC_ARM_REG_PC, SWI_VEC)
        self.uc.hook_add(UC_HOOK_INTR, _on_intr)

        def _inject_chip(uc, a, s, u):
            r4 = uc.reg_read(UC_ARM_REG_R4)
            uc.mem_write(r4, _s.pack("<I", 0x7231C))       # chip id
            uc.mem_write(r4 + 8, _s.pack("<I", 0x20150414))  # device id
        self.uc.hook_add(UC_HOOK_CODE, _inject_chip, begin=0xA7A44, end=0xA7A44)

    def _on_read(self, uc, access, address, size, value, _):
        # model the peripheral read: inject the chosen value so the fetch returns it
        self.mmio_read_log[address] += 1
        if address in self.mmio_counter:
            self._counter = (self._counter + 0x100) & 0x7FFFFFFF
            val = self._counter
        elif address in self.mmio_toggle:
            # a busy/ready status bit the firmware waits to both SET and CLEAR at
            # different points — alternate it so every such poll eventually passes
            self._toggle_state[address] = self._toggle_state.get(address, 0) ^ self.mmio_toggle[address]
            val = self._toggle_state[address]
        elif address in self.mmio_overrides:
            val = self.mmio_overrides[address]
        elif 0x00800000 <= address < 0x00900000:
            # SCTRL / UART / ICU block: ready bits are active-high here, so the
            # all-ones default lets "wait-for-set" polls pass (e.g. 0x802108 bit20).
            val = self.mmio_default
        else:
            # Everything above 0x00900000 — BLE/XVR (0x0090xxxx), RF/PHY
            # (0x0105xxxx), Wi-Fi MAC (nxmac 0xc00xxxxx): status/done/busy regs
            # read idle = 0. All-ones trips every "done/idle/self-clear" check
            # (nxmac_soft_reset @0xc0008050, RF-cali @0x1050004, BLE @0x9000f8).
            val = 0
        try:
            uc.mem_write(address, val.to_bytes(4, "little")[:size] or b"\x00")
        except Exception:  # noqa: BLE001
            pass
        # spin = the same status register hammered with no other MMIO activity
        if address == self._last_mmio:
            self._mmio_run += 1
            if self._mmio_run > self.spin_reads:
                self.stop_reason = ("spin", address)
                uc.emu_stop()
        else:
            self._last_mmio = address
            self._mmio_run = 1

    # ---- dynamic mapping of the unknown SoC map ------------------------
    def _region(self, addr):
        return addr & ~0xFFFFF                     # 1 MB bucket for logging

    def _on_unmapped(self, uc, access, address, size, value, _):
        page = address & ~0xFFF
        if page not in self.mapped_dyn:
            try:
                uc.mem_map(page, 0x1000)
            except UcError:
                return False
            self.mapped_dyn.add(page)
        reg = self._region(address)
        self.mmio[reg] = self.mmio.get(reg, 0) + 1
        return True                                # retry the access

    # ---- coarse boot trace + spin detection ----------------------------
    def _on_block(self, uc, address, size, _):
        self.icount += max(1, size // 4)
        page = address & ~0xFFF
        if page not in self.seen_pages:
            self.seen_pages.add(page)
            self.pages.append((self.icount, address))
        if self.icount >= self.max_insns:
            self.stop_reason = ("maxinsns", address)
            uc.emu_stop()

    def run(self, max_insns=5_000_000, spin_reads=20000):
        self.max_insns = max_insns
        self.spin_reads = spin_reads
        self._last_mmio = -1
        self._mmio_run = 0
        self.stop_reason = None
        self.uc.reg_write(UC_ARM_REG_PC, self.base)
        try:
            self.uc.emu_start(self.base, 0)   # single-shot; hooks stop it
        except UcError as e:
            return ("error", (self.uc.reg_read(UC_ARM_REG_PC), str(e)))
        return self.stop_reason or ("halted", self.uc.reg_read(UC_ARM_REG_PC))

    def report(self):
        print(f"instructions executed: ~{self.icount}")
        print(f"\nboot page trace (first entry into each code page):")
        for ic, addr in self.pages[:60]:
            tag = "" if self.base <= addr < self.base + len(self.img) else "  [non-image]"
            print(f"  @~{ic:>9} insns  pc={addr:#010x}{tag}")
        print(f"\nMMIO 1MB regions touched (addr bucket : hits):")
        for reg, n in sorted(self.mmio.items()):
            print(f"  {reg:#010x} : {n}")
        if self.mmio_read_log:
            print(f"\nmost-read MMIO registers (candidate status/poll regs):")
            for addr, n in self.mmio_read_log.most_common(10):
                print(f"  {addr:#010x} : {n} reads"
                      f"{'  <- OVERRIDE'+hex(self.mmio_overrides[addr]) if addr in self.mmio_overrides else ''}")


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else \
        __import__("os").path.join(__import__("os").path.dirname(__file__),
                                   "firmware", "app.bin")
    m = Machine(path)
    print(f"booting {path} at {LOAD_BASE:#x} (ARM)…\n")
    why, where = m.run()
    m.report()
    print(f"\nstopped: {why}  at/with {where if isinstance(where,str) else hex(where) if isinstance(where,int) else where}")


if __name__ == "__main__":
    main()
