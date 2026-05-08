#!/usr/bin/env python3
"""
Concurrent load test: runs get_mx_for_message() from NUM_THREADS threads
simultaneously, all starting at the exact same instant via a Barrier.

Validates thread-safety invariants:
  - No thread raised an exception
  - No server has a negative mails_sent count
"""

import asyncio
import os
import sys
import io
import re
import time
import threading
import traceback
import unittest.mock as mock

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
CONFIG_PATH = os.path.join(SCRIPT_DIR, 'jolly-relay-test.yaml')
ADDRESSES_PATH = os.path.join(SCRIPT_DIR, 'payloads', 'addresses.txt')

NUM_THREADS           = 24
ITERATIONS_PER_THREAD = 791

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
    lines = f.read().strip().splitlines()[1:]

address_pairs = []
for line in lines:
    if not line.strip():
        continue
    parts = re.split(r'\t', line.strip())
    if len(parts) >= 2:
        address_pairs.append((parts[0], parts[1]))

total = len(address_pairs) * ITERATIONS_PER_THREAD * NUM_THREADS
print(f'Concurrent load test')
print(f'  {len(address_pairs)} pairs × {ITERATIONS_PER_THREAD} iter × {NUM_THREADS} threads = {total:,} calls')

barrier       = threading.Barrier(NUM_THREADS)
thread_errors = []
errors_lock   = threading.Lock()


async def _worker_async():
    for _ in range(ITERATIONS_PER_THREAD):
        for sender, recipient in address_pairs:
            await get_mx_for_message(sender, recipient, config, service)


def worker(thread_id):
    try:
        barrier.wait()
        asyncio.run(_worker_async())
    except Exception as e:
        with errors_lock:
            thread_errors.append((thread_id, e, traceback.format_exc()))


real_stdout = sys.stdout
sys.stdout  = io.StringIO()

threads = [threading.Thread(target=worker, args=(i,), name=f'worker-{i}')
           for i in range(NUM_THREADS)]

start = time.time()
for t in threads:
    t.start()
for t in threads:
    t.join()
elapsed = time.time() - start

sys.stdout = real_stdout

errors = []

if thread_errors:
    for tid, exc, tb in thread_errors:
        errors.append(f'Thread {tid} raised {exc.__class__.__name__}: {exc}\n{tb}')

for server in config.servers_obj.servers:
    if server.mails_sent < 0:
        errors.append(f'Server {server.name} has negative mails_sent ({server.mails_sent})')

for gname, grp in config.server_groups.items():
    for server in grp.servers:
        if server.mails_sent < 0:
            errors.append(f'Group {gname} server {server.name}: negative mails_sent')

rps = total / elapsed if elapsed > 0 else 0
print(f'\n{total:,} calls in {elapsed:.2f}s  ({rps:,.0f} calls/s)  threads={NUM_THREADS}')

if errors:
    print(f'\n❌ Concurrency issues ({len(errors)}):')
    for e in errors:
        print(f'  {e}')
    sys.exit(1)

print('\n✅ No concurrency issues')
print(config.print_usage())
