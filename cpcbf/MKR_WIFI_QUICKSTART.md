# MKR WiFi 1010 Quick Start

## Prerequisites

- 2x Arduino MKR WiFi 1010 boards
- 2x Raspberry Pi (any model) as USB-serial bridges
- USB cables connecting each Arduino to its bridge RPi
- Ethernet connectivity from your workstation to both RPis
- PlatformIO CLI (`pip install platformio`)
- Python 3.10+ with `pyserial` on the bridge RPis (`pip install pyserial`)

## 1. Flash firmware

```bash
cd cpcbf/agent/platforms/arduino_mkr_wifi1010
pio run -t upload --upload-port /dev/ttyACM0
```

Repeat for the second Arduino (connect to the second RPi or your workstation).

## 2. Verify serial communication

On a bridge RPi, test that the Arduino responds:

```bash
# Install pyserial if needed
sudo apt install -y python3-serial

# Quick test via minicom or screen
screen /dev/ttyACM0 115200
# Type: {"command":"RADIO_STATUS"}
# Expect: {"status":"ok","message":"radio status","data":{"wifi_active":true,"bluetooth_active":false}}
```

## 3. Configure inventory

Edit `cpcbf/inventory-lab-mkr.yaml` with:
- Bridge RPi IP addresses (reachable via Ethernet/SSH)
- Serial port paths (usually `/dev/ttyACM0`)
- SSH credentials for the RPis

## 4. Install pyserial on bridge RPis

```bash
ssh pi@<rpi_ip> "sudo apt install -y python3-serial screen"
```

## 5. Run smoke test

From your workstation:

```bash
cd cpcbf
pip install -e .
cpcbf plans/wifi_mkr1010_quick_smoke.yaml -i inventory-lab-mkr.yaml -o ./results -v
```

## 6. Analyze results

```bash
python run_analysis.py --results results/results.jsonl --output results/
```

## Architecture

```
Workstation                   Bridge RPi A              Arduino MKR A
+-----------+   SSH/Ethernet  +-----------+   USB-Serial +-----------+
| Controller| <-------------> |serial_relay| <---------> | Agent FW  |
+-----------+                 +-----------+              +-----------+

                              Bridge RPi B              Arduino MKR B
                              +-----------+   USB-Serial +-----------+
                              |serial_relay| <---------> | Agent FW  |
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

- Max payload: 512 bytes (SAMD21 SRAM limit)
- Max stored results: 110 entries per test run

## Troubleshooting

### Check NINA firmware version (BLE diagnostics)

BLE requires NINA firmware >= 1.2.0. To check the version, connect to a board via serial monitor and send BLE setup commands:

```bash
# Open serial monitor (115200 baud)
screen /dev/ttyACM0 115200
```

Then paste these two JSON commands one at a time, pressing Enter after each:

```json
{"command":"CONFIGURE","params":{"protocol":"ble","topology":"ble_gatt","role":"receiver","mode":"rssi","payload_size":0,"repetitions":100}}
```

```json
{"command":"BLE_SETUP"}
```

Watch the log output for the firmware version line:

```
ble: NINA firmware version: X.X.X (need >= 1.2.0 for BLE)
```

If the version is below 1.2.0, update the NINA firmware:

1. Open Arduino IDE
2. Open **Tools → WiFi101 / WiFiNINA Firmware Updater**
3. Select the board and flash the latest firmware (>= 1.5.0)
4. Repeat for all MKR boards

To exit `screen`: press `Ctrl-A` then `K`, then confirm with `y`.
