# dreo-fw-emu

Emulate **real Dreo smart-fan firmware** under [Unicorn](https://www.unicorn-engine.org/).
This runs the *actual* firmware instructions — not a reimplementation — for both
chips in the fan:

- the **Wi-Fi module** — Beken **BK7231N**, ARMv5TE, RT-Thread (`app.bin`)
- the **motor MCU** — **TS32Fx Cortex-M0** (`mcu.bin`)

It boots the module through its whole stack (RTOS → Wi-Fi → RF-cal → TCP/IP → BLE →
sockets) to **pairing mode**, and runs the MCU's `55AA` UART protocol end to end —
all on a flash image you supply, with no hardware and nothing to brick.

> This is a research / right-to-repair tool for firmware **you own**. It does not
> distribute vendor firmware, contains no device secrets, and does not exploit or
> talk to any real device or cloud — it only executes a firmware image locally.

## Install

```bash
pip install unicorn capstone
```

## Firmware (you supply it)

Vendor firmware isn't redistributed here. Drop your own images (from your own
device) into `firmware/` as `app.bin` and `mcu.bin` — see `firmware/README.md`
for what they are and the SHA-256 the captured proof was made against.

## Demos

Run from the repo root once `firmware/app.bin` / `firmware/mcu.bin` are in place:

| Command | What it shows |
|---|---|
| `python3 mcu.py` | **MCU 55AA conversation** — boots the MCU firmware; its real main loop receives a fake-module frame, parses it, and replies (version `001+mcu+0.0.1`, `0x07` status report, valid checksums) |
| `python3 validate.py` | **Parity** — runs the real `/ota` parser vs a model on 8 inputs; agrees on all, incl. a real off-by-one in the firmware the naive model gets wrong |
| `python3 machine.py` | **Module full-system boot** — boots the BK7231N image from reset, discovers the SoC memory map, traces how far it gets |
| `python3 pairing_boot.py` | **Pairing mode** — module boots the full stack, then its real `app_demo_tcp_main` server runs `socket → bind → listen → accept → recv` |
| `python3 pairing_http.py` | **Serves the pairing page** — the booted firmware's real HTTP handler returns the actual `/model.html` upload page |
| `python3 pairing.py` | **HTTP routing** — the real request dispatcher's full route table (GET/POST) |

See **[PROOF.md](PROOF.md)** for captured real-run output.

## How it works (short)

`emu.py` is a small Unicorn harness: it maps the image at its load base, models
peripherals on demand, and runs real functions with the real ABI. `machine.py`
adds full-system module bring-up — it discovers the memory map, models the ready
bits the firmware polls, handles the RT-Thread **SWI scheduler-start**, injects a
supported Wi-Fi chip ID, and fast-forwards the RF-cal delay, which carries the real
firmware all the way into pairing mode. `mcu.py` boots the Cortex-M0 MCU and drives
its real `55AA` parser/processor/builder via the UART RX ring.

## Honest scope

- **Function / handler level is ground truth** — real instructions, real results.
- **Full-system boot** reaches pairing mode by modeling the peripherals the firmware
  touches plus a small in-RAM socket layer for the server demos. The fully
  *autonomous* preemptive-scheduling path (OS-tick IRQ) is **not** emulated —
  Unicorn's generic ARM core doesn't faithfully do ARM9 exception return
  (banked `ldm {..}^` + SPSR), so the server demos drive the real server code from
  the booted state rather than waiting on the timer tick. Everything executed is
  real firmware; that one scheduling mechanism is the known gap.
- Results are tied to the firmware build you use (see `firmware/README.md` hashes).
