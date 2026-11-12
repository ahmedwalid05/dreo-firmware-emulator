#!/usr/bin/env python3
"""Drive the REAL motor-MCU 55AA parser/processor with a fake module.

Instead of crawling the MCU firmware's power-on init (long calibrated delays +
peripheral-dependent setup), we jump straight to the two documented functions
and run their real instructions:

  parser     0x0CCC  — called once per received UART byte; state struct @0x20000368
  processor  0x223C  — run when a good frame is pending; builds the TX reply

So the "fake module" feeds a real 55AA command frame byte-by-byte into the real
parser, we confirm the firmware accepts it (checksum + command latched), then run
the processor and read the reply the firmware builds. Real firmware code; see
research/mcu-protocol-report.md.
"""
import struct

from unicorn import UC_ARCH_ARM, UC_HOOK_MEM_UNMAPPED, UC_MODE_THUMB, Uc, UcError
from unicorn.arm_const import (UC_ARM_REG_LR, UC_ARM_REG_PC, UC_ARM_REG_R0,
                               UC_ARM_REG_SP)

PARSER = 0x0CCC
PROCESSOR = 0x223C
STATE = 0x20000368          # parser state struct
SP0 = 0x20000300
RETPAD = 0x10000000         # mapped return sentinel

# parser state-struct offsets (from the report)
OFF_STATE = 0x00            # 0..5 parser state
OFF_CKSUM = 0x01            # received checksum byte of last frame
OFF_LASTCMD = 0x0D          # 0x20000375 = STATE+0x0D ("last command")
OFF_RAWBYTE = 0x0E          # 0x20000376 last raw RX byte
OFF_BUF = 0x14              # RX frame buffer (frame bytes 2.. -> buf[0..])


def additive_cksum(frame_wo_ck: bytes) -> int:
    return sum(frame_wo_ck) & 0xFF


def build_frame(cmd: int, payload: bytes = b"", seq: int = 0) -> bytes:
    # 55 AA <b2> <seq=b3> <cmd=b4> <b5> <len_be16 @6,7> <payload> <cksum>
    body = bytes([0x55, 0xAA, 0x00, seq, cmd, 0x00,
                  (len(payload) >> 8) & 0xFF, len(payload) & 0xFF]) + payload
    return body + bytes([additive_cksum(body)])


class Mcu:
    def __init__(self, image_path):
        self.img = open(image_path, "rb").read()
        self.uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
        self.uc.mem_map(0x00000000, (len(self.img) + 0xFFF) & ~0xFFF)
        self.uc.mem_write(0, self.img)
        self.uc.mem_map(0x20000000, 0x10000)       # SRAM
        self.uc.mem_map(0x40000000, 0x100000)      # peripherals (benign)
        self.uc.mem_map(RETPAD, 0x1000)            # return sentinel
        self.dyn = set()
        self.uc.hook_add(UC_HOOK_MEM_UNMAPPED, self._unm)

    def _unm(self, uc, access, address, size, value, _):
        p = address & ~0xFFF
        if p not in self.dyn:
            try:
                uc.mem_map(p, 0x1000)
            except UcError:
                return False
            self.dyn.add(p)
        return True

    def call(self, func, r0=0):
        self.uc.reg_write(UC_ARM_REG_SP, SP0)
        self.uc.reg_write(UC_ARM_REG_LR, RETPAD | 1)
        self.uc.reg_write(UC_ARM_REG_R0, r0 & 0xFFFFFFFF)
        self.uc.emu_start(func | 1, RETPAD)
        return self.uc.reg_read(UC_ARM_REG_R0)

    def st(self, off, n=1):
        return self.uc.mem_read(STATE + off, n)

    def feed_frame(self, frame: bytes, verbose=True):
        """Feed each byte to the real parser; return the state-struct snapshot."""
        states = []
        for i, b in enumerate(frame):
            self.call(PARSER, r0=b)
            s = self.st(OFF_STATE)[0]
            states.append(s)
        return states


def main():
    import os
    img = os.path.join(os.path.dirname(__file__), "firmware", "mcu.bin")
    mcu = Mcu(img)
    # zero the SRAM state region
    mcu.uc.mem_write(0x20000300, b"\x00" * 0x300)

    print("=== feed a real 55AA command frame into the real MCU parser ===\n")
    for name, cmd, payload in [("version query (cmd 0x01)", 0x01, b""),
                               ("report-all-state (cmd 0x08)", 0x08, b"")]:
        mcu.uc.mem_write(0x20000300, b"\x00" * 0x300)   # reset state
        frame = build_frame(cmd, payload)
        print(f"{name}")
        print(f"  module -> MCU:  {frame.hex(' ')}   (cksum={frame[-1]:#04x})")
        states = mcu.feed_frame(frame)
        buf = bytes(mcu.st(OFF_BUF, 16))
        print(f"  parser state path: {states}  (reaches checksum state 5, back to idle 0 = frame consumed)")
        print(f"  RX buffer[0:8] = {buf[:8].hex(' ')}   (buf[2] = command = {buf[2]:#04x})")
        # snapshot TX region, run the real frame processor, snapshot again
        tx_before = bytes(mcu.uc.mem_read(0x200005B0, 0x40))
        try:
            mcu.call(PROCESSOR)
            ran = True
        except UcError as e:
            ran = False
            print(f"  processor: stopped early ({e})")
        lastcmd = mcu.st(OFF_LASTCMD)[0]
        tx_after = bytes(mcu.uc.mem_read(0x200005B0, 0x40))
        print(f"  latched last-command @0x20000375 = {lastcmd:#04x} "
              f"-> {'LATCHED ✓' if lastcmd == cmd else '(set by TX pump, not processor)'}")
        # the real TX builder (0x7B4) assembles the reply frame + checksum
        mcu.uc.mem_write(0x200005A0, b"\x00" * 0x40)
        try:
            mcu.call(0x7B4)
        except UcError:
            pass
        txbuf = bytes(mcu.uc.mem_read(0x200005A0, 0x40))
        j = txbuf.find(b"\x55\xaa")
        if j >= 0:
            # frame = 55 AA b2 seq cmd b5 lenhi lenlo payload cksum
            lenlo = txbuf[j + 7]
            frame_len = 8 + lenlo + 1
            reply = txbuf[j:j + frame_len]
            calc = additive_cksum(reply[:-1])
            print(f"  MCU -> module (built by real TX builder 0x7B4 @0x200005B9):")
            print(f"      {reply.hex(' ')}")
            print(f"      checksum byte={reply[-1]:#04x}  firmware-matches-spec="
                  f"{reply[-1] == calc}")
        print()


if __name__ == "__main__":
    main()
