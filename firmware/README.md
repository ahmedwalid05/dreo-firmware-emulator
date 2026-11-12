# Put your firmware images here

This repo does **not** distribute Dreo firmware (vendor copyright). Supply your
own images, extracted from **your own device** (e.g. a read-only UART flash dump
with `bk7231tools`, then carve the partitions):

- `app.bin` — the Beken **BK7231N Wi-Fi module** Application partition (ARMv5TE,
  RT-Thread). Loaded at `0x10000`.
- `mcu.bin` — the **TS32Fx Cortex-M0 motor-MCU** firmware (32 KB, vector table
  at 0).

The demos were validated against these exact images (verify yours match):

```
app.bin  sha256 50ec899324c4e9cd045b64b37d2880b08fc8576495a5dc1bcda184517925e6b9
mcu.bin  sha256 ec2fe33a01ac01927455f9c84874b2b2c3b9056b92acef1ddab8b497a6ec1be0
```

Other builds will boot too, but offsets/results may differ from the captured proof.
