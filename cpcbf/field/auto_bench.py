#!/usr/bin/env python3
"""CPCBF autonomous field benchmark runner — dual-track, two MKR boards.

Reads a role JSON path from argv[1] (defaults to field/role.json) and walks
plans/<plans_subdir>/**/*.yaml in alphabetical order. For each plan:

    1. acquire BluetoothControlArbiter (track-aware, with plan-boundary
       fairness so 2_4ghz yields to a pending lora request)
    2. firmware_flash.flash_with_retry(...) — bossac the env named in the
       plan's top-level `firmware:` field
    3. bridge_sync.sync(...) — L2CAP CoC manifest exchange between the two
       RPi bridges so neither side starts a test with stale state
    4. (2_4ghz only) rfkill block wifi+bluetooth for the test plan duration
    5. run_test_plan(...) — same SYNC/CONFIGURE/SETUP/START flow as before,
       extended with a `lora` protocol branch
    6. release the lock at plan boundary (lora releases earlier so its
       lock-free LoRa test can overlap with the next 2_4ghz plan)

See cpcbf/../design-choices/auto-flasher-and-runner-arduino.md for the
authoritative design.
"""
from __future__ import annotations

import datetime
import glob
import json
import os
import signal
import subprocess
import sys
import time
import yaml

from bluetooth_control_arbiter import BluetoothControlArbiter
import bridge_sync
import firmware_flash
import radio_isolation_local


REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT_BIN = "/bin/cpcbf_agent"
RELAY_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "serial_relay.py")
PLANS_DIR = os.path.join(REPO_DIR, "plans")
DEFAULT_ROLE_FILE = os.path.join(REPO_DIR, "field", "role.json")
OUTPUT_DIR = os.path.join(REPO_DIR, "field", "results")
FLASH_LOG = "/tmp/cpcbf_flash.log"
LED_PATH = "/sys/class/leds/ACT"

MAX_RETRIES = 3
SETUP_FAIL_PACING_S = 30
BOUNDARY_GRACE_S = 5.0
LORA_PENDING_CEILING_S = 60.0

SETUP_BY_PROTO = {
    "ble": "BLE_SETUP",
    "wifi": "WIFI_SETUP",
    "lora": "LORA_SETUP",
}


def led_heartbeat():
    try:
        with open(f"{LED_PATH}/trigger", "w") as f:
            f.write("heartbeat")
    except Exception:
        pass


def led_off():
    try:
        with open(f"{LED_PATH}/trigger", "w") as f:
            f.write("none")
        with open(f"{LED_PATH}/brightness", "w") as f:
            f.write("0")
    except Exception:
        pass


def send_cmd(proc, cmd_dict, timeout=120):
    proc.stdin.write(json.dumps(cmd_dict) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    return json.loads(line)


def _start_proc(role_cfg):
    board = role_cfg.get("board_type", "rpi4")
    if board.startswith("mkr_"):
        port = role_cfg["serial_port"]
        baud = str(role_cfg.get("serial_baud", 115200))
        proc = subprocess.Popen(
            ["python3", RELAY_SCRIPT, port, baud],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=open("/tmp/cpcbf_relay.log", "a"),
            text=True,
        )
        time.sleep(3)
        return proc
    return subprocess.Popen(
        ["sudo", AGENT_BIN],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=open("/tmp/cpcbf_agent.log", "a"),
        text=True,
    )


def _kill_agent(proc, protocol, board_type="rpi4"):
    try:
        send_cmd(proc, {"command": "STOP"}, timeout=10)
    except Exception:
        pass
    proc.terminate()
    proc.wait()
    if protocol != "ble" and not board_type.startswith("mkr_"):
        subprocess.run(["systemctl", "start", "NetworkManager"],
                       capture_output=True, timeout=10)


def _build_params(role_cfg, plan_global, test, payload_size):
    g = plan_global
    role_str = role_cfg["role"]
    protocol = test.get("protocol", "wifi")
    board = test.get("board", role_cfg.get("board_type", "rpi4"))

    common = {
        "role": role_str,
        "mode": test["mode"],
        "payload_size": payload_size,
        "repetitions": g.get("repetitions", test.get("repetitions", 100)),
        "warmup": g.get("warmup", test.get("warmup", 5)),
        "timeout_ms": g.get("timeout_ms", test.get("timeout_ms", 5000)),
        "inter_packet_us": g.get("inter_packet_us", test.get("inter_packet_us", 0)),
        "protocol": protocol,
    }

    if protocol == "lora":
        common.update({
            "iface_name": "",
            "peer_addr": "",
            "peer_mac": "",
            "port": 0,
            "channel": 0,
            "essid": "",
            "local_ip": "",
            "netmask": "",
            "topology": "p2p",
        })
        return common

    if protocol == "ble":
        topology = ("ble_gatt" if board == "mkr_wifi_1010"
                    else g.get("topology", test.get("topology", "ble_l2cap")))
        common.update({
            "iface_name": "" if board == "mkr_wifi_1010" else "hci0",
            "peer_addr": "",
            "peer_mac": role_cfg.get("ble_mac_peer", ""),
            "port": g.get("port", test.get("port", 128)),
            "channel": 0,
            "essid": "",
            "local_ip": "",
            "netmask": "",
            "topology": topology,
        })
        return common

    if protocol == "wifi" and board == "mkr_wifi_1010":
        is_sender = role_str == "sender"
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
        return common

    common.update({
        "iface_name": "wlan0",
        "peer_addr": role_cfg["peer_addr"],
        "peer_mac": role_cfg["peer_mac"],
        "port": g.get("port", test.get("port", 5201)),
        "channel": g.get("channel", test.get("channel", 2437)),
        "essid": g.get("essid", test.get("essid", "CPCBF_TEST")),
        "local_ip": role_cfg["local_ip"],
        "netmask": "255.255.255.0",
        "topology": g.get("topology", test.get("topology", "p2p")),
    })
    return common


def flatten_schedule(plan, rounds):
    schedule = []
    idx = 0
    for r in range(1, rounds + 1):
        for test in plan["tests"]:
            for payload_size in test["payload_sizes"]:
                schedule.append({
                    "round": r, "test": test,
                    "payload_size": payload_size, "test_idx": idx,
                })
                idx += 1
    return schedule


def run_test_plan(role_cfg, plan, label_prefix, rounds=1):
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
        setup_cmd = SETUP_BY_PROTO[protocol]

        success = False
        idx_skipped = False

        for attempt in range(1, MAX_RETRIES + 1):
            proc = _start_proc(role_cfg)
            try:
                send_cmd(proc, {"command": "CONFIGURE", "params": params})

                setup_resp = send_cmd(proc, {"command": setup_cmd}, timeout=120)
                if setup_resp.get("status") != "ok":
                    print(f"[idx={test_idx} attempt {attempt}/{MAX_RETRIES}] "
                          f"{setup_cmd} failed: {setup_resp.get('message', '?')}")
                    time.sleep(SETUP_FAIL_PACING_S)
                    continue

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
                    print(f"[idx={test_idx}] idx mismatch, peer at {peer_idx} — skipping")
                    skip_target = None
                    for j in range(i + 1, len(schedule)):
                        if schedule[j]["test_idx"] == peer_idx:
                            skip_target = j
                            break
                    if skip_target is not None:
                        i = skip_target - 1
                    else:
                        i = len(schedule)
                    idx_skipped = True
                    break

                if sync_resp.get("status") != "ok":
                    print(f"[idx={test_idx} attempt {attempt}/{MAX_RETRIES}] "
                          f"SYNC failed: {sync_resp.get('message', '?')}")
                    continue

                max_test_s = ((params["repetitions"] + params["warmup"])
                              * (params["timeout_ms"] / 1000))
                if params["inter_packet_us"] > 0:
                    max_test_s += params["repetitions"] * (params["inter_packet_us"] / 1_000_000)
                start_timeout = max(300, int(max_test_s) + 60)
                send_cmd(proc, {"command": "START"}, timeout=start_timeout)

                resp = send_cmd(proc, {"command": "GET_RESULTS"})

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
                break

            except Exception as e:
                print(f"[idx={test_idx} attempt {attempt}/{MAX_RETRIES}] exception: {e}")

            finally:
                _kill_agent(proc, protocol,
                            board_type=role_cfg.get("board_type", "rpi4"))

        if not success and not idx_skipped:
            print(f"FAILED after {MAX_RETRIES} attempts: "
                  f"{test['name']} payload={payload_size} test_idx={test_idx}")

        i += 1
        time.sleep(g.get("cooldown_s", test.get("cooldown_s", 5)))


def _flash_for_plan(role_cfg, plan, plan_name):
    env = plan.get("firmware")
    if not env:
        print(f"[{plan_name}] no `firmware:` field — skipping flash")
        return
    port = role_cfg["serial_port"]
    fw_dir = role_cfg.get("firmware_dir",
                          os.path.join(os.path.dirname(__file__), "firmware"))
    print(f"[{plan_name}] flashing {env} on {port}")
    firmware_flash.flash_with_retry(port, env, fw_dir, FLASH_LOG)


def _bridge_sync_for_plan(role_cfg, plan, plan_name, plan_idx, rounds):
    """Run one bridge-side handshake per plan (covers all rounds in that plan).

    Used after every successful flash. A mismatch raises BridgeSyncError so
    the caller can skip the plan.
    """
    peer_mac = role_cfg.get("bridge_peer_bt_mac")
    if not peer_mac:
        print(f"[{plan_name}] no bridge_peer_bt_mac — skipping bridge sync")
        return
    manifest = {
        "track": role_cfg["track"],
        "plan": plan_name,
        "firmware": plan.get("firmware", ""),
        "round": rounds,
        "mode": ",".join(t["mode"] for t in plan.get("tests", [])),
        "role": role_cfg["role"],
    }
    print(f"[{plan_name}] bridge sync with {peer_mac}")
    bridge_sync.sync(role_cfg["role"], peer_mac, manifest)


def _wait_for_lora_pending_clear(timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    flag = "/var/lib/cpcbf/lora_pending.flag"
    while os.path.exists(flag) and time.monotonic() < deadline:
        time.sleep(0.5)


def _run_track(role_cfg, arbiter):
    track = role_cfg["track"]
    label = role_cfg.get("label", datetime.datetime.now().strftime("%Y%m%d_%H%M"))
    label = f"{label}_{track}"
    rounds = role_cfg.get("rounds", 1)
    plans_subdir = role_cfg.get("plans_subdir", "")
    plans_dir = os.path.join(PLANS_DIR, plans_subdir)
    plan_files = sorted(glob.glob(os.path.join(plans_dir, "**/*.yaml"),
                                  recursive=True))

    if not plan_files:
        print(f"[{track}] no plans found in {plans_dir}")
        return

    for plan_idx, plan_path in enumerate(plan_files):
        plan_name = os.path.splitext(os.path.basename(plan_path))[0]

        # Boundary fairness yield (skip on first plan).
        if plan_idx > 0:
            arbiter.release()
            time.sleep(BOUNDARY_GRACE_S)
            if track == "2_4ghz":
                _wait_for_lora_pending_clear(LORA_PENDING_CEILING_S)

        arbiter.acquire(blocking=True)

        with open(plan_path) as f:
            plan = yaml.safe_load(f)

        try:
            _flash_for_plan(role_cfg, plan, plan_name)
        except firmware_flash.FlashError as e:
            print(f"[{plan_name}] FLASH_FAILED: {e}")
            continue

        try:
            _bridge_sync_for_plan(role_cfg, plan, plan_name, plan_idx, rounds)
        except bridge_sync.BridgeSyncError as e:
            print(f"[{plan_name}] BRIDGE_SYNC_FAILED: {e}")
            continue

        # 2_4ghz: block RPi wifi+bt for the test plan; lora: drop the lock so
        # the next 2_4ghz plan can flash while LoRa runs.
        if track == "2_4ghz":
            radio_isolation_local.block_2_4ghz()
            try:
                run_test_plan(role_cfg, plan, label, rounds=rounds)
            finally:
                radio_isolation_local.unblock_all()
        else:
            arbiter.release()
            run_test_plan(role_cfg, plan, label, rounds=rounds)


def _install_signal_handlers(arbiter):
    def _handler(signum, _frame):
        try:
            radio_isolation_local.unblock_all()
        except Exception:
            pass
        try:
            arbiter.release()
        except Exception:
            pass
        led_off()
        sys.exit(128 + signum)

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def main():
    role_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ROLE_FILE

    led_heartbeat()
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    with open(role_path) as f:
        role_cfg = json.load(f)

    track = role_cfg.get("track", "2_4ghz")
    role_cfg["track"] = track
    arbiter = BluetoothControlArbiter(track)
    _install_signal_handlers(arbiter)

    try:
        _run_track(role_cfg, arbiter)
    finally:
        try:
            radio_isolation_local.unblock_all()
        except Exception:
            pass
        try:
            arbiter.release()
        except Exception:
            pass
        led_off()


if __name__ == "__main__":
    main()
