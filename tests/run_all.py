#!/usr/bin/env python3
import os
import sys
import subprocess

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

TEST_FILES = [
    "test_domain_lookup.py",   # in-process unit test — runs first, fastest
    "test_simple.py",
    "test_direction.py",
    "test_roundrobin.py",
    "test_improper_usage.py",
    "test_auto_populate.py",
    "test_full.py",
    "load_test.py",
    # "load_concurrent.py",   # uncomment for concurrency stress test
]


def run_all_tests():
    print(f"Running {len(TEST_FILES)} test suites...\n")

    all_passed  = True
    failed_tests = []

    for test_file in TEST_FILES:
        test_path = os.path.join(SCRIPT_DIR, test_file)
        print(f"⌛ {test_file:<35}", end="", flush=True)

        try:
            result = subprocess.run(
                [sys.executable, test_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            print("\r", end="")
            if result.returncode == 0:
                print(f"✅ {test_file:<35}")
            else:
                print(f"❌ {test_file:<35}")
                failed_tests.append((test_file, result.stdout, result.stderr))
                all_passed = False
        except Exception as e:
            print("\r", end="")
            print(f"❌ {test_file:<35}")
            failed_tests.append((test_file, "", str(e)))
            all_passed = False

    if all_passed:
        print("\n✅ All tests passed!")
        sys.exit(0)
    else:
        print(f"\n❌ {len(failed_tests)} test(s) failed:\n")
        for name, stdout, stderr in failed_tests:
            print(f"--- {name} ---")
            if stdout.strip():
                print(stdout.strip())
            if stderr.strip():
                print("STDERR:", stderr.strip())
            print()
        sys.exit(1)


if __name__ == "__main__":
    run_all_tests()
