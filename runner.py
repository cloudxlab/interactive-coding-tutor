"""Executes a learner's code and test cases in an isolated subprocess.

Usage: python -I runner.py <payload.json>      (or "-" to read the JSON payload from stdin)
payload = {"code": str, "tests": [{"call": str, "expected": str}]}
Prints a single line starting with RESULT: followed by JSON.

Timeouts use a worker thread (not signals) so this works on Windows, macOS and Linux.
A stuck thread can't be killed, so the process exits with os._exit() once results are printed.
"""
import ast
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
            box["tb"] = clean_tb(e)

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        return "timeout", None
    if "error" in box:
        return "error", box
    return "ok", box["ok"]


def clean_tb(exc):
    """Traceback limited to the learner's own code (cells / tests), without this runner's internals."""
    frames = [f for f in traceback.extract_tb(exc.__traceback__) if f.filename.startswith("<")]
    lines = ["Traceback (most recent call last):"] if frames else []
    lines += [l.rstrip("\n") for l in traceback.format_list(frames)]
    lines += [l.rstrip("\n") for l in traceback.format_exception_only(type(exc), exc)]
    return "\n".join(lines)


def evaluate(src, ns):
    """Evaluates a test snippet. A plain expression works; so does a multi-line snippet whose
    last line is an expression (earlier lines are executed first, e.g. to capture printed output)."""
    tree = ast.parse(src.strip(), mode="exec")
    if not tree.body or not isinstance(tree.body[-1], ast.Expr):
        raise SyntaxError("a test must end with an expression whose value is compared")
    last = ast.Expression(tree.body[-1].value)
    tree.body = tree.body[:-1]
    exec(compile(tree, "<test>", "exec"), ns)
    return eval(compile(last, "<test>", "eval"), ns)


def main():
    if sys.argv[1] == "-":
        payload = json.load(sys.stdin)
    else:
        with open(sys.argv[1], encoding="utf-8") as f:
            payload = json.load(f)

    ns = {"__name__": "__main__"}
    out = {"load_error": None, "cell_errors": [], "results": []}

    # Run each notebook cell on its own, in one shared namespace, like Jupyter does. A scratch cell that
    # raises (e.g. uses an undefined variable) is reported but must not stop the other cells or the tests.
    cells = payload.get("cells") or [payload.get("code", "")]
    for i, src in enumerate(cells, 1):
        if not src.strip():
            continue
        status, val = run_with_timeout(lambda: exec(compile(src, f"<cell {i}>", "exec"), ns), PER_TEST_SECONDS * 2)
        if status == "timeout":
            out["cell_errors"].append({"cell": i, "error": f"Cell {i} did not finish within {PER_TEST_SECONDS * 2}s "
                                                           "(infinite loop outside a function?)."})
        elif status == "error":
            out["cell_errors"].append({"cell": i, "error": val["tb"]})

    for t in payload["tests"]:
        r = {"call": t["call"], "expected": t["expected"], "passed": False, "actual": None, "error": None}
        try:
            expected = evaluate(t["expected"], ns)
            status, val = run_with_timeout(lambda: evaluate(t["call"], ns), PER_TEST_SECONDS)
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
    # The learner's prints are never shown, so discard them (a print-forever loop must not flood memory/pipes).
    # Only the RESULT line goes to the real stdout.
    real_stdout = sys.stdout
    sys.stdout = sys.stderr = open(os.devnull, "w")
    result = main()
    real_stdout.write("RESULT:" + json.dumps(result) + "\n")
    real_stdout.flush()
    os._exit(0)
