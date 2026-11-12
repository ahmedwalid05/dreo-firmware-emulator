#!/usr/bin/env python3
"""Serve the real /model.html PAIRING PAGE from the fully-booted module firmware.

Boots the module to its parked full-stack state (globals initialized), then
redirects — in the same emu_start — into the real HTTP request dispatcher
`0x188AC` with a `GET /model.html` request in the global request buffer
(0x3FE390). The real route handler `0x17B8C` builds the page and calls
`send` (`0x2C94C`); we capture those bytes and stop at the first send.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import machine as M  # noqa: E402
from unicorn import UC_HOOK_BLOCK, UC_HOOK_CODE, UcError  # noqa: E402
from unicorn.arm_const import (UC_ARM_REG_CPSR, UC_ARM_REG_LR, UC_ARM_REG_PC,  # noqa: E402
                               UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2,
                               UC_ARM_REG_R4, UC_ARM_REG_SP)

DISPATCH = 0x188AC
SEND = 0x2C94C
PAGE_HANDLER = 0x17B8C
REQBUF = 0x3FE390
SENT = 0x70000000
DELAY = 0x364A4
PRINTF = 0x269F4
REQ = b"GET /model.html HTTP/1.1\r\nHost:192.168.0.1\r\n\r\n\x00"


def main():
    img = os.path.join(os.path.dirname(__file__), "firmware", "app.bin")
    m = M.Machine(img)
    m.spin_reads = 10 ** 9
    m.enable_rtos_and_wifi()
    m.uc.hook_add(UC_HOOK_CODE, lambda uc, a, s, u: uc.reg_write(UC_ARM_REG_R0, 0),
                  begin=DELAY, end=DELAY)
    try:
        m.uc.mem_map(SENT & ~0xFFF, 0x1000)
        m.mapped_dyn.add(SENT & ~0xFFF)
    except UcError:
        pass
    m.uc.mem_write(SEND, b"\x70\x47")   # send -> bx lr; capture in hook

    phase = ["boot"]
    parked = [False]
    sent = bytearray()
    hit = []

    def onp(uc, a, s, u):
        if (a & ~1) == PRINTF:
            o = b""
            try:
                for i in range(48):
                    b = uc.mem_read(uc.reg_read(UC_ARM_REG_R0) + i, 1)
                    if b == b"\x00":
                        break
                    o += b
            except Exception:  # noqa: BLE001
                pass
            if b"zero socket" in o:
                parked[0] = True
    m.uc.hook_add(UC_HOOK_CODE, onp, begin=PRINTF, end=PRINTF)

    def onpage(uc, a, s, u):
        hit.append("route -> serve /model.html handler 0x17B8C")
    m.uc.hook_add(UC_HOOK_CODE, onpage, begin=PAGE_HANDLER, end=PAGE_HANDLER)

    def onsend(uc, a, s, u):
        r1 = uc.reg_read(UC_ARM_REG_R1)
        r2 = uc.reg_read(UC_ARM_REG_R2) & 0xFFFF
        try:
            sent.extend(uc.mem_read(r1, min(r2, 4096)))
        except Exception:  # noqa: BLE001
            pass
        uc.reg_write(UC_ARM_REG_R0, r2)
        uc.emu_stop()                   # got the page -> stop
    m.uc.hook_add(UC_HOOK_CODE, onsend, begin=SEND, end=SEND)

    K = [0]

    def blk(uc, a, s, u):
        K[0] += 1
        if phase[0] == "boot":
            if parked[0]:
                m.uc.mem_write(REQBUF, REQ)
                sp = uc.reg_read(UC_ARM_REG_SP)
                uc.reg_write(UC_ARM_REG_SP, (sp - 0x1000) & ~7)
                uc.reg_write(UC_ARM_REG_LR, SENT | 1)
                uc.reg_write(UC_ARM_REG_CPSR, uc.reg_read(UC_ARM_REG_CPSR) | 0x20)
                for r, v in ((UC_ARM_REG_R0, REQBUF), (UC_ARM_REG_R1, 5),
                             (UC_ARM_REG_R2, 5), (UC_ARM_REG_R4, REQBUF)):
                    uc.reg_write(r, v)
                uc.reg_write(UC_ARM_REG_PC, DISPATCH | 1)
                phase[0] = "http"
                K[0] = 0
            elif K[0] > 9_000_000:
                uc.emu_stop()
        elif (a & ~1) == (SENT & ~1) or K[0] > 800_000:
            uc.emu_stop()
    m.uc.hook_add(UC_HOOK_BLOCK, blk)
    m.uc.reg_write(UC_ARM_REG_PC, m.base)
    try:
        m.uc.emu_start(m.base, 0)
    except UcError as e:
        print("stop:", e, "pc", hex(m.uc.reg_read(UC_ARM_REG_PC)), "phase", phase[0])

    print("parked:", parked[0], " route hits:", hit)
    print(f"\n=== /model.html PAIRING PAGE served by booted firmware ({len(sent)} bytes) ===")
    print(bytes(sent[:1000]).decode("latin-1", "replace"))


if __name__ == "__main__":
    main()
