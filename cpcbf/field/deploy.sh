#!/bin/bash
# Usage: sudo ./deploy.sh sender|receiver [2_4ghz|lora|both]
#
# Sets up the autonomous field rig on this RPi: copies role files, installs
# systemd units, registers udev symlinks, creates the arbiter state dir, and
# installs the OS-level dependencies (bossa-cli for flashing, rfkill,
# python3-serial, python3-yaml, bluez).
set -euo pipefail

ROLE="${1:-}"
TRACK="${2:-both}"

if [[ "$ROLE" != "sender" && "$ROLE" != "receiver" ]]; then
    echo "Usage: sudo ./deploy.sh sender|receiver [2_4ghz|lora|both]"
    exit 1
fi
if [[ "$TRACK" != "2_4ghz" && "$TRACK" != "lora" && "$TRACK" != "both" ]]; then
    echo "Track must be 2_4ghz, lora, or both"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# OS deps
apt-get install -y bossa-cli rfkill python3-serial python3-yaml bluez

mkdir -p /var/lib/cpcbf

install_track() {
    local t="$1"
    cp "${SCRIPT_DIR}/role_${ROLE}_${t}.json" "${SCRIPT_DIR}/role_${t}.json"
    sed "s|__SCRIPT_DIR__|${SCRIPT_DIR}|g" \
        "${SCRIPT_DIR}/cpcbf-${t}.service" \
        > "/etc/systemd/system/cpcbf-${t}.service"
    systemctl enable "cpcbf-${t}.service"
    echo "Installed cpcbf-${t}.service (role_${t}.json)"
}

if [[ "$TRACK" == "both" ]]; then
    install_track 2_4ghz
    install_track lora
else
    install_track "$TRACK"
fi

# udev rules for stable ttyACM_WIFI / ttyACM_WAN symlinks
cp "${SCRIPT_DIR}/udev/99-cpcbf-mkr.rules" /etc/udev/rules.d/99-cpcbf-mkr.rules
udevadm control --reload
udevadm trigger

systemctl daemon-reload

if [[ ! -f "${SCRIPT_DIR}/active_plans.json" ]]; then
    echo "WARNING: ${SCRIPT_DIR}/active_plans.json missing — the runner will" \
         "exit immediately. Create it before reboot."
fi

cat <<EOF
Deployed as ${ROLE} (track=${TRACK}).
  Role configs:   ${SCRIPT_DIR}/role_2_4ghz.json, ${SCRIPT_DIR}/role_lora.json
  Active plans:   ${SCRIPT_DIR}/active_plans.json
  Plans dir:      ${SCRIPT_DIR}/../plans/field/
  Firmware bin:   ${SCRIPT_DIR}/firmware/
  Results dir:    ${SCRIPT_DIR}/results/

Next steps:
  1. Build firmware on the workstation and copy to ${SCRIPT_DIR}/firmware/:
       mkrwifi1010_{ble,wifi}_{rssi,rtt,flood}.bin
       mkrwan1300_lora_{rssi,rtt,flood}.bin
  2. Verify runtime PIDs of attached MKR boards: lsusb
  3. Edit role_*.json to set bridge_peer_bt_mac (the OTHER RPi's BT MAC).
  4. Edit active_plans.json to pick which plan each track runs at boot.
  5. Flash the MKRs to match the active plan:
       sudo python3 ${SCRIPT_DIR}/firmware_flash.py /dev/ttyACM_WIFI \\
            mkrwifi1010_<plan> ${SCRIPT_DIR}/firmware /tmp/flash.log
  6. Reboot. Both units will start in parallel, run their plan once, exit.
EOF
