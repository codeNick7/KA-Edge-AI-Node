#!/usr/bin/env python3
"""
test_reader.py — Console reader for test_hardware.ino diagnostic output.

Listens on the "diag" Bridge channel and prints every message to stdout
with a timestamp. Run this via App Lab while test_hardware.ino is flashed.

Usage (App Lab):
  Place this file in the test-hardware/ App Lab project directory.
  App Lab will start it automatically when you open the project.
"""

import sys
from datetime import datetime, timezone

from arduino.app_utils import App, Bridge


def timestamp():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


bridge = Bridge()


def handle_diag(msg: str):
    """Print every diagnostic message with a UTC timestamp."""
    # Detect section headers and result lines for visual separation
    if msg.startswith("===") or msg.startswith("RESULT"):
        print(f"\n[{timestamp()}] {msg}", flush=True)
    elif msg.startswith("---"):
        print(f"[{timestamp()}] {msg}", flush=True)
    else:
        print(f"[{timestamp()}]   {msg}", flush=True)


bridge.provide("diag", handle_diag)

if __name__ == "__main__":
    print(f"[{timestamp()}] test_reader.py started — waiting for diagnostic output...")
    print(f"[{timestamp()}] Listening on Bridge channel: diag")
    print("-" * 60, flush=True)
    App.run()
