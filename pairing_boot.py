#!/usr/bin/env python3
"""Boot the module firmware to PAIRING MODE and drive its real TCP server.

This is the full-system result: the Beken module image boots under Unicorn
(machine.Machine.enable_rtos_and_wifi() + the MMIO region policy + the RF-cal
delay fast-forward), runs its entire stack (RTOS + Wi-Fi + RF-cal + TCP/IP + BLE
+ sockets), and then we drive its real server entry `app_demo_tcp_main`
(`0x1659C`) with a modeled socket layer. The firmware's OWN code executes:

    socket(AF_INET, SOCK_STREAM) -> bind(port 5001) -> listen()   <- pairing server up
    -> select/accept(conn) -> recv(client request)

i.e. the firmware enters pairing mode, stands up its listening server, accepts a
client connection, and receives the request — all real firmware instructions.

Modeled socket syscalls (identified in the server loop):
  socket 0x2C9E0  bind 0x2C464  listen 0x2C608  select 0x2CA4C
  accept 0x2C324  setsockopt 0x2CD8C  recv 0x2C860  send 0x2C94C  close 0x2C4C4
"""
import struct
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import machine as M  # noqa: E402
from unicorn import UC_HOOK_BLOCK, UC_HOOK_CODE, UcError  # noqa: E402
from unicorn.arm_const import (UC_ARM_REG_CPSR, UC_ARM_REG_LR, UC_ARM_REG_PC,  # noqa: E402
                               UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2,
                               UC_ARM_REG_SP)

SERVER = 0x1659C
SENT = 0x70000000
DELAY = 0x364A4
PRINTF = 0x269F4
SYS = {0x2C9E0: "socket", 0x2C464: "bind", 0x2C608: "listen", 0x2CA4C: "select",
       0x2C324: "accept", 0x2CD8C: "setsockopt", 0x2C860: "recv",
       0x2C94C: "send", 0x2C4C4: "close"}
REQ = b"GET /model.html HTTP/1.1\r\nHost:192.168.0.1\r\n\r\n"


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
    for a in SYS:
        m.uc.mem_write(a, b"\x70\x47")       # patch each syscall -> `bx lr`

    phase = ["boot"]
    parked = [False]
    ev = []
    st = {"conn": 4, "recvd": set(), "sel": 0}

    def onp(uc, a, s, u):
        if (a & ~1) == PRINTF:
            o = b""
            try:
                for i in range(40):
                    b = uc.mem_read(uc.reg_read(UC_ARM_REG_R0) + i, 1)
                    if b == b"\x00":
                        break
                    o += b
            except Exception:  # noqa: BLE001
                pass
            if b"zero socket" in o:
                parked[0] = True
    m.uc.hook_add(UC_HOOK_CODE, onp, begin=PRINTF, end=PRINTF)

    def sysc(uc, a, s, u):
        a &= ~1
        if a not in SYS:
            return
        R = UC_ARM_REG_R0
        r0 = uc.reg_read(UC_ARM_REG_R0)
        r1 = uc.reg_read(UC_ARM_REG_R1)
        if a == 0x2C9E0:
            uc.reg_write(R, 4)
        elif a == 0x2C464:
            try:
                port = struct.unpack(">H", uc.mem_read(r1 + 2, 2))[0]
            except Exception:  # noqa: BLE001
                port = -1
            ev.append("bind      -> port %d" % port)
            uc.reg_write(R, 0)
        elif a == 0x2C608:
            ev.append("listen    -> PAIRING SERVER UP (backlog %d)" % r1)
            uc.reg_write(R, 0)
        elif a == 0x2CA4C:
            st["sel"] += 1
            uc.reg_write(R, 0 if st["sel"] > 6 else 1)
        elif a == 0x2C324:
            st["conn"] += 1
            try:
                uc.mem_write(r1, struct.pack("<HH", 2, 0) + bytes(12))
            except Exception:  # noqa: BLE001
                pass
            ev.append("accept    -> client connected, conn fd %d" % st["conn"])
            uc.reg_write(R, st["conn"])
        elif a == 0x2CD8C:
            uc.reg_write(R, 0)
        elif a == 0x2C860:
            if r0 >= 5 and r0 not in st["recvd"]:
                st["recvd"].add(r0)
                uc.mem_write(r1, REQ)
                ev.append("recv fd %d -> %d-byte client request" % (r0, len(REQ)))
                uc.reg_write(R, len(REQ))
            else:
                uc.reg_write(R, 0)
        elif a == 0x2C94C:
            ev.append("send fd %d -> %d bytes" % (r0, uc.reg_read(UC_ARM_REG_R2)))
            uc.reg_write(R, uc.reg_read(UC_ARM_REG_R2))
        elif a == 0x2C4C4:
            uc.reg_write(R, 0)
    m.uc.hook_add(UC_HOOK_CODE, sysc, begin=0x2C324, end=0x2CD8C)

    K = [0]

    def blk(uc, a, s, u):
        K[0] += 1
        if phase[0] == "boot":
            if parked[0]:
                sp = uc.reg_read(UC_ARM_REG_SP)
                uc.reg_write(UC_ARM_REG_SP, (sp - 0x1000) & ~7)
                uc.reg_write(UC_ARM_REG_LR, SENT | 1)
                uc.reg_write(UC_ARM_REG_CPSR, uc.reg_read(UC_ARM_REG_CPSR) | 0x20)
                uc.reg_write(UC_ARM_REG_PC, SERVER | 1)
                phase[0] = "server"
                K[0] = 0
            elif K[0] > 9_000_000:
                uc.emu_stop()
        elif (a & ~1) == (SENT & ~1) or K[0] > 5_000_000:
            uc.emu_stop()
    m.uc.hook_add(UC_HOOK_BLOCK, blk)
    m.uc.reg_write(UC_ARM_REG_PC, m.base)
    try:
        m.uc.emu_start(m.base, 0)
    except UcError as e:
        print("stop:", e)

    print("=== module boots full stack, then runs app_demo_tcp_main (real firmware) ===\n")
    print("   firmware entered pairing mode:" if parked[0] else "   (did not reach parked state)")
    for e in ev[:14]:
        print("     ", e)


if __name__ == "__main__":
    main()
