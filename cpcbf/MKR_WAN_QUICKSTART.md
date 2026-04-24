# MKR WAN 1300 — LoRa Quick Start

End-to-end benchmarking of RSSI, RTT, and throughput over raw point-to-point
LoRa (SX1276 via Sandeep Mistry's library — **not** LoRaWAN) between two
MKR WAN 1300 boards.

## Prerequisites

- 2× Arduino MKR WAN 1300 boards
- 2× **LoRa antennas** connected to the U.FL connector (transmitting without
  an antenna can damage the radio)
- 2× Raspberry Pi (any model) as USB-serial bridges
- USB cables connecting each Arduino to its bridge RPi
- Ethernet connectivity from your workstation to both RPis
- PlatformIO CLI (`pip install platformio`)
- Python 3.10+ with `pyserial` on the bridge RPis (`pip install pyserial`)

## 1. Flash the per-test firmware

The MKR WAN 1300 has only 32 KB of SRAM, so each test (RSSI / RTT / flood)
ships as its own firmware — memory-tuned to the buffers that test actually
needs. Flash the env that matches the plan you want to run.

| Plan                                               | PlatformIO env              |
|----------------------------------------------------|-----------------------------|
| `plans/arduino/lora/lora_mkr1300_rssi.yaml`        | `mkrwan1300_lora_rssi`      |
| `plans/arduino/lora/lora_mkr1300_rtt.yaml`         | `mkrwan1300_lora_rtt`       |
| `plans/arduino/lora/lora_mkr1300_flood.yaml`       | `mkrwan1300_lora_flood`     |

Each plan YAML also lists its env in a top-level `firmware:` field — re-check
there if you're unsure.

```bash
cd cpcbf/agent/platforms/arduino_mkr_wan1300
pio run -e mkrwan1300_lora_rssi -t upload --upload-port /dev/ttyACM0
```

Repeat for the second Arduino. Switching between test modes requires
re-flashing the matching firmware.

## 2. Verify serial communication

On a bridge RPi:

```bash
sudo apt install -y python3-serial screen
screen /dev/ttyACM0 115200
# Type: {"command":"RADIO_STATUS"}
# Expect: {"status":"ok","message":"radio status",
#          "data":{"lora_active":false,"wifi_active":false,"bluetooth_active":false}}
```

To exit `screen`: `Ctrl-A` then `K`, confirm `y`.

## 3. Configure inventory

Edit `cpcbf/inventory/inventory-lab-wan.yaml`:
- Bridge RPi IP addresses (reachable via Ethernet/SSH)
- Serial port paths (`/dev/ttyACM0`, or `/dev/ttyACM1` if the bridge RPi
  also hosts an MKR WiFi 1010)
- SSH credentials for the RPis

## 4. Install pyserial on bridge RPis

```bash
ssh pi@<rpi_ip> "sudo apt install -y python3-serial screen"
```

## 5. Run a test

From your workstation, pick the plan that matches the firmware currently
flashed on both boards:

```bash
cd cpcbf
pip install -e .
cpcbf plans/arduino/lora/lora_mkr1300_rssi.yaml  -i inventory/inventory-lab-wan.yaml -o ./results_lora -v
cpcbf plans/arduino/lora/lora_mkr1300_rtt.yaml   -i inventory/inventory-lab-wan.yaml -o ./results_lora -v
cpcbf plans/arduino/lora/lora_mkr1300_flood.yaml -i inventory/inventory-lab-wan.yaml -o ./results_lora -v
```

To run all three in one session: flash `lora_rssi` → run RSSI plan →
flash `lora_rtt` → run RTT plan → flash `lora_flood` → run flood plan.

## 6. Analyse results

```bash
python run_analysis.py --results results_lora/results.jsonl --output results_lora/
```

## Architecture

```
Workstation                   Bridge RPi A              Arduino MKR WAN A
+-----------+   SSH/Ethernet  +-----------+   USB-Serial +-----------+
| Controller| <-------------> |serial_relay| <---------> | LoRa FW   |
+-----------+                 +-----------+              +-----------+

                              Bridge RPi B              Arduino MKR WAN B
                              +-----------+   USB-Serial +-----------+
                              |serial_relay| <---------> | LoRa FW   |
                              +-----------+              +-----------+
```

The controller SSHes to each bridge RPi, uploads and runs `serial_relay.py`,
which bridges stdin/stdout JSON to/from the Arduino's USB-Serial port.
The firmware runs the same JSON command protocol as the RPi4 and MKR WiFi
1010 agents — only the `LORA_SETUP` command replaces `WIFI_SETUP` /
`BLE_SETUP`.

## LoRa radio parameters

Configured in `cpcbf/agent/platforms/arduino_mkr_wan1300/src/config.h`:

| Parameter | Default | Notes |
|-----------|---------|-------|
| Frequency | 868 MHz (EU868) | Define `CPCBF_LORA_REGION_US` to switch to 915 MHz (US915) |
| Spreading factor (SF) | 7 | Fastest, shortest range. Raise to 9–12 for more range at the cost of air-time |
| Bandwidth (BW) | 125 kHz | Standard |
| Coding rate (CR) | 4/5 | |
| TX power | +14 dBm | PA_BOOST on MKR WAN 1300. Reduce to 10 dBm for lab tests to stay well inside duty-cycle limits |
| Preamble | 8 symbols | |
| Sync word | 0x12 (private) | Use 0x34 for LoRaWAN-compatible networks |

Both boards **must** use identical parameters — a single SF or sync-word
mismatch silently drops all packets.

## Topology

- Raw P2P broadcast — no addressing, no handshake.
- Both boards run identical `LoRa.begin()` init after receiving `LORA_SETUP`.
- Half-duplex: the adapter re-enters RX immediately after every TX so the
  receive loop can drain the peer's next packet.

## RSSI mode specifics

LoRa has no ambient/connection RSSI (unlike WiFi's continuous beacon-based
measurement or BLE's per-connection-event read). The adapter's `get_rssi()`
therefore runs a tiny probe ping-pong every call:

- **Sender role**: transmits a 1-byte probe and waits up to 500 ms for the
  peer's reply; reports RSSI of the reply.
- **Receiver role**: waits up to 2 s for the probe and reports RSSI of it,
  then transmits the reply.

Both sides thus record the counter-party's signal, producing symmetric data.

## Constraints

- **Max payload**: 200 bytes (SX1276 MTU 222 − 14 B bench header − margin).
  The plan validator rejects larger sizes.
- **Per-test static results cap** (`CPCBF_STATIC_RESULTS_MAX` in
  `platformio.ini`):
  - `lora_rssi`: 110 slots
  - `lora_rtt`: 320 slots (covers 10 warmup + 300 measured)
  - `lora_flood`: 8 slots (aggregate-only + 5 chunks)
- **Flood** uses agent-side 5-chunk bucketing so throughput std-dev lands
  in the same `flood_chunks` DB table as the RPi4 / WiFi / BLE runs.
- **Duty cycle (EU868)**: at +14 dBm the 1 % sub-band limit is ~36 s of
  TX per hour. The default plans stay under this:
  - RSSI: 100 reps × ~70 ms = 7 s
  - RTT: 100 reps × ~140 ms = 14 s per side
  - Flood: 60 reps × ~370 ms = 22 s
  Increasing reps or payload may violate regulations. For higher volumes,
  lower `LORA_TX_POWER_DBM` to 10 in `config.h` or use US915.

## Troubleshooting

### `LoRa.begin() failed` in the agent log

The Murata module failed to initialize. Usually means the MKRWAN bootloader
variant isn't selected — re-check `board = mkrwan1300` in `platformio.ini`
and that you flashed via `-t upload` (not just `build`).

### No packets received (100 % loss)

1. **Antennas** — both boards must have antennas plugged into the U.FL
   connector.
2. **Matching parameters** — both boards must run the same firmware env
   (same SF, BW, CR, sync word, frequency).
3. **Region mismatch** — if one board was flashed with `CPCBF_LORA_REGION_US`
   and the other without, they're on different frequencies.
4. **Distance / obstructions** — for SF7 at +14 dBm, start with the boards
   in the same room for initial testing.

### Arduino not responding on serial

1. Check the USB cable is connected and the serial port exists
   (`ls /dev/ttyACM*`)
2. Ensure no other process holds the port (kill stale `screen` sessions)
3. Reset the Arduino (press reset button) and retry

### Flood loss higher than expected

LoRa flood is air-time-paced by the library (`LoRa.endPacket()` blocks
until TX completes). Loss > 5 % usually means RF interference or one
board missed the receiver-head-start window. Check the orchestrator log
for `2s receiver head start` and confirm both sides report `link setup
complete` before the START command.
