# BLE Benchmarking Quick-Start (RPi4)

## 1. Prerequisites (on each RPi4)

Install the BlueZ development library:

```bash
sudo apt install -y libbluetooth-dev
```

## 2. Bring up the Bluetooth adapter

Run on **both** RPi4 boards:

```bash
# Unblock Bluetooth radio
sudo rfkill unblock bluetooth

# Bring up the HCI interface
sudo hciconfig hci0 up

# Verify — should show hci0 with a BD Address
hcitool dev
```

Expected output:

```
Devices:
        hci0    DC:A6:32:81:07:A1
```

Copy the address shown — this is the `ble_mac` for this board.

## 3. Update the inventory file

Edit your inventory YAML (e.g. `inventory-lab-rpi.yaml`) and fill in each board's
BLE MAC address from the previous step:

```yaml
hosts:
  rpi4_a:
    hostname: 192.168.8.20
    username: pi
    password: raspberry
    wifi_mac: "dc:a6:32:81:07:a0"
    ble_mac: "DC:A6:32:81:07:A1"      # <-- from hcitool dev on rpi4_a
    agent_binary: /bin/cpcbf_agent

  rpi4_b:
    hostname: 192.168.8.21
    username: pi
    password: raspberry
    wifi_mac: "d8:3a:dd:38:36:b7"
    ble_mac: "D8:3A:DD:38:36:B9"      # <-- from hcitool dev on rpi4_b
    agent_binary: /bin/cpcbf_agent
```

## 4. Build the agent (on each RPi4)

```bash
cd cpcbf/agent
mkdir -p build && cd build
cmake -DBUILD_RPI4=ON ..
make
```

Optionally install system-wide:

```bash
sudo cp cpcbf_agent /bin/cpcbf_agent
```

## 5. Run a BLE smoke test

From the controller machine (connected to both RPis via Ethernet):

```bash
cd cpcbf
pip install -e .
cpcbf plans/ble_rpi4_quick_smoke.yaml -i inventory-lab-rpi.yaml -o ./results -v
```

This runs 30 ping-pong repetitions with 20-byte payloads over BLE L2CAP.

## 6. Available BLE test plans

| Plan file                      | Mode      | Reps | Payload sizes     |
|--------------------------------|-----------|------|-------------------|
| `ble_rpi4_quick_smoke.yaml`    | ping_pong | 30   | 20 B              |
| `ble_rpi4_rtt.yaml`            | ping_pong | 300  | 20, 100, 244 B    |
| `ble_rpi4_flood.yaml`          | flood     | 1000 | 20, 100, 230 B    |

## 7. Troubleshooting

**`hcitool dev` shows no devices:**

```bash
# Check if Bluetooth service is running
sudo systemctl start bluetooth
sudo hciconfig hci0 up
hcitool dev
```

**`hciconfig hci0 up` fails:**

```bash
# Check kernel logs for Bluetooth firmware issues
dmesg | grep -i bluetooth
# The BCM43455 firmware should be at /lib/firmware/brcm/
ls /lib/firmware/brcm/BCM4345C0.hcd
```

**L2CAP connection times out:**

- Ensure both boards have Bluetooth unblocked: `rfkill list bluetooth`
- Ensure WiFi is disabled during BLE tests (the framework does this automatically via radio isolation)
- Check that the `ble_mac` values in the inventory match the actual addresses

**Permission errors on HCI/L2CAP sockets:**

The agent must run as root (the deploy script handles this). For manual testing:

```bash
sudo ./cpcbf_agent
```
