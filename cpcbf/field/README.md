# Field Deployment

## Overview

Field tests run without an orchestrator. Each RPi powers on, two systemd
units (`cpcbf-2_4ghz`, `cpcbf-lora`) start in parallel, each runs **one**
plan for the configured number of rounds, then exits. To run a different
plan: flash the matching firmware on the MKR, edit `active_plans.json`,
and reboot.

This is intentionally simpler than the previous "walk every plan in one
boot" runner — fewer moving parts, easier to debug, the harness behaves
the same way as the lab orchestrator over SSH.

## Layout

```
cpcbf/field/
  auto_bench.py             # the runner — boots, runs ONE plan, exits
  bridge_sync.py            # L2CAP CoC handshake between the two RPis
  bluetooth_control_arbiter.py  # plain mutex around the BT controller
  firmware_flash.py         # MANUAL flash helper (CLI; not run at boot)
  radio_isolation_local.py  # rfkill wrapper for the 2_4ghz track
  serial_relay.py           # JSON pipe to the MKR over USB-Serial

  active_plans.json         # << EDIT THIS to change what runs next boot
  role_sender_2_4ghz.json   # this Pi's role for the 2_4ghz track
  role_receiver_2_4ghz.json
  role_sender_lora.json     # this Pi's role for the lora track
  role_receiver_lora.json

  cpcbf-2_4ghz.service
  cpcbf-lora.service

  firmware/                 # prebuilt .bin files (deployed once)
  results/                  # per-test JSONL outputs
```

## Workflow: running a different plan

1. Pick a plan from `cpcbf/plans/field/track_2_4ghz/` or
   `track_lora/` — for example `04_wifi_rssi`.

2. **Flash both MKRs** with the matching firmware. From a workstation
   over SSH (run on **each** Pi):
   ```bash
   sudo python3 ~/common-bench/cpcbf/field/firmware_flash.py \
       /dev/ttyACM_WIFI mkrwifi1010_wifi_rssi \
       ~/common-bench/cpcbf/field/firmware /tmp/flash.log
   ```
   The flasher writes a marker at `/var/lib/cpcbf/flashed_<port>.txt`;
   the runner reads that marker at boot and aborts with
   `FIRMWARE_MISMATCH` if it doesn't match the active plan.

3. **Edit `active_plans.json`** on both Pis to point at the chosen plan:
   ```json
   {
       "2_4ghz": "track_2_4ghz/04_wifi_rssi",
       "lora": "track_lora/01_lora_rssi"
   }
   ```
   Set a track to `null` to skip it. Plan paths are relative to
   `cpcbf/plans/field/`; the `.yaml` suffix is optional.

4. **Reboot both Pis** (or `sudo systemctl restart cpcbf-2_4ghz
   cpcbf-lora`). They each bridge-sync once with the peer Pi, then run
   their plan for `rounds` (set in `role_*.json`) iterations.

5. Pull `cpcbf/field/results/` for the data.

## What the runner does at boot

```
read role_*.json (path passed by the systemd unit)
read active_plans.json[track]
load the resolved plan YAML
verify firmware marker matches plan.firmware (else: FIRMWARE_MISMATCH, exit)
if BLE plan: open relay → GET_BLE_ADDR → close relay (so the manifest can
             carry our local MKR's BD_ADDR for peer-discovery)
acquire BT mutex
bridge_sync.sync(role, peer_mac, manifest, estimate_clock_offset=is_flood)
    ↳ retries up to BRIDGE_SYNC_RETRIES = 4 times, releasing/re-acquiring
      the mutex between attempts (single-PSM design, mismatches surface
      in ~1 s as a manifest mismatch, not a 60 s connect timeout)
if lora: stamp /var/lib/cpcbf/lora_bridge_sync_done with this boot's id
release BT mutex
if 2_4ghz: wait up to 60 s for lora's flag (so its bridge_sync — which
           also needs RPi BT — isn't cut off by the rfkill below)
           rfkill block wifi+bt
run the plan: for round in 1..N: for test in plan.tests: ...
              CONFIGURE → SETUP → SYNC → (sender: sleep 2 s if mode != rssi)
              → START → GET_RESULTS → SYNC(phase=1)
if 2_4ghz: rfkill unblock
exit
```

## Role configs

Per Pi, four files: one per (role, track) combo. `deploy.sh` copies the
active pair to `role_2_4ghz.json` / `role_lora.json` (the stable names
the systemd units invoke).

```json
{
    "role": "sender",
    "track": "2_4ghz",
    "board_type": "mkr_wifi_1010",
    "serial_port": "/dev/ttyACM_WIFI",
    "serial_baud": 115200,
    "rounds": 10,
    "label": "field",
    "bridge_peer_bt_mac": "D8:3A:DD:38:36:B9",
    "essid": "CPCBF_MKR"
}
```

- `bridge_peer_bt_mac`: BT MAC of the **other** RPi (used by
  `bridge_sync.py`).
- The MKR's BLE MAC is **discovered** at boot via `GET_BLE_ADDR` — no
  more `ble_mac_peer` field. Swap MKRs without touching the role file.
- Optional `active_plan` overrides `active_plans.json` for this Pi only
  (rare — useful for asymmetric bench tests).

## Per-track services

| Service | Owns | rfkill? | Mutex window |
|---|---|---|---|
| `cpcbf-2_4ghz` | `/dev/ttyACM_WIFI` (MKR WiFi 1010) | yes — blocks RPi wifi+bt during the test so they don't bleed into the MKR's 2.4 GHz radios | bridge_sync only |
| `cpcbf-lora` | `/dev/ttyACM_WAN` (MKR WAN 1300) | no — LoRa 868 MHz can't interfere with on-board RPi radios | bridge_sync only |

Both services run in parallel. They contend for the RPi's BT controller
only during the bridge_sync window (a few seconds). After that they
operate on their own MKR over USB-serial, independent of each other.

## Manual flash (during development)

```bash
sudo python3 firmware_flash.py /dev/ttyACM_WIFI \
    mkrwifi1010_ble_flood ./firmware /tmp/flash.log
```

This invokes the same `flash()` function the runner used to call
in-line; the runner no longer flashes anything itself.

## Diagnostics

```bash
# what each track did this boot
journalctl -u cpcbf-2_4ghz -u cpcbf-lora -b

# bridge-sync arbiter trace
journalctl -t auto_bench -b | grep arbiter

# what firmware is currently on the MKRs
cat /var/lib/cpcbf/flashed_*.txt
```

Logs:
- Relay stderr: `/tmp/cpcbf_relay.log`
- Flash log: `/tmp/cpcbf_flash.log`
