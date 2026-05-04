#!/usr/bin/env python3
"""
External service smoke test.

Connects to a *live* jolly-relay instance (must already be running) and
submits every address pair from addresses.txt.  Prints the SMTP response
code and the routing result from the CSV log.

Usage:
    python3 tests/test_external_service.py [-H host] [-p port] [-i csv]

This test is intentionally informational (no assertions) — it is useful
for inspecting a running production or staging relay without disrupting it.
"""

import os
import sys
import argparse

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)

sys.path.insert(0, PROJECT_DIR)

from tests.helpers import send_mail, load_addresses, config_port

DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = config_port()


def main():
    parser = argparse.ArgumentParser(description='External service smoke test')
    parser.add_argument('-H', '--host', default=DEFAULT_HOST)
    parser.add_argument('-p', '--port', type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    pairs = load_addresses()

    print(f"\n--- External service test on {args.host}:{args.port} ---")
    print(f"{'SENDER':<35} {'RECIPIENT':<35} {'CODE'}")
    print("-" * 80)

    for sender, recipient, expected in pairs:
        try:
            code, msg = send_mail(sender, recipient, args.port)
            print(f"{sender:<35} {recipient:<35} {code}  (expected: {expected})")
        except Exception as e:
            print(f"{sender:<35} {recipient:<35} ERROR: {e}")

    print("\n--- Done ---")


if __name__ == '__main__':
    main()
