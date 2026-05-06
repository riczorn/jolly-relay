#!/usr/bin/env python3
"""
Config validation tests.

Verifies that Config.validate() catches bad rule references and bad
addresses, and that a valid config produces no errors.
All tests run in-process — no subprocess, no network, no SMTP.
"""

import os
import sys
import tempfile
import unittest.mock as mock

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, PROJECT_DIR)

import yaml
from src.config import Config


# ── helpers ───────────────────────────────────────────────────────────

def _make_config(data):
    """Write a YAML config dict to a temp file, return the path."""
    fd, path = tempfile.mkstemp(suffix='.yaml')
    with os.fdopen(fd, 'w') as f:
        yaml.dump(data, f)
    return path


def _load_config(data):
    """
    Instantiate Config, point it at a temp file built from data, call load().
    Returns the Config instance.
    """
    path = _make_config(data)
    try:
        with mock.patch('sys.argv', ['jolly-relay.py']):
            config = Config()
        config.config_file = path
        config.verbose = False
        config.load()
        return config
    finally:
        os.remove(path)


def _validate_only(data):
    """
    Build a Config from data and return the raw validation error list
    without calling sys.exit (we intercept before load() reaches that).
    """
    path = _make_config(data)
    try:
        with mock.patch('sys.argv', ['jolly-relay.py']):
            config = Config()
        config.config_file = path

        # Manually drive the load pipeline up to validate(), bypassing sys.exit.
        import yaml as _yaml
        with open(path) as f:
            config.config_dict = _yaml.safe_load(f)
        if not isinstance(config.config_dict, dict):
            config.config_dict = {}
        if 'config' not in config.config_dict:
            config.config_dict['config'] = {}

        # Minimal bootstrap so validate() can inspect server groups.
        config._load_servers()
        config._load_combined_rules()
        return config.validate()
    finally:
        os.remove(path)


# ── minimal valid config ──────────────────────────────────────────────

_BASE = {
    'config': {'bind_host': '127.0.0.1', 'bind_port': 19990},
    'servers': {
        'hosts': {
            'mx1': {'address': 'mx1.example.com:25'},
            'mx2': {'address': 'mx2.example.com:25'},
        },
        'groups': {
            'good': ['mx1', 'mx2'],
        },
        'default': 'DUNNO',
    },
}


# ── test cases ────────────────────────────────────────────────────────

def test_valid_config_no_errors():
    """A well-formed config produces zero validation errors."""
    data = dict(_BASE)
    data['sender_rules']    = {'example.com': 'good'}
    data['recipient_rules'] = {'gmail.com': 'good'}
    errors = _validate_only(data)
    assert errors == [], f"Expected no errors, got: {errors}"
    print('  ✅ valid config → no errors')


def test_sender_rule_unknown_group():
    """sender_rules referencing a non-existent group is an error."""
    data = dict(_BASE)
    data['sender_rules'] = {'example.com': 'nonexistent_group'}
    errors = _validate_only(data)
    assert any('nonexistent_group' in e for e in errors), \
        f"Expected error about 'nonexistent_group', got: {errors}"
    print('  ✅ unknown sender_rule group detected')


def test_recipient_rule_unknown_group():
    """recipient_rules referencing a non-existent group is an error."""
    data = dict(_BASE)
    data['recipient_rules'] = {'gmail.com': 'no_such_group'}
    errors = _validate_only(data)
    assert any('no_such_group' in e for e in errors), \
        f"Expected error about 'no_such_group', got: {errors}"
    print('  ✅ unknown recipient_rule group detected')


def test_combined_rule_unknown_group():
    """combined_rules referencing an unknown group string is an error."""
    data = dict(_BASE)
    data['combined_rules'] = {'good,good': 'phantom_group'}
    errors = _validate_only(data)
    assert any('phantom_group' in e for e in errors), \
        f"Expected error about 'phantom_group', got: {errors}"
    print('  ✅ unknown combined_rule group detected')


def test_combined_rule_unknown_server():
    """combined_rules referencing an unknown server name in a list is an error."""
    data = dict(_BASE)
    data['combined_rules'] = {'good,good': ['mx1', 'ghost_server']}
    errors = _validate_only(data)
    assert any('ghost_server' in e for e in errors), \
        f"Expected error about 'ghost_server', got: {errors}"
    print('  ✅ unknown combined_rule server detected')


def test_server_missing_address():
    """A server entry with no address field is an error."""
    import copy
    data = copy.deepcopy(_BASE)
    data['servers']['hosts']['mx_bad'] = {}   # no 'address' key
    errors = _validate_only(data)
    assert any('mx_bad' in e for e in errors), \
        f"Expected error about 'mx_bad', got: {errors}"
    print('  ✅ server missing address detected')


def test_group_references_unknown_server():
    """A group that lists a server name not in hosts emits a warning but not a validate() error."""
    # This is handled during _load_servers with a WARNING log, not a hard error.
    # validate() only checks rule→group references.  Verify no crash occurs.
    import copy
    data = copy.deepcopy(_BASE)
    data['servers']['groups']['bad'] = ['mx1', 'ghost']
    errors = _validate_only(data)
    # The group 'bad' ends up with only mx1 (ghost is skipped with a warning).
    # No validation error is expected here — the group simply has fewer members.
    print(f'  ✅ group with missing server: loaded with warning, errors={errors}')


def test_default_unknown_group_does_not_crash():
    """servers.default referencing an unknown group logs a warning but does not crash."""
    import copy
    data = copy.deepcopy(_BASE)
    data['servers']['default'] = 'no_such_group'
    # Should not raise; validate() won't flag the default (it's already warned).
    errors = _validate_only(data)
    print(f'  ✅ unknown default group: loaded with warning, errors={errors}')


# ── runner ────────────────────────────────────────────────────────────

def run_config_validation_tests():
    tests = [
        test_valid_config_no_errors,
        test_sender_rule_unknown_group,
        test_recipient_rule_unknown_group,
        test_combined_rule_unknown_group,
        test_combined_rule_unknown_server,
        test_server_missing_address,
        test_group_references_unknown_server,
        test_default_unknown_group_does_not_crash,
    ]

    print('\n--- Config validation tests ---\n')
    passed = 0
    failed = 0

    for test_fn in tests:
        try:
            test_fn()
            passed += 1
        except Exception as e:
            print(f'  ❌ {test_fn.__name__}: {e}')
            import traceback; traceback.print_exc()
            failed += 1

    print(f'\n--- Results: {passed} passed, {failed} failed ---')
    if failed:
        sys.exit(1)
    print('\n✅ Config validation tests passed!')


if __name__ == '__main__':
    run_config_validation_tests()
