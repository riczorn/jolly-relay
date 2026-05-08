#!/usr/bin/env python3
"""
Direction detection test.

Verifies that mail addressed to a local domain is logged as INCOMING
and mail addressed to an external domain is logged as OUTGOING.
Uses the sample .eml payloads when available; falls back to stub bodies.
"""

import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(SCRIPT_DIR))

from tests.helpers import (
    make_test_config, start_server, stop_server,
    send_mail, wait_for_csv, read_csv, make_temp_csv, load_sample_mail,
    config_port,
)

PORT = config_port()


def run_direction_test():
    passed = 0
    failed = 0

    inbound_body  = load_sample_mail('inbound')   # None if file absent
    outbound_body = load_sample_mail('outbound')  # None if file absent

    # local_domains contains 'local.example.com'; the inbound sample
    # mail targets user@local.example.com.
    csv_path = make_temp_csv()
    config_path, _ = make_test_config(
        overrides={'local_domains': ['local.example.com']},
        port=PORT,
        csv_path=csv_path,
    )
    server_proc = start_server(config_path, PORT)

    cases = [
        # (sender, recipient, body, expected_direction)
        ('external@example.org', 'user@local.example.com',  inbound_body,  'INCOMING'),
        ('sender@example.com',   'recipient@gmail.com',      outbound_body, 'OUTGOING'),
        ('bounce@somewhere.net', 'admin@local.example.com',  inbound_body,  'INCOMING'),
        ('nobody@example.com',   'user@unknown-domain.xyz',  None,          'OUTGOING'),
    ]

    print("\n--- Direction detection test ---\n")

    try:
        for sender, recipient, body, expected_dir in cases:
            try:
                send_mail(sender, recipient, PORT, body=body)
            except Exception:
                pass  # delivery failure is expected (fake MXes)

        wait_for_csv(csv_path, len(cases))
        rows = read_csv(csv_path)

        for i, (sender, recipient, _, expected_dir) in enumerate(cases):
            if i >= len(rows):
                print(f"  ❌ {sender} -> {recipient}: no CSV entry")
                failed += 1
                continue

            got = rows[i]['direction']
            if got == expected_dir:
                print(f"  ✅ {sender} -> {recipient}: {got}")
                passed += 1
            else:
                print(f"  ❌ {sender} -> {recipient}: expected={expected_dir} got={got}")
                failed += 1

    finally:
        stop_server(server_proc)
        os.remove(config_path)
        os.remove(csv_path)

    print(f"\n--- Results: {passed} passed, {failed} failed ---")
    if failed:
        sys.exit(1)
    print("\n✅ Direction tests passed!")


if __name__ == '__main__':
    run_direction_test()
