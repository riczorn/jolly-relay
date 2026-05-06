#!/usr/bin/env python3
"""
End-to-end SMTP delivery test.

Architecture:
  smtplib client → jolly-relay (test port) → capture server (aiosmtpd)

The "capture server" is a minimal aiosmtpd instance that accepts every
message and records it in memory.  jolly-relay is configured to route
all outbound mail to it.

Verifies:
  1. A plain outbound message actually arrives at the capture server.
  2. An inbound message (recipient in local_domains) is forwarded to the
     local_delivery address — also served by the same capture server on a
     different port.
  3. The CSV log reflects the correct direction for both cases.
"""

import os
import sys
import threading
import time

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, PROJECT_DIR)

from tests.helpers import (
    start_server, stop_server,
    send_mail, wait_for_csv, read_csv, make_temp_csv,
)

# Ports used in this test — well outside the normal range to avoid clashes.
RELAY_PORT   = 19750
CAPTURE_PORT = 19751   # remote MX target
LOCAL_PORT   = 19752   # local_delivery target

# --- Capture server ----------------------------------------------------------

try:
    from aiosmtpd.controller import Controller
    from aiosmtpd.handlers import Sink
    _AIOSMTPD_OK = True
except ImportError:
    _AIOSMTPD_OK = False


class _CapturingHandler:
    """aiosmtpd handler that accumulates received envelopes."""
    def __init__(self):
        self.received = []
        self.lock = threading.Lock()

    async def handle_DATA(self, server, session, envelope):
        with self.lock:
            self.received.append({
                'sender':     envelope.mail_from,
                'recipients': list(envelope.rcpt_tos),
                'content':    envelope.content,
            })
        return '250 OK'


class CaptureServer:
    """Thin wrapper around an aiosmtpd Controller with its own event loop."""

    def __init__(self, port):
        self.port = port
        self.handler = _CapturingHandler()
        self._controller = None
        self._thread = None

    def start(self):
        self._controller = Controller(
            self.handler,
            hostname='127.0.0.1',
            port=self.port,
        )
        self._controller.start()
        time.sleep(0.3)

    def stop(self):
        if self._controller:
            self._controller.stop()

    @property
    def received(self):
        with self.handler.lock:
            return list(self.handler.received)


# --- Test config helpers ------------------------------------------------------

def _make_e2e_config(relay_port, capture_port, local_port, csv_path):
    """
    Build a config that:
      - binds the relay on relay_port
      - routes all outbound mail to capture_port (our capture server)
      - forwards inbound mail to local_port (also a capture server)
    """
    import yaml, tempfile
    config_data = {
        'config': {
            'bind_host': '127.0.0.1',
            'bind_port': relay_port,
            'allowed_hosts': ['127.0.0.1'],
            'local_domains': ['local.example.com'],
            'local_delivery': f'127.0.0.1:{local_port}',
            'verbose': False,
            'csv_file': csv_path,
        },
        'servers': {
            'hosts': {
                'capture': {'address': f'127.0.0.1:{capture_port}'},
            },
            'groups': {
                'all_out': ['capture'],
            },
            'default': 'all_out',
        },
    }
    fd, path = tempfile.mkstemp(suffix='.yaml')
    with os.fdopen(fd, 'w') as f:
        yaml.dump(config_data, f)
    return path


# --- Tests -------------------------------------------------------------------

def run_e2e_tests():
    if not _AIOSMTPD_OK:
        print('SKIP: aiosmtpd not available')
        sys.exit(0)

    passed = 0
    failed = 0

    csv_path        = make_temp_csv()
    capture_out     = CaptureServer(CAPTURE_PORT)   # receives outbound mail
    capture_local   = CaptureServer(LOCAL_PORT)     # receives inbound mail

    capture_out.start()
    capture_local.start()

    config_path = _make_e2e_config(RELAY_PORT, CAPTURE_PORT, LOCAL_PORT, csv_path)
    relay_proc  = start_server(config_path, RELAY_PORT)

    print('\n--- End-to-end SMTP delivery test ---\n')

    try:
        # ── Test 1: outbound message arrives at capture server ────────────────
        print('Test 1: outbound mail delivered to capture server')
        code, _ = send_mail(
            'sender@example.com', 'user@gmail.com',
            RELAY_PORT,
        )
        time.sleep(0.5)
        msgs = capture_out.received
        if msgs and msgs[-1]['recipients'] == ['user@gmail.com']:
            print(f'  ✅ Delivered to capture server (smtp code={code})')
            passed += 1
        else:
            print(f'  ❌ Message not received at capture server (code={code}, msgs={msgs})')
            failed += 1

        # ── Test 2: inbound message forwarded to local_delivery ───────────────
        print('Test 2: inbound mail forwarded to local_delivery')
        code2, _ = send_mail(
            'external@example.org', 'user@local.example.com',
            RELAY_PORT,
        )
        time.sleep(0.5)
        local_msgs = capture_local.received
        if local_msgs and local_msgs[-1]['recipients'] == ['user@local.example.com']:
            print(f'  ✅ Forwarded to local_delivery (smtp code={code2})')
            passed += 1
        else:
            print(f'  ❌ Message not received at local_delivery (code={code2}, msgs={local_msgs})')
            failed += 1

        # ── Test 3: CSV records correct directions ────────────────────────────
        print('Test 3: CSV directions are correct')
        wait_for_csv(csv_path, 2, timeout=5.0)
        rows = read_csv(csv_path)

        directions = {r['recipient']: r['direction'] for r in rows if r.get('recipient')}
        out_dir   = directions.get('user@gmail.com', '')
        local_dir = directions.get('user@local.example.com', '')

        if out_dir == 'OUTGOING' and local_dir == 'INCOMING':
            print(f'  ✅ Directions correct: outbound={out_dir}, inbound={local_dir}')
            passed += 1
        else:
            print(f'  ❌ Unexpected directions: outbound={out_dir!r}, inbound={local_dir!r}')
            print(f'     (rows={rows})')
            failed += 1

    finally:
        stop_server(relay_proc)
        capture_out.stop()
        capture_local.stop()
        os.remove(config_path)
        os.remove(csv_path)

    print(f'\n--- Results: {passed} passed, {failed} failed ---')
    if failed:
        sys.exit(1)
    print('\n✅ End-to-end SMTP tests passed!')


if __name__ == '__main__':
    run_e2e_tests()
