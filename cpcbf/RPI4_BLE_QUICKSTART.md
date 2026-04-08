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
| `ble_rpi4_flood_2m.yaml`      | flood     | 1000 | 20, 100, 237 B    |

## 7. BLE 5.0 2M PHY

The RPi4's BCM43455 is a BLE 5.0 chip that supports the 2M PHY (2 Msym/s),
roughly doubling the symbol rate compared to the default 1M PHY. With DLE
already enabled, switching to 2M PHY should push flood throughput from ~750 Kbps
toward ~1.2-1.4 Mbps.

To use 2M PHY, add `ble_phy: "2m"` to the global section (or per-test) in your
plan YAML:

```yaml
global:
  ble_phy: "2m"    # default is "1m"
```

A ready-made plan is provided: `ble_rpi4_flood_2m.yaml`.

The agent sends the HCI LE Set PHY command (`0x08 0x0032`) after establishing
the L2CAP connection and negotiating DLE/CI parameters. Both TX and RX are
requested at 2M. The remote controller must also support 2M PHY for the switch
to take effect; otherwise the link stays at 1M (backward compatible).

## 8. Throughput optimization

The framework automatically applies these optimizations after each BLE connection
is established (in `ble_setup.c`). This section explains the rationale.

### Data Length Extension (DLE)

By default, BLE 4.0/4.1 controllers use **27-byte PDUs** (Link Layer payload).
With the 4-byte L2CAP header, only 23 bytes of application data fit per radio
event, fragmenting every L2CAP SDU into many tiny chunks and capping throughput
at roughly **130 Kbps** even with a short connection interval.

BLE 4.2+ introduced DLE, which raises the PDU to **251 bytes** (247 B usable
after the L2CAP header). The RPi4's BCM43455 supports DLE.

The agent enables DLE in two places:

1. **Default suggested data length** (adapter reset, before any connection):

   ```
   hcitool -i hci0 cmd 0x08 0x0024 FB 00 48 08
   ```

   - `0x08 0x0024` = LE Write Suggested Default Data Length
   - `FB 00` = TxOctets = 251 (0x00FB)
   - `48 08` = TxTime = 2120 us (0x0848) — time to transmit 251 bytes at 1 Mbps

2. **Per-connection data length** (after L2CAP connect/accept):

   ```
   hcitool -i hci0 cmd 0x08 0x0022 <handle_lo> <handle_hi> FB 00 48 08
   ```

   - `0x08 0x0022` = LE Set Data Length (per-connection)
   - Both peers negotiate the actual value; the minimum of the two sides' max wins.

### Connection interval

The connection interval (CI) determines how often the central and peripheral
exchange data. Shorter CI = lower latency and higher throughput, but more power.

The framework requests the minimum BLE 4.2 CI of **7.5 ms** (6 × 1.25 ms):

```
hcitool -i hci0 lecup --handle <h> --min 6 --max 6 --latency 0 --timeout 200
```

### Theoretical maximum throughput

With DLE (251 B PDU) and CI = 7.5 ms on 1M PHY:

- Each connection event can carry multiple PDUs (controller-dependent)
- One PDU per event: 251 B × 8 / 7.5 ms ≈ **267 Kbps**
- Multiple PDUs per event (typical): up to **~800 Kbps**

The BCM43455 typically achieves 4–6 PDUs per connection event, so measured
throughput in the range of **500–800 Kbps** is expected for flood tests.

### L2CAP MTU

The L2CAP CoC MTU is set to **2048 bytes** via socket options. The BLE controller
fragments/reassembles this into 251-byte PDUs transparently. Larger MTU reduces
per-SDU overhead at the L2CAP layer.

## 9. Troubleshooting

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
