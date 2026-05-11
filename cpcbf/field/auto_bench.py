#!/usr/bin/env python3
"""CPCBF autonomous field benchmark runner."""

import subprocess, json, yaml, sys, os, glob, time, datetime

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT_BIN = "/bin/cpcbf_agent"
RELAY_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "serial_relay.py")
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
SETUP_FAIL_PACING_S = 30  # sleep after link setup failure to stay in step with peer


def _start_proc(role_cfg):
    """Start agent or serial relay subprocess based on board_type."""
    board = role_cfg.get("board_type", "rpi4")
    if board.startswith("mkr_"):
        port = role_cfg.get("serial_port", "/dev/ttyACM0")
        baud = str(role_cfg.get("serial_baud", 115200))
        proc = subprocess.Popen(
            ["python3", RELAY_SCRIPT, port, baud],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=open("/tmp/cpcbf_relay.log", "a"),
            text=True,
        )
        # Wait for relay to be ready (2s Arduino reset + boot drain)
        time.sleep(3)
        return proc
    else:
        return subprocess.Popen(
            ["sudo", AGENT_BIN],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=open("/tmp/cpcbf_agent.log", "a"),
            text=True,
        )


def _kill_agent(proc, protocol, board_type="rpi4"):
    """Stop agent cleanly and restart NetworkManager if needed."""
    try:
        send_cmd(proc, {"command": "STOP"}, timeout=10)
    except Exception:
        pass
    proc.terminate()
    proc.wait()
    if protocol != "ble" and not board_type.startswith("mkr_"):
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
    elif protocol == "wifi" and role_cfg.get("board_type", "rpi4").startswith("mkr_"):
        # MKR WiFi: SoftAP topology, fixed IPs
        is_sender = (role_str == "sender")
        common.update({
            "iface_name": "",
            "peer_addr": "192.168.4.2" if is_sender else "192.168.4.1",
            "peer_mac": "",
            "port": g.get("port", test.get("port", 5201)),
            "channel": g.get("channel", test.get("channel", 1)),
            "essid": g.get("essid", test.get("essid", "CPCBF_MKR")),
            "local_ip": "192.168.4.1" if is_sender else "192.168.4.2",
            "netmask": "255.255.255.0",
            "topology": "softap",
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


def flatten_schedule(plan, rounds):
    """Build an ordered list of (round, test, payload_size) with test_idx.

    Both sides compute the same list from the same YAML plan in the same
    order, so test_idx is deterministic and identical on sender and receiver.
    """
    schedule = []
    idx = 0
    for r in range(1, rounds + 1):
        for test in plan["tests"]:
            for payload_size in test["payload_sizes"]:
                schedule.append({
                    "round": r,
                    "test": test,
                    "payload_size": payload_size,
                    "test_idx": idx,
                })
                idx += 1
    return schedule


def run_test_plan(role_cfg, plan, label_prefix, rounds=1):
    """Run one YAML plan through the agent with indexed SYNC."""
    g = plan["global"]
    schedule = flatten_schedule(plan, rounds)

    i = 0
    while i < len(schedule):
        entry = schedule[i]
        test = entry["test"]
        payload_size = entry["payload_size"]
        test_idx = entry["test_idx"]
        round_num = entry["round"]

        role_str = role_cfg["role"]
        protocol = test.get("protocol", "wifi")
        params = _build_params(role_cfg, g, test, payload_size)
        setup_cmd = "BLE_SETUP" if protocol == "ble" else "WIFI_SETUP"

        success = False
        idx_skipped = False
        sync_resp = {}

        for attempt in range(1, MAX_RETRIES + 1):
            proc = _start_proc(role_cfg)
            try:
                # CONFIGURE
                send_cmd(proc, {"command": "CONFIGURE", "params": params})

                # Link setup
                setup_resp = send_cmd(proc, {"command": setup_cmd}, timeout=120)
                if setup_resp.get("status") != "ok":
                    print(f"[idx={test_idx} attempt {attempt}/{MAX_RETRIES}] "
                          f"{setup_cmd} failed: {setup_resp.get('message', '?')}")
                    time.sleep(SETUP_FAIL_PACING_S)
                    continue

                # Pre-test SYNC with test_idx + phase=0
                sync_resp = send_cmd(
                    proc,
                    {"command": "SYNC", "params": {
                        "timeout_ms": 120000,
                        "test_idx": test_idx,
                        "phase": 0,
                    }},
                    timeout=180,
                )

                if sync_resp.get("status") == "idx_mismatch":
                    peer_idx = sync_resp.get("data", {}).get("peer_test_idx", test_idx)
                    print(f"[idx={test_idx}] idx mismatch, peer at {peer_idx} — skipping forward")
                    skip_target = None
                    for j in range(i + 1, len(schedule)):
                        if schedule[j]["test_idx"] == peer_idx:
                            skip_target = j
                            break
                    if skip_target is not None:
                        i = skip_target - 1  # -1 to compensate for i += 1 at loop bottom
                    else:
                        print(f"[idx={test_idx}] peer_idx {peer_idx} beyond schedule, done")
                        i = len(schedule)
                    idx_skipped = True
                    break

                if sync_resp.get("status") != "ok":
                    print(f"[idx={test_idx} attempt {attempt}/{MAX_RETRIES}] "
                          f"SYNC failed: {sync_resp.get('message', '?')}")
                    continue

                # START (blocking) — dynamic timeout based on test params
                max_test_s = ((params["repetitions"] + params["warmup"])
                              * (params["timeout_ms"] / 1000))
                if params["inter_packet_us"] > 0:
                    max_test_s += params["repetitions"] * (params["inter_packet_us"] / 1_000_000)
                start_timeout = max(300, int(max_test_s) + 60)
                send_cmd(proc, {"command": "START"}, timeout=start_timeout)

                # GET_RESULTS
                resp = send_cmd(proc, {"command": "GET_RESULTS"})

                # Post-test SYNC — dynamic timeout for early abort drain
                post_sync_timeout = max(120000, 50 * params["timeout_ms"] + 30000)
                send_cmd(
                    proc,
                    {"command": "SYNC", "params": {
                        "timeout_ms": post_sync_timeout,
                        "test_idx": test_idx,
                        "phase": 1,
                    }},
                    timeout=int(post_sync_timeout / 1000) + 60,
                )

                # Save results
                test_name = (f"{label_prefix}_r{round_num:02d}"
                             f"_{test['name']}_{payload_size}B")
                result_file = os.path.join(
                    OUTPUT_DIR, f"{test_name}_{role_str}.jsonl"
                )
                result_data = resp.get("data", {})
                record = {
                    "test_name": test_name,
                    "test_idx": test_idx,
                    "mode": test["mode"],
                    "protocol": protocol,
                    "board": test.get("board", role_cfg.get("board_type", "rpi4")),
                    "payload_size": payload_size,
                    "repetitions": params["repetitions"],
                    "warmup": params["warmup"],
                    "topology": params["topology"],
                    "early_aborted": result_data.get("early_aborted", 0),
                    role_str: result_data,
                    "clock_offset_us": None,
                    "timestamp": time.time(),
                }
                with open(result_file, "w") as f:
                    json.dump(record, f)
                    f.write("\n")

                success = True
                break  # test succeeded, move to next

            except Exception as e:
                print(f"[idx={test_idx} attempt {attempt}/{MAX_RETRIES}] "
                      f"exception: {e}")

            finally:
                _kill_agent(proc, protocol,
                           board_type=role_cfg.get("board_type", "rpi4"))

        if not success and not idx_skipped:
            print(f"FAILED after {MAX_RETRIES} attempts: "
                  f"{test['name']} payload={payload_size} test_idx={test_idx}")

        i += 1

        # Cooldown between tests
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

    role_cfg = {}
    try:
        with open(ROLE_FILE) as f:
            role_cfg = json.load(f)

        label = role_cfg.get("label", datetime.datetime.now().strftime("%Y%m%d_%H%M"))

        rounds = role_cfg.get("rounds", 1)
        plans_subdir = role_cfg.get("plans_subdir", "")
        plans_dir = os.path.join(PLANS_DIR, plans_subdir)
        plan_files = sorted(glob.glob(os.path.join(plans_dir, "**/*.yaml"), recursive=True))
        for plan_path in plan_files:
            with open(plan_path) as f:
                plan = yaml.safe_load(f)
            run_test_plan(role_cfg, plan, label, rounds=rounds)
    finally:
        if not role_cfg.get("board_type", "rpi4").startswith("mkr_"):
            restore_network()

    led_off()


if __name__ == "__main__":
    main()
