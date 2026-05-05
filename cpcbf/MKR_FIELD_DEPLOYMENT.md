# CPCBF — Autonomous Field Deployment

End-to-end guide for deploying the dual-track autonomous field rig:

- 2× Raspberry Pi (bridges; one is `sender`, one is `receiver`)
- 2× Arduino MKR WiFi 1010 (run BLE + WiFi tests on the 2.4 GHz track)
- 2× Arduino MKR WAN 1300 (run LoRa tests on the 868 MHz track)

Each RPi runs two systemd units in parallel: `cpcbf-2_4ghz.service` (BLE or WiFi on the MKR WiFi 1010) and `cpcbf-lora.service` (LoRa on the MKR WAN 1300). Each unit runs **one** plan per boot, for the configured number of rounds, then exits. To switch to a different plan: flash the matching firmware on the MKR (manually, with `firmware_flash.py`), edit `cpcbf/field/active_plans.json`, and reboot. The two RPis synchronise once per boot via a Bluetooth bridge link, and per round via the existing over-radio sync barrier.

For the *why*, see `design-choices/auto-flasher-and-runner-arduino.md`.

---

## 0. Prerequisites

**Hardware**

- 2× RPi (RPi 4 recommended) with built-in Wi-Fi/BT
- 2× Arduino MKR WiFi 1010 + USB-A→micro cables
- 2× Arduino MKR WAN 1300 + **LoRa antenna for each** (do NOT power without antenna — TX without antenna can damage the SX1276)
- 4 USB ports per RPi (or a powered hub) — each RPi hosts one MKR WiFi and one MKR WAN
- Network for first-time setup (Ethernet to the workstation; not needed in the field)

**Software (workstation)**

- PlatformIO (`pip install platformio`) — for building firmware
- `git`, `ssh` to the RPis
- `bluetoothctl` to read each RPi's BT MAC

**Software (RPis)**

- Raspberry Pi OS (Bookworm or later)
- Python 3.10+
- `bossa-cli`, `rfkill`, `python3-serial`, `python3-yaml`, `bluez` — installed by `deploy.sh`

---

## 1. Workstation: build the 9 firmware binaries

Each MKR firmware is split per (protocol × mode) so the SAMD21's 32 KB SRAM is enough. The runner doesn't flash anything itself; you flash manually before each boot using `firmware_flash.py` (step 8). All 9 binaries are still kept on the RPis so the manual flash is a one-liner.

```bash
cd cpcbf/agent/platforms/arduino_mkr_wifi1010
for env in mkrwifi1010_ble_rssi  mkrwifi1010_ble_rtt  mkrwifi1010_ble_flood \
           mkrwifi1010_wifi_rssi mkrwifi1010_wifi_rtt mkrwifi1010_wifi_flood; do
    pio run -e "$env"
    cp ".pio/build/$env/firmware.bin" "../../../field/firmware/$env.bin"
done

cd ../arduino_mkr_wan1300
for env in mkrwan1300_lora_rssi mkrwan1300_lora_rtt mkrwan1300_lora_flood; do
    pio run -e "$env"
    cp ".pio/build/$env/firmware.bin" "../../../field/firmware/$env.bin"
done

ls cpcbf/field/firmware/   # should list 9 .bin files
```

---

## 2. Workstation: read each RPi's Bluetooth MAC

You need each RPi's built-in BT MAC so the role files can name the *other* RPi as the bridge-sync peer.

On each RPi:

```bash
bluetoothctl show | grep -i 'controller'
# Controller D8:3A:DD:38:36:B7 (public)
```

Record both MACs — you'll fill them into the role files in step 5.

---

## 3. RPis: clone and copy the repo

On *each* RPi:

```bash
git clone <your-fork-or-repo-url> ~/cpcbf
# or rsync from the workstation
```

The repo path is referenced as `/home/pi/cpcbf` in the default role files; if you check out elsewhere, edit `firmware_dir` in the role files accordingly.

---

## 4. RPis: copy the prebuilt firmware

From the workstation, push the 9 `.bin` files to both RPis:

```bash
scp cpcbf/field/firmware/*.bin pi@<rpi-sender-ip>:~/cpcbf/cpcbf/field/firmware/
scp cpcbf/field/firmware/*.bin pi@<rpi-receiver-ip>:~/cpcbf/cpcbf/field/firmware/
```

---

## 5. RPis: edit the role files

There are four role-file templates in `cpcbf/field/`:

| File | Used by |
|------|---------|
| `role_sender_2_4ghz.json`   | sender RPi, 2.4 GHz track |
| `role_sender_lora.json`     | sender RPi, LoRa track |
| `role_receiver_2_4ghz.json` | receiver RPi, 2.4 GHz track |
| `role_receiver_lora.json`   | receiver RPi, LoRa track |

On **each RPi**, edit the two role files for the role that RPi will play. Set:

- `bridge_peer_bt_mac` — the **other** RPi's BT MAC (from step 2). Both tracks use the same value because the bridge-sync link is between the two RPi controllers, not per-board.
- `rounds` — how many times to repeat the plan in one boot.
- `label` — optional. Result files are tagged `<label>_<track>_<plan>_rNN_<test>_<size>B_<role>.jsonl`.

You do **not** need to set the MKR's BLE MAC — it's discovered at boot via `GET_BLE_ADDR` and exchanged through the bridge-sync manifest, so swapping MKRs doesn't require a config edit.

Leave `serial_port` as `/dev/ttyACM_WIFI` and `/dev/ttyACM_WAN` — they're udev symlinks created by the rules in step 6.

Then create `cpcbf/field/active_plans.json` (the file you'll edit between reboots — a default is shipped):

```json
{
    "2_4ghz": "track_2_4ghz/01_ble_rssi",
    "lora": "track_lora/01_lora_rssi"
}
```

Plan paths are relative to `cpcbf/plans/field/`; the `.yaml` suffix is optional. Set a track to `null` to skip it for this boot. **Both Pis must have the same `active_plans.json`** — bridge_sync compares the plan name and aborts if they differ.

---

## 6. RPis: run `deploy.sh`

On the **sender** RPi:

```bash
cd ~/cpcbf/cpcbf/field
sudo ./deploy.sh sender both
```

On the **receiver** RPi:

```bash
cd ~/cpcbf/cpcbf/field
sudo ./deploy.sh receiver both
```

`deploy.sh` will:

- `apt install bossa-cli rfkill python3-serial python3-yaml bluez`
- create `/var/lib/cpcbf` (arbiter state dir)
- copy `role_<role>_<track>.json` → `role_<track>.json` (the names the systemd units expect)
- install both systemd units into `/etc/systemd/system/`
- install the udev rule for `ttyACM_WIFI` / `ttyACM_WAN` symlinks
- `systemctl enable cpcbf-2_4ghz.service cpcbf-lora.service` (does **not** start them — wait for reboot or smoke test)

---

## 7. Plug in the boards and verify symlinks

Connect to **each** RPi:

- 1× MKR WiFi 1010 (any USB port)
- 1× MKR WAN 1300 with antenna attached (any USB port)

Then:

```bash
ls -l /dev/ttyACM_WIFI /dev/ttyACM_WAN
# lrwxrwxrwx 1 root root 7 Apr 28 ... /dev/ttyACM_WIFI -> ttyACM0
# lrwxrwxrwx 1 root root 7 Apr 28 ... /dev/ttyACM_WAN  -> ttyACM1
```

If a symlink is missing, the udev rule's `idProduct` may not match the runtime PID of your particular board revision:

```bash
lsusb | grep -i arduino
# Bus 001 Device 005: ID 2341:8054 Arduino SA  (MKR WiFi 1010 runtime)
# Bus 001 Device 006: ID 2341:8057 Arduino SA  (MKR WAN 1300 runtime)
```

Edit `cpcbf/field/udev/99-cpcbf-mkr.rules` if needed, then `sudo udevadm control --reload && sudo udevadm trigger`.

---

## 8. Flash the MKRs and smoke-test one boot

For each plan you want to run, both RPis need the matching firmware on their MKRs first. The runner refuses to start (`FIRMWARE_MISMATCH` in the journal) if the on-disk marker at `/var/lib/cpcbf/flashed_<port>.txt` doesn't match the active plan's `firmware:` field.

On **each RPi**, flash the firmware that matches what's in `active_plans.json`:

```bash
# 2.4 GHz track — flash the MKR WiFi 1010
sudo python3 ~/common-bench/cpcbf/field/firmware_flash.py \
    /dev/ttyACM_WIFI mkrwifi1010_ble_rssi \
    ~/common-bench/cpcbf/field/firmware /tmp/flash.log

# lora track — flash the MKR WAN 1300
sudo python3 ~/common-bench/cpcbf/field/firmware_flash.py \
    /dev/ttyACM_WAN mkrwan1300_lora_rssi \
    ~/common-bench/cpcbf/field/firmware /tmp/flash.log
```

(The 2nd argument matches the plan YAML's `firmware:` field; the 3rd is the directory holding the `.bin`s; the 4th is a log path for `bossac` output.)

Then start a single boot's worth of work:

```bash
sudo systemctl start cpcbf-2_4ghz.service
sudo systemctl start cpcbf-lora.service

# Watch progress
sudo journalctl -fu cpcbf-2_4ghz -u cpcbf-lora
```

You should see, on both RPis, roughly:

1. `[arbiter] {"track":"2_4ghz","event":"acquired",...}`
2. `[01_ble_rssi] bridge sync with D8:3A:DD:...` (single PSM 0x81)
3. `[arbiter] {"track":"2_4ghz","event":"released",...}` (immediately after sync)
4. `[2_4ghz] waiting up to 60s for lora bridge_sync...` (only on the 2.4 GHz side)
5. (rfkill blocks wifi+bt on the 2.4 GHz side; lora track's test runs in parallel)
6. (the plan's tests run for `rounds` iterations)
7. (rfkill unblocks; both units exit)

Result files appear under `cpcbf/field/results/`:

```text
field_2_4ghz_01_ble_rssi_r01_ble_mkr1010_rssi_0B_sender.jsonl
field_lora_01_lora_rssi_r01_lora_mkr1300_rssi_32B_sender.jsonl
…
```

Check `rfkill list` while the 2.4 GHz unit is running its **test** phase: both wifi and bluetooth must be `Soft blocked: yes`. They're unblocked when the unit exits.

---

## 9. Running another plan

```bash
# 1. on both RPis, flash the new firmware (e.g. switching to wifi_rssi)
sudo python3 ~/cpcbf/cpcbf/field/firmware_flash.py \
    /dev/ttyACM_WIFI mkrwifi1010_wifi_rssi \
    ~/cpcbf/cpcbf/field/firmware /tmp/flash.log

# 2. on both RPis, edit active_plans.json
sed -i 's|track_2_4ghz/01_ble_rssi|track_2_4ghz/04_wifi_rssi|' \
    ~/cpcbf/cpcbf/field/active_plans.json

# 3. reboot (or just restart the units)
sudo systemctl restart cpcbf-2_4ghz cpcbf-lora
```

A single boot's runtime depends only on `rounds × tests-per-plan`; expect a few minutes per plan with the default `rounds: 10`. Power-cycling mid-run is safe — `flock` releases on process exit, the rfkill `ExecStopPost` restores radios, and the persistent flash markers in `/var/lib/cpcbf/flashed_*.txt` mean the next boot won't re-flash unless you ask it to.

---

## 10. Collect and analyse results

After both units are done (`systemctl status cpcbf-2_4ghz.service` shows `inactive (dead)` — the units run with `RemainAfterExit=no` so they go fully inactive on exit), pull the JSONLs back:

```bash
rsync -av pi@<rpi-sender-ip>:~/cpcbf/cpcbf/field/results/ ./results/sender/
rsync -av pi@<rpi-receiver-ip>:~/cpcbf/cpcbf/field/results/ ./results/receiver/

cd cpcbf
python3 field/merge_results.py ./results/sender ./results/receiver --output ./results/merged
python3 analysis/run_analysis.py --results ./results/merged/results.jsonl --output ./results/merged/
```

---

## Troubleshooting

**`FIRMWARE_MISMATCH expected=X actual=Y`** — the on-disk flash marker doesn't match the active plan's `firmware:` field. Either flash the right firmware (step 8) or edit `active_plans.json` to point at a plan that matches what's currently on the MKR. The runner exits non-zero rather than running tests against the wrong firmware.

**`bossac: No device found on /dev/ttyACM…`** during a manual `firmware_flash.py` run — the 1200-baud touch failed to enter the bootloader. `flash_with_retry` double-taps on retry; if that still fails, press the MKR's reset button twice in quick succession to manually arm the bootloader, then re-run the flash command.

**`BRIDGE_SYNC_FAILED ... could not connect ... within 60s`** — the peer Pi isn't reachable on PSM 0x81. Confirm the other RPi's BT controller is up (`bluetoothctl show`) and that page-scan is enabled (`hciconfig hci0` should show `ISCAN`; `ExecStartPre=hciconfig hci0 piscan` in the unit handles this at boot). The runner retries up to 4 times with arbiter release in between, then exits.

**`BRIDGE_SYNC_FAILED ... manifest mismatch on 'plan'`** — the two Pis disagree on the active plan. Check that `cpcbf/field/active_plans.json` is identical on both Pis (and that the plan name matches an existing YAML in `cpcbf/plans/field/`).

**Both tracks try to acquire BT simultaneously** — by design, only one acquires at a time (`fcntl.flock`-based exclusion in `bluetooth_control_arbiter.py`). The mutex is held only around the brief bridge_sync window. If you suspect a stuck lock, `sudo rm /var/lib/cpcbf/bluetooth_control.lock` and restart the units.

**`[2_4ghz] lora bridge_sync flag not fresh — proceeding anyway`** — the 2.4 GHz unit waited up to 60 s for lora's `/var/lib/cpcbf/lora_bridge_sync_done` flag (stamped with the current boot_id) and didn't see it. Either lora wasn't configured for this boot (`active_plans.json` has `"lora": null`), or its bridge_sync took too long, or the unit failed early. The 2.4 GHz unit proceeds with rfkill anyway, but if lora is still mid-sync that will fail.

**Wi-Fi/BT stays blocked after a crash** — `ExecStopPost=/usr/sbin/rfkill unblock all` should always restore. If you killed the unit with `SIGKILL`, `ExecStopPost` may be skipped — manually `sudo rfkill unblock all` and `sudo systemctl start NetworkManager`.

**MKR WAN won't transmit / `LoRa.begin() failed`** — antenna not plugged in, or wrong region build. The MKR WAN firmware defaults to EU868; flash the `_us` variant if you're in a US915 region.
