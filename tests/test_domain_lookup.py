#!/usr/bin/env python3
"""
Unit test for MX-based recipient rule matching.

Loads Config in-process and monkey-patches aiodns.DNSResolver so
no real DNS queries are made.  Verifies that:
  1. A domain whose MX record contains 'protection.outlook.com' matches
     the 'microsoft' recipient rule.
  2. A domain with an unrelated MX record produces no route (n/a).
"""

import asyncio
import os
import sys
import tempfile
import yaml

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)

sys.path.insert(0, PROJECT_DIR)

from src.config import Config
import src.router as router_module
from src.router import get_mx_for_message


class _MockAnswer:
    def __init__(self, host, priority=10):
        self.host = host
        self.priority = priority


_MX_MAP = {
    'microsoft.com': [_MockAnswer('microsoft-com.mail.protection.outlook.com.')],
    'other.com':     [_MockAnswer('mail.other.com.')],
}


class _MockResolver:
    async def query(self, domain, _record_type):
        if domain in _MX_MAP:
            return _MX_MAP[domain]
        raise Exception('NXDOMAIN')


def _make_config():
    config_data = {
        'config': {
            'bind_host': '127.0.0.1',
            'bind_port': 19999,
            'verbose': False,
        },
        'servers': {
            'hosts': {
                'mx_ms': {'address': 'mx.microsoft.example:25'},
            },
            'groups': {
                'microsoft': ['mx_ms'],
            },
            'default': 'DUNNO',
        },
        'recipient_rules': {
            # substring match on MX record
            'protection.outlook.com': 'microsoft',
        },
    }
    fd, path = tempfile.mkstemp(suffix='.yaml')
    with os.fdopen(fd, 'w') as f:
        yaml.dump(config_data, f)
    return path


class _FakeService:
    """Minimal stand-in for RelayService (cache + config reference)."""
    def __init__(self, config):
        import threading
        self.config = config
        self.mx_cache = {}
        self.cache_lock = threading.Lock()


def test_domain_lookup():
    import argparse
    # Prevent parse_args from reading pytest/test runner argv
    import unittest.mock as mock
    with mock.patch('sys.argv', ['jolly-relay.py']):
        config = Config()

    config_path = _make_config()
    config.config_file = config_path
    config.verbose = False
    config.load()

    service = _FakeService(config)

    # Patch the module-level DNS resolver with our mock
    import aiodns
    aiodns.DNSResolver = _MockResolver
    router_module._dns_resolver = None  # force re-creation with mock class

    print('\n--- Domain MX lookup unit test ---\n')
    passed = 0
    failed = 0

    try:
        # Test 1: MX record contains 'protection.outlook.com' → microsoft group
        print('Test 1: user@microsoft.com (MX has protection.outlook.com)')
        server, group = asyncio.run(
            get_mx_for_message('sender@example.com', 'user@microsoft.com', config, service)
        )
        host = server.address if server else None
        print(f'  server={host}, group={group}')
        if host == 'mx.microsoft.example:25' and group == 'microsoft':
            print('  ✅ Matched microsoft group via MX substring')
            passed += 1
        else:
            print('  ❌ Expected microsoft group')
            failed += 1

        # Test 2: MX record does not match any rule → no route
        print('\nTest 2: user@other.com (MX is mail.other.com)')
        server, group = asyncio.run(
            get_mx_for_message('sender@example.com', 'user@other.com', config, service)
        )
        host = server.address if server else None
        print(f'  server={host}, group={group}')
        if server is None and group == 'n/a':
            print('  ✅ No route — correct')
            passed += 1
        else:
            print(f'  ❌ Expected no route, got server={host} group={group}')
            failed += 1

    except Exception as e:
        print(f'\n❌ Exception: {e}')
        import traceback; traceback.print_exc()
        failed += 1
    finally:
        os.remove(config_path)

    print(f'\n--- Results: {passed} passed, {failed} failed ---')
    if failed:
        sys.exit(1)
    print('\n✅ Domain lookup test passed!')


if __name__ == '__main__':
    test_domain_lookup()
