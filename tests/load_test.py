#!/usr/bin/env python3
"""
Load test: calls get_mx_for_message() in-process over all address pairs
from addresses.txt for ITERATIONS cycles (~53 000 routing calls).

This tests the routing logic and round-robin scheduler at speed without
any real network I/O.  DNS lookups hit the in-process cache after the
first pass; misses resolve instantly to [] for unknown test domains.
Console output is suppressed during the hot loop.
"""

import os
import sys
import io
import re
import time
import asyncio
import threading
import unittest.mock as mock

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
CONFIG_PATH = os.path.join(SCRIPT_DIR, 'jolly-relay-test.yaml')
ADDRESSES_PATH = os.path.join(SCRIPT_DIR, 'payloads', 'addresses.txt')

ITERATIONS = 1168

sys.path.insert(0, PROJECT_DIR)

from src.config import Config
from src.router import get_mx_for_message

with mock.patch('sys.argv', ['jolly-relay.py']):
    config = Config()
config.config_file = CONFIG_PATH
config.verbose = False
config.load()


class _Service:
    def __init__(self, cfg):
        self.config = cfg
        self.mx_cache = {}
        self.cache_lock = threading.Lock()


service = _Service(config)

if not os.path.exists(ADDRESSES_PATH):
    print("SKIP: tests/payloads/addresses.txt not found.")
    sys.exit(0)

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


async def _run():
    for _ in range(ITERATIONS):
        for sender, recipient in address_pairs:
            await get_mx_for_message(sender, recipient, config, service)


start = time.time()
asyncio.run(_run())
elapsed = time.time() - start

sys.stdout = real_stdout

rps = total / elapsed if elapsed > 0 else 0
print(f'✅ {total:,} calls in {elapsed:.2f}s  ({rps:,.0f} calls/s)')
print()
print(config.print_usage())
