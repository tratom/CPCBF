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


MAX_RETRIES = 3


def _kill_agent(proc, protocol):
    """Stop agent cleanly and restart NetworkManager if needed."""
    try:
        send_cmd(proc, {"command": "STOP"}, timeout=10)
    except Exception:
        pass
    proc.terminate()
    proc.wait()
    if protocol != "ble":
        subprocess.run(
            ["systemctl", "start", "NetworkManager"],
            capture_output=True, timeout=10,
        )


def _build_params(role_cfg, plan_global, test, payload_size):
    """Build CONFIGURE params from role config and plan."""
    g = plan_global
    role_str = role_cfg["role"]
    protocol = test.get("protocol", "wifi")
    topology = g.get("topology", test.get("topology",
                     "ble_l2cap" if protocol == "ble" else "p2p"))

    common = {
        "role": role_str,
        "topology": topology,
        "mode": test["mode"],
        "payload_size": payload_size,
        "repetitions": g.get("repetitions", test.get("repetitions", 100)),
        "warmup": g.get("warmup", test.get("warmup", 5)),
        "timeout_ms": g.get("timeout_ms", test.get("timeout_ms", 5000)),
        "inter_packet_us": g.get("inter_packet_us", test.get("inter_packet_us", 0)),
    }

    if protocol == "ble":
        common.update({
            "iface_name": "hci0",
            "peer_addr": "",
            "peer_mac": role_cfg["ble_mac_peer"],
            "port": g.get("port", test.get("port", 128)),
            "channel": 0,
            "essid": "",
            "local_ip": "",
            "netmask": "",
            "protocol": "ble",
        })
    else:
        common.update({
            "iface_name": "wlan0",
            "peer_addr": role_cfg["peer_addr"],
            "peer_mac": role_cfg["peer_mac"],
            "port": g.get("port", test.get("port", 5201)),
            "channel": g.get("channel", test.get("channel", 2437)),
            "local_ip": role_cfg["local_ip"],
            "netmask": "255.255.255.0",
        })

    return common


def run_test_plan(role_cfg, plan, label_prefix, round_num=1):
    """Run one YAML plan through the agent."""
    g = plan["global"]
    for test in plan["tests"]:
        for payload_size in test["payload_sizes"]:
            role_str = role_cfg["role"]
            protocol = test.get("protocol", "wifi")
            params = _build_params(role_cfg, g, test, payload_size)
            setup_cmd = "BLE_SETUP" if protocol == "ble" else "WIFI_SETUP"

            # Retry loop — on link/sync failure, both sides retry the
            # same test with a fresh agent so they stay aligned.
            success = False
            for attempt in range(1, MAX_RETRIES + 1):
                proc = subprocess.Popen(
                    ["sudo", AGENT_BIN],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=open("/tmp/cpcbf_agent.log", "a"),
                    text=True,
                )
                try:
                    # CONFIGURE
                    send_cmd(proc, {"command": "CONFIGURE", "params": params})

                    # Link setup
                    setup_resp = send_cmd(proc, {"command": setup_cmd}, timeout=120)
                    if setup_resp.get("status") != "ok":
                        print(f"[attempt {attempt}/{MAX_RETRIES}] "
                              f"{setup_cmd} failed: {setup_resp.get('message', '?')}")
                        continue

                    # Pre-test SYNC
                    sync_resp = send_cmd(
                        proc,
                        {"command": "SYNC", "params": {"timeout_ms": 120000}},
                        timeout=180,
                    )
                    if sync_resp.get("status") != "ok":
                        print(f"[attempt {attempt}/{MAX_RETRIES}] "
                              f"SYNC failed: {sync_resp.get('message', '?')}")
                        continue

                    # START (blocking)
                    send_cmd(proc, {"command": "START"}, timeout=300)

                    # GET_RESULTS
                    resp = send_cmd(proc, {"command": "GET_RESULTS"})

                    # Post-test SYNC — wait for peer to also finish
                    send_cmd(
                        proc,
                        {"command": "SYNC", "params": {"timeout_ms": 120000}},
                        timeout=180,
                    )

                    # Save results
                    test_name = (f"{label_prefix}_r{round_num:02d}"
                                 f"_{test['name']}_{payload_size}B")
                    result_file = os.path.join(
                        OUTPUT_DIR, f"{test_name}_{role_str}.jsonl"
                    )
                    record = {
                        "test_name": test_name,
                        "mode": test["mode"],
                        "protocol": protocol,
                        "board": test.get("board", "rpi4"),
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

                    success = True
                    break  # test succeeded, move to next payload/test

                except Exception as e:
                    print(f"[attempt {attempt}/{MAX_RETRIES}] "
                          f"exception: {e}")

                finally:
                    _kill_agent(proc, protocol)

            if not success:
                print(f"FAILED after {MAX_RETRIES} attempts: "
                      f"{test['name']} payload={payload_size}")

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
