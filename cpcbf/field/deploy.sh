#!/bin/bash
# Usage: sudo ./deploy.sh sender|receiver
set -e
ROLE=$1

if [ -z "$ROLE" ] || { [ "$ROLE" != "sender" ] && [ "$ROLE" != "receiver" ]; }; then
    echo "Usage: sudo ./deploy.sh sender|receiver"
    exit 1
fi

# Install PyYAML if needed
pip3 install --quiet pyyaml 2>/dev/null || apt-get install -y python3-yaml

# Deploy files
mkdir -p /etc/cpcbf/plans /opt/cpcbf /home/pi/cpcbf_results
cp auto_bench.py /opt/cpcbf/
cp role_${ROLE}.json /etc/cpcbf/role.json
cp ../plans/*.yaml /etc/cpcbf/plans/
cp cpcbf-auto.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable cpcbf-auto.service
echo "Deployed as ${ROLE}. Edit /etc/cpcbf/role.json to set label before each run."
