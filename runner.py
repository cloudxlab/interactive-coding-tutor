"""Executes a learner's code and test cases in an isolated subprocess.

Usage: python -I runner.py <payload.json>
payload = {"code": str, "tests": [{"call": str, "expected": str}]}
Prints a single line starting with RESULT: followed by JSON.
"""
import json
import signal
import sys
import traceback

PER_TEST_SECONDS = 3


class _Timeout(Exception):
    pass


def _on_alarm(signum, frame):
    raise _Timeout()


def main():
    with open(sys.argv[1]) as f:
        payload = json.load(f)

    ns = {"__name__": "__main__"}
    out = {"load_error": None, "results": []}
    signal.signal(signal.SIGALRM, _on_alarm)

    try:
        signal.alarm(PER_TEST_SECONDS * 2)
        exec(compile(payload["code"], "<your notebook>", "exec"), ns)
        signal.alarm(0)
    except BaseException:
        signal.alarm(0)
        out["load_error"] = traceback.format_exc(limit=3)
        print("RESULT:" + json.dumps(out))
        return

    for t in payload["tests"]:
        r = {"call": t["call"], "expected": t["expected"], "passed": False, "actual": None, "error": None}
        try:
            expected = eval(t["expected"], ns)
            signal.alarm(PER_TEST_SECONDS)
            actual = eval(t["call"], ns)
            signal.alarm(0)
            r["actual"] = repr(actual)
            r["passed"] = bool(actual == expected)
        except _Timeout:
            r["error"] = f"Timed out after {PER_TEST_SECONDS}s (infinite loop?)"
        except BaseException as e:
            signal.alarm(0)
            r["error"] = f"{type(e).__name__}: {e}"
        out["results"].append(r)

    print("RESULT:" + json.dumps(out))


if __name__ == "__main__":
    main()
