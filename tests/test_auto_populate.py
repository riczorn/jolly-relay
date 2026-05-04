#!/usr/bin/env python3
"""
Auto-populate local_domains test.

Starts the relay with auto_populate_local_domains=True pointing at a
synthetic Postfix virtual file containing domain1.net and domain2.org.
Sends three messages and verifies direction via the CSV:
  - domain1.net  → INCOMING  (in virtual file)
  - domain3.com  → OUTGOING  (not in virtual file)
  - domain2.org  → INCOMING  (in virtual file)
"""

import os
import sys
import tempfile

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(SCRIPT_DIR))

from tests.helpers import (
    make_test_config, start_server, stop_server,
    send_mail, wait_for_csv, read_csv, make_temp_csv,
    config_port,
)

PORT = config_port()
PAYLOADS_DIR = os.path.join(SCRIPT_DIR, 'payloads')


def _write_virtual_file(domains):
    """Write a minimal Postfix virtual file listing the given domains."""
    fd, path = tempfile.mkstemp(suffix='.virtual')
    with os.fdopen(fd, 'w') as f:
        for domain in domains:
            f.write(f"{domain}   OK\n")
    return path


def run_auto_populate_test():
    virtual_path = _write_virtual_file(['domain1.net', 'domain2.org'])
    csv_path = make_temp_csv()

    config_path, _ = make_test_config(
        overrides={
            'auto_populate_local_domains': True,
            'postfix_virtual_file': virtual_path,
            'local_domains': [],
        },
        port=PORT,
        csv_path=csv_path,
    )

    proc = start_server(config_path, PORT)

    cases = [
        ('sender@example.com', 'user@domain1.net',  'INCOMING'),
        ('sender@example.com', 'user@domain3.com',  'OUTGOING'),
        ('sender@example.com', 'user@domain2.org',  'INCOMING'),
    ]

    print("\n--- Auto-populate local_domains test ---\n")

    try:
        for sender, recipient, _ in cases:
            try:
                send_mail(sender, recipient, PORT)
            except Exception:
                pass

        wait_for_csv(csv_path, len(cases))
        rows = read_csv(csv_path)

        passed = 0
        failed = 0
        for i, (sender, recipient, expected_dir) in enumerate(cases):
            if i >= len(rows):
                print(f"  ❌ {recipient}: no CSV entry")
                failed += 1
                continue
            got = rows[i]['direction']
            if got == expected_dir:
                print(f"  ✅ {recipient}: {got}")
                passed += 1
            else:
                print(f"  ❌ {recipient}: expected={expected_dir} got={got}")
                failed += 1

    finally:
        stop_server(proc)
        os.remove(config_path)
        os.remove(csv_path)
        os.remove(virtual_path)

    print(f"\n--- Results: {passed} passed, {failed} failed ---")
    if failed:
        sys.exit(1)
    print("\n✅ Auto-populate test passed!")


if __name__ == '__main__':
    run_auto_populate_test()
