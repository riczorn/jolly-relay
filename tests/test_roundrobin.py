#!/usr/bin/env python3
"""
Roundrobin tests.

1. servers.default=ALL  → unknown sender falls back to global pool
2. servers.default=DUNNO → unknown sender gets no route (n/a)
"""

import os
import sys
import yaml
import tempfile

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(SCRIPT_DIR))

from tests.helpers import (
    start_server, stop_server,
    send_mail, wait_for_csv, read_csv, make_temp_csv,
    CONFIG_PATH, config_port,
)

PORT = config_port()


def _make_config(default_action):
    with open(CONFIG_PATH, 'r') as f:
        data = yaml.safe_load(f)
    data['config']['bind_port'] = PORT
    data['config']['verbose'] = False
    data['servers']['default'] = default_action
    fd, path = tempfile.mkstemp(suffix='.yaml')
    with os.fdopen(fd, 'w') as f:
        yaml.dump(data, f)
    return path, data


def _all_server_addresses(config_data):
    return {info['address'] for info in config_data['servers']['hosts'].values()}


def run_roundrobin_test():
    passed = 0
    failed = 0

    # ── Test 1: default=ALL ────────────────────────────────────────────
    print("\nTest 1: default=ALL → unknown sender uses global pool")
    csv_path = make_temp_csv()
    config_path, config_data = _make_config(default_action='ALL')
    all_addresses = _all_server_addresses(config_data)

    with open(config_path) as f:
        d = yaml.safe_load(f)
    d['config']['csv_file'] = csv_path
    with open(config_path, 'w') as f:
        yaml.dump(d, f)

    proc = start_server(config_path, PORT)
    try:
        send_mail('random@unknown.com', 'user@nowhere.net', PORT)
        wait_for_csv(csv_path, 1)
        rows = read_csv(csv_path)
        host = rows[0]['host'] if rows else ''
        if host in all_addresses:
            print(f"  ✅ Got global-pool server: {host}")
            passed += 1
        else:
            print(f"  ❌ Expected a global-pool server, got: {host!r}")
            failed += 1
    finally:
        stop_server(proc)
        os.remove(config_path)
        os.remove(csv_path)

    # ── Test 2: default=DUNNO ──────────────────────────────────────────
    print("\nTest 2: default=DUNNO → no route for unknown sender")
    csv_path = make_temp_csv()
    config_path, _ = _make_config(default_action='DUNNO')
    with open(config_path) as f:
        d = yaml.safe_load(f)
    d['config']['csv_file'] = csv_path
    with open(config_path, 'w') as f:
        yaml.dump(d, f)

    proc = start_server(config_path, PORT)
    try:
        send_mail('random@unknown.com', 'user@nowhere.net', PORT)
        wait_for_csv(csv_path, 1)
        rows = read_csv(csv_path)
        host = rows[0]['host'] if rows else ''
        if host in ('n/a', ''):
            print(f"  ✅ No route assigned (host={host!r})")
            passed += 1
        else:
            print(f"  ❌ Expected no route, got host: {host!r}")
            failed += 1
    finally:
        stop_server(proc)
        os.remove(config_path)
        os.remove(csv_path)

    print(f"\n--- Results: {passed} passed, {failed} failed ---")
    if failed:
        sys.exit(1)
    print("\n✅ Roundrobin tests passed!")


if __name__ == '__main__':
    run_roundrobin_test()
