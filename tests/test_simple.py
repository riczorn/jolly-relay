#!/usr/bin/env python3
"""
Smoke test: start the relay, submit every address pair from addresses.txt,
and print the CSV routing result. No assertions — useful for quick inspection.
"""

import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(SCRIPT_DIR))

from tests.helpers import (
    make_test_config, start_server, stop_server,
    send_mail, wait_for_csv, read_csv, load_addresses, make_temp_csv,
    config_port,
)

PORT = config_port()


def run_simple():
    pairs = load_addresses()
    if pairs is None:
        print("SKIP: tests/payloads/addresses.txt not found.")
        sys.exit(0)

    csv_path = make_temp_csv()
    config_path, _ = make_test_config(port=PORT, csv_path=csv_path)
    server_proc = start_server(config_path, PORT)

    print(f"\n--- Simple smoke test: {len(pairs)} address pairs ---\n")
    print(f"{'SENDER':<35} {'RECIPIENT':<35} {'SMTP'}")
    print("-" * 80)

    try:
        for sender, recipient, _ in pairs:
            try:
                code, _ = send_mail(sender, recipient, PORT)
                print(f"{sender:<35} {recipient:<35} {code}")
            except Exception as e:
                print(f"{sender:<35} {recipient:<35} ERROR: {e}")

        wait_for_csv(csv_path, len(pairs))
        rows = read_csv(csv_path)

        print(f"\n--- CSV routing decisions ---\n")
        print(f"{'SENDER':<35} {'RECIPIENT':<35} {'GROUP':<15} {'HOST':<30} {'DIR'}")
        print("-" * 120)
        for row in rows:
            print(
                f"{row['sender']:<35} {row['recipient']:<35} "
                f"{row['group']:<15} {row['host']:<30} {row['direction']}"
            )

    finally:
        stop_server(server_proc)
        os.remove(config_path)
        os.remove(csv_path)

    print("\n✅ Smoke test complete.")


if __name__ == '__main__':
    run_simple()
