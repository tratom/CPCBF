"""Bridge-side synchronization between the two RPi bridges.

Used once per boot in the single-plan-per-boot field runner. Confirms
both Pis are about to run the same plan, and piggybacks two pieces of
peer-discovery info the test harness will need afterwards:

  - ble_mac: each side's local MKR BD_ADDR (so the BLE CONFIGURE step can
    use the actual peer address instead of a stale role-file constant)
  - clock_offset_us: optional NTP-like estimate, requested for FLOOD
    plans only

Transport: BR/EDR L2CAP Connection-Oriented Channels (SOCK_SEQPACKET) on
PSM 0x81. Same family as the benchmark BLE adapter
(cpcbf/agent/adapters/ble_adapter.c) but on a distinct PSM so a stray
packet from the test rig can't land on this listener. No pairing —
security level LOW.

Receiver RPi binds + listens; sender RPi connects to the receiver's BT
MAC. A manifest mismatch (including on 'track') raises BridgeSyncError
fast — ~1 s round-trip — which the caller's retry loop can absorb. We
intentionally use a SINGLE PSM so cross-track collisions surface as that
fast manifest mismatch instead of as a 60 s kernel connect-timeout (the
previous PSM_BY_TRACK split caused exactly that 60× slowdown).
"""
from __future__ import annotations

import json
import socket
import time
from typing import Optional


BTPROTO_L2CAP = 0
BDADDR_ANY = "00:00:00:00:00:00"

PSM = 0x81

CONNECT_TIMEOUT_S = 60.0
EXCHANGE_TIMEOUT_S = 30.0
LISTEN_BACKLOG = 1
RETRY_INTERVAL_S = 1.0

CLOCK_SYNC_ROUNDS = 11
CLOCK_SYNC_INTER_S = 0.05


class BridgeSyncError(Exception):
    pass


def _send_line(sock: socket.socket, payload: dict) -> None:
    line = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
    sock.sendall(line)


def _recv_line(sock: socket.socket, timeout_s: float) -> dict:
    sock.settimeout(timeout_s)
    chunks: list[bytes] = []
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            raise BridgeSyncError("peer closed before line received")
        chunks.append(chunk)
        if b"\n" in chunk:
            break
    line = b"".join(chunks).split(b"\n", 1)[0]
    try:
        return json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise BridgeSyncError(f"bad line from peer: {e}")


def _check_match(local: dict, remote: dict) -> None:
    keys = ("track", "plan", "firmware", "round", "mode")
    for k in keys:
        if local[k] != remote[k]:
            raise BridgeSyncError(
                f"manifest mismatch on {k!r}: local={local[k]!r} peer={remote[k]!r}")
    pair = {local["role"], remote["role"]}
    if pair != {"sender", "receiver"}:
        raise BridgeSyncError(f"roles not complementary: {pair}")


def _clock_offset_exchange(sock: socket.socket,
                           rounds: int = CLOCK_SYNC_ROUNDS) -> int:
    """Naive symmetric NTP-like exchange over the already-open L2CAP socket.

    Each round both sides send a local monotonic timestamp and recv the
    peer's. The diff (local_recv_ts - peer_send_ts) folds clock-offset
    and one-way link latency together; we record the median of N rounds.
    Same accuracy class as cpcbf/controller/clock_sync.py (decorative —
    the orchestrator's version doesn't get true agent timestamps either).
    """
    diffs: list[int] = []
    for i in range(rounds):
        send_ts_us = time.monotonic_ns() // 1000
        _send_line(sock, {"i": i, "ts_us": send_ts_us})
        peer = _recv_line(sock, EXCHANGE_TIMEOUT_S)
        recv_ts_us = time.monotonic_ns() // 1000
        diffs.append(recv_ts_us - int(peer["ts_us"]))
        time.sleep(CLOCK_SYNC_INTER_S)
    diffs.sort()
    return diffs[len(diffs) // 2]


def _exchange(sock: socket.socket, local_manifest: dict,
              want_clock_offset: bool) -> dict:
    _send_line(sock, local_manifest)
    remote_manifest = _recv_line(sock, EXCHANGE_TIMEOUT_S)
    _check_match(local_manifest, remote_manifest)
    out = {
        "peer_ble_mac": remote_manifest.get("ble_mac", ""),
        "clock_offset_us": None,
    }
    if want_clock_offset:
        out["clock_offset_us"] = _clock_offset_exchange(sock)
    return out


def _listen_and_exchange(local_manifest: dict, timeout_s: float,
                         want_clock_offset: bool) -> dict:
    srv = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, BTPROTO_L2CAP)
    srv.bind((BDADDR_ANY, PSM))
    srv.listen(LISTEN_BACKLOG)
    srv.settimeout(timeout_s)
    try:
        client, _peer = srv.accept()
    except socket.timeout:
        raise BridgeSyncError(f"no peer connected within {timeout_s}s")
    finally:
        srv.close()

    try:
        return _exchange(client, local_manifest, want_clock_offset)
    finally:
        try:
            client.close()
        except Exception:
            pass


def _connect_and_exchange(peer_mac: str, local_manifest: dict,
                          timeout_s: float, want_clock_offset: bool) -> dict:
    deadline = time.monotonic() + timeout_s
    last_err: Optional[Exception] = None
    while time.monotonic() < deadline:
        s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET,
                          BTPROTO_L2CAP)
        s.settimeout(min(10.0, max(2.0, deadline - time.monotonic())))
        try:
            s.connect((peer_mac, PSM))
        except (OSError, socket.timeout) as e:
            last_err = e
            try:
                s.close()
            except Exception:
                pass
            time.sleep(RETRY_INTERVAL_S)
            continue

        try:
            return _exchange(s, local_manifest, want_clock_offset)
        finally:
            try:
                s.close()
            except Exception:
                pass

    raise BridgeSyncError(
        f"could not connect to {peer_mac} on PSM {PSM:#x} within "
        f"{timeout_s}s (last={last_err})"
    )


def sync(role: str, peer_mac: str, manifest: dict,
         estimate_clock_offset: bool = False,
         connect_timeout_s: float = CONNECT_TIMEOUT_S) -> dict:
    """Run one bridge-side sync barrier.

    role     — "sender" or "receiver"; sender connects, receiver listens.
    peer_mac — BT MAC of the OTHER RPi (XX:XX:XX:XX:XX:XX).
    manifest — must include track / plan / firmware / round / mode / role.
               May include ble_mac (this Pi's MKR BD_ADDR for BLE plans);
               left "" for non-BLE plans.
    estimate_clock_offset — if True, runs an extra NTP-like timestamp
               exchange after the manifest match. Set for FLOOD plans.

    Returns {"peer_ble_mac": str, "clock_offset_us": int | None}.
    Raises BridgeSyncError on any timeout, connect failure, or mismatch.
    """
    required = {"track", "plan", "firmware", "round", "mode", "role"}
    missing = required - set(manifest)
    if missing:
        raise BridgeSyncError(f"manifest missing keys: {missing}")
    if manifest["role"] != role:
        raise BridgeSyncError(
            f"manifest role {manifest['role']!r} != configured role {role!r}")
    manifest.setdefault("ble_mac", "")

    if role == "receiver":
        return _listen_and_exchange(manifest, connect_timeout_s,
                                    estimate_clock_offset)
    return _connect_and_exchange(peer_mac, manifest, connect_timeout_s,
                                 estimate_clock_offset)
