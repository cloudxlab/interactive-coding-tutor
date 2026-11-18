"""Executes a learner's code and test cases in an isolated subprocess.

Usage: python -I runner.py <payload.json>
payload = {"code": str, "tests": [{"call": str, "expected": str}]}
Prints a single line starting with RESULT: followed by JSON.

Timeouts use a worker thread (not signals) so this works on Windows, macOS and Linux.
A stuck thread can't be killed, so the process exits with os._exit() once results are printed.
"""
import json
import os
import sys
import threading
import traceback

PER_TEST_SECONDS = 3


def run_with_timeout(fn, seconds):
    """Returns ("ok", value) | ("error", exc_or_traceback) | ("timeout", None)."""
    box = {}

    def target():
        try:
            box["ok"] = fn()
        except BaseException as e:
            box["error"] = e
            box["tb"] = traceback.format_exc(limit=3)

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        return "timeout", None
    if "error" in box:
        return "error", box
    return "ok", box["ok"]


def main():
    with open(sys.argv[1], encoding="utf-8") as f:
        payload = json.load(f)

    ns = {"__name__": "__main__"}
    out = {"load_error": None, "results": []}

    status, val = run_with_timeout(
        lambda: exec(compile(payload["code"], "<your notebook>", "exec"), ns), PER_TEST_SECONDS * 2)
    if status == "timeout":
        out["load_error"] = f"Your code did not finish within {PER_TEST_SECONDS * 2}s (infinite loop outside a function?)."
    elif status == "error":
        out["load_error"] = val["tb"]
    if out["load_error"]:
        return out

    for t in payload["tests"]:
        r = {"call": t["call"], "expected": t["expected"], "passed": False, "actual": None, "error": None}
        try:
            expected = eval(t["expected"], ns)
            status, val = run_with_timeout(lambda: eval(t["call"], ns), PER_TEST_SECONDS)
            if status == "timeout":
                r["error"] = f"Timed out after {PER_TEST_SECONDS}s (infinite loop?)"
            elif status == "error":
                e = val["error"]
                r["error"] = f"{type(e).__name__}: {e}"
            else:
                r["actual"] = repr(val)
                r["passed"] = bool(val == expected)
        except BaseException as e:
            r["error"] = f"{type(e).__name__}: {e}"
        out["results"].append(r)
    return out


if __name__ == "__main__":
    result = main()
    print("RESULT:" + json.dumps(result), flush=True)
    sys.stdout.flush()
    os._exit(0)
