# MKR WiFi 1010 — BLE Quick Start

## Prerequisites

- 2x Arduino MKR WiFi 1010 boards
- 2x Raspberry Pi (any model) as USB-serial bridges
- USB cables connecting each Arduino to its bridge RPi
- Ethernet connectivity from your workstation to both RPis
- PlatformIO CLI (`pip install platformio`)
- Python 3.10+ with `pyserial` on the bridge RPis (`pip install pyserial`)
- NINA firmware >= 1.2.0 (see Troubleshooting below)

## 1. Flash the per-test firmware

The MKR WiFi 1010 has only 32 KB of SRAM, so each BLE test (RSSI / RTT /
flood) ships as its own firmware — memory-tuned to the buffers that test
actually needs. Flash the env that matches the plan you want to run.

| Plan                                           | PlatformIO env          |
|------------------------------------------------|-------------------------|
| `plans/arduino/ble/ble_mkr1010_rssi.yaml`      | `mkrwifi1010_ble_rssi`  |
| `plans/arduino/ble/ble_mkr1010_rtt.yaml`       | `mkrwifi1010_ble_rtt`   |
| `plans/arduino/ble/ble_mkr1010_flood.yaml`     | `mkrwifi1010_ble_flood` |

Each plan YAML also lists its env in a top-level `firmware:` field — re-check
there if you're unsure.

```bash
cd cpcbf/agent/platforms/arduino_mkr_wifi1010
pio run -e mkrwifi1010_ble_rssi -t upload --upload-port /dev/ttyACM0
```

Repeat for the second Arduino (connect to the second RPi or your workstation).

Switching between WiFi and BLE, or between test modes, requires re-flashing
the matching firmware.

## 2. Verify serial communication

On a bridge RPi:

```bash
screen /dev/ttyACM0 115200
# Type: {"command":"RADIO_STATUS"}
# Expect: {"status":"ok","message":"radio status","data":{"wifi_active":false,"bluetooth_active":false}}
```

## 3. Configure inventory

Edit `cpcbf/inventory/inventory-lab-mkr.yaml` — same inventory file as WiFi tests.
No BLE-specific inventory fields needed (ArduinoBLE discovers peers by service UUID).

## 4. Run a test

From your workstation, pick the plan that matches the firmware currently
flashed on both boards:

```bash
cd cpcbf
pip install -e .
cpcbf plans/arduino/ble/ble_mkr1010_rssi.yaml  -i inventory/inventory-lab-mkr.yaml -o ./results_ble -v
cpcbf plans/arduino/ble/ble_mkr1010_rtt.yaml   -i inventory/inventory-lab-mkr.yaml -o ./results_ble -v
cpcbf plans/arduino/ble/ble_mkr1010_flood.yaml -i inventory/inventory-lab-mkr.yaml -o ./results_ble -v
```

To run all three: flash `ble_rssi` → run RSSI plan → flash `ble_rtt` → run
RTT plan → flash `ble_flood` → run flood plan.

## BLE topology

- Receiver Arduino = Peripheral (advertises GATT service, waits for central)
- Sender Arduino = Central (scans for service UUID, connects, discovers attributes)
- Data exchange via GATT characteristics:
  - TX characteristic: central writes data to peripheral
  - RX characteristic: peripheral notifies data to central

## Flood-as-ping-pong

ArduinoBLE GATT has no L2CAP flow control, so unpaced writes silently drop
at the NINA queue. The `ble_flood` firmware therefore translates `mode:
flood` into synchronous ping-pong at max payload (230 B) at runtime —
each round-trip puts 2 packets on the air, naturally paced by the
acknowledgement. The plan's `repetitions` field is the number of round-trips
(half the equivalent one-way budget).

5-chunk throughput stats are produced agent-side and land in the same
`flood_chunks` DB table as the RPi4 and WiFi runs.

## Constraints

- Max payload: 230 bytes (ATT_MTU 244 − 14 B benchmark header), negotiated
  via DLE 251-byte LL PDU.
- Per-test static results cap (set in `platformio.ini` via `CPCBF_STATIC_RESULTS_MAX`):
  - `ble_rssi`: 110 slots
  - `ble_rtt`: 320 slots (covers 10 warmup + 300 measured)
  - `ble_flood`: 8 slots (aggregate-only + 5 chunks)
- BLE RX pool / ring sizing is also per-env:
  - RSSI firmware: no pool, tiny ring (4 slots)
  - RTT firmware: no pool, full ring (16 slots)
  - Flood firmware: no pool (ping-pong doesn't burst), full ring
- BLE and WiFi cannot run in the same firmware (separate builds required).

## Troubleshooting

### Check NINA firmware version

BLE requires NINA firmware >= 1.2.0. To check, connect via serial monitor:

```bash
screen /dev/ttyACM0 115200
```

Send these JSON commands one at a time:

```json
{"command":"CONFIGURE","params":{"protocol":"ble","topology":"ble_gatt","role":"receiver","mode":"rssi","payload_size":0,"repetitions":100}}
```

```json
{"command":"BLE_SETUP"}
```

Watch the log output for the firmware version. If below 1.2.0, update:

1. Open Arduino IDE
2. Open **Tools > WiFi101 / WiFiNINA Firmware Updater**
3. Select the board and flash the latest firmware (>= 1.5.0)
4. Repeat for all MKR boards

To exit `screen`: press `Ctrl-A` then `K`, then confirm with `y`.

### BLE setup fails with "discoverAttributes() failed"

This error means the central (sender) connected to the peripheral but could not
discover GATT services. Possible causes:

1. Boards too far apart — move within 1 meter for initial testing
2. NINA firmware version too old — update as described above
3. Stale BLE state — reset both boards and retry

### RSSI / RTT hang for ~10 min, then time out

Fixed in the split-firmware rebuild: the peripheral HCI flush now runs for
1000 ms (up from 500 ms) after connection, and the RSSI mode runs one
throwaway `central.rssi()` call on entry to absorb the one-time Read_RSSI
stall. If the symptom returns, re-check that you flashed the `ble_rssi` /
`ble_rtt` firmware (not a stale combined build).
