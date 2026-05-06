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
import importlib.util

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
APP_PATH    = os.path.join(PROJECT_DIR, 'jolly-relay.py')
CONFIG_PATH = os.path.join(SCRIPT_DIR, 'jolly-relay-test.yaml')
ADDRESSES_PATH = os.path.join(SCRIPT_DIR, 'payloads', 'addresses.txt')

NUM_THREADS          = 24
ITERATIONS_PER_THREAD = 791

sys.path.insert(0, PROJECT_DIR)

spec = importlib.util.spec_from_file_location('jolly_relay', APP_PATH)
jmx  = importlib.util.module_from_spec(spec)
sys.modules['jolly_relay'] = jmx
spec.loader.exec_module(jmx)

jmx.config.config_file = CONFIG_PATH
jmx.config.verbose = False
jmx.config.load()

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
            await jmx.get_mx_for_message(sender, recipient, 3600)


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

for server in jmx.config.servers_obj.servers:
    if server.mails_sent < 0:
        errors.append(f'Server {server.name} has negative mails_sent ({server.mails_sent})')

group_names = [g for g in vars(jmx.config.server_groups) if not g.startswith('__')]
for gname in group_names:
    for server in getattr(jmx.config.server_groups, gname).servers:
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
print(jmx.config.print_usage())
