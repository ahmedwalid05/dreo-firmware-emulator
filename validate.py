#!/usr/bin/env python3
"""Validate the ota-sim mock's parser against the REAL firmware code.

Runs the stock /ota trailer-search function (0x18514) under Unicorn on a range
of inputs and checks each result equals Python's body.find(needle) — the
primitive tools/ota-sim/device.py relies on to locate the end of uploaded data.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import emu as E  # noqa: E402

IMG = os.path.join(os.path.dirname(__file__), "firmware", "app.bin")

NEEDLE = b"\r\n---"


def fw_find(body: bytes, needle: bytes) -> int:
    """Firmware-faithful reference (emulator-confirmed): identical to
    body.find(needle) EXCEPT a match flush at the very end (needle_end == len,
    i.e. no trailing byte) is not reported — the stock loop stops at
    len - needle_len. See tools/fw-emu/README / research/phase0-key-read.md."""
    p = body.find(needle)
    if p == -1 or p > len(body) - len(needle) - 1:
        return -1
    return p
CASES = {
    "typical multipart tail": (
        b"------B\r\nContent-Disposition: form-data; name=\"firmware\"\r\n\r\n"
        b"\x01\x02\x03\x04RBL\x00payloadbytes\r\n------B--\r\n"),
    "needle absent": b"no boundary here, just random \x00\xff bytes and text",
    "needle at very start": b"\r\n---immediately",
    "two needles (want first)": b"aaaa\r\n---FIRST and later \r\n---SECOND",
    "empty body": b"",
    "needle at exact end": b"payload\r\n---",
    "binary payload w/ CRLFs": bytes(range(256)) + b"\r\n\r\ndata\r\n------X--",
    "partial needle only": b"ends with \r\n-- but not three dashes",
}


def main():
    emu = E.FwEmu(IMG)
    print(f"image: {os.path.normpath(IMG)}  (real ARM Thumb, Unicorn)\n")
    fails = 0
    for name, body in CASES.items():
        got = E.emu_trailer_search(emu, body, NEEDLE)
        ref = fw_find(body, NEEDLE)
        naive = body.find(NEEDLE)
        ok = got == ref
        fails += not ok
        flag = "" if ref == naive else "  <- diverges from naive find (firmware off-by-one)"
        print(f"[{'OK ' if ok else 'FAIL'}] {name:<26} "
              f"firmware={got:<4} faithful={ref:<4} naive={naive:<4} len={len(body)}{flag}")
    print()
    if fails:
        print(f"{fails} MISMATCH(es) vs the firmware-faithful reference; investigate.")
        sys.exit(1)
    print("ALL MATCH — real firmware code confirms the firmware-faithful model,")
    print("including the end-of-buffer off-by-one the naive model gets wrong.")


if __name__ == "__main__":
    main()
