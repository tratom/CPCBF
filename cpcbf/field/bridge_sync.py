"""Bridge-side synchronization between the two RPi bridges.

Used after every successful flash, before the agents launch, to confirm
that both RPis are about to run the same plan / firmware / round / mode
under the same track. Fails closed: if the manifests don't match or the
peer is unreachable, the caller skips the plan rather than running with
desynchronised state.

Transport: BR/EDR L2CAP Connection-Oriented Channels (SOCK_SEQPACKET) on
PSM 0x81. Same family as the benchmark BLE adapter
(cpcbf/agent/adapters/ble_adapter.c) but on a distinct PSM so there is no
risk of a stray packet from the test rig hitting this listener (PSMs are
temporally separated anyway). No pairing — security level LOW.

Receiver RPi listens; sender RPi connects to the receiver's BT MAC.
Manifest is one JSON line ("\\n"-terminated) each way.
"""
from __future__ import annotations

import json
import socket
import time
from typing import Optional


BTPROTO_L2CAP = 0
PSM = 0x81

CONNECT_TIMEOUT_S = 60.0
EXCHANGE_TIMEOUT_S = 30.0
LISTEN_BACKLOG = 1
RETRY_INTERVAL_S = 1.0


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
            raise BridgeSyncError("peer closed before manifest received")
        chunks.append(chunk)
        if b"\n" in chunk:
            break
    line = b"".join(chunks).split(b"\n", 1)[0]
    try:
        return json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise BridgeSyncError(f"bad manifest from peer: {e}")


def _check_match(local: dict, remote: dict) -> None:
    keys = ("track", "plan", "firmware", "round", "mode")
    for k in keys:
        if local[k] != remote[k]:
            raise BridgeSyncError(
                f"manifest mismatch on {k!r}: local={local[k]!r} peer={remote[k]!r}")
    pair = {local["role"], remote["role"]}
    if pair != {"sender", "receiver"}:
        raise BridgeSyncError(f"roles not complementary: {pair}")


def _listen_and_exchange(local_manifest: dict, timeout_s: float) -> dict:
    srv = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, BTPROTO_L2CAP)
    srv.bind(("", PSM))
    srv.listen(LISTEN_BACKLOG)
    srv.settimeout(timeout_s)
    try:
        client, _peer = srv.accept()
    except socket.timeout:
        raise BridgeSyncError(f"no peer connected within {timeout_s}s")
    finally:
        srv.close()

    try:
        _send_line(client, local_manifest)
        return _recv_line(client, EXCHANGE_TIMEOUT_S)
    finally:
        try:
            client.close()
        except Exception:
            pass


def _connect_and_exchange(peer_mac: str, local_manifest: dict,
                          timeout_s: float) -> dict:
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
            _send_line(s, local_manifest)
            return _recv_line(s, EXCHANGE_TIMEOUT_S)
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
         connect_timeout_s: float = CONNECT_TIMEOUT_S) -> None:
    """Run one bridge-side sync barrier.

    role     — "sender" or "receiver"; sender connects, receiver listens.
    peer_mac — BT MAC of the OTHER RPi (XX:XX:XX:XX:XX:XX).
    manifest — must include track / plan / firmware / round / mode / role.

    Raises BridgeSyncError on any timeout, connect failure, or mismatch.
    """
    required = {"track", "plan", "firmware", "round", "mode", "role"}
    missing = required - set(manifest)
    if missing:
        raise BridgeSyncError(f"manifest missing keys: {missing}")
    if manifest["role"] != role:
        raise BridgeSyncError(
            f"manifest role {manifest['role']!r} != configured role {role!r}")

    if role == "receiver":
        peer = _listen_and_exchange(manifest, connect_timeout_s)
    else:
        peer = _connect_and_exchange(peer_mac, manifest, connect_timeout_s)

    _check_match(manifest, peer)
