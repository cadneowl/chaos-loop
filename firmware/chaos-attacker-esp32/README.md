# chaos-attacker-esp32

Minimal firmware sketch for the **attack ESP32** used in Phase 2 of the
NeoOwl hardware-chaos bench. Receives JSON commands over USB serial,
emits the requested RF interference, reports status back.

This sketch is intentionally minimal — its job is to be the bench-side
counterpart to `HilHardwareIO`'s serial protocol, not a production
appliance. The serious version migrates to
[`neoowl-defense`](https://github.com/cadneowl/neoowl-defense) once
neoowl's RF-emission certification scope is settled.

## Hardware

- 1 × **ESP32-WROOM-32** (any module with the standard radio works;
  ESP32-S3 also fine).
- USB-to-serial bridge (CP2102 / CH340 / native USB).
- For LoRa support: external **SX1276** or **SX1262** module wired over
  SPI to GPIO5 (CS), GPIO18 (SCK), GPIO23 (MOSI), GPIO19 (MISO),
  GPIO14 (RST), GPIO26 (DIO0).

## Build (PlatformIO)

```bash
cd firmware/chaos-attacker-esp32
pio run -t upload          # build + flash
pio device monitor -b 115200
```

## Wire protocol

One JSON object per line, both directions. Lines are LF-terminated; CR
is tolerated but stripped. Commands and their replies:

| Command | Reply on success |
|---|---|
| `{"cmd":"info"}` | `{"ok":true, "firmware":"<ver>", "serial":"<id>", "hardware":"<rev>", "mode":"BENCH"}` |
| `{"cmd":"inject", "fault":"<name>", "params":{…}, "duration_seconds":<n>}` | `{"ok":true, "handle":"<id>"}` |
| `{"cmd":"cleanup", "handle":"<id>"}` | `{"ok":true}` |
| `{"cmd":"reset"}` | `{"ok":true}` |

On error: `{"ok":false, "error":"<reason>"}`.

The Python side ([`agents/chaos/hardware_io.py`](../../agents/chaos/hardware_io.py)
`HilHardwareIO`) generates these requests and parses the responses
identically to the simulator path, so a plan that runs against
`SimulatedHardwareIO` runs unchanged against this firmware.

### Supported faults

Phase 2 ships four:

| `fault` | `params` keys | What the firmware does |
|---|---|---|
| `wifi.deauth` | `target_bssid` (str, "auto"), `channel` (int 1–14), `intensity` (low/med/high) | Broadcasts 802.11 deauth frames |
| `wifi.jam` | `channel` (int, 0=sweep), `power_dbm` (int), `sweep_period_ms` (int) | Sweeps a noise carrier across 2.4 GHz |
| `ble.advertising_flood` | `rate_per_second` (int), `spoofed_macs` (int), `adv_data_size` (int) | High-rate BLE adv from cycling spoofed MACs |
| `lora.jam` | `frequency_hz` (int), `bandwidth_hz` (int), `spreading_factor` (int 7–12), `power_dbm` (int) | Continuous unmodulated carrier on the configured LoRa channel |

## Safety

The firmware refuses every `inject` if it boots in non-BENCH mode (set
via a GPIO pull-up — open = BENCH, grounded = PRODUCTION). Production
mode reports `{"ok":false,"error":"production-mode"}` for every chaos
command. The Python-side hardware safety gates double-check this
discriminator before issuing any fault.

`power_dbm` parameters are clamped at compile time to the
operator-declared maximum (`MAX_TX_DBM` in `src/main.cpp`). If a plan
requests more than the configured cap, the firmware emits at the cap
and the reply includes `"clamped":true` so the audit trail captures
the discrepancy.

## License

Lives under the parent repo's Apache-2.0. The serious-deployment
firmware that lives in `neoowl-defense` may be licensed differently —
this sketch is bench-only.
