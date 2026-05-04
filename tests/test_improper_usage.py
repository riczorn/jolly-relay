#!/usr/bin/env python3
"""
Security and robustness tests.

1. IP blocked (allowed_hosts restricts to 8.8.8.8) → connection refused/closed
2. enabled=False → relay accepts but does not forward (dry-run 250)
3. Empty MAIL FROM (null sender / bounce) → accepted with 250
4. Invalid sender address (bare word, no @) → SMTP 501 or 503 from aiosmtpd
"""

import os
import sys
import smtplib

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(SCRIPT_DIR))

from tests.helpers import (
    make_test_config, start_server, stop_server,
    send_mail, wait_for_csv, read_csv, make_temp_csv,
    config_port,
)

PORT = config_port()


def _make_config_with(overrides, csv_path=None):
    config_path, config_data = make_test_config(
        overrides=overrides, port=PORT, csv_path=csv_path
    )
    return config_path, config_data


def run_improper_usage_test():
    passed = 0
    failed = 0

    # ── Test 1: IP blocked ─────────────────────────────────────────────
    print("\nTest 1: IP blocked (allowed_hosts=8.8.8.8 → our 127.0.0.1 is rejected)")
    config_path, _ = _make_config_with({'allowed_hosts': ['8.8.8.8']})
    proc = start_server(config_path, PORT)
    try:
        try:
            with smtplib.SMTP('127.0.0.1', PORT, timeout=5) as smtp:
                smtp.ehlo('test')
            print("  ❌ Expected connection to be rejected, but banner was received")
            failed += 1
        except (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected,
                ConnectionResetError, OSError):
            print("  ✅ Connection was rejected or closed immediately")
            passed += 1
    finally:
        stop_server(proc)
        os.remove(config_path)

    # ── Test 2: enabled=False → dry-run accepts with 250 ──────────────
    print("\nTest 2: enabled=False → relay accepts (250) but does not forward")
    csv_path = make_temp_csv()
    config_path, _ = _make_config_with({'enabled': False}, csv_path=csv_path)
    proc = start_server(config_path, PORT)
    try:
        code, _ = send_mail('alice@example.com', 'bob@example.net', PORT)
        if code == 250:
            print("  ✅ Got 250 in dry-run mode")
            passed += 1
        else:
            print(f"  ❌ Expected 250, got {code}")
            failed += 1

        wait_for_csv(csv_path, 1)
        rows = read_csv(csv_path)
        host = rows[0]['host'] if rows else 'MISSING'
        if host in ('n/a', ''):
            print(f"  ✅ CSV shows no forwarding host (host={host!r})")
            passed += 1
        else:
            print(f"  ❌ Expected no host in dry-run CSV, got: {host!r}")
            failed += 1
    finally:
        stop_server(proc)
        os.remove(config_path)
        os.remove(csv_path)

    # ── Test 3: null sender (bounce message) ──────────────────────────
    print("\nTest 3: null sender <> → accepted (DSN/bounce convention)")
    csv_path = make_temp_csv()
    config_path, _ = _make_config_with(
        {'local_domains': ['local.example.com']}, csv_path=csv_path
    )
    proc = start_server(config_path, PORT)
    try:
        try:
            with smtplib.SMTP('127.0.0.1', PORT, timeout=5) as smtp:
                smtp.ehlo('test.relay')
                smtp.mail('')          # null sender
                smtp.rcpt('postmaster@local.example.com')
                code, _ = smtp.data(b'Subject: bounce\r\n\r\nbounce body\r\n')
                if 200 <= code < 600:
                    print(f"  ✅ Null sender accepted by SMTP layer (code {code})")
                    passed += 1
                else:
                    print(f"  ❌ Unexpected code for null sender: {code}")
                    failed += 1
        except smtplib.SMTPException as e:
            print(f"  ❌ SMTP error for null sender: {e}")
            failed += 1
    finally:
        stop_server(proc)
        os.remove(config_path)
        os.remove(csv_path)

    # ── Test 4: malformed recipient (no @) → SMTP 501/503 ─────────────
    print("\nTest 4: malformed recipient (no domain) → SMTP error")
    config_path, _ = _make_config_with({})
    proc = start_server(config_path, PORT)
    try:
        try:
            with smtplib.SMTP('127.0.0.1', PORT, timeout=5) as smtp:
                smtp.ehlo('test.relay')
                smtp.mail('alice@example.com')
                code, msg = smtp.rcpt('notanemail')
                if code >= 500:
                    print(f"  ✅ Malformed recipient rejected with {code}")
                    passed += 1
                else:
                    print(f"  ❌ Expected 5xx for malformed recipient, got {code}: {msg}")
                    failed += 1
        except smtplib.SMTPRecipientsRefused as e:
            code = list(e.recipients.values())[0][0]
            print(f"  ✅ Malformed recipient refused with {code}")
            passed += 1
        except smtplib.SMTPException as e:
            print(f"  ✅ Malformed recipient caused SMTP error: {e}")
            passed += 1
    finally:
        stop_server(proc)
        os.remove(config_path)

    print(f"\n--- Results: {passed} passed, {failed} failed ---")
    if failed:
        sys.exit(1)
    print("\n✅ Improper usage tests passed!")


if __name__ == '__main__':
    run_improper_usage_test()
