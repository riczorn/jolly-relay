"""
Shared test helpers for jolly-relay.

All E2E tests:
  1. Start jolly-relay as a subprocess (SMTP relay on a test port).
  2. Submit mail via smtplib (plain SMTP — fine for the test client).
  3. Wait briefly, then read the CSV log to check routing decisions.

The relay's outbound leg will fail (no real MXes in the test config);
the SMTP response to the test client will therefore be 4xx/5xx, which is
expected and tested where relevant.  Routing decisions are always verified
via the CSV, not by checking delivery.
"""

import os
import re
import sys
import time
import smtplib
import tempfile
import subprocess
import yaml

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
APP_PATH = os.path.join(PROJECT_DIR, 'jolly-relay.py')
CONFIG_PATH = os.path.join(SCRIPT_DIR, 'jolly-relay-test.yaml')
ADDRESSES_PATH = os.path.join(SCRIPT_DIR, 'payloads', 'addresses.txt')
PAYLOADS_DIR = os.path.join(SCRIPT_DIR, 'payloads')

# Sample mail bodies (loaded on first use)
_INBOUND_MAIL = None
_OUTBOUND_MAIL = None

# Cached config port (read once at import time)
_config_port_cache = None


def config_port():
    """Return bind_port from jolly-relay-test.yaml, defaulting to 9725."""
    global _config_port_cache
    if _config_port_cache is None:
        try:
            with open(CONFIG_PATH, 'r') as f:
                data = yaml.safe_load(f)
            _config_port_cache = int(
                data.get('config', {}).get('bind_port', 9725)
            )
        except Exception:
            _config_port_cache = 9725
    return _config_port_cache


def load_sample_mail(kind):
    """Return the raw bytes of sample_inbound_mail.eml or sample_outbound_mail.eml."""
    global _INBOUND_MAIL, _OUTBOUND_MAIL
    path = os.path.join(PAYLOADS_DIR, f'sample_{kind}_mail.eml')
    with open(path, 'rb') as f:
        return f.read()


def make_test_config(overrides=None, port=None, csv_path=None):
    """
    Load jolly-relay-test.yaml, apply overrides dict, set the test port
    and csv path, write to a temp file, and return its path.

    overrides is merged into config_data['config'].
    """
    with open(CONFIG_PATH, 'r') as f:
        config_data = yaml.safe_load(f)

    if 'config' not in config_data or not config_data['config']:
        config_data['config'] = {}

    cfg = config_data['config']
    cfg['verbose'] = False

    if port is not None:
        cfg['bind_port'] = port

    if csv_path is not None:
        cfg['csv_file'] = csv_path
    else:
        # suppress csv output by default
        cfg.pop('csv_file', None)

    if overrides:
        cfg.update(overrides)

    fd, temp_path = tempfile.mkstemp(suffix='.yaml')
    with os.fdopen(fd, 'w') as f:
        yaml.dump(config_data, f)

    return temp_path, config_data


def start_server(config_path, port, quiet=True):
    """Launch jolly-relay as a subprocess and wait for it to bind."""
    stdout = subprocess.DEVNULL if quiet else subprocess.PIPE
    stderr = subprocess.DEVNULL if quiet else subprocess.PIPE
    proc = subprocess.Popen(
        [sys.executable, APP_PATH, '-p', str(port), '-c', config_path],
        stdout=stdout,
        stderr=stderr,
    )
    time.sleep(1.2)  # wait for aiosmtpd to bind
    if proc.poll() is not None:
        if not quiet:
            out, err = proc.communicate()
            print(f"Server stdout: {out.decode()}")
            print(f"Server stderr: {err.decode()}")
        raise RuntimeError(f"Server failed to start on port {port}")
    return proc


def stop_server(proc):
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def send_mail(sender, recipient, port, body=None, timeout=5):
    """
    Submit a single message to the relay via smtplib.
    Returns the (code, message) from DATA or raises on connection failure.
    body: bytes or None (a minimal stub is used when None).
    """
    if body is None:
        body = (
            f"From: {sender}\r\n"
            f"To: {recipient}\r\n"
            f"Subject: test\r\n"
            f"Message-ID: <test@relay-test>\r\n"
            f"\r\ntest body\r\n"
        ).encode()
    elif isinstance(body, str):
        body = body.encode()

    try:
        with smtplib.SMTP('127.0.0.1', port, timeout=timeout) as smtp:
            smtp.ehlo('test.relay')
            smtp.sendmail(sender, [recipient], body)
            # sendmail raises on error; if we get here it's 250
            return 250, "OK"
    except smtplib.SMTPRecipientsRefused as e:
        code, msg = list(e.recipients.values())[0]
        return code, msg.decode() if isinstance(msg, bytes) else msg
    except smtplib.SMTPResponseException as e:
        return e.smtp_code, e.smtp_error.decode() if isinstance(e.smtp_error, bytes) else e.smtp_error
    except (ConnectionRefusedError, OSError) as e:
        raise


def wait_for_csv(csv_path, min_lines, timeout=3.0):
    """Poll until the CSV has at least min_lines non-empty lines, then flush."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with open(csv_path, 'r') as f:
                lines = [l for l in f.readlines() if l.strip()]
            if len(lines) >= min_lines:
                return lines
        except FileNotFoundError:
            pass
        time.sleep(0.1)
    return []


def read_csv(csv_path):
    """Return list of dicts from the CSV. Fields: date,sender,recipient,group,host,client,direction,sasl"""
    rows = []
    try:
        with open(csv_path, 'r') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split(';')
                rows.append({
                    'date':      parts[0] if len(parts) > 0 else '',
                    'sender':    parts[1] if len(parts) > 1 else '',
                    'recipient': parts[2] if len(parts) > 2 else '',
                    'group':     parts[3] if len(parts) > 3 else '',
                    'host':      parts[4] if len(parts) > 4 else '',
                    'client':    parts[5] if len(parts) > 5 else '',
                    'direction': parts[6] if len(parts) > 6 else '',
                    'sasl':      parts[7] if len(parts) > 7 else '',
                })
    except FileNotFoundError:
        pass
    return rows


def build_group_addresses(config_data):
    """
    From config_data, build:
      server_addresses: {name -> 'host:port'}
      group_addresses:  {group_name -> set of 'host:port'}
    """
    hosts = config_data.get('servers', {}).get('hosts', {})
    server_addresses = {name: info['address'] for name, info in hosts.items()}

    group_addresses = {}
    for gname, members in config_data.get('servers', {}).get('groups', {}).items():
        group_addresses[gname] = {server_addresses[s] for s in members}

    return server_addresses, group_addresses


def resolve_expected(expected, server_addresses, group_addresses):
    """
    Parse an expected column from addresses.txt.
    Returns 'DUNNO', a set of valid 'host:port' strings, or raises ValueError.
    """
    expected = expected.strip()
    if expected == 'DUNNO':
        return 'DUNNO'
    if expected.startswith('[') and expected.endswith(']'):
        names = [n.strip() for n in expected[1:-1].split(',')]
        return {server_addresses[n] for n in names}
    if expected in group_addresses:
        return group_addresses[expected]
    raise ValueError(f"Unknown expected result: {expected!r}")


def load_addresses():
    """Return list of (sender, recipient, expected) from addresses.txt."""
    pairs = []
    with open(ADDRESSES_PATH, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('sender'):
                continue
            parts = re.split(r'\t', line)
            if len(parts) >= 3:
                pairs.append((parts[0].strip(), parts[1].strip(), parts[2].strip()))
    return pairs


def make_temp_csv():
    """Create an empty temp file for CSV logging; return its path."""
    fd, path = tempfile.mkstemp(suffix='.csv')
    os.close(fd)
    return path
