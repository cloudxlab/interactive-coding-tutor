"""Interactive Python coding tutor: chat (left) + JupyterLab (right)."""
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path

import nbformat
import uvicorn
from anthropic import Anthropic
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).parent
WORKSPACE = ROOT / "workspace"
STATE_FILE = ROOT / "state.json"
APP_PORT = int(os.environ.get("TUTOR_PORT", "8765"))
JUPYTER_PORT = int(os.environ.get("TUTOR_JUPYTER_PORT", "8766"))
JUPYTER_TOKEN = secrets.token_urlsafe(16)
MAX_LEVEL = 10

_lock = threading.RLock()
_client = None
_jupyter_proc = None


# ----------------------------------------------------------------------------
# State
# ----------------------------------------------------------------------------
def default_state():
    return {
        "onboarded": False,
        "beginning_knowledge": "",
        "current_knowledge": "",
        "knowledge_log": [],
        "level": 1,
        "counter": 0,
        "history": [],  # {id,title,difficulty,result}
        "current": None,  # active problem (includes hidden reference_solution)
        "chat": [],  # {role: user|assistant, content}
    }


def load_state():
    with _lock:
        if STATE_FILE.exists():
            return {**default_state(), **json.loads(STATE_FILE.read_text(encoding="utf-8"))}
        return default_state()


def save_state(s):
    with _lock:
        STATE_FILE.write_text(json.dumps(s, indent=2), encoding="utf-8")


def public_state(s):
    cur = s["current"]
    pub_cur = None
    if cur:
        pub_cur = {k: v for k, v in cur.items() if k not in ("reference_solution", "tests")}
        pub_cur["test_count"] = len(cur["tests"])
        pub_cur["notebook_url"] = notebook_url(cur["notebook"])
    return {
        "onboarded": s["onboarded"],
        "beginning_knowledge": s["beginning_knowledge"],
        "current_knowledge": s["current_knowledge"],
        "knowledge_log": s["knowledge_log"],
        "level": s["level"],
        "history": s["history"],
        "current": pub_cur,
        "chat": s["chat"],
    }


# ----------------------------------------------------------------------------
# LLM
# ----------------------------------------------------------------------------
def pick_provider():
    """TUTOR_LLM = anthropic | openai | claude-cli, else auto-detect from keys / the `claude` command."""
    choice = os.environ.get("TUTOR_LLM", "").lower()
    if choice:
        return choice
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    if os.environ.get("OPENROUTER_API_KEY"):
        return "openrouter"
    if shutil.which("claude"):
        return "claude-cli"
    raise HTTPException(500, "No LLM configured. Set ANTHROPIC_API_KEY, OPENAI_API_KEY or OPENROUTER_API_KEY, or install the `claude` CLI.")


def _llm_anthropic(system, messages, max_tokens, json_mode=False):
    global _client
    if _client is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise HTTPException(500, "ANTHROPIC_API_KEY is not set.")
        _client = Anthropic()
    resp = _client.messages.create(model=os.environ.get("TUTOR_MODEL", "claude-sonnet-5-5"), max_tokens=max_tokens,
                                   system=system, messages=messages)
    return "".join(b.text for b in resp.content if b.type == "text")


def _llm_openai(system, messages, max_tokens, json_mode=False):
    try:
        from openai import OpenAI
    except ImportError:
        raise HTTPException(500, "The `openai` package is not installed. Run: .venv/bin/pip install openai")
    if not os.environ.get("OPENAI_API_KEY"):
        raise HTTPException(500, "OPENAI_API_KEY is not set.")
    kwargs = {"response_format": {"type": "json_object"}} if json_mode else {}
    resp = OpenAI().chat.completions.create(
        model=os.environ.get("TUTOR_MODEL", "gpt-4.1"), max_completion_tokens=max_tokens,
        messages=[{"role": "system", "content": system}] + messages, **kwargs)
    return resp.choices[0].message.content


DEFAULT_OPENROUTER_MODEL = "anthropic/claude-sonnet-4.5"


def _llm_openrouter(system, messages, max_tokens, json_mode=False):
    """OpenRouter's OpenAI-compatible API (https://openrouter.ai/docs). Plain HTTP, no extra package.
    json_mode is ignored: not every OpenRouter model supports response_format, and parse_json is lenient."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise HTTPException(500, "OPENROUTER_API_KEY is not set. Run setup or openrouter_oauth.py.")
    base = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
    body = json.dumps({
        "model": os.environ.get("TUTOR_MODEL") or DEFAULT_OPENROUTER_MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": system}] + messages,
    }).encode()
    req = urllib.request.Request(base + "/chat/completions", data=body, method="POST", headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "HTTP-Referer": f"http://localhost:{APP_PORT}",  # optional attribution headers OpenRouter recommends
        "X-Title": "Python Coding Tutor",
    })
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        hint = " Your key may be invalid or out of credits." if e.code in (401, 402) else ""
        raise HTTPException(502, f"OpenRouter error {e.code}: {detail}{hint}")
    except Exception as e:
        raise HTTPException(502, f"Could not reach OpenRouter: {e}")
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise HTTPException(502, f"Unexpected OpenRouter response: {str(data)[:300]}")


def _llm_claude_cli(system, messages, max_tokens, json_mode=False):
    """Shells out to `claude -p` (uses the user's existing Claude Code login; no API key needed)."""
    if len(messages) == 1:
        prompt = messages[0]["content"]
    else:
        prompt = "Conversation so far:\n\n" + "\n\n".join(
            f'{"Learner" if m["role"] == "user" else "Tutor"}: {m["content"]}' for m in messages
        ) + "\n\nWrite the Tutor's next reply only."
    cmd = [shutil.which("claude") or "claude", "-p", "--system-prompt", system, "--tools", "", "--no-session-persistence", "--disable-slash-commands"]
    if os.environ.get("TUTOR_MODEL"):
        cmd += ["--model", os.environ["TUTOR_MODEL"]]
    with tempfile.TemporaryDirectory() as d:  # neutral cwd so no project CLAUDE.md/settings leak in
        try:
            p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180, cwd=d)
        except subprocess.TimeoutExpired:
            raise HTTPException(504, "The `claude` command timed out.")
    if p.returncode != 0 or not p.stdout.strip():
        raise HTTPException(502, f"`claude` command failed: {(p.stderr or p.stdout).strip()[-500:]}")
    return p.stdout.strip()


PROVIDERS = {"anthropic": _llm_anthropic, "openai": _llm_openai, "openrouter": _llm_openrouter, "claude-cli": _llm_claude_cli}


def llm(system, messages, max_tokens=4000, json_mode=False):
    provider = pick_provider()
    if provider not in PROVIDERS:
        raise HTTPException(500, f"Unknown TUTOR_LLM '{provider}'. Use one of: {', '.join(PROVIDERS)}.")
    return PROVIDERS[provider](system, messages, max_tokens, json_mode)


_JSON_ESCAPE = re.compile(r'\\(["\\/bfnrt]|u[0-9a-fA-F]{4})|\\')


def _fix_escapes(text):
    """Models often put Python code (e.g. regexes like \\d) in JSON strings with un-escaped backslashes.
    Keep valid JSON escapes, double any other backslash."""
    return _JSON_ESCAPE.sub(lambda m: m.group(0) if m.group(1) else "\\\\", text)


def parse_json(text):
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b < 0:
        raise ValueError("no JSON object found")
    raw = text[a : b + 1]
    try:
        return json.loads(raw, strict=False)
    except json.JSONDecodeError:
        return json.loads(_fix_escapes(raw), strict=False)


def llm_json(system, user, max_tokens=4000):
    system += "\n\nRespond with a single JSON object and nothing else. Escape every backslash inside strings as \\\\ ."
    last = None
    for _ in range(2):
        text = llm(system, [{"role": "user", "content": user}], max_tokens, json_mode=True)
        try:
            return parse_json(text)
        except (ValueError, json.JSONDecodeError) as e:
            last = e
    raise HTTPException(502, f"The model returned malformed JSON: {last}")


# ----------------------------------------------------------------------------
# Jupyter
# ----------------------------------------------------------------------------
def notebook_url(name):
    return f"http://localhost:{JUPYTER_PORT}/doc/tree/{name}?token={JUPYTER_TOKEN}"


def start_jupyter():
    global _jupyter_proc
    WORKSPACE.mkdir(exist_ok=True)
    settings = ROOT / "jlab_settings"
    settings.mkdir(exist_ok=True)
    # Autosave every 5s so the notebook on disk is (almost) always current when we run the tests.
    (settings / "overrides.json").write_text(
        json.dumps({"@jupyterlab/docmanager-extension:plugin": {"autosaveInterval": 5}}), encoding="utf-8"
    )
    tornado = {"headers": {"Content-Security-Policy": "frame-ancestors 'self' http://localhost:* http://127.0.0.1:*"}}
    cmd = [
        sys.executable, "-m", "jupyterlab",
        "--no-browser", f"--port={JUPYTER_PORT}", "--ServerApp.port_retries=0", "--ip=127.0.0.1",
        f"--ServerApp.token={JUPYTER_TOKEN}",
        f"--ServerApp.root_dir={WORKSPACE}",
        f"--ServerApp.tornado_settings={json.dumps(tornado)}",
        f"--LabApp.app_settings_dir={settings}",
    ]
    log = open(ROOT / "jupyter.log", "w")
    _jupyter_proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{JUPYTER_PORT}/api/status?token={JUPYTER_TOKEN}", timeout=1)
            return
        except Exception:
            if _jupyter_proc.poll() is not None:
                raise RuntimeError("JupyterLab failed to start; see jupyter.log")
            time.sleep(0.5)
    raise RuntimeError("JupyterLab did not become ready in time; see jupyter.log")


def stop_jupyter():
    if _jupyter_proc and _jupyter_proc.poll() is None:
        _jupyter_proc.terminate()
        try:
            _jupyter_proc.wait(5)
        except subprocess.TimeoutExpired:
            _jupyter_proc.kill()


def create_notebook(pid, title, description, starter):
    name = f"problem_{pid:03d}.ipynb"
    nb = nbformat.v4.new_notebook()
    nb.cells = [
        nbformat.v4.new_markdown_cell(f"# {title}\n\n{description}\n\n---\n*Write your solution in the cell below, "
                                      "then go back to the chat and tell the tutor you're done. "
                                      "The tutor runs the tests on all code cells in this notebook.*"),
        nbformat.v4.new_code_cell(starter),
    ]
    nb.metadata["kernelspec"] = {"display_name": "Python 3", "language": "python", "name": "python3"}
    nbformat.write(nb, WORKSPACE / name)
    return name


def notebook_code(name):
    nb = nbformat.read(WORKSPACE / name, as_version=4)
    lines = []
    for cell in nb.cells:
        if cell.cell_type == "code":
            # drop IPython magics / shell escapes; they aren't valid plain Python
            lines.append("\n".join(l for l in cell.source.splitlines() if not l.lstrip().startswith(("%", "!"))))
    return "\n\n".join(lines)


# ----------------------------------------------------------------------------
# Running tests
# ----------------------------------------------------------------------------
def run_tests(code, tests):
    with tempfile.TemporaryDirectory() as d:
        payload = Path(d) / "payload.json"
        payload.write_text(json.dumps({"code": code, "tests": tests}), encoding="utf-8")
        try:
            p = subprocess.run(
                [sys.executable, "-I", str(ROOT / "runner.py"), str(payload)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=40, stdin=subprocess.DEVNULL, cwd=d,
            )
        except subprocess.TimeoutExpired:
            return {"load_error": "Your code took too long to run (over 40s). Check for infinite loops.", "results": []}
    for line in reversed(p.stdout.splitlines()):
        if line.startswith("RESULT:"):
            return json.loads(line[len("RESULT:"):])
    return {"load_error": (p.stderr or "Runner crashed")[-1500:], "results": []}


# ----------------------------------------------------------------------------
# Tutor logic
# ----------------------------------------------------------------------------
DIFFICULTY = """Difficulty scale: 1 = print/loop basics (sum a range, count items); 2 = loops with conditions (FizzBuzz-style);
3 = simple string/list loops (reverse, count vowels, max without max()); 4 = LeetCode-easy warm-ups (two-sum brute force, palindromes);
5 = LeetCode-easy (hash maps, two pointers); 6 = easy/medium; 7 = medium; 8 = medium-hard; 9-10 = hard."""


def assess_beginner(text):
    return llm_json(
        "You are a Python tutor assessing a new learner. " + DIFFICULTY +
        '\nReturn {"level": <1-10 int, the difficulty to START at; be conservative>, '
        '"knowledge": "<2-4 sentence summary of what they can and cannot do, third person>", '
        '"topic": "<first topic to practice>"}.',
        f"Learner's description of their Python knowledge:\n{text}",
    )


def generate_problem(s, direction, topic_hint=None):
    seen = [f'{h["title"]} ({h["result"]})' for h in s["history"][-15:]]
    system = (
        "You are an expert Python tutor generating ONE coding problem with test cases. " + DIFFICULTY + "\n"
        "Rules: the learner writes a single function in a Jupyter notebook. Problems must be fully specified with 1-2 examples "
        "in the description (markdown). The description must NOT start with a heading or repeat the title (the title is shown separately); use plain paragraphs, and \"### Examples\"-style subheadings only if needed. Provide a starter code stub (function signature + docstring + `pass`). "
        "Provide 6-10 test cases including edge cases; each test is {\"call\": \"<python expression calling the function>\", "
        "\"expected\": \"<python expression for expected value>\"}. Tests must be deterministic and use ==-comparable values. "
        "IMPORTANT: the function must RETURN its result (never print or use input()), even for absolute beginners: "
        "for example 'return a greeting string', 'return the sum', 'return True/False'. Each \"call\" and \"expected\" must be a single "
        "Python expression on one line (no imports, no multiple statements, no stdout capturing). Also provide a correct reference_solution (full code defining the function).\n"
        'Return {"title","description","function_name","starter_code","tests":[...],"reference_solution","difficulty":<int>,"topic"}.'
    )
    user = (
        f"Learner's current knowledge: {s['current_knowledge']}\n"
        f"Target difficulty level: {s['level']} ({direction}).\n"
        f"Topic: {topic_hint or 'continue with loops-focused problems, drifting toward LeetCode-easy style as they improve'}.\n"
        f"Problems already given (do not repeat): {seen or 'none'}"
    )
    last_err = None
    for _ in range(3):
        prob = llm_json(system, user, max_tokens=5000)
        try:
            for k in ("title", "description", "starter_code", "tests", "reference_solution"):
                assert prob.get(k), f"missing {k}"
            assert all("call" in t and "expected" in t for t in prob["tests"]), "bad test shape"
            # Validate the generated tests against the reference solution before showing them to the learner.
            res = run_tests(prob["reference_solution"], prob["tests"])
            assert not res["load_error"], res["load_error"]
            bad = [r for r in res["results"] if not r["passed"]]
            assert not bad, f"reference solution fails tests: {bad[:2]}"
            prob["description"] = re.sub(r"\A\s*#{1,6}[ \t]+[^\n]*\n+", "", prob["description"])
            return prob
        except AssertionError as e:
            last_err = str(e)
            user += f"\n\nYour previous attempt was invalid ({last_err}). Fix it."
    raise HTTPException(502, f"Could not generate a valid problem: {last_err}")


def issue_problem(s, direction, topic_hint=None):
    prob = generate_problem(s, direction, topic_hint)
    s["counter"] += 1
    pid = s["counter"]
    nb = create_notebook(pid, prob["title"], prob["description"], prob["starter_code"])
    s["current"] = {
        "id": pid,
        "title": prob["title"],
        "description": prob["description"],
        "difficulty": prob.get("difficulty", s["level"]),
        "topic": prob.get("topic", ""),
        "notebook": nb,
        "tests": prob["tests"],
        "reference_solution": prob["reference_solution"],
        "attempts": 0,
        "passed": False,
    }
    s["chat"].append({"role": "assistant", "kind": "problem", "content": f"**Problem {pid}: {prob['title']}**\n\n{prob['description']}\n\n"
                      f"_Difficulty {s['current']['difficulty']}/10 · open `{nb}` in the notebook on the right, solve it, then click **I've solved it**._"})


def update_knowledge(s, outcome_text):
    out = llm_json(
        "You track a Python learner's skills. Given their current knowledge summary and the latest outcome, return "
        '{"knowledge": "<updated 2-4 sentence summary of strengths and weaknesses>"}.',
        f"Current knowledge: {s['current_knowledge']}\nLatest outcome: {outcome_text}",
        max_tokens=600,
    )
    s["current_knowledge"] = out["knowledge"]
    s["knowledge_log"].append({"t": int(time.time()), "knowledge": out["knowledge"], "level": s["level"]})


# ----------------------------------------------------------------------------
# API
# ----------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app):
    start_jupyter()
    yield
    stop_jupyter()


app = FastAPI(lifespan=lifespan)


class Onboard(BaseModel):
    text: str


class Chat(BaseModel):
    message: str


@app.get("/api/state")
def get_state():
    return public_state(load_state())


@app.post("/api/onboard")
def onboard(body: Onboard):
    if not body.text.strip():
        raise HTTPException(400, "Please describe your Python knowledge.")
    with _lock:
        s = default_state()
        a = assess_beginner(body.text)
        s["onboarded"] = True
        s["beginning_knowledge"] = a["knowledge"]
        s["current_knowledge"] = a["knowledge"]
        s["level"] = max(1, min(MAX_LEVEL, int(a.get("level", 1))))
        s["knowledge_log"].append({"t": int(time.time()), "knowledge": a["knowledge"], "level": s["level"]})
        s["chat"].append({"role": "assistant", "content": f"Thanks! Here's where I think you're starting:\n\n{a['knowledge']}\n\nLet's begin."})
        issue_problem(s, "starting problem", a.get("topic"))
        save_state(s)
        return public_state(s)


@app.post("/api/check")
def check():
    with _lock:
        s = load_state()
        cur = s["current"]
        if not cur:
            raise HTTPException(400, "No active problem.")
        try:
            code = notebook_code(cur["notebook"])
        except Exception as e:
            raise HTTPException(500, f"Could not read notebook: {e}")
        result = run_tests(code, cur["tests"])
        cur["attempts"] += 1
        n_pass = sum(r["passed"] for r in result["results"])
        total = len(cur["tests"])
        passed = not result["load_error"] and n_pass == total
        cur["passed"] = passed
        details = result["load_error"] or "; ".join(
            f'{r["call"]} -> {r["actual"] or r["error"]} (expected {r["expected"]})' for r in result["results"] if not r["passed"]
        )
        outcome = f'Problem "{cur["title"]}" (difficulty {cur["difficulty"]}): ' + (
            f"PASSED all {total} tests on attempt {cur['attempts']}." if passed else f"{n_pass}/{total} tests passed. Failures: {details[:800]}"
        )
        update_knowledge(s, outcome + f"\nTheir code:\n{code[:1500]}")
        if passed:
            s["history"].append({"id": cur["id"], "title": cur["title"], "difficulty": cur["difficulty"], "result": "passed"})
            s["level"] = min(MAX_LEVEL, s["level"] + 1)
            msg = "All tests passed! 🎉 Click **Next problem** when you're ready for a harder one."
        else:
            msg = "Not quite yet. Review the failures below, fix your code, and run the tests again. Ask me for a hint if you're stuck."
        s["chat"].append({"role": "assistant", "kind": "result", "content": msg, "result": result, "passed": passed})
        save_state(s)
        return {"state": public_state(s), "result": result, "passed": passed}


@app.post("/api/next")
def next_problem(skip: bool = False):
    with _lock:
        s = load_state()
        cur = s["current"]
        if cur and skip and not cur["passed"]:
            s["history"].append({"id": cur["id"], "title": cur["title"], "difficulty": cur["difficulty"], "result": "skipped"})
            s["level"] = max(1, s["level"] - 1)
            update_knowledge(s, f'Could not solve "{cur["title"]}" (difficulty {cur["difficulty"]}) after {cur["attempts"]} attempts and asked for an easier problem.')
            s["chat"].append({"role": "assistant", "content": "No problem, let's step back with something a bit easier. "
                              f"For reference, one way to solve the last one:\n\n```python\n{cur['reference_solution']}\n```"})
        elif cur and not cur["passed"]:
            raise HTTPException(400, "Solve the current problem first, or ask for an easier one.")
        issue_problem(s, "easier than before" if skip else "harder than before")
        save_state(s)
        return public_state(s)


@app.post("/api/chat")
def chat(body: Chat):
    with _lock:
        s = load_state()
        s["chat"].append({"role": "user", "content": body.message})
        cur = s["current"]
        system = (
            "You are a friendly, concise Python tutor. The learner is solving a problem in a Jupyter notebook next to this chat. "
            "Give hints and explanations, NOT the full solution (never paste the reference solution unless the learner has given up, "
            "which is handled by a separate button). Guide with questions and small nudges. Use markdown.\n"
            f"Learner knowledge: {s['current_knowledge']}\n"
            + (f"Current problem: {cur['title']}\n{cur['description']}\nReference solution (PRIVATE): {cur['reference_solution']}" if cur else "")
        )
        msgs = [{"role": m["role"], "content": m["content"]} for m in s["chat"] if m["role"] in ("user", "assistant")][-20:]
        # Anthropic requires the first message to be from the user
        while msgs and msgs[0]["role"] != "user":
            msgs.pop(0)
        reply = llm(system, msgs, max_tokens=1200)
        s["chat"].append({"role": "assistant", "content": reply})
        save_state(s)
        return {"reply": reply}


@app.post("/api/reset")
def reset():
    with _lock:
        STATE_FILE.unlink(missing_ok=True)
    return {"ok": True}


app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="static")

if __name__ == "__main__":
    print(f"\n  Coding tutor running at http://localhost:{APP_PORT}\n")
    uvicorn.run(app, host="127.0.0.1", port=APP_PORT)
