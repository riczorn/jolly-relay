#!/usr/bin/env python3
"""
Load test: calls get_mx_for_message() in-process over all address pairs
from addresses.txt for ITERATIONS cycles (~53 000 routing calls).

This tests the routing logic and round-robin scheduler at speed without
any network I/O.  Console output is suppressed during the hot loop.
"""

import os
import sys
import io
import re
import time
import importlib.util

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
APP_PATH    = os.path.join(PROJECT_DIR, 'jolly-relay.py')
CONFIG_PATH = os.path.join(SCRIPT_DIR, 'jolly-relay-test.yaml')
ADDRESSES_PATH = os.path.join(SCRIPT_DIR, 'payloads', 'addresses.txt')

ITERATIONS = 1168

sys.path.insert(0, PROJECT_DIR)

spec = importlib.util.spec_from_file_location('jolly_relay', APP_PATH)
jmx  = importlib.util.module_from_spec(spec)
sys.modules['jolly_relay'] = jmx
spec.loader.exec_module(jmx)

jmx.config.config_file = CONFIG_PATH
jmx.config.verbose = False
jmx.config.load()

with open(ADDRESSES_PATH) as f:
    lines = f.read().strip().splitlines()[1:]  # skip header

address_pairs = []
for line in lines:
    if not line.strip():
        continue
    parts = re.split(r'\t', line.strip())
    if len(parts) >= 2:
        address_pairs.append((parts[0], parts[1]))

total = len(address_pairs) * ITERATIONS
print(f'Load test: {len(address_pairs)} pairs × {ITERATIONS} iterations = {total:,} calls')

real_stdout = sys.stdout
sys.stdout  = io.StringIO()

start = time.time()
for _ in range(ITERATIONS):
    for sender, recipient in address_pairs:
        jmx.get_mx_for_message(sender, recipient, 3600)
elapsed = time.time() - start

sys.stdout = real_stdout

rps = total / elapsed if elapsed > 0 else 0
print(f'✅ {total:,} calls in {elapsed:.2f}s  ({rps:,.0f} calls/s)')
print()
print(jmx.config.print_usage())
