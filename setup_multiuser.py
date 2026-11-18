#!/usr/bin/env python3
"""Interactive setup for multi-user mode (OAuth sign-in). Writes .env.multiuser.

Walks you through registering the app with Google, GitHub and/or Microsoft, shows the exact redirect URI to
enter in each console, and collects the credentials. Standard library only.

    python3 setup_multiuser.py
"""
import getpass
import secrets
import sys
import urllib.parse
import webbrowser
from pathlib import Path

ROOT = Path(__file__).parent
ENV_FILE = ROOT / ".env.multiuser"


def load_existing():
    vals = {}
    for path in (ENV_FILE, ROOT / ".env"):
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.split("=", 1)
                    vals.setdefault(k.strip(), v.strip().strip("'"))
    return vals


def ask(prompt, default=None, secret=False, check=None):
    while True:
        suffix = f" [{default}]" if default else ""
        raw = (getpass.getpass if secret else input)(f"{prompt}{suffix}: ").strip()
        value = raw or default or ""
        if not value:
            print("  A value is required.")
            continue
        if check and (msg := check(value)):
            print(f"  {msg}")
            continue
        return value


def yes(prompt, default=False):
    d = "Y/n" if default else "y/N"
    raw = input(f"{prompt} [{d}] ").strip().lower()
    return default if not raw else raw.startswith("y")


def open_url(url):
    if yes("  Open it in your browser now?", True):
        try:
            webbrowser.open(url)
        except Exception:
            pass


def valid_base_url(v):
    p = urllib.parse.urlparse(v)
    if p.scheme not in ("http", "https") or not p.netloc or p.path.strip("/") or p.query:
        return "Enter just the address, like https://tutor.example.com (no path)."
    if p.scheme == "http" and p.hostname not in ("localhost", "127.0.0.1"):
        return "Only https is allowed for a public address (http is OK for localhost)."
    return None


def q(v):
    return "'" + v.replace("'", "'\\''") + "'"


def section(title):
    print(f"\n=== {title} " + "=" * max(0, 60 - len(title)))


def main():
    existing = load_existing()
    vals = {}
    section("Multi-user setup")
    print("Lets people sign in with Google / GitHub / Microsoft. Each person gets their own progress and their own\n"
          "notebook environment. Run this again any time to change a setting; press Enter to keep a shown default.")

    # 1. public address ------------------------------------------------------------------
    section("1. Where will the tutor live?")
    print("The public https address people will open, e.g. https://tutor.example.com.\n"
          "To just try it on this computer, use http://localhost:8765.")
    base = ask("Public address", existing.get("TUTOR_BASE_URL", "http://localhost:8765"), check=valid_base_url).rstrip("/")
    vals["TUTOR_BASE_URL"] = base
    host = urllib.parse.urlparse(base).hostname
    if base.startswith("https://"):
        vals["TUTOR_DOMAIN"] = host  # used by the Caddy HTTPS proxy in docker-compose.yml
    else:
        vals["TUTOR_PORT"] = str(urllib.parse.urlparse(base).port or 8765)

    # 2. providers ---------------------------------------------------------------------------
    section("2. Sign-in providers")
    print("Enable at least one. For each, you create an 'OAuth app' in the provider's console and paste the\n"
          "Client ID and Client Secret here. The exact redirect (callback) URL to enter is shown for you.")
    enabled = []

    if yes("\nEnable Google?", True):
        redirect = f"{base}/auth/callback/google"
        print(f"""
  --- Google ---
  1. Open https://console.cloud.google.com/apis/credentials (create/select a project).
  2. First time only: 'OAuth consent screen' -> choose External (Internal if everyone is in your Google
     Workspace) -> app name, support email -> scopes: openid, email, profile -> save.
     While the app is in 'Testing', only listed test users can sign in. Click 'Publish app' for everyone.
  3. Credentials -> Create credentials -> OAuth client ID -> Application type: Web application.
  4. Authorized redirect URIs -> Add URI:   {redirect}
  5. Create, then copy the Client ID and Client secret.""")
        open_url("https://console.cloud.google.com/apis/credentials")
        vals["GOOGLE_CLIENT_ID"] = ask("  Google Client ID", existing.get("GOOGLE_CLIENT_ID"))
        vals["GOOGLE_CLIENT_SECRET"] = ask("  Google Client secret (hidden)", existing.get("GOOGLE_CLIENT_SECRET"), secret=True)
        enabled.append("google")

    if yes("\nEnable GitHub?", True):
        redirect = f"{base}/auth/callback/github"
        print(f"""
  --- GitHub ---
  1. Open https://github.com/settings/developers -> OAuth Apps -> New OAuth App
     (for an organization: the organization's Settings -> Developer settings -> OAuth Apps).
  2. Application name: anything.   Homepage URL: {base}
  3. Authorization callback URL:   {redirect}
  4. Register application, then 'Generate a new client secret'. Copy the Client ID and the secret.
     (GitHub apps allow only ONE callback URL: use a separate OAuth App for localhost testing.)""")
        open_url("https://github.com/settings/applications/new")
        vals["GITHUB_CLIENT_ID"] = ask("  GitHub Client ID", existing.get("GITHUB_CLIENT_ID"))
        vals["GITHUB_CLIENT_SECRET"] = ask("  GitHub Client secret (hidden)", existing.get("GITHUB_CLIENT_SECRET"), secret=True)
        enabled.append("github")

    if yes("\nEnable Microsoft?", False):
        redirect = f"{base}/auth/callback/microsoft"
        print(f"""
  --- Microsoft (Entra ID / Azure AD) ---
  1. Open https://portal.azure.com -> Microsoft Entra ID -> App registrations -> New registration.
  2. Supported account types:
       * only your organization  -> 'Single tenant' (recommended for company use)
       * anyone / personal accounts -> multi-tenant (then you cannot use the allow-list below)
  3. Redirect URI: platform 'Web', value:   {redirect}
  4. Register. On the Overview page copy 'Application (client) ID' and 'Directory (tenant) ID'.
  5. Certificates & secrets -> New client secret -> copy the secret VALUE (not the 'Secret ID').""")
        open_url("https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade")
        vals["MICROSOFT_CLIENT_ID"] = ask("  Microsoft Application (client) ID", existing.get("MICROSOFT_CLIENT_ID"))
        vals["MICROSOFT_CLIENT_SECRET"] = ask("  Microsoft client secret value (hidden)", existing.get("MICROSOFT_CLIENT_SECRET"), secret=True)
        print("  Tenant: paste your Directory (tenant) ID for a single-tenant app, or 'common' for multi-tenant.")
        vals["MICROSOFT_TENANT"] = ask("  Microsoft tenant", existing.get("MICROSOFT_TENANT", "common"))
        enabled.append("microsoft")

    if not enabled:
        sys.exit("\nNo provider enabled; nothing to sign in with. Run the script again.")

    # 3. access control ------------------------------------------------------------------------
    section("3. Who may sign in?")
    print("By default anyone with an account at an enabled provider can sign in.\n"
          "To restrict access, list allowed email domains and/or addresses (only VERIFIED emails are accepted).\n"
          "Microsoft users are limited by tenant instead (set a single tenant above).")
    if yes("Restrict sign-in?", bool(existing.get("TUTOR_ALLOWED_DOMAINS") or existing.get("TUTOR_ALLOWED_EMAILS"))):
        d = input(f"  Allowed domains, comma-separated, e.g. gsk.com [{existing.get('TUTOR_ALLOWED_DOMAINS', '')}]: ").strip() or existing.get("TUTOR_ALLOWED_DOMAINS", "")
        e = input(f"  Extra allowed emails, comma-separated [{existing.get('TUTOR_ALLOWED_EMAILS', '')}]: ").strip() or existing.get("TUTOR_ALLOWED_EMAILS", "")
        if d:
            vals["TUTOR_ALLOWED_DOMAINS"] = d
        if e:
            vals["TUTOR_ALLOWED_EMAILS"] = e

    # 4. LLM -----------------------------------------------------------------------------------------
    section("4. The AI behind the tutor")
    print("One shared key is used for everyone, so each user is rate-limited (below).\n"
          "(The 'claude' command can't be used here: it belongs to one person's login.)")
    print("  1) Anthropic   2) OpenAI   3) OpenRouter")
    default_choice = {"anthropic": "1", "openai": "2", "openrouter": "3"}.get(existing.get("TUTOR_LLM", ""), "1")
    choice = ask("Choose 1-3", default_choice, check=lambda v: None if v in ("1", "2", "3") else "Enter 1, 2 or 3.")
    provider, keyvar, prefix, url = {
        "1": ("anthropic", "ANTHROPIC_API_KEY", "sk-ant-", "https://console.anthropic.com/settings/keys"),
        "2": ("openai", "OPENAI_API_KEY", "sk-", "https://platform.openai.com/api-keys"),
        "3": ("openrouter", "OPENROUTER_API_KEY", "sk-or-", "https://openrouter.ai/settings/keys"),
    }[choice]
    print(f"  Create a key at {url}")
    vals["TUTOR_LLM"] = provider
    vals[keyvar] = ask(f"  {provider.title()} API key (hidden)", existing.get(keyvar), secret=True)
    if not vals[keyvar].startswith(prefix):
        print(f"  Note: this doesn't start with '{prefix}'; double-check it.")
    model = input(f"  Model override, optional [{existing.get('TUTOR_MODEL', '')}]: ").strip() or existing.get("TUTOR_MODEL", "")
    if model:
        vals["TUTOR_MODEL"] = model

    # 5. isolation & limits ------------------------------------------------------------------------------
    section("5. Isolation and limits")
    print("Every user can run arbitrary Python. 'docker' gives each user a locked-down container (required for the\n"
          "public internet). 'none' runs everything as the same OS user with NO isolation: trusted people only.")
    iso = ask("Isolation (docker/none)", existing.get("TUTOR_ISOLATION", "docker"), check=lambda v: None if v in ("docker", "none") else "Type docker or none.")
    if iso == "none":
        print("\n  WARNING: with 'none', any user can read other users' files and the server's data.")
        if input("  Type 'yes' to confirm you only have fully trusted users: ").strip().lower() != "yes":
            iso = "docker"
            print("  Using docker.")
    vals["TUTOR_ISOLATION"] = iso
    vals["TUTOR_LLM_CALLS_PER_HOUR"] = ask("AI calls allowed per user per hour", existing.get("TUTOR_LLM_CALLS_PER_HOUR", "120"),
                                            check=lambda v: None if v.isdigit() else "Enter a number.")
    vals["TUTOR_MAX_CONTAINERS"] = ask("Max notebook environments running at once", existing.get("TUTOR_MAX_CONTAINERS", "50"),
                                       check=lambda v: None if v.isdigit() else "Enter a number.")
    vals["TUTOR_IDLE_MINUTES"] = ask("Stop an idle environment after (minutes)", existing.get("TUTOR_IDLE_MINUTES", "30"),
                                     check=lambda v: None if v.replace('.', '', 1).isdigit() else "Enter a number.")

    vals["TUTOR_MODE"] = "multiuser"
    vals["TUTOR_SECRET_KEY"] = existing.get("TUTOR_SECRET_KEY") or secrets.token_urlsafe(48)  # keeps sessions valid across re-runs

    lines = ["# Generated by setup_multiuser.py. Contains secrets: never commit or share this file."]
    lines += [f"{k}={q(v)}" for k, v in vals.items()]
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        ENV_FILE.chmod(0o600)
    except OSError:
        pass

    section("Done")
    print(f"Saved {ENV_FILE.name} (readable only by you). Sign-in providers: {', '.join(enabled)}.")
    print("\nRedirect URIs you must have registered:")
    for p in enabled:
        print(f"  {p:<10} {base}/auth/callback/{p}")
    print("\nNext steps:")
    if iso == "docker":
        print("  Production (Docker + automatic HTTPS):  see docs/MULTIUSER.md")
        print("    docker compose --profile build build jupyter-image   # builds the per-user image (once)")
        print("    DOCKER_GID=$(stat -c %g /var/run/docker.sock) docker compose up -d --build")
        print("  Or run the app directly on this machine (still uses Docker for users):")
        print("    docker build -t coding-tutor-jupyter -f docker/Dockerfile.jupyter .   # once")
    print("    ./launch_multiuser.sh")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        sys.exit("\nCancelled; nothing was saved.")
