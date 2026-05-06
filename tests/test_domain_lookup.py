#!/usr/bin/env python3
"""
Unit test for MX-based recipient rule matching.

Loads jolly-relay.py in-process and monkey-patches aiodns.DNSResolver so
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
import importlib.util

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
APP_PATH    = os.path.join(PROJECT_DIR, 'jolly-relay.py')

sys.path.insert(0, PROJECT_DIR)


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


def test_domain_lookup():
    config_path = _make_config()

    spec = importlib.util.spec_from_file_location('jolly_relay', APP_PATH)
    jmx  = importlib.util.module_from_spec(spec)
    sys.modules['jolly_relay'] = jmx
    spec.loader.exec_module(jmx)

    jmx.config.config_file = config_path
    jmx.config.verbose = False
    jmx.config.load()

    # Patch aiodns.DNSResolver with our mock so no real DNS queries happen
    import aiodns
    aiodns.DNSResolver = _MockResolver

    print('\n--- Domain MX lookup unit test ---\n')
    passed = 0
    failed = 0

    try:
        # Test 1: MX record contains 'protection.outlook.com' → microsoft group
        print('Test 1: user@microsoft.com (MX has protection.outlook.com)')
        server, group = asyncio.run(
            jmx.get_mx_for_message('sender@example.com', 'user@microsoft.com', 3600)
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
            jmx.get_mx_for_message('sender@example.com', 'user@other.com', 3600)
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
