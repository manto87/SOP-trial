"""
ElectroTech S&OP — Master Test Runner
======================================
Run all test modules and produce a summary report.

Usage:
    python tests/test_runner.py               # requires Flask server already running
    python tests/test_runner.py --start-server # starts/stops the server automatically

The runner exits with code 0 (all pass) or 1 (any failure), so it works in CI.
"""

import sys
import os
import time
import subprocess
import importlib
import importlib.util
import traceback

# Add parent dir to path so test modules can import from sop_app
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))

SERVER_URL = "http://localhost:5050"
SERVER_PROC = None


# ── Colour helpers ────────────────────────────────────────────────────────────
def green(s):  return f"\033[92m{s}\033[0m"
def red(s):    return f"\033[91m{s}\033[0m"
def yellow(s): return f"\033[93m{s}\033[0m"
def bold(s):   return f"\033[1m{s}\033[0m"


def server_is_up():
    import urllib.request
    try:
        urllib.request.urlopen(f"{SERVER_URL}/api/products", timeout=3)
        return True
    except Exception:
        return False


def start_server():
    global SERVER_PROC
    app_py = os.path.join(os.path.dirname(TESTS_DIR), "app.py")
    SERVER_PROC = subprocess.Popen(
        [sys.executable, app_py],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    for _ in range(20):
        time.sleep(0.5)
        if server_is_up():
            return True
    return False


def stop_server():
    if SERVER_PROC:
        SERVER_PROC.terminate()
        SERVER_PROC.wait()


def run_module(name):
    """Import and run a test module; return (passed, failed, errors) counts."""
    try:
        spec = importlib.util.spec_from_file_location(
            name, os.path.join(TESTS_DIR, f"{name}.py")
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if hasattr(mod, "run"):
            return mod.run()
        return 0, 0, [f"{name}.py has no run() function"]
    except Exception as e:
        tb = traceback.format_exc()
        return 0, 1, [f"Import/run error in {name}:\n{tb}"]


def main():
    auto_start = "--start-server" in sys.argv

    print(bold("\n" + "=" * 68))
    print(bold("  ElectroTech S&OP — Full Test Suite"))
    print(bold("=" * 68))

    # Server availability check
    if not server_is_up():
        if auto_start:
            print(yellow("  Server not running — starting automatically…"))
            if not start_server():
                print(red("  ERROR: could not start Flask server. Aborting."))
                sys.exit(1)
            print(green("  Server started on http://localhost:5050"))
        else:
            print(red("  ERROR: Flask server not reachable at http://localhost:5050"))
            print(yellow("  Tip: run  python app.py  in another terminal,"))
            print(yellow("       or use  python tests/test_runner.py --start-server"))
            sys.exit(1)
    else:
        print(green(f"  Server reachable at {SERVER_URL}"))

    # Discover test modules (alphabetical, skip __*)
    modules = sorted(
        f[:-3] for f in os.listdir(TESTS_DIR)
        if f.startswith("test_") and f.endswith(".py") and f != "test_runner.py"
    )

    total_pass = total_fail = 0
    module_results = []

    # Modules that trigger plan runs need a brief settling pause after them
    PLAN_RUN_MODULES = {"test_plan_engine"}

    for mod_name in modules:
        print(f"\n{bold(f'  ▶ {mod_name}')}")
        t0 = time.time()
        passed, failed, errors = run_module(mod_name)
        elapsed = time.time() - t0
        if mod_name in PLAN_RUN_MODULES:
            time.sleep(5)  # allow DB writes to complete before next module
        total_pass += passed
        total_fail += failed
        status = green("PASS") if failed == 0 else red("FAIL")
        print(f"    [{status}]  {passed} passed, {failed} failed  ({elapsed:.1f}s)")
        for err in errors:
            print(red(f"    ✗ {err}"))
        module_results.append((mod_name, passed, failed))

    # Summary
    print(bold("\n" + "=" * 68))
    total = total_pass + total_fail
    if total_fail == 0:
        print(green(bold(f"  ALL TESTS PASSED  ({total_pass}/{total})")))
    else:
        print(red(bold(f"  {total_fail} TESTS FAILED  ({total_pass}/{total} passed)")))
    print(bold("=" * 68 + "\n"))

    # Module summary table
    for mod, p, f in module_results:
        bar = green("●") if f == 0 else red("●")
        print(f"  {bar}  {mod:<40} {p:>3} pass  {f:>3} fail")
    print()

    if auto_start:
        stop_server()

    sys.exit(0 if total_fail == 0 else 1)


if __name__ == "__main__":
    main()
