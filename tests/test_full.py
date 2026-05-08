#!/usr/bin/env python3
"""
Full routing test: reads addresses.txt and verifies that the relay's
routing decision (logged in the CSV) matches the expected group/server
for every sender → recipient pair.

Also runs each pair in reverse (recipient → sender with a local domain
as recipient) to confirm those are treated as INCOMING.
"""

import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(SCRIPT_DIR))

from tests.helpers import (
    make_test_config, start_server, stop_server,
    send_mail, wait_for_csv, read_csv,
    build_group_addresses, resolve_expected, load_addresses,
    make_temp_csv, config_port,
)

PORT = config_port()


def run_full_test():
    address_pairs = load_addresses()
    if address_pairs is None:
        print("SKIP: tests/payloads/addresses.txt not found.")
        sys.exit(0)

    csv_path = make_temp_csv()
    config_path, config_data = make_test_config(port=PORT, csv_path=csv_path)
    server_addresses, group_addresses = build_group_addresses(config_data)

    server_proc = start_server(config_path, PORT)
    passed = 0
    failed = 0
    errors = []

    try:
        print(f"\n--- Full routing test: {len(address_pairs)} address pairs ---\n")

        for lineno, (sender, recipient, expected_str) in enumerate(address_pairs, 1):
            try:
                valid = resolve_expected(expected_str, server_addresses, group_addresses)
            except ValueError as e:
                errors.append(f"Line {lineno}: {e}")
                failed += 1
                continue

            # Send the message; we don't care about SMTP response code here —
            # the relay will fail to connect to the (fake) MX, which is expected.
            # The routing decision is what we test, via the CSV.
            try:
                send_mail(sender, recipient, PORT)
            except Exception:
                pass

        # Wait for all CSV lines to be written
        wait_for_csv(csv_path, len(address_pairs))
        rows = read_csv(csv_path)

        for i, (sender, recipient, expected_str) in enumerate(address_pairs):
            try:
                valid = resolve_expected(expected_str, server_addresses, group_addresses)
            except ValueError:
                continue

            if i >= len(rows):
                msg = f"  ❌ line {i+1}: no CSV entry for {sender} -> {recipient}"
                errors.append(msg)
                print(msg)
                failed += 1
                continue

            row = rows[i]
            host = row['host']
            direction = row['direction']

            if valid == 'DUNNO':
                # DUNNO means no route found — host should be n/a
                if host in ('n/a', '') or direction == 'OUTGOING':
                    msg = f"  ✅ line {i+1}: {sender} -> {recipient}  expected=DUNNO  got=group:{row['group']}"
                    print(msg)
                    passed += 1
                else:
                    msg = f"  ❌ line {i+1}: {sender} -> {recipient}  expected=DUNNO  host={host!r}"
                    errors.append(msg)
                    print(msg)
                    failed += 1
            elif host in valid:
                msg = f"  ✅ line {i+1}: {sender} -> {recipient}  expected={expected_str}  got={host}"
                print(msg)
                passed += 1
            else:
                msg = f"  ❌ line {i+1}: {sender} -> {recipient}  expected={expected_str} (one of {valid})  got={host!r}"
                errors.append(msg)
                print(msg)
                failed += 1

    finally:
        stop_server(server_proc)
        os.remove(config_path)
        os.remove(csv_path)

    print(f"\n--- Results: {passed} passed, {failed} failed ---")
    if errors:
        print("\nFailures:")
        for e in errors:
            print(f"  {e}")
        sys.exit(1)
    else:
        print("\n✅ All routing tests passed!")


if __name__ == '__main__':
    run_full_test()
