# Field Deployment

## Overview

Field tests run without an orchestrator — each RPi powers on and `auto_bench.py` runs automatically at startup via a systemd unit. This works for both native RPi4 agents and MKR boards connected via USB-Serial.

## Quick Start

1. Copy the appropriate role template to `role_sender.json` / `role_receiver.json` and fill in your values.
2. Deploy with `deploy.sh`:
   ```bash
   ./deploy.sh sender    # on sender RPi
   ./deploy.sh receiver  # on receiver RPi
   ```
   This copies `role_sender.json` or `role_receiver.json` to `role.json`.
3. The systemd unit (`cpcbf-auto.service`) runs `auto_bench.py` on boot, which reads `role.json` and iterates over all YAML plans found under `plans/<plans_subdir>/`.

## Role Configuration

There is one `role_sender.json` and one `role_receiver.json` per deployment — customize the fields for the target board.

### RPi4 (Native Agent)

```json
{
    "role": "sender",
    "peer_mac": "d8:3a:dd:38:36:b7",
    "peer_addr": "192.168.49.2",
    "local_ip": "192.168.49.1",
    "ble_mac_peer": "D8:3A:DD:38:36:B9",
    "plans_subdir": "rpi",
    "label": "lab-test",
    "rounds": 10
}
```

Key fields:
- `peer_mac`, `peer_addr`, `local_ip`: WiFi Direct P2P addressing.
- `ble_mac_peer`: Peer BLE MAC for L2CAP tests.
- `plans_subdir`: `"rpi"` — finds all plans under `plans/rpi/` recursively (wifi, ble, etc.).

### MKR WiFi 1010 (Serial Bridge)

The RPi acts as a serial bridge: `auto_bench.py` spawns `serial_relay.py` instead of `cpcbf_agent`, piping JSON to the Arduino over USB-Serial.

```json
{
    "role": "sender",
    "board_type": "mkr_wifi_1010",
    "serial_port": "/dev/ttyACM0",
    "serial_baud": 115200,
    "plans_subdir": "arduino",
    "label": "field-mkr-wifi",
    "rounds": 10
}
```

Key fields:
- `board_type`: Must start with `mkr_` to trigger serial-bridge mode.
- `serial_port`: USB-Serial device path (default `/dev/ttyACM0`).
- `serial_baud`: Baud rate (default `115200`).
- `plans_subdir`: `"arduino"` — finds all plans under `plans/arduino/` recursively.

No `peer_addr` or `local_ip` needed — SoftAP topology uses fixed IPs derived from the role (sender=AP at 192.168.4.1, receiver=STA at 192.168.4.2).

## Plans Folder Structure

```
plans/
  rpi/
    wifi/
      wifi_rpi4_quick_smoke.yaml
      wifi_rpi4_flood_5min.yaml
    ble/
      ble_rpi4_quick_smoke.yaml
  arduino/
    wifi/
      wifi_mkr1010_quick_smoke.yaml
```

The `plans_subdir` field in the role file determines which subtree is scanned. The glob is recursive, so any nesting depth works.

## MKR Setup

1. Flash the MKR board:
   ```bash
   cd cpcbf/agent/platforms/arduino_mkr_wifi1010 && pio run -t upload
   ```
2. Connect the MKR to the bridge RPi via USB.
3. Set `role_sender.json` / `role_receiver.json` with MKR fields (see above).
4. Deploy: `./deploy.sh sender` or `./deploy.sh receiver`.

## Differences: MKR vs RPi4

| Aspect | RPi4 | MKR |
|--------|------|-----|
| Subprocess | `cpcbf_agent` | `serial_relay.py` |
| Startup delay | None | 3s (Arduino reset) |
| WiFi topology | P2P (Wi-Fi Direct) | SoftAP (fixed IPs) |
| NetworkManager | Restarted between tests | Not touched |
| Plans | `plans/rpi/` | `plans/arduino/` |

## Logs

- Relay stderr: `/tmp/cpcbf_relay.log`
- Agent stderr (RPi4): `/tmp/cpcbf_agent.log`
