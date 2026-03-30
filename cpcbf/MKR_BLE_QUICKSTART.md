# MKR WiFi 1010 — BLE Quick Start

## Prerequisites

- 2x Arduino MKR WiFi 1010 boards
- 2x Raspberry Pi (any model) as USB-serial bridges
- USB cables connecting each Arduino to its bridge RPi
- Ethernet connectivity from your workstation to both RPis
- PlatformIO CLI (`pip install platformio`)
- Python 3.10+ with `pyserial` on the bridge RPis (`pip install pyserial`)
- NINA firmware >= 1.2.0 (see Troubleshooting below)

## 1. Flash BLE firmware

```bash
cd cpcbf/agent/platforms/arduino_mkr_wifi1010
pio run -e mkrwifi1010_ble -t upload --upload-port /dev/ttyACM0
```

Repeat for the second Arduino (connect to the second RPi or your workstation).

**Note:** The BLE firmware is separate from the WiFi firmware. To switch between
WiFi and BLE tests, re-flash the appropriate firmware (`-e mkrwifi1010_wifi` or
`-e mkrwifi1010_ble`).

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

## 4. Run smoke test

From your workstation:

```bash
cd cpcbf
pip install -e .
cpcbf plans/arduino/ble/ble_mkr1010_quick_smoke.yaml -i inventory/inventory-lab-mkr.yaml -o ./results_ble -v
```

## BLE topology

- Receiver Arduino = Peripheral (advertises GATT service, waits for central)
- Sender Arduino = Central (scans for service UUID, connects, discovers attributes)
- Data exchange via GATT characteristics:
  - TX characteristic: central writes data to peripheral
  - RX characteristic: peripheral notifies data to central

## Constraints

- Max payload: 230 bytes (ATT_MTU 244 - 14 byte benchmark header)
- Max stored results: 500 entries per test run
- BLE and WiFi cannot run in the same firmware (separate builds required)

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
