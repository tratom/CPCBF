# CPCBF — Cross-Platform Communication Benchmarking Framework

Automated WiFi latency and throughput benchmarking between Raspberry Pi 4 pairs over Wi-Fi Direct.

## Prerequisites

### Hardware

- 2× Raspberry Pi 4 Model B (referred to as **rpi4_a** and **rpi4_b**)
- Both RPis connected to the same Ethernet network (for SSH control)
- A workstation on the same Ethernet network (the **controller**)

### Software — Raspberry Pi (both boards)

- Raspberry Pi OS (Bookworm or Bullseye, 32-bit or 64-bit)
- Build tools: `sudo apt update && sudo apt install -y build-essential cmake git libbluetooth-dev`
- SSH enabled: `sudo raspi-config` → Interface Options → SSH → Enable
- `wpa_supplicant` installed (ships by default)

### Software — Controller (your workstation)

- Python 3.10+
- pip

---

## Step 1: Build the Agent on Each Raspberry Pi

SSH into each RPi and clone the repository (or copy the `cpcbf/agent/` directory):

```bash
# On each RPi:
ssh pi@rpi4-a.local   # (or rpi4-b.local)

# Clone or copy the project
git clone https://github.com/tratom/CPCBF.git ~/common-bench
cd ~/common-bench/cpcbf/agent

# Build the agent binary
mkdir -p build && cd build
cmake -DBUILD_RPI4=ON ..
make

# Deploy to the expected location
sudo cp cpcbf_agent /bin/cpcbf_agent
sudo chmod +x /bin/cpcbf_agent
```

Repeat on both RPis.

### Verify the build

```bash
# Run the packet codec unit tests (should show 10/10 passed)
./test_packet_codec
```

---

## Step 2: Get WiFi MAC Addresses

On each RPi, note the WiFi MAC address:

```bash
cat /sys/class/net/wlan0/address
```

Example output: `dc:a6:32:81:07:a0`

---

## Step 3: Configure the Inventory

On the controller workstation, edit `cpcbf/inventory.yaml` with the actual values:

```yaml
hosts:
  rpi4_a:
    hostname: rpi4-a.local       # or IP address
    username: pi
    password: raspberry          # or use key_filename instead
    wifi_mac: "DC:A6:32:81:07:A0"
    agent_binary: /tmp/cpcbf_agent

  rpi4_b:
    hostname: rpi4-b.local
    username: pi
    password: raspberry
    wifi_mac: "DC:A6:32:xx:xx:xx"  # <-- replace with rpi4_b's actual MAC
    agent_binary: /tmp/cpcbf_agent
```

### Verify SSH connectivity

```bash
ssh pi@rpi4-a.local echo "OK"
ssh pi@rpi4-b.local echo "OK"
```

---

## Step 4: Install the Python Controller

On the controller workstation:

```bash
cd common-bench/cpcbf

# Create a virtual environment (recommended)
python3 -m venv .venv
source .venv/bin/activate

# Install the controller and all dependencies
pip install -e .
```

This installs the `cpcbf` command-line tool.

---

## Step 5: Run a Smoke Test

Start with the quick smoke test (30 reps, 128B payload, ~2 minutes):

```bash
cpcbf plans/wifi_rpi4_quick_smoke.yaml -i inventory.yaml -o ./results -v
```

Flags:
- `-i` — path to inventory file
- `-o` — output directory (default: `./results`)
- `-v` — verbose logging

### What happens during a test

1. The controller connects to both RPis via SSH over Ethernet
2. Launches the agent binary on each RPi (`sudo /tmp/cpcbf_agent`)
3. Disables Bluetooth on both boards (radio isolation)
4. Configures Wi-Fi Direct: rpi4_a becomes the GO (192.168.49.1), rpi4_b the client (192.168.49.2)
5. The receiver starts listening, then 2 seconds later the sender begins
6. Packets are exchanged over UDP, timestamped, and CRC-verified
7. Results are collected and saved to `results/results.jsonl`

---

## Step 6: Run Full Benchmarks

### RTT benchmark (ping-pong, 100 reps, 5 payload sizes)

```bash
cpcbf plans/wifi_rpi4_rtt.yaml -i inventory.yaml -o ./results -v
```

### Throughput benchmark (flood mode, 1000 reps, 5 payload sizes)

```bash
cpcbf plans/wifi_rpi4_flood.yaml -i inventory.yaml -o ./results -v
```

Both append results to `results/results.jsonl`.

---

## Step 7: Analyse Results

### Ingest into SQLite

```python
from analysis.ingest import ingest_jsonl

count = ingest_jsonl("results/results.jsonl", "results/benchmark.db")
print(f"Ingested {count} test runs")
```

### Compute statistics

```python
from analysis.stats import compute_all_stats

df = compute_all_stats("results/benchmark.db")
print(df.to_string())
```

Output includes per-run: RTT mean/median/P95/P99, 95% CI, packet loss %, jitter, RSSI, throughput (flood mode).

### Generate plots

```python
from analysis.plots import (
    plot_rtt_boxplot,
    plot_rtt_cdf,
    plot_throughput_bar,
    plot_loss_bar,
    plot_rssi_timeseries,
)

db = "results/benchmark.db"
plot_rtt_boxplot(db, "results/rtt_boxplot.png")
plot_rtt_cdf(db, "results/rtt_cdf.png")
plot_throughput_bar(db, "results/throughput.png")
plot_loss_bar(db, "results/loss.png")
plot_rssi_timeseries(db, "results/rssi.png")
```

### Comparison table

```python
from analysis.compare import comparison_table

table = comparison_table("results/benchmark.db")
print(table.to_markdown(index=False))
```

---

## Step 8: Validate Against Baseline Tools

To sanity-check results, compare against standard tools over the same Wi-Fi Direct link:

```bash
# On rpi4_a (after Wi-Fi Direct is established):
ping -c 100 192.168.49.2

# Throughput (install iperf3 on both RPis first):
# On rpi4_b: iperf3 -s
# On rpi4_a: iperf3 -c 192.168.49.2
```

---

## Field Mode: Autonomous Benchmarking (No Ethernet)

For field benchmarks (underground garage, open air, indoor) where Ethernet is impractical, both RPis can run tests autonomously on boot using a Python wrapper + systemd.

Each RPi reads the same YAML plans from disk, spawns the C agent as a subprocess, and pipes JSON commands — the same protocol the SSH controller uses. The ACT LED blinks during tests and turns off when done.

### Field Prerequisites

- Both RPis already have the agent binary built and installed (Step 1 above)
- A USB battery pack or portable power supply for each RPi

### Field Step 1: Deploy to Each RPi

SSH into each RPi and copy the `cpcbf/field/` directory:

```bash
# On each RPi:
cd ~/common-bench/cpcbf/field

# TODO: ADD MAC ADDRESS CHANGE ON THE ROLE JSON FILE after ifconfig --> p2p-wlan0

# On rpi4_a (the GO / sender):
sudo ./deploy.sh sender

# On rpi4_b (the client / receiver):
sudo ./deploy.sh receiver
```

This will:
- Install PyYAML if needed
- Copy the role template to `cpcbf/field/role.json` (the active config)
- Install and enable the `cpcbf-auto` systemd service

Everything else (plans, script, results) stays in the repo directory — no files copied to `/etc` or `/opt`.

### Field Step 2: Set the Label for Each Location

Before each field session, edit the label on both RPis:

```bash
nano ~/common-bench/cpcbf/field/role.json
```

Change `"label"` to describe the location and distance, e.g. `"garage_10m"`, `"openair_40m"`, `"indoor_20m"`. Both RPis should use the same label.

### Field Step 3: Run Tests

1. Place RPis at the desired distance
2. Power on both (battery pack)
3. **LED blinks** = tests running
4. **LED off** = all tests completed, safe to power off

No manual intervention needed. The Wi-Fi Direct P2P handshake provides synchronization — if one RPi boots faster, the P2P setup simply waits for the other.

### Field Step 4: Collect and Merge Results

After all locations are done, connect the RPis to a network and pull the results into a single directory:

```bash
mkdir -p field_data
scp pi@rpi4-a.local:~/common-bench/cpcbf/field/results/*.jsonl field_data/
scp pi@rpi4-b.local:~/common-bench/cpcbf/field/results/*.jsonl field_data/
```

Each RPi saves its own side of the data (e.g. `garage_10m_rtt_128B_sender.jsonl` and `garage_10m_rtt_128B_receiver.jsonl`). Merge matching pairs into a single JSONL file for analysis:

```bash
cd cpcbf
python field/merge_results.py ../field_data/ -o results/results.jsonl
```

### Field Step 5: Run Analysis

Once merged, run the full analysis pipeline:

```bash
python run_analysis.py --results results/results.jsonl --output results/
```

This will:
1. Ingest the JSONL into a SQLite database (`results/benchmark.db`)
2. Compute per-run statistics (RTT mean/median/P95/P99, 95% CI, packet loss, jitter, RSSI, throughput)
3. Print a comparison table and save it as CSV
4. Generate plots in `results/plots/` (RTT boxplot, RTT CDF, throughput bar, loss bar, RSSI timeseries)
5. Export raw per-packet data as CSV

### Re-running at Another Location

Either reboot both RPis or manually restart the service:

```bash
sudo systemctl restart cpcbf-auto
```

### LED Reference

| State | ACT LED |
|-------|---------|
| Booting / tests running | Heartbeat blink |
| All tests completed | Off (solid) |
| Error (service crashed) | Stays blinking |

### Field Troubleshooting

| Problem | Fix |
|---------|-----|
| RPi loses Ethernet after reboot | The service restarts NetworkManager automatically after tests. If it still fails, run `sudo systemctl start NetworkManager` manually |
| Empty result files | Check agent logs: `journalctl -u cpcbf-auto` and `/tmp/cpcbf_agent.log` |
| P2P connection never forms | Ensure both RPis are powered on within ~60s of each other. Check that `peer_mac` in `role.json` matches the other RPi's wlan0 MAC |
| Tests take too long | Edit the YAML plans in `cpcbf/plans/` to reduce repetitions or payload sizes |
| Want to disable auto-run on boot | `sudo systemctl disable cpcbf-auto` |

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| SSH connection refused | Ensure SSH is enabled: `sudo systemctl enable --now ssh` |
| Agent binary not found | Re-run Step 1 and verify `/tmp/cpcbf_agent` exists |
| Wi-Fi Direct fails to form | Ensure `wpa_supplicant` is installed and wlan0 exists. Try `sudo wpa_cli -i wlan0 p2p_find` manually |
| `SO_BINDTODEVICE` permission error | The agent must run with `sudo` (the controller does this automatically) |
| Bluetooth still active error | Run `sudo rfkill block bluetooth` manually on both RPis to verify rfkill works |
| Timeout during test | Increase `timeout_ms` in the plan YAML, or check that the P2P link is established with `ip addr show` |
| No results / empty JSONL | Check agent stderr logs: `sudo /tmp/cpcbf_agent < /dev/null` to see if it starts correctly |

---

## Project Structure

```
cpcbf/
├── agent/                      # C agent (runs on RPi4)
│   ├── common/                 # Platform-independent core
│   │   ├── benchmark_packet.h/c   # Wire protocol + CRC-32
│   │   ├── test_engine.h/c        # Ping-pong & flood test logic
│   │   ├── protocol_adapter.h     # Adapter vtable interface
│   │   ├── platform_hal.h         # Platform abstraction
│   │   └── cjson/                 # Vendored cJSON (MIT)
│   ├── platforms/rpi4/         # RPi4-specific code
│   │   ├── main.c                 # JSON command loop
│   │   ├── platform_rpi4.c        # HAL implementation
│   │   └── wifi_setup.h/c         # Wi-Fi Direct & ad-hoc setup
│   ├── adapters/
│   │   └── wifi_adapter.c         # UDP socket adapter
│   ├── tests/
│   │   └── test_packet_codec.c    # Unit tests (10 cases)
│   └── CMakeLists.txt
├── controller/                 # Python controller (runs on workstation)
│   ├── cli.py                     # CLI entry point
│   ├── orchestrator.py            # Test orchestration
│   ├── ssh_transport.py           # SSH + JSON communication
│   ├── agent_manager.py           # Multi-agent management
│   ├── radio_isolation.py         # Preflight radio checks
│   ├── clock_sync.py              # NTP-like clock offset
│   ├── plan_parser.py             # YAML plan loader
│   └── models.py                  # Data classes
├── analysis/                   # Post-processing (runs on workstation)
│   ├── ingest.py                  # JSONL → SQLite
│   ├── stats.py                   # Statistical analysis
│   ├── plots.py                   # Matplotlib visualizations
│   └── compare.py                 # Cross-run comparison
├── field/                      # Autonomous field benchmarking
│   ├── auto_bench.py              # Boot-time test runner
│   ├── merge_results.py           # Merge sender+receiver JSONL
│   ├── role_sender.json           # Sender board config template
│   ├── role_receiver.json         # Receiver board config template
│   ├── cpcbf-auto.service         # systemd unit file
│   └── deploy.sh                  # One-command setup script
├── plans/                      # Test plan YAML files
│   ├── wifi_rpi4_rtt.yaml
│   ├── wifi_rpi4_flood.yaml
│   └── wifi_rpi4_quick_smoke.yaml
├── inventory.yaml              # Host inventory (edit this)
└── pyproject.toml
```
