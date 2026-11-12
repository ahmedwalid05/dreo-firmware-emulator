#!/usr/bin/env python3
"""Drive the REAL module pairing/OTA HTTP dispatcher (0x188AC) with requests.

The Beken module's pairing AP serves a tiny HTTP server whose URI dispatcher is
at 0x188AC (htf004s build; our fan's build renames the /model* routes to /ota).
It reads the request line from the global buffer at 0x3FE390 and strncmp/strstr-
branches to a route handler. We write a request there, run the real dispatcher,
and report which handler it dispatched to — i.e. the real pairing-mode routing,
executing real firmware instructions. Route map from research/module-firmware-report.md.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import machine as M  # noqa: E402
from unicorn import UC_HOOK_CODE, UcError  # noqa: E402
from unicorn.arm_const import (UC_ARM_REG_LR, UC_ARM_REG_PC, UC_ARM_REG_R0,  # noqa: E402
                               UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R4,
                               UC_ARM_REG_SP)

REQBUF = 0x003FE390
DISPATCH = 0x188AC
SENTINEL = 0x70000000
STACK = 0x00390000

ROUTES = {
    0x1865C: "upload state machine  (POST /model|/mcu  ->  firmware write to flash)",
    0x17AF8: "/a.rbl downtest       (GET  dumps staged image)",
    0x17B8C: "serve OTA form        (GET  /model.html  ->  the pairing upload page)",
    0x17C10: "serve MCU-OTA form    (GET  /mcu.html)",
    0x17D48: "appinfoset            (POST account JSON: userID/token/register)",
    0x17ECC: "wifiinfoset           (POST SSID/Key  ->  join Wi-Fi)",
    0x1800C: "otaset                (POST OTA params)",
    0x18314: "wifilist              (GET  scan list)",
    0x183C0: "devinfoget            (GET  pair/mac/ver/mode)",
    0x18164: "otaLocalUp            (POST {otastr:url})",
}


def drive(req: bytes):
    m = M.Machine(os.path.join(os.path.dirname(__file__), "firmware", "app.bin"))
    m.mmio_overrides[0x800010] = 0          # UART TX ready (so console printf won't hang)
    for page in (SENTINEL & ~0xFFF, REQBUF & ~0xFFF, STACK & ~0xFFF,
                 (STACK - 0x1000) & ~0xFFF):
        try:
            m.uc.mem_map(page, 0x1000)
            m.mapped_dyn.add(page)
        except UcError:
            pass
    # request line into the global buffer the dispatcher reads
    m.uc.mem_write(REQBUF, req + b"\x00")
    m.uc.reg_write(UC_ARM_REG_SP, STACK)
    m.uc.reg_write(UC_ARM_REG_LR, SENTINEL | 1)
    for r, v in ((UC_ARM_REG_R0, REQBUF), (UC_ARM_REG_R1, 0),
                 (UC_ARM_REG_R2, 0), (UC_ARM_REG_R4, REQBUF)):
        m.uc.reg_write(r, v)

    hit = {"route": None}

    def route_hook(uc, address, size, _):
        a = address & ~1
        if a in ROUTES and hit["route"] is None:
            hit["route"] = a
            uc.emu_stop()
        elif a == (SENTINEL & ~1):
            uc.emu_stop()
    m.uc.hook_add(UC_HOOK_CODE, route_hook)
    m.max_insns = 2_000_000
    try:
        m.uc.emu_start(DISPATCH | 1, 0)
    except UcError:
        pass
    return hit["route"]


def main():
    reqs = [
        (b"GET /model.html HTTP/1.1\r\nHost: 192.168.0.1\r\n\r\n",
         "browser opens the pairing page"),
        (b"POST /model.html HTTP/1.1\r\nContent-Length: 4096\r\n\r\n--x\r\n",
         "app uploads firmware (the OTA write path)"),
        (b"GET /devinfoget HTTP/1.1\r\n\r\n",
         "app queries device info during pairing"),
        (b"POST /wifiinfoset HTTP/1.1\r\nContent-Length: 40\r\n\r\n{\"SSID\":\"x\"}",
         "app hands the module Wi-Fi credentials"),
        (b"GET /nonsense HTTP/1.1\r\n\r\n",
         "unknown path (should fall through to 404)"),
    ]
    print("=== real module pairing-mode HTTP dispatcher (0x188AC), real firmware ===\n")
    for req, desc in reqs:
        line = req.split(b"\r\n", 1)[0].decode()
        route = drive(req)
        where = ROUTES.get(route, "no route matched -> 404 default") if route else \
            "no route matched -> 404 default"
        print(f"  {line:<40} {desc}")
        print(f"     -> dispatched to {hex(route) if route else '(default)'}: {where}\n")


if __name__ == "__main__":
    main()
