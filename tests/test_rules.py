#!/usr/bin/env python3
"""
Rule debugger: replays the jolly-relay-messages.csv through the relay
and prints the routing decision for every sender → recipient pair.

Usage:
    python3 tests/test_rules.py [-c config.yaml] [-i messages.csv]

Defaults:
    -c  tests/jolly-relay-test.yaml
    -i  tests/payloads/jolly-relay-messages.csv

Useful for testing configuration changes against real historical traffic
without running in production.  The relay must NOT log to the same CSV
file being used as input.
"""

import os
import sys
import argparse
import yaml

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)

sys.path.insert(0, PROJECT_DIR)

from tests.helpers import start_server, stop_server, send_mail, config_port

PORT = config_port()
DEFAULT_CSV = os.path.join(SCRIPT_DIR, 'payloads', 'jolly-relay-messages.csv')


def main():
    parser = argparse.ArgumentParser(description='Jolly Relay rule debugger')
    parser.add_argument('-c', '--config',
                        default=os.path.join(SCRIPT_DIR, 'jolly-relay-test.yaml'),
                        help='Path to jolly-relay YAML config')
    parser.add_argument('-i', '--input', default=DEFAULT_CSV,
                        help='Path to CSV of historical messages')
    args = parser.parse_args()

    config_path_arg = os.path.abspath(args.config)
    input_csv       = os.path.abspath(args.input)

    if not os.path.exists(config_path_arg):
        print(f'ERROR: config not found: {config_path_arg}')
        sys.exit(1)
    if not os.path.exists(input_csv):
        print(f'ERROR: input CSV not found: {input_csv}')
        sys.exit(1)

    # Safety: refuse if the server would log to the same file we read from
    with open(config_path_arg) as f:
        raw = yaml.safe_load(f)
    log_csv = raw.get('config', {}).get('csv_file', '')
    if log_csv and os.path.abspath(log_csv) == input_csv:
        print('ERROR: input CSV is the same file the server logs to.')
        print('Use a different file for -i, or disable csv_file in the config.')
        sys.exit(1)

    # Patch the config for testing: disable csv output, set port
    with open(config_path_arg) as f:
        config_data = yaml.safe_load(f)
    config_data.setdefault('config', {})
    config_data['config']['bind_port'] = PORT
    config_data['config']['verbose']   = False
    config_data['config'].pop('csv_file', None)

    import tempfile
    fd, tmp_config = tempfile.mkstemp(suffix='.yaml')
    with os.fdopen(fd, 'w') as f:
        yaml.dump(config_data, f)

    print(f'\n--- Jolly Relay Rule Debugger ---')
    print(f'Config: {config_path_arg}')
    print(f'Input:  {input_csv}')
    print('-' * 34 + '\n')

    proc = start_server(tmp_config, PORT, quiet=False)

    total  = 0
    errors = 0

    print(f'{"SENDER":<40} {"RECIPIENT":<40} {"RESULT"}')
    print('-' * 105)

    try:
        with open(input_csv) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split(';')
                if len(parts) < 3:
                    continue
                sender    = parts[1]
                recipient = parts[2]

                try:
                    code, msg = send_mail(sender, recipient, PORT)
                    icon = '✅' if code < 500 else '⚠️ '
                    result = f'{code} {msg[:60]}'
                except Exception as e:
                    icon   = '❌'
                    result = f'ERROR: {e}'
                    errors += 1

                print(f'{sender[:38]:<40} {recipient[:38]:<40} {icon} {result}')
                total += 1

    except KeyboardInterrupt:
        print('\nInterrupted.')
    finally:
        stop_server(proc)
        os.remove(tmp_config)

    print(f'\n--- Complete: {total} messages, {errors} errors ---')
    if errors:
        sys.exit(1)
    print('✅ No connection errors.')


if __name__ == '__main__':
    main()
