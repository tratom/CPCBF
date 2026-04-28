# CPCBF — Autonomous Field Deployment

End-to-end guide for deploying the dual-track autonomous field rig:

- 2× Raspberry Pi (bridges; one is `sender`, one is `receiver`)
- 2× Arduino MKR WiFi 1010 (run BLE + WiFi tests on the 2.4 GHz track)
- 2× Arduino MKR WAN 1300 (run LoRa tests on the 868 MHz track)

Each RPi runs two systemd units in parallel: `cpcbf-2_4ghz.service` (BLE → WiFi on the MKR WiFi 1010) and `cpcbf-lora.service` (LoRa on the MKR WAN 1300). They synchronise via a Bluetooth bridge link between the two RPis after every firmware flash, and via the existing over-radio sync per round.

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

Each MKR firmware is split per (protocol × mode) so the SAMD21's 32 KB SRAM is enough. The autonomous runner reflashes between plans, so all 9 binaries must be present on the RPis.

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
- `ble_mac_peer` (in `*_2_4ghz.json` only) — the BT MAC of the **other** RPi's MKR WiFi 1010 NINA module. Read it once with: flash `mkrwifi1010_ble_rssi` to one MKR, run `screen /dev/ttyACM0 115200`, send `{"command":"BLE_SETUP"}`, copy the advertised MAC from the response.
- `firmware_dir` — adjust if the repo isn't at `/home/pi/cpcbf`.
- `label` — optional. Result files are tagged `<label>_<track>_rNN_<test>_<size>B_<role>.jsonl`.

Leave `serial_port` as `/dev/ttyACM_WIFI` and `/dev/ttyACM_WAN` — they're udev symlinks created by the rules in step 6.

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

## 8. Smoke test (one round, before the full field run)

The fastest way to validate end-to-end is to keep the role files but lower `rounds: 10` to `rounds: 1` and only one plan in each track folder:

```bash
sudo systemctl start cpcbf-2_4ghz.service
sudo systemctl start cpcbf-lora.service

# Watch progress
sudo journalctl -fu cpcbf-2_4ghz -u cpcbf-lora
```

You should see, in order, on both RPis:

1. `[arbiter] {"track":"2_4ghz","event":"acquired",...}`
2. `[01_ble_rssi] flashing mkrwifi1010_ble_rssi on /dev/ttyACM_WIFI`
3. `[01_ble_rssi] bridge sync with D8:3A:DD:...`
4. (test runs)
5. `[arbiter] {"track":"2_4ghz","event":"released",...}`
6. `[arbiter] {"track":"lora","event":"acquired",...}`
7. `[01_lora_rssi] flashing mkrwan1300_lora_rssi on /dev/ttyACM_WAN`
8. (test runs)

Result files appear under `cpcbf/field/results/`:

```text
field_2_4ghz_r01_ble_mkr1010_rssi_0B_sender.jsonl
field_lora_r01_lora_mkr1300_rssi_32B_sender.jsonl
…
```

Check `rfkill list` while the 2.4 GHz unit is running its **test** phase: both wifi and bluetooth must be `Soft blocked: yes`. They're unblocked again before the next plan's flash.

Restore the 10× `rounds` in the role files when smoke is good.

---

## 9. Field run

In the field, no Ethernet, no workstation. Power both RPis. systemd starts both units automatically. The ACT LED blinks (heartbeat) while any unit is active and goes solid-off when both finish.

A full run:

- 2.4 GHz track: 6 plans × 10 rounds × 3 modes (rssi/rtt/flood across BLE then WiFi) ≈ 60–90 min
- LoRa track:    3 plans × 10 rounds                                                ≈ 30–60 min (driven mostly by EU868 duty-cycle)

Power-cycling mid-run is safe — `flock` releases on process exit, the rfkill `ExecStopPost` restores radios, and the persistent flash markers in `/var/lib/cpcbf/flashed_*.txt` skip already-correct firmwares on restart (so a partial run resumes near where it stopped at plan boundaries).

---

## 10. Collect and analyse results

After both units are done (`systemctl status cpcbf-2_4ghz.service` shows `inactive (dead)` with `RemainAfterExit=yes`), pull the JSONLs back:

```bash
rsync -av pi@<rpi-sender-ip>:~/cpcbf/cpcbf/field/results/ ./results/sender/
rsync -av pi@<rpi-receiver-ip>:~/cpcbf/cpcbf/field/results/ ./results/receiver/

cd cpcbf
python3 field/merge_results.py ./results/sender ./results/receiver --output ./results/merged
python3 analysis/run_analysis.py --results ./results/merged/results.jsonl --output ./results/merged/
```

---

## Troubleshooting

**`bossac: No device found on /dev/ttyACM…`** — the 1200-baud touch failed to enter the bootloader. The runner's `flash_with_retry` does a double-tap on retry; if that still fails, press the MKR's reset button twice in quick succession to manually arm the bootloader, then `systemctl restart cpcbf-2_4ghz`.

**Bridge sync hangs at `connect`** — confirm the *other* RPi's BT controller is powered (`bluetoothctl show`). The `cpcbf-2_4ghz` unit blocks BT during test execution; sync only happens between plans, when BT is unblocked. If both RPis are simultaneously trying to be sender (config error), sync will time out.

**`PLAN_SKIPPED` in journald** — flash or bridge sync failed on this RPi. The peer's identical retry timing means schedules tend to realign; the radio-side `idx_mismatch` recovery will skip the plan on the other RPi too. Check `/tmp/cpcbf_flash.log` for bossac output and `journalctl -u cpcbf-…` for the failure phase.

**Both tracks try to acquire BT simultaneously** — by design, only one acquires at a time (`fcntl.flock`-based exclusion in `bluetooth_control_arbiter.py`). If you observe interleaved `acquired` events without `released` between, something is wrong with the lock file in `/var/lib/cpcbf/` — clear it (`sudo rm /var/lib/cpcbf/bluetooth_control.lock /var/lib/cpcbf/lora_pending.flag`) and restart both units.

**Wi-Fi/BT stays blocked after a crash** — `ExecStopPost=/usr/sbin/rfkill unblock all` should always restore. If you killed the unit with `SIGKILL`, `ExecStopPost` may be skipped — manually `sudo rfkill unblock all` and `sudo systemctl start NetworkManager`.

**MKR WAN won't transmit / `LoRa.begin() failed`** — antenna not plugged in, or wrong region build. The MKR WAN firmware defaults to EU868; flash the `_us` variant if you're in a US915 region.
