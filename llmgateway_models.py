#!/usr/bin/env python3
"""Checks an LLM Gateway API key and lists the model ids it can use. Standard library only.

    LLMGATEWAY_API_KEY=... python llmgateway_models.py        # one model id per line on stdout

The key is read from the environment (never from the command line, where other users could see it).
Exit codes: 0 ok, 2 key rejected, 3 could not reach LLM Gateway (network/proxy), 4 unexpected response.
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("LLMGATEWAY_BASE_URL", "https://api.llmgateway.io/v1").rstrip("/")


def main():
    key = os.environ.get("LLMGATEWAY_API_KEY", "")
    if not key:
        print("LLMGATEWAY_API_KEY is not set.", file=sys.stderr)
        return 2
    req = urllib.request.Request(f"{BASE}/models", headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"LLM Gateway answered HTTP {e.code}.", file=sys.stderr)
        return 2 if e.code in (401, 403) else 4
    except Exception as e:
        print(f"Could not reach LLM Gateway: {e}", file=sys.stderr)
        return 3
    items = data.get("data", data) if isinstance(data, dict) else data
    ids = sorted({m["id"] for m in items if isinstance(m, dict) and "id" in m}) if isinstance(items, list) else []
    if not ids:
        print("LLM Gateway returned no models.", file=sys.stderr)
        return 4
    print("\n".join(ids))
    return 0


if __name__ == "__main__":
    sys.exit(main())
