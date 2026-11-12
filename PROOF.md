# Proof of parity

Captured real runs of this emulator. Reproducible against these exact images
(SHA-256 — see `firmware/README.md`):

```
app.bin  50ec899324c4e9cd045b64b37d2880b08fc8576495a5dc1bcda184517925e6b9
mcu.bin  ec2fe33a01ac01927455f9c84874b2b2c3b9056b92acef1ddab8b497a6ec1be0
```

## 1. MCU 55AA conversation — `mcu.py`  (`proof/mcu.txt`)

The real MCU firmware boots; its own main loop drains an injected frame through the
real parser and replies — spec-exact, valid checksums:

```
module -> MCU : 55 aa 00 00 01 00 00 00 00                 (version query)
  real main loop: parser 0x0CCC x9 -> latched cmd 0x01
MCU -> module : 55 aa 00 00 01 00 00 0d 30 30 31 2b 6d 63 75 2b 30 2e 30 2e 31 26
                payload "001+mcu+0.0.1", checksum 0x26
module -> MCU : 55 aa 00 00 08 00 00 00 07                 (report-all-state)
MCU -> module : 55 aa 00 00 07 00 00 40 <64-byte TLV state> ba   (cmd 0x07, checksum 0xba)
```

## 2. Parser parity — `validate.py`  (`proof/validate.txt`)

Real `/ota` trailer-search vs a model across 8 inputs: all match, **including a
real off-by-one in the firmware** (its loop stops at `len - needle_len`) that the
naive model gets wrong — the payoff of running real code.

## 3. Pairing mode — serves the real page — `pairing_http.py`  (`proof/pairing.txt`)

The booted firmware's own HTTP handler (`0x188AC` dispatch -> `0x17B8C`) returns the
actual `/model.html` upload page:

```html
<!DOCTYPE html>...<title>DR-HTF001</title>...<form method="POST" enctype="multipart/form-data">
<h3>     MODEL OTA</h3><h2>model=</h2><input type="file" name="firmware"><input type="submit" value="update"></form>...
```

See `proof/` for the full captured logs.
