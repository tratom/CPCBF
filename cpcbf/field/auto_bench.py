#!/usr/bin/env python3
"""CPCBF autonomous field benchmark runner."""

import subprocess, json, yaml, sys, os, glob, time, datetime

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT_BIN = "/bin/cpcbf_agent"
PLANS_DIR = os.path.join(REPO_DIR, "plans")
ROLE_FILE = os.path.join(REPO_DIR, "field", "role.json")
OUTPUT_DIR = os.path.join(REPO_DIR, "field", "results")
LED_PATH = "/sys/class/leds/ACT"


def led_heartbeat():
    """Set ACT LED to heartbeat mode (blinking)."""
    try:
        with open(f"{LED_PATH}/trigger", "w") as f:
            f.write("heartbeat")
    except Exception:
        pass


def led_off():
    """Turn ACT LED off (solid off = done)."""
    try:
        with open(f"{LED_PATH}/trigger", "w") as f:
            f.write("none")
        with open(f"{LED_PATH}/brightness", "w") as f:
            f.write("0")
    except Exception:
        pass


def send_cmd(proc, cmd_dict, timeout=120):
    """Send JSON command to agent, read JSON response."""
    proc.stdin.write(json.dumps(cmd_dict) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    return json.loads(line)


def run_test_plan(role_cfg, plan, label_prefix, round_num=1):
    """Run one YAML plan through the agent."""
    g = plan["global"]
    for test in plan["tests"]:
        for payload_size in test["payload_sizes"]:
            # Start agent process
            proc = subprocess.Popen(
                ["sudo", AGENT_BIN],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=open("/tmp/cpcbf_agent.log", "a"),
                text=True,
            )
            try:
                # CONFIGURE
                role_str = role_cfg["role"]
                params = {
                    "iface_name": "wlan0",
                    "peer_addr": role_cfg["peer_addr"],
                    "peer_mac": role_cfg["peer_mac"],
                    "port": g.get("port", test.get("port", 5201)),
                    "channel": g.get("channel", test.get("channel", 2437)),
                    "local_ip": role_cfg["local_ip"],
                    "netmask": "255.255.255.0",
                    "role": role_str,
                    "topology": g.get("topology", test.get("topology", "p2p")),
                    "mode": test["mode"],
                    "payload_size": payload_size,
                    "repetitions": g.get("repetitions", test.get("repetitions", 100)),
                    "warmup": g.get("warmup", test.get("warmup", 5)),
                    "timeout_ms": g.get("timeout_ms", test.get("timeout_ms", 5000)),
                    "inter_packet_us": g.get("inter_packet_us", test.get("inter_packet_us", 0)),
                }
                send_cmd(proc, {"command": "CONFIGURE", "params": params})

                # WIFI_SETUP (GO creates group, client finds & joins)
                send_cmd(proc, {"command": "WIFI_SETUP"}, timeout=120)

                # SYNC barrier — both sides exchange beacons before starting
                sync_resp = send_cmd(
                    proc,
                    {"command": "SYNC", "params": {"timeout_ms": 120000}},
                    timeout=180,
                )
                if sync_resp.get("status") != "ok":
                    print(f"SYNC failed: {sync_resp.get('message', '?')}, skipping test")
                    continue

                # START (blocking -- runs entire test)
                send_cmd(proc, {"command": "START"}, timeout=300)

                # GET_RESULTS
                resp = send_cmd(proc, {"command": "GET_RESULTS"})

                # Save results in ingest.py-compatible format
                test_name = f"{label_prefix}_r{round_num:02d}_{test['name']}_{payload_size}B"
                result_file = os.path.join(
                    OUTPUT_DIR, f"{test_name}_{role_str}.jsonl"
                )
                # Match orchestrator.py output format exactly:
                # sender/receiver contain the full agent data dict
                record = {
                    "test_name": test_name,
                    "mode": test["mode"],
                    "protocol": "wifi",
                    "board": "rpi4",
                    "payload_size": payload_size,
                    "repetitions": params["repetitions"],
                    "warmup": params["warmup"],
                    "topology": params["topology"],
                    role_str: resp.get("data", {}),
                    "clock_offset_us": None,
                    "timestamp": time.time(),
                }
                with open(result_file, "w") as f:
                    json.dump(record, f)
                    f.write("\n")

            finally:
                # Send STOP first so agent runs wifi_teardown (restarts NetworkManager)
                try:
                    send_cmd(proc, {"command": "STOP"}, timeout=10)
                except Exception:
                    pass
                proc.terminate()
                proc.wait()
                # Safety net: always restart NetworkManager in case agent didn't
                subprocess.run(
                    ["systemctl", "start", "NetworkManager"],
                    capture_output=True, timeout=10,
                )

            # Cooldown between payload sizes
            time.sleep(g.get("cooldown_s", test.get("cooldown_s", 5)))


def restore_network():
    """Safety net: always restart NetworkManager so ethernet works."""
    subprocess.run(
        ["systemctl", "start", "NetworkManager"],
        capture_output=True, timeout=10,
    )


def main():
    led_heartbeat()
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    try:
        with open(ROLE_FILE) as f:
            role_cfg = json.load(f)

        label = role_cfg.get("label", datetime.datetime.now().strftime("%Y%m%d_%H%M"))

        # Run all YAML plans in the plans directory, repeated for N rounds
        rounds = role_cfg.get("rounds", 1)
        plan_files = sorted(glob.glob(os.path.join(PLANS_DIR, "*.yaml")))
        for r in range(1, rounds + 1):
            for plan_path in plan_files:
                with open(plan_path) as f:
                    plan = yaml.safe_load(f)
                run_test_plan(role_cfg, plan, label, round_num=r)
    finally:
        restore_network()

    led_off()


if __name__ == "__main__":
    main()
