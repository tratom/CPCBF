#!/usr/bin/env python3
"""CPCBF field benchmark runner — one plan per boot.

Reads a role JSON path from argv[1], looks up which plan to run for this
role's track in cpcbf/field/active_plans.json, runs that one plan for
role_cfg["rounds"] rounds, then exits. The two systemd units
(cpcbf-2_4ghz.service, cpcbf-lora.service) start in parallel at boot and
each invoke this script with its own role file.

To run a different plan:
    1. flash the matching firmware on the MKR with bossac (or with
       cpcbf/field/firmware_flash.py invoked directly from the CLI)
    2. edit cpcbf/field/active_plans.json so the entry for the relevant
       track points at the new plan name
    3. reboot both Pis (or `systemctl restart cpcbf-2_4ghz cpcbf-lora`)

Earlier multi-plan / in-runner-flash logic was removed: the cross-track
plan-boundary fairness arbiter, per-plan reflashing, plan globbing, and
the per-track PSM split in bridge_sync. The simpler v2 architecture
makes the harness behaviour line up with the lab orchestrator
(cpcbf/controller/orchestrator.py) for everything except its choice of
transport.
"""
from __future__ import annotations

import datetime
import json
import os
import signal
import subprocess
import sys
import time
import yaml

from bluetooth_control_arbiter import BluetoothControlArbiter
import bridge_sync
import radio_isolation_local


REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT_BIN = "/bin/cpcbf_agent"
RELAY_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "serial_relay.py")
PLANS_DIR = os.path.join(REPO_DIR, "plans", "field")
ACTIVE_PLANS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "active_plans.json")
DEFAULT_ROLE_FILE = os.path.join(REPO_DIR, "field", "role.json")
OUTPUT_DIR = os.path.join(REPO_DIR, "field", "results")
FIRMWARE_MARKER_DIR = "/var/lib/cpcbf"
LED_PATH = "/sys/class/leds/ACT"

MAX_RETRIES = 3
SETUP_FAIL_PACING_S = 30
BRIDGE_SYNC_RETRIES = 4
BRIDGE_SYNC_BACKOFF_S = 5.0

# Flag the lora track touches once its bridge_sync is done. The 2_4ghz
# track waits for it before rfkill-blocking BT, so lora's bridge_sync
# (which needs RPi BT) can't be cut off by rfkill mid-handshake.
LORA_BRIDGE_DONE_FLAG = "/var/lib/cpcbf/lora_bridge_sync_done"
LORA_BRIDGE_WAIT_S = 60.0

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


def _build_params(role_cfg, plan_global, test, payload_size, peer_ble_mac=""):
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
        "inter_packet_us": g.get("inter_packet_us",
                                 test.get("inter_packet_us", 0)),
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
        # peer_mac comes from bridge_sync (peer's GET_BLE_ADDR), not role
        # config — see _discover_local_ble_mac and the manifest in main().
        common.update({
            "iface_name": "" if board == "mkr_wifi_1010" else "hci0",
            "peer_addr": "",
            "peer_mac": peer_ble_mac,
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


def run_test_plan(role_cfg, plan, label_prefix, rounds=1,
                  peer_ble_mac="", clock_offset_us=None):
    g = plan["global"]
    schedule = flatten_schedule(plan, rounds)
    role_str = role_cfg["role"]

    i = 0
    while i < len(schedule):
        entry = schedule[i]
        test = entry["test"]
        payload_size = entry["payload_size"]
        test_idx = entry["test_idx"]
        round_num = entry["round"]

        protocol = test.get("protocol", "wifi")
        params = _build_params(role_cfg, g, test, payload_size, peer_ble_mac)
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
                    peer_idx = sync_resp.get("data", {}).get("peer_test_idx",
                                                              test_idx)
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

                # Receiver fires START immediately; sender pauses 2 s for
                # non-RSSI modes so the receiver is past its setup epilogue
                # before the first packet arrives. Mirrors orchestrator.py.
                if role_str == "sender" and test["mode"] != "rssi":
                    time.sleep(2.0)

                max_test_s = ((params["repetitions"] + params["warmup"])
                              * (params["timeout_ms"] / 1000))
                if params["inter_packet_us"] > 0:
                    max_test_s += (params["repetitions"]
                                   * (params["inter_packet_us"] / 1_000_000))
                start_timeout = max(300, int(max_test_s) + 60)
                send_cmd(proc, {"command": "START"}, timeout=start_timeout)

                resp = send_cmd(proc, {"command": "GET_RESULTS"})

                post_sync_timeout = max(120000,
                                        50 * params["timeout_ms"] + 30000)
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
                    "board": test.get("board",
                                      role_cfg.get("board_type", "rpi4")),
                    "payload_size": payload_size,
                    "repetitions": params["repetitions"],
                    "warmup": params["warmup"],
                    "topology": params["topology"],
                    "early_aborted": result_data.get("early_aborted", 0),
                    role_str: result_data,
                    "clock_offset_us": clock_offset_us,
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


def _firmware_marker_path(serial_port):
    # Key on the udev symlink basename (e.g. "ttyACM_WIFI"), not realpath:
    # the kernel may hand out a different /dev/ttyACMN minor after a flash
    # re-enumerate, but the symlink stays the same. Mirrors firmware_flash.py.
    return os.path.join(FIRMWARE_MARKER_DIR,
                        f"flashed_{os.path.basename(serial_port)}.txt")


def _verify_firmware_marker(role_cfg, plan, plan_name):
    """Sanity check: the on-disk flash marker should match the plan's firmware.

    The marker is written by cpcbf/field/firmware_flash.py after a
    successful bossac. If it doesn't match (or doesn't exist), the user
    forgot to flash, so we abort instead of running the test against the
    wrong firmware.
    """
    expected = plan.get("firmware")
    if not expected:
        return
    marker = _firmware_marker_path(role_cfg["serial_port"])
    try:
        with open(marker) as f:
            actual = f.read().strip()
    except FileNotFoundError:
        actual = None
    if actual != expected:
        raise SystemExit(
            f"[{plan_name}] FIRMWARE_MISMATCH expected={expected!r} "
            f"actual={actual!r} (marker={marker}) — please flash and retry"
        )


def _discover_local_ble_mac(role_cfg):
    """Open a relay to the MKR, query GET_BLE_ADDR, close the relay.

    Only meaningful for BLE plans on the mkr_wifi_1010. Returns "" if the
    board can't answer.
    """
    board = role_cfg.get("board_type", "rpi4")
    if not board.startswith("mkr_"):
        return ""
    proc = _start_proc(role_cfg)
    resp = {}
    try:
        resp = send_cmd(proc, {"command": "GET_BLE_ADDR"}, timeout=15)
    except Exception as e:
        print(f"[ble_mac] GET_BLE_ADDR failed: {e}")
    finally:
        try:
            send_cmd(proc, {"command": "STOP"}, timeout=5)
        except Exception:
            pass
        proc.terminate()
        proc.wait()
    if resp.get("status") != "ok":
        return ""
    return (resp.get("data", {}).get("ble_mac") or "").lower()


def _resolve_active_plan(role_cfg):
    """Decide which plan path to load this boot.

    Order of precedence:
      1. role_cfg["active_plan"] — per-Pi pin (rare, for benches)
      2. active_plans.json[role_cfg["track"]] — normal case

    Plan paths are RELATIVE to cpcbf/plans/field/. The .yaml suffix is
    optional. None / empty value = skip this track for this boot.
    """
    rel = role_cfg.get("active_plan")
    if not rel:
        try:
            with open(ACTIVE_PLANS) as f:
                rel = json.load(f).get(role_cfg["track"])
        except FileNotFoundError:
            raise SystemExit(
                f"active_plans.json not found at {ACTIVE_PLANS} — create it "
                f"with an entry for {role_cfg['track']!r}"
            )
    if not rel:
        print(f"[{role_cfg['track']}] no active plan configured — exiting")
        return None
    if not rel.endswith(".yaml"):
        rel = rel + ".yaml"
    path = os.path.join(PLANS_DIR, rel)
    if not os.path.isfile(path):
        raise SystemExit(f"active plan {rel!r} not found at {path}")
    return path


def _bridge_sync_with_retry(role_cfg, plan, plan_name, rounds, local_ble_mac,
                            want_clock_offset, arbiter):
    """Run bridge_sync.sync with up to BRIDGE_SYNC_RETRIES attempts.

    Each failure releases the arbiter, sleeps a beat, and re-acquires —
    in case the peer Pi's other-track service is contending for its own
    BT controller. With single-PSM bridge sync, a track/plan mismatch
    surfaces in ~1 s as a manifest mismatch rather than as a 60 s
    connect timeout, so the budget converges fast.
    """
    manifest = {
        "track": role_cfg["track"],
        "plan": plan_name,
        "firmware": plan.get("firmware", ""),
        "round": rounds,
        "mode": ",".join(t["mode"] for t in plan.get("tests", [])),
        "role": role_cfg["role"],
        "ble_mac": local_ble_mac,
    }
    peer_mac = role_cfg.get("bridge_peer_bt_mac")
    if not peer_mac:
        print(f"[{plan_name}] no bridge_peer_bt_mac — skipping bridge sync")
        return {"peer_ble_mac": "", "clock_offset_us": None}

    last_err = None
    for attempt in range(1, BRIDGE_SYNC_RETRIES + 1):
        try:
            print(f"[{plan_name}] bridge sync with {peer_mac} "
                  f"(attempt {attempt}/{BRIDGE_SYNC_RETRIES})")
            return bridge_sync.sync(role_cfg["role"], peer_mac, manifest,
                                    estimate_clock_offset=want_clock_offset)
        except bridge_sync.BridgeSyncError as e:
            last_err = e
            print(f"[{plan_name}] BRIDGE_SYNC_FAILED "
                  f"(attempt {attempt}/{BRIDGE_SYNC_RETRIES}): {e}")
            if attempt < BRIDGE_SYNC_RETRIES:
                arbiter.release()
                time.sleep(BRIDGE_SYNC_BACKOFF_S)
                arbiter.acquire(blocking=True)
    raise SystemExit(
        f"[{plan_name}] giving up bridge sync after "
        f"{BRIDGE_SYNC_RETRIES} attempts (last={last_err})"
    )


def _read_boot_id():
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except OSError:
        return ""


def _signal_lora_bridge_done():
    """Stamp the flag with the current boot_id so a stale flag left over
    from a previous boot can't confuse this boot's 2_4ghz track."""
    try:
        os.makedirs(FIRMWARE_MARKER_DIR, exist_ok=True)
        with open(LORA_BRIDGE_DONE_FLAG, "w") as f:
            f.write(_read_boot_id())
    except OSError:
        pass


def _clear_lora_bridge_done():
    try:
        os.unlink(LORA_BRIDGE_DONE_FLAG)
    except FileNotFoundError:
        pass


def _lora_track_configured():
    try:
        with open(ACTIVE_PLANS) as f:
            return bool(json.load(f).get("lora"))
    except (OSError, json.JSONDecodeError):
        return False


def _lora_bridge_done_fresh():
    """True iff the flag exists AND its content matches this boot."""
    try:
        with open(LORA_BRIDGE_DONE_FLAG) as f:
            return f.read().strip() == _read_boot_id()
    except OSError:
        return False


def _wait_for_lora_bridge_done(timeout_s):
    """Block until lora track signals bridge_sync done (this boot), or
    timeout, or lora is confirmed not running this boot. Returns True iff
    a fresh flag was seen.
    """
    if not _lora_track_configured():
        return False
    if _lora_bridge_done_fresh():
        return True
    print(f"[2_4ghz] waiting up to {timeout_s:.0f}s for lora bridge_sync...")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _lora_bridge_done_fresh():
            return True
        time.sleep(0.5)
    print("[2_4ghz] lora bridge_sync flag not fresh — proceeding anyway")
    return False


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
        plan_path = _resolve_active_plan(role_cfg)
        if plan_path is None:
            return
        plan_name = os.path.splitext(os.path.basename(plan_path))[0]
        with open(plan_path) as f:
            plan = yaml.safe_load(f)

        _verify_firmware_marker(role_cfg, plan, plan_name)

        rounds = role_cfg.get("rounds", 1)
        is_ble = any(t.get("protocol") == "ble"
                     for t in plan.get("tests", []))
        is_flood = any(t.get("mode") == "flood"
                       for t in plan.get("tests", []))

        local_ble_mac = _discover_local_ble_mac(role_cfg) if is_ble else ""

        arbiter.acquire(blocking=True)
        try:
            peer_info = _bridge_sync_with_retry(
                role_cfg, plan, plan_name, rounds, local_ble_mac,
                want_clock_offset=is_flood, arbiter=arbiter,
            )
        finally:
            if track == "lora":
                _signal_lora_bridge_done()
            arbiter.release()

        peer_ble_mac = peer_info["peer_ble_mac"]
        clock_offset_us = peer_info["clock_offset_us"]

        label = role_cfg.get("label",
                             datetime.datetime.now().strftime("%Y%m%d_%H%M"))
        label = f"{label}_{track}_{plan_name}"

        if track == "2_4ghz":
            # Don't rfkill-block BT until lora has finished its bridge_sync;
            # otherwise lora's L2CAP CoC handshake gets cut off mid-stream.
            _wait_for_lora_bridge_done(LORA_BRIDGE_WAIT_S)
            radio_isolation_local.block_2_4ghz()
            try:
                run_test_plan(role_cfg, plan, label, rounds=rounds,
                              peer_ble_mac=peer_ble_mac,
                              clock_offset_us=clock_offset_us)
            finally:
                radio_isolation_local.unblock_all()
        else:
            try:
                run_test_plan(role_cfg, plan, label, rounds=rounds,
                              peer_ble_mac=peer_ble_mac,
                              clock_offset_us=clock_offset_us)
            finally:
                _clear_lora_bridge_done()
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
