# OpenRouter support (hidden for now)

OpenRouter is **not offered** in the setup scripts, the UI or the user-facing docs; LLM Gateway is the default.
The code is kept so it can be switched back on. Nothing here is linked from the README.

## What still works
- `TUTOR_LLM=openrouter` (or just `OPENROUTER_API_KEY` with nothing else configured) still selects the OpenRouter backend,
  using `OPENROUTER_API_KEY`, `TUTOR_MODEL` (default `anthropic/claude-sonnet-4.5`) and optional `OPENROUTER_BASE_URL`.
  `launch.sh` / `launch.ps1` accept it, so existing configs keep working.
- `python openrouter_oauth.py` (OAuth PKCE, no copy/paste) still saves a key to `.env`; see its `--help`.

## Bring-your-own OpenRouter in multi-user mode (off by default)
Set `TUTOR_ENABLE_OPENROUTER=1` on the server to enable `byo.py`: a one-click "Connect with OpenRouter" flow where each
signed-in user's key is obtained via OAuth PKCE, stored encrypted (Fernet, key derived from `TUTOR_SECRET_KEY`) and used only
for that user. Related settings: `TUTOR_REQUIRE_BYO_LLM=1` (users must connect; no shared AI needed),
`OPENROUTER_AUTH_URL` / `OPENROUTER_BASE_URL` (testing/proxies). The callback is `<TUTOR_BASE_URL>/openrouter/callback`;
OpenRouter requires https (or `http://localhost:3000` in development). With the flag off, none of its routes or UI exist
(`/api/me` reports `features.byo_llm = false`).

## LLM Gateway has no equivalent yet
LLM Gateway's OAuth logins (email, GitHub, Google) are for dashboard accounts; no OAuth flow that issues an API key was
found, so a one-click "bring your own LLM Gateway account" is not possible today. A paste-your-key version would be.
