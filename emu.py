#!/usr/bin/env python3
"""A real (Unicorn) emulator engine for Dreo BK7231N firmware functions.

Unlike tools/ota-sim/ (a behavioral Python mock), this EXECUTES the actual ARM
Thumb instructions from the decrypted firmware image. Use it to validate — or
correct — the mock against ground truth, one function at a time.

Scope: leaf/pure functions (string, CRC, parse) that touch only CPU + RAM. The
whole image is mapped at its load base (0x10000), so in-image callees (strlen,
memcmp, …) just run. Functions that hit MMIO (flash driver, UART) must be
stubbed via `stubs=`.

ABI: ARM EABI — args in r0..r3, return in r0, return via LR.
"""
from unicorn import (UC_ARCH_ARM, UC_HOOK_CODE, UC_HOOK_MEM_UNMAPPED,
                     UC_MODE_THUMB, Uc)
from unicorn.arm_const import (UC_ARM_REG_LR, UC_ARM_REG_PC, UC_ARM_REG_R0,
                               UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3,
                               UC_ARM_REG_SP)

LOAD_BASE = 0x10000

# scratch RAM region, clear of the image and of the SoC MMIO windows
# (0x004xxxxx peripherals, 0x0E2xxxxx flash-ctrl, 0x0E33xxxx WDT)
SCRATCH = 0x80000000
SCRATCH_SZ = 0x00100000
STACK_TOP = SCRATCH + 0x00090000
BUF = SCRATCH + 0x00010000
NEEDLE = SCRATCH + 0x00008000
SENTINEL = SCRATCH + 0x000A0000   # LR target: return lands here, we stop


class FwEmu:
    def __init__(self, image_path: str, load_base: int = LOAD_BASE,
                 trace: bool = False):
        self.img = open(image_path, "rb").read()
        self.base = load_base
        self.trace = trace
        self.uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
        size = (len(self.img) + 0xFFF) & ~0xFFF
        self.uc.mem_map(load_base & ~0xFFF, size + 0x1000)
        self.uc.mem_write(load_base, self.img)
        self.uc.mem_map(SCRATCH, SCRATCH_SZ)
        self._stubs: dict[int, int] = {}
        self.uc.hook_add(UC_HOOK_CODE, self._code_hook)
        self.uc.hook_add(UC_HOOK_MEM_UNMAPPED, self._bad_mem)

    # ---- hooks ----------------------------------------------------------
    def _code_hook(self, uc, address, size, _):
        if self.trace:
            print(f"  pc={address:#08x}")
        a = address & ~1
        if a == (SENTINEL & ~1):
            uc.emu_stop()
            return
        if a in self._stubs:                     # skip a call, resume elsewhere
            uc.reg_write(UC_ARM_REG_PC, self._stubs[a] | 1)

    def _bad_mem(self, uc, access, address, size, value, _):
        pc = uc.reg_read(UC_ARM_REG_PC)
        raise RuntimeError(f"unmapped mem access @ {address:#x} (pc={pc:#x}) — "
                           f"likely an MMIO call needing a stub")

    # ---- call primitive -------------------------------------------------
    def call(self, func: int, r0=0, r1=0, r2=0, r3=0, stubs=None):
        """Run `func` with the given args; return r0. stubs: {call_addr: resume_addr}."""
        self._stubs = dict(stubs or {})
        self.uc.reg_write(UC_ARM_REG_SP, STACK_TOP)
        self.uc.reg_write(UC_ARM_REG_LR, SENTINEL | 1)
        for reg, val in ((UC_ARM_REG_R0, r0), (UC_ARM_REG_R1, r1),
                         (UC_ARM_REG_R2, r2), (UC_ARM_REG_R3, r3)):
            self.uc.reg_write(reg, val & 0xFFFFFFFF)
        self.uc.emu_start(func | 1, SENTINEL & ~1)
        return self.uc.reg_read(UC_ARM_REG_R0)

    def write(self, addr: int, data: bytes):
        self.uc.mem_write(addr, data)


# ---------------------------------------------------------------------------
# Driver for the stock /ota trailer search @ 0x18514:
#   char* find_substr(char* start r0, int len r1, char* needle r2)
#   -> pointer to first occurrence of needle, or 0 if absent.
# strlen=0x5e30c, memcmp=0x5e21c (pure, run as-is); printf=0x26a88 (not-found
# path) stubbed to resume at 0x1854c ("movs r0,#0; return 0").
# ---------------------------------------------------------------------------
TRAILER_SEARCH = 0x18514
PRINTF = 0x26a88
AFTER_PRINTF = 0x1854c


def emu_trailer_search(emu: "FwEmu", body: bytes, needle: bytes = b"\r\n---") -> int:
    """Return the offset of `needle` in `body` as the REAL firmware computes it,
    or -1 if not found."""
    emu.write(BUF, body)
    emu.write(NEEDLE, needle + b"\x00")
    ret = emu.call(TRAILER_SEARCH, r0=BUF, r1=len(body), r2=NEEDLE,
                   stubs={PRINTF: AFTER_PRINTF})
    return -1 if ret == 0 else ret - BUF
