"""Per-user environments for multi-user mode.

* Spawner       - starts/stops one JupyterLab per user.
    DockerSpawner  : a hardened container per user (production; required for untrusted users).
    ProcessSpawner : a plain subprocess per user. NO isolation between users: dev / fully trusted groups only.
* ContainerManager - keeps track of who is running, starts on demand, stops idle ones.
* Notebook helpers - create/read notebooks through each user's Jupyter REST API (no shared filesystem needed).
* run_tests        - runs the learner's code + tests in a throwaway sandbox (Docker: no network, no secrets).
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).parent
DATA_DIR = Path(os.environ.get("TUTOR_DATA_DIR", ROOT / "data"))
ISOLATION = os.environ.get("TUTOR_ISOLATION", "docker").lower()  # docker | none
DOCKER_IMAGE = os.environ.get("TUTOR_DOCKER_IMAGE", "coding-tutor-jupyter")
DOCKER_NETWORK = os.environ.get("TUTOR_DOCKER_NETWORK", "")  # set when the app itself runs in Docker (see docker-compose.yml)
IDLE_MINUTES = float(os.environ.get("TUTOR_IDLE_MINUTES", "30"))
MAX_CONTAINERS = int(os.environ.get("TUTOR_MAX_CONTAINERS", "50"))
JUPYTER_PORT_IN_CONTAINER = 8888
BASE_PATH = "/jupyter/"

# Never route to the user's Jupyter through any system/corporate proxy.
def http_client(timeout=30):
    return httpx.Client(timeout=timeout, trust_env=False)


def scrubbed_env():
    """Environment for processes that run learner code: no API keys, OAuth secrets or session keys."""
    keep = ("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "SYSTEMDRIVE", "TEMP", "TMP", "TMPDIR")
    return {k: os.environ[k] for k in keep if k in os.environ}


class SandboxError(RuntimeError):
    pass


def jupyter_args(port, root_dir, ip, settings_dir):
    tornado = {"headers": {"Content-Security-Policy": "frame-ancestors 'self'"}}
    return [
        "--no-browser", f"--ip={ip}", f"--port={port}", "--ServerApp.port_retries=0",
        f"--ServerApp.base_url={BASE_PATH}",
        f"--ServerApp.root_dir={root_dir}",
        # The container/process is only reachable through our authenticated proxy, which injects the token,
        # so Jupyter's own Host/Origin checks (which see our proxy, not the browser) are relaxed.
        "--ServerApp.allow_origin=*", "--ServerApp.allow_remote_access=True", "--ServerApp.trust_xheaders=True",
        "--ServerApp.terminals_enabled=False",
        f"--ServerApp.tornado_settings={json.dumps(tornado)}",
        f"--LabApp.app_settings_dir={settings_dir}",
    ]


def write_server_config(config_dir):
    """Settings that can't be passed on the command line (dict traits). Disables the web terminal extension."""
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "jupyter_server_config.py").write_text(
        'c.ServerApp.jpserver_extensions = {"jupyter_server_terminals": False}  # no web terminal\n', encoding="utf-8")


def write_jlab_overrides(settings_dir):
    settings_dir.mkdir(parents=True, exist_ok=True)
    # Autosave every 5s so the saved notebook is (almost) always current when tests are run.
    (settings_dir / "overrides.json").write_text(
        json.dumps({"@jupyterlab/docmanager-extension:plugin": {"autosaveInterval": 5}}), encoding="utf-8")


# ----------------------------------------------------------------------------
# Spawners
# ----------------------------------------------------------------------------
class ProcessSpawner:
    """One JupyterLab subprocess per user. All users share the OS account: NOT a security boundary."""

    def __init__(self):
        self.procs = {}
        self.ports = {}

    def start(self, uid, token):
        user_dir = DATA_DIR / "users" / str(uid)
        work, run = user_dir / "work", user_dir / "run"
        for d in (work, run / "runtime", run / "config", run / "data", run / "tmp"):
            d.mkdir(parents=True, exist_ok=True)
        settings = DATA_DIR / "jlab_settings"
        write_jlab_overrides(settings)
        write_server_config(run / "config")
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        # Scrubbed environment: the app's API keys / OAuth secrets must not be visible to learners' code.
        # HOME is kept (Python may need it to find user-site packages); everything Jupyter writes goes to the user's dir.
        env = scrubbed_env()
        env.update({
            "HOME": os.environ.get("HOME", str(user_dir)), "TMPDIR": str(run / "tmp"), "JUPYTER_TOKEN": token,
            "JUPYTER_CONFIG_DIR": str(run / "config"), "JUPYTER_DATA_DIR": str(run / "data"),
            "JUPYTER_RUNTIME_DIR": str(run / "runtime"), "IPYTHONDIR": str(run / "ipython"),
            "XDG_CACHE_HOME": str(run / "cache"), "XDG_CONFIG_HOME": str(run / "xdg-config"),
        })
        cmd = [sys.executable, "-m", "jupyterlab"] + jupyter_args(port, work, "127.0.0.1", settings)
        log = open(user_dir / "jupyter.log", "ab")
        self.procs[uid] = subprocess.Popen(cmd, env=env, cwd=work, stdout=log, stderr=subprocess.STDOUT)
        self.ports[uid] = port
        return f"http://127.0.0.1:{port}"

    def running(self, uid):
        p = self.procs.get(uid)
        return p is not None and p.poll() is None

    def address(self, uid):
        return f"http://127.0.0.1:{self.ports[uid]}"

    def stop(self, uid):
        p = self.procs.pop(uid, None)
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()

    def count_running(self):
        return sum(1 for u in list(self.procs) if self.running(u))

    def stop_all(self):
        for u in list(self.procs):
            self.stop(u)

    def adopt_existing(self):
        return []


def _docker(*args, timeout=60, check=True):
    try:
        p = subprocess.run(["docker", *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except FileNotFoundError:
        raise SandboxError("The `docker` command was not found. Install Docker, or set TUTOR_ISOLATION=none (trusted users only).")
    except subprocess.TimeoutExpired:
        raise SandboxError(f"`docker {args[0]}` timed out")
    if check and p.returncode != 0:
        raise SandboxError(f"docker {args[0]} failed: {(p.stderr or p.stdout).strip()[:400]}")
    return p


class DockerSpawner:
    """One hardened container per user. Workspace lives in a named volume that survives container removal."""

    LABEL = "tutor.user"

    @staticmethod
    def name(uid):
        return f"tutor-u{int(uid)}"

    def run_args(self, uid, token):
        n = self.name(uid)
        args = [
            "run", "-d", "--name", n, "--label", f"{self.LABEL}={int(uid)}", "--init",
            # filesystem: read-only root, small tmpfs for scratch/home, the user's volume for notebooks
            "--read-only",
            "--tmpfs", "/tmp:rw,nosuid,size=128m,uid=10001,gid=10001",
            "-v", f"tutor-ws-{int(uid)}:/work",
            # privileges and resources
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", "256", "--memory", "768m", "--memory-swap", "768m", "--cpus", "1",
            "--user", "10001:10001",
            "-e", f"JUPYTER_TOKEN={token}", "-e", "HOME=/tmp/home",
        ]
        if DOCKER_NETWORK:
            args += ["--network", DOCKER_NETWORK]  # internal network: no route to the internet
        else:
            args += ["-p", f"127.0.0.1::{JUPYTER_PORT_IN_CONTAINER}"]
        args += [DOCKER_IMAGE]
        return args

    def running(self, uid):
        p = _docker("inspect", "-f", "{{.State.Running}}", self.name(uid), check=False)
        return p.returncode == 0 and p.stdout.strip() == "true"

    def exists(self, uid):
        return _docker("inspect", "-f", "{{.Id}}", self.name(uid), check=False).returncode == 0

    def start(self, uid, token):
        if self.exists(uid):
            _docker("start", self.name(uid))
        else:
            _docker(*self.run_args(uid, token))
        return self.address(uid)

    def address(self, uid):
        n = self.name(uid)
        if DOCKER_NETWORK:
            return f"http://{n}:{JUPYTER_PORT_IN_CONTAINER}"
        p = _docker("port", n, f"{JUPYTER_PORT_IN_CONTAINER}/tcp")
        hostport = p.stdout.strip().splitlines()[0]  # e.g. 127.0.0.1:49153
        return f"http://127.0.0.1:{hostport.rsplit(':', 1)[1]}"

    def stop(self, uid):
        _docker("stop", "-t", "5", self.name(uid), check=False)

    def count_running(self):
        p = _docker("ps", "-q", "--filter", f"label={self.LABEL}", check=False)
        return len(p.stdout.split())

    def adopt_existing(self):
        """uids of containers already running (e.g. after the app restarted)."""
        p = _docker("ps", "--filter", f"label={self.LABEL}", "--format", f'{{{{.Label "{self.LABEL}"}}}}', check=False)
        return [int(x) for x in p.stdout.split() if x.isdigit()]

    def stop_all(self):
        pass  # containers keep running across app restarts; the idle reaper stops them


# ----------------------------------------------------------------------------
# Manager: on-demand start, readiness, idle reaping
# ----------------------------------------------------------------------------
class ContainerManager:
    def __init__(self, spawner, idle_minutes=IDLE_MINUTES, max_running=MAX_CONTAINERS):
        self.spawner = spawner
        self.idle_seconds = idle_minutes * 60
        self.max_running = max_running
        self._locks = {}
        self._glock = threading.Lock()
        self._base = {}        # uid -> upstream base url
        self._verified = {}    # uid -> last time we confirmed it is running
        self._seen = {}        # uid -> last activity
        self._ws = {}          # uid -> open websocket count
        self._stop = threading.Event()

    def _lock(self, uid):
        with self._glock:
            return self._locks.setdefault(uid, threading.Lock())

    def touch(self, uid):
        self._seen[uid] = time.time()

    def ws_opened(self, uid):
        with self._glock:
            self._ws[uid] = self._ws.get(uid, 0) + 1
        self.touch(uid)

    def ws_closed(self, uid):
        with self._glock:
            self._ws[uid] = max(0, self._ws.get(uid, 1) - 1)
        self.touch(uid)

    def ensure(self, uid, token):
        """Returns the upstream base URL of the user's Jupyter, starting it if needed."""
        with self._lock(uid):
            self.touch(uid)
            if uid in self._base and time.time() - self._verified.get(uid, 0) < 15:
                return self._base[uid]
            if uid in self._base and self.spawner.running(uid):
                self._base[uid] = self.spawner.address(uid)
                self._verified[uid] = time.time()
                return self._base[uid]
            if self.spawner.count_running() >= self.max_running and not self.spawner.running(uid):
                raise SandboxError("The tutor is at capacity right now. Please try again in a few minutes.")
            base = self.spawner.start(uid, token)
            self._wait_ready(uid, base, token)
            self._base[uid] = base
            self._verified[uid] = time.time()
            return base

    def _wait_ready(self, uid, base, token, timeout=90):
        deadline = time.time() + timeout
        with http_client(5) as c:
            while time.time() < deadline:
                try:
                    r = c.get(f"{base}{BASE_PATH}api/status", headers={"Authorization": f"token {token}"})
                    if r.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                if not self.spawner.running(uid):
                    raise SandboxError("Your notebook environment failed to start.")
                time.sleep(0.5)
        raise SandboxError("Your notebook environment did not start in time. Please try again.")

    # --- idle reaper ---------------------------------------------------------------
    def start_reaper(self, interval=60):
        for uid in self.spawner.adopt_existing():
            self._seen[uid] = time.time()
        threading.Thread(target=self._reap_loop, args=(interval,), daemon=True).start()

    def _reap_loop(self, interval):
        while not self._stop.wait(interval):
            self.reap_idle()

    def reap_idle(self):
        now = time.time()
        for uid, seen in list(self._seen.items()):
            if now - seen > self.idle_seconds and self._ws.get(uid, 0) == 0:
                with self._lock(uid):
                    if now - self._seen.get(uid, 0) > self.idle_seconds:
                        self.spawner.stop(uid)
                        self._base.pop(uid, None)
                        self._seen.pop(uid, None)

    def shutdown(self):
        self._stop.set()
        self.spawner.stop_all()


def make_manager():
    spawner = DockerSpawner() if ISOLATION == "docker" else ProcessSpawner()
    return ContainerManager(spawner)


def check_docker_ready():
    """Fail fast at startup if docker isolation is requested but not usable."""
    _docker("info", timeout=20)
    if _docker("image", "inspect", DOCKER_IMAGE, check=False).returncode != 0:
        raise SandboxError(f"Docker image '{DOCKER_IMAGE}' not found. Build it with: docker build -t {DOCKER_IMAGE} -f docker/Dockerfile.jupyter .")


# ----------------------------------------------------------------------------
# Notebooks through the user's Jupyter REST API
# ----------------------------------------------------------------------------
def put_notebook(base, token, name, nb_dict):
    with http_client() as c:
        r = c.put(f"{base}{BASE_PATH}api/contents/{name}", headers={"Authorization": f"token {token}"},
                  json={"type": "notebook", "format": "json", "content": nb_dict})
    if r.status_code not in (200, 201):
        raise SandboxError(f"Could not create the notebook ({r.status_code}).")


def get_notebook_cells(base, token, name):
    with http_client() as c:
        r = c.get(f"{base}{BASE_PATH}api/contents/{name}", params={"content": 1}, headers={"Authorization": f"token {token}"})
    if r.status_code != 200:
        raise SandboxError(f"Could not read the notebook ({r.status_code}).")
    cells = []
    for cell in r.json()["content"]["cells"]:
        if cell["cell_type"] == "code":
            src = cell["source"] if isinstance(cell["source"], str) else "".join(cell["source"])
            cells.append("\n".join(l for l in src.splitlines() if not l.lstrip().startswith(("%", "!"))))
    return cells


# ----------------------------------------------------------------------------
# Running tests
# ----------------------------------------------------------------------------
TEST_TIMEOUT = 40


def docker_sandbox_cmd(name):
    """Throwaway container: no network, read-only fs, no capabilities, tight CPU/memory/pids, no secrets."""
    return [
        "docker", "run", "--rm", "-i", "--init", "--name", name,
        "--network", "none", "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--pids-limit", "128", "--memory", "256m", "--memory-swap", "256m", "--cpus", "1",
        "--user", "10001:10001", "-e", "PYTHONDONTWRITEBYTECODE=1",
        DOCKER_IMAGE, "python", "-I", "/opt/tutor/runner.py", "-",
    ]


def _parse(p):
    for line in reversed(p.stdout.splitlines()):
        if line.startswith("RESULT:"):
            return json.loads(line[len("RESULT:"):])
    return {"load_error": (p.stderr or "Runner crashed")[-1500:], "cell_errors": [], "results": []}


def run_tests(code, tests, use_docker):
    """code: one source string or a list of cell sources (each run separately)."""
    cells = [code] if isinstance(code, str) else code
    payload = json.dumps({"cells": cells, "tests": tests})
    timeout_result = {"load_error": f"Your code took too long to run (over {TEST_TIMEOUT}s). Check for infinite loops.",
                      "cell_errors": [], "results": []}
    if use_docker:
        name = f"tutor-run-{uuid.uuid4().hex[:12]}"
        try:
            p = subprocess.run(docker_sandbox_cmd(name), input=payload, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=TEST_TIMEOUT + 10)
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "kill", name], capture_output=True)
            return timeout_result
        except FileNotFoundError:
            raise SandboxError("The `docker` command was not found.")
        return _parse(p)
    with tempfile.TemporaryDirectory() as d:
        pf = Path(d) / "payload.json"
        pf.write_text(payload, encoding="utf-8")
        try:
            p = subprocess.run([sys.executable, "-I", str(ROOT / "runner.py"), str(pf)], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=TEST_TIMEOUT, stdin=subprocess.DEVNULL, cwd=d,
                               env=scrubbed_env())
        except subprocess.TimeoutExpired:
            return timeout_result
    return _parse(p)
