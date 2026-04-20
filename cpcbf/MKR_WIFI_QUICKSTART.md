# MKR WiFi 1010 — WiFi Quick Start

## Prerequisites

- 2x Arduino MKR WiFi 1010 boards
- 2x Raspberry Pi (any model) as USB-serial bridges
- USB cables connecting each Arduino to its bridge RPi
- Ethernet connectivity from your workstation to both RPis
- PlatformIO CLI (`pip install platformio`)
- Python 3.10+ with `pyserial` on the bridge RPis (`pip install pyserial`)

## 1. Flash the per-test firmware

The MKR WiFi 1010 has only 32 KB of SRAM, so each test (RSSI / RTT / flood)
ships as its own firmware — memory-tuned to the buffers that test actually
needs. Flash the env that matches the plan you want to run.

| Plan                                              | PlatformIO env             |
|---------------------------------------------------|----------------------------|
| `plans/arduino/wifi/wifi_mkr1010_rssi.yaml`       | `mkrwifi1010_wifi_rssi`    |
| `plans/arduino/wifi/wifi_mkr1010_rtt.yaml`        | `mkrwifi1010_wifi_rtt`     |
| `plans/arduino/wifi/wifi_mkr1010_flood.yaml`      | `mkrwifi1010_wifi_flood`   |

Each plan YAML also lists its env in a top-level `firmware:` field — re-check
there if you're unsure.

```bash
cd cpcbf/agent/platforms/arduino_mkr_wifi1010
pio run -e mkrwifi1010_wifi_rssi -t upload --upload-port /dev/ttyACM0
```

Repeat for the second Arduino (connect to the second RPi or your workstation).

Switching between WiFi and BLE, or between test modes, requires re-flashing
the matching firmware.

## 2. Verify serial communication

On a bridge RPi, test that the Arduino responds:

```bash
# Install pyserial if needed
sudo apt install -y python3-serial screen

# Quick test via screen
screen /dev/ttyACM0 115200
# Type: {"command":"RADIO_STATUS"}
# Expect: {"status":"ok","message":"radio status","data":{"wifi_active":false,"bluetooth_active":false}}
```

## 3. Configure inventory

Edit `cpcbf/inventory/inventory-lab-mkr.yaml` with:
- Bridge RPi IP addresses (reachable via Ethernet/SSH)
- Serial port paths (usually `/dev/ttyACM0`)
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
cpcbf plans/arduino/wifi/wifi_mkr1010_rssi.yaml  -i inventory/inventory-lab-mkr.yaml -o ./results -v
cpcbf plans/arduino/wifi/wifi_mkr1010_rtt.yaml   -i inventory/inventory-lab-mkr.yaml -o ./results -v
cpcbf plans/arduino/wifi/wifi_mkr1010_flood.yaml -i inventory/inventory-lab-mkr.yaml -o ./results -v
```

To run all three in one session: flash `wifi_rssi` → run RSSI plan → flash
`wifi_rtt` → run RTT plan → flash `wifi_flood` → run flood plan.

## 6. Analyze results

```bash
python run_analysis.py --results results/results.jsonl --output results/
```

## Architecture

```
Workstation                   Bridge RPi A              Arduino MKR A
+-----------+   SSH/Ethernet  +-----------+   USB-Serial +-----------+
| Controller| <-------------> |serial_relay| <---------> | WiFi FW   |
+-----------+                 +-----------+              +-----------+

                              Bridge RPi B              Arduino MKR B
                              +-----------+   USB-Serial +-----------+
                              |serial_relay| <---------> | WiFi FW   |
                              +-----------+              +-----------+
```

The controller SSHes to each bridge RPi, uploads and runs `serial_relay.py`,
which bridges stdin/stdout JSON to/from the Arduino's USB-Serial port.
The Arduino firmware runs the same JSON command protocol as the RPi4 agent.

## WiFi topology

- Sender Arduino = SoftAP (creates network `CPCBF_MKR`, IP 192.168.4.1)
- Receiver Arduino = STA (joins network, IP 192.168.4.2)
- Data exchange via WiFiUDP on port 5201

## Constraints

- Max payload: 1432 bytes (WiFiNINA maximum UDP payload)
- Per-test static results cap (set in `platformio.ini` via `CPCBF_STATIC_RESULTS_MAX`):
  - `wifi_rssi`: 110 slots
  - `wifi_rtt`: 320 slots (covers 10 warmup + 300 measured)
  - `wifi_flood`: 8 slots (aggregate-only + 5 chunks)
- Flood runs agent-side 5-chunk bucketing so throughput std-dev lands in the
  same `flood_chunks` table as the RPi4 runs.

## Troubleshooting

### Arduino not responding on serial

1. Check the USB cable is connected and the serial port exists (`ls /dev/ttyACM*`)
2. Ensure no other process holds the port (kill stale `screen` sessions)
3. Reset the Arduino (press reset button) and retry

### WiFi SoftAP not connecting

1. Ensure both boards have the matching WiFi firmware flashed
2. The sender must start first (creates the AP), then the receiver joins
3. Check the orchestrator log for IP assignment errors

### `pp_tx: seq mismatch` during RTT

The RTT firmware pre-drains any packets left over from a prior run before the
loop starts. If you still see mismatches, power-cycle both boards — a mid-run
abort can leave UDP buffers holding stale data.

### Flood loss > 10 %

`wifi_mkr1010_flood.yaml` sets `inter_packet_us: 200` as a first guess — just
enough pacing to let the RPi UDP socket drain without throttling. Widen to
500 µs if loss stays high.
