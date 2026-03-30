# MKR WiFi 1010 — WiFi Quick Start

## Prerequisites

- 2x Arduino MKR WiFi 1010 boards
- 2x Raspberry Pi (any model) as USB-serial bridges
- USB cables connecting each Arduino to its bridge RPi
- Ethernet connectivity from your workstation to both RPis
- PlatformIO CLI (`pip install platformio`)
- Python 3.10+ with `pyserial` on the bridge RPis (`pip install pyserial`)

## 1. Flash WiFi firmware

```bash
cd cpcbf/agent/platforms/arduino_mkr_wifi1010
pio run -e mkrwifi1010_wifi -t upload --upload-port /dev/ttyACM0
```

Repeat for the second Arduino (connect to the second RPi or your workstation).

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

## 5. Run smoke test

From your workstation:

```bash
cd cpcbf
pip install -e .
cpcbf plans/arduino/wifi/wifi_mkr1010_rssi.yaml -i inventory/inventory-lab-mkr.yaml -o ./results -v
```

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
- Max stored results: 500 entries per test run

## Troubleshooting

### Arduino not responding on serial

1. Check the USB cable is connected and the serial port exists (`ls /dev/ttyACM*`)
2. Ensure no other process holds the port (kill stale `screen` sessions)
3. Reset the Arduino (press reset button) and retry

### WiFi SoftAP not connecting

1. Ensure both boards have the WiFi firmware flashed (`pio run -e mkrwifi1010_wifi`)
2. The sender must start first (creates the AP), then the receiver joins
3. Check the orchestrator log for IP assignment errors
