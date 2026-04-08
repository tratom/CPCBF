#!/usr/bin/env python3
"""
CPCBF Serial Relay — bridges stdin/stdout ↔ USB-Serial for Arduino agents.

Runs on the bridge Raspberry Pi.  The controller SSHes into the RPi and
communicates with this script via stdin/stdout (JSON lines).  This script
relays those lines to the Arduino over USB-Serial, and forwards Arduino
responses back.

Lines from the Arduino starting with '{' (JSON) → stdout (to controller)
Lines from the Arduino starting with '# '       → stderr (log)
Everything from stdin                            → serial (to Arduino)

Usage:
    python3 serial_relay.py /dev/ttyACM0 115200
"""
from __future__ import annotations

import sys
import threading
import time

import serial


def reader_thread(ser: serial.Serial) -> None:
    """Read from serial in chunks, route complete lines to stdout/stderr."""
    buf = b""
    last_report = 0
    while True:
        try:
            # Read available data in chunks (more efficient than readline)
            n = ser.in_waiting
            if n == 0:
                chunk = ser.read(1)  # block for at least one byte
            else:
                chunk = ser.read(n)

            if not chunk:
                continue

            buf += chunk

            # Process all complete lines in the buffer
            while b"\n" in buf:
                line_raw, buf = buf.split(b"\n", 1)
                line = line_raw.decode("utf-8", errors="replace").rstrip("\r")
                if not line:
                    continue

                if line.startswith("{"):
                    sys.stderr.write(f"# [relay] json {len(line)}b\n")
                    sys.stderr.flush()
                    sys.stdout.write(line + "\n")
                    sys.stdout.flush()
                elif line.startswith("#"):
                    sys.stderr.write(line + "\n")
                    sys.stderr.flush()
                else:
                    sys.stderr.write(f"# [relay] ?? {line[:80]}\n")
                    sys.stderr.flush()

            # Report buffer growth every 4KB while waiting for \n
            kb = len(buf) // 4096
            if kb > last_report:
                last_report = kb
                sys.stderr.write(
                    f"# [relay] buffering {len(buf)}b (no newline yet)\n")
                sys.stderr.flush()
            if len(buf) == 0:
                last_report = 0

        except serial.SerialException:
            sys.stderr.write("# [relay] serial read error, exiting reader\n")
            break
        except Exception as e:
            sys.stderr.write(f"# [relay] reader error: {e}\n")


def writer_thread(ser: serial.Serial) -> None:
    """Read lines from stdin, write to serial."""
    for line in sys.stdin:
        line = line.rstrip("\n")
        if not line:
            continue
        try:
            ser.write((line + "\n").encode("utf-8"))
            ser.flush()
        except serial.SerialException:
            sys.stderr.write("# [relay] serial write error, exiting writer\n")
            break


def main() -> None:
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <serial_port> [baud_rate]", file=sys.stderr)
        sys.exit(1)

    port = sys.argv[1]
    baud = int(sys.argv[2]) if len(sys.argv) > 2 else 115200

    sys.stderr.write(f"# [relay] opening {port} at {baud} baud\n")
    ser = serial.Serial(port, baud, timeout=1)

    # Wait for Arduino to reset after serial open
    time.sleep(2)

    # Drain any boot messages (short timeout so we don't block on partial data)
    while ser.in_waiting:
        ser.readline()

    # Switch to long timeout for normal operation so readline() can handle
    # large GET_RESULTS responses (500 results ≈ 40KB, ~3.5s at 115200 baud).
    # Short responses still return instantly because readline() stops at '\n'.
    ser.timeout = 120

    sys.stderr.write("# [relay] ready\n")

    reader = threading.Thread(target=reader_thread, args=(ser,), daemon=True)
    writer = threading.Thread(target=writer_thread, args=(ser,), daemon=True)

    reader.start()
    writer.start()

    # Block until writer exits (stdin closed = controller disconnected)
    writer.join()
    ser.close()


if __name__ == "__main__":
    main()
