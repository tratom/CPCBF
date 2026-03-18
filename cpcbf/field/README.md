# Field Deployment

## Overview

Field tests run without an orchestrator — each RPi powers on and `auto_bench.py` runs automatically at startup via a systemd unit. This works for both native RPi4 agents and MKR boards connected via USB-Serial.

## RPi4 (Native Agent)

The RPi spawns the `cpcbf_agent` binary directly. Configure by symlinking the appropriate role file:

```bash
ln -sf role_sender.json role.json    # on sender RPi
ln -sf role_receiver.json role.json  # on receiver RPi
```

## MKR Serial-Bridge

For MKR boards, the RPi acts as a serial bridge: instead of spawning `cpcbf_agent`, it spawns `serial_relay.py` which pipes JSON to the Arduino over USB-Serial. The JSON protocol is identical (CONFIGURE, WIFI_SETUP, SYNC, START, GET_RESULTS, STOP), so `send_cmd()` works unchanged — only the subprocess and CONFIGURE params differ.

### Topology

MKR WiFi uses SoftAP with fixed IPs:

| Role     | WiFi Mode | IP Address    |
|----------|-----------|---------------|
| Sender   | AP        | 192.168.4.1   |
| Receiver | STA       | 192.168.4.2   |

No `peer_addr` or `local_ip` fields are needed in `role.json` — they are derived automatically from the role.

### Setup

1. Flash the MKR board with the PlatformIO firmware:
   ```bash
   cd cpcbf/agent/platforms/arduino_mkr_wifi1010 && pio run -t upload
   ```

2. Connect the MKR to the bridge RPi via USB.

3. Symlink the appropriate role file on each bridge RPi:
   ```bash
   # RPi A (sender)
   ln -sf role_mkr_wifi_sender.json role.json

   # RPi B (receiver)
   ln -sf role_mkr_wifi_receiver.json role.json
   ```

4. The same systemd unit (`cpcbf-auto.service`) is used — only the `role.json` differs.

### Role Configuration

Example sender role (`role_mkr_wifi_sender.json`):

```json
{
    "role": "sender",
    "board_type": "mkr_wifi_1010",
    "serial_port": "/dev/ttyACM0",
    "serial_baud": 115200,
    "plans_subdir": "arduino/wifi",
    "label": "field-mkr-wifi",
    "rounds": 10
}
```

Key fields:

- `board_type`: Must start with `mkr_` to trigger serial-bridge mode.
- `serial_port`: USB-Serial device path (default `/dev/ttyACM0`).
- `serial_baud`: Baud rate (default `115200`).
- `plans_subdir`: Subdirectory under `plans/` to glob for YAML plans (e.g. `arduino/wifi`).
- `rounds`: Number of times to repeat all plans.

### What Changes for MKR vs RPi4

- **Subprocess**: `serial_relay.py` instead of `cpcbf_agent`.
- **Startup delay**: 3s wait after spawning relay (Arduino reset + boot drain).
- **CONFIGURE params**: SoftAP topology with fixed IPs, empty `iface_name` and `peer_mac`.
- **NetworkManager**: Not restarted between tests or on exit (MKR doesn't touch the RPi's WiFi).
- **Plans directory**: Uses `plans_subdir` from role config to find MKR-specific YAML plans.

### Logs

- Relay stderr: `/tmp/cpcbf_relay.log`
- Agent stderr (RPi4): `/tmp/cpcbf_agent.log`
