#!/bin/bash
# Usage: sudo ./deploy.sh sender|receiver
set -e
ROLE=$1

if [ -z "$ROLE" ] || { [ "$ROLE" != "sender" ] && [ "$ROLE" != "receiver" ]; }; then
    echo "Usage: sudo ./deploy.sh sender|receiver"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Install PyYAML if needed
pip3 install --quiet pyyaml 2>/dev/null || apt-get install -y python3-yaml

# Set up role config
cp "${SCRIPT_DIR}/role_${ROLE}.json" "${SCRIPT_DIR}/role.json"

# Install systemd service (only file that needs to live outside the repo)
sed "s|__SCRIPT_DIR__|${SCRIPT_DIR}|g" "${SCRIPT_DIR}/cpcbf-auto.service" > /etc/systemd/system/cpcbf-auto.service
systemctl daemon-reload
systemctl enable cpcbf-auto.service

echo "Deployed as ${ROLE}."
echo "  Role config: ${SCRIPT_DIR}/role.json"
echo "  Plans dir:   ${SCRIPT_DIR}/../plans/"
echo "  Results dir: ${SCRIPT_DIR}/results/"
echo "Edit ${SCRIPT_DIR}/role.json to set label before each run."
