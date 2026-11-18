# Interactive setup for Windows: creates .venv, installs dependencies, asks which LLM to use.
# Writes the choice (and API key, if any) to .env, which launch.ps1 reads.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Fail($msg) { Write-Host ""; Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

# ---- Find Python 3.9+ ----------------------------------------------------------
$pyExe = $null; $pyArgs = @()
foreach ($cand in @(@("py", "-3"), @("python"), @("python3"))) {
    $cmd = Get-Command $cand[0] -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    $a = @(); if ($cand.Length -gt 1) { $a = $cand[1..($cand.Length - 1)] }
    try {
        & $cmd.Source @a -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) { $pyExe = $cmd.Source; $pyArgs = $a; break }
    } catch { }
}
if (-not $pyExe) {
    Fail "Python 3.9 or newer was not found. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH' on the first screen), then run setup again."
}

Write-Host "=== Python Coding Tutor setup ==="
Write-Host ""
if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment..."
    & $pyExe @pyArgs -m venv .venv
    if ($LASTEXITCODE -ne 0) { Fail "Could not create the virtual environment." }
}
$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
Write-Host "Installing dependencies (JupyterLab is large; this can take a few minutes)..."
& $venvPy -m pip install -q --upgrade pip
& $venvPy -m pip install -q -r requirements.txt
if ($LASTEXITCODE -ne 0) { Fail "Installing dependencies failed. Check your internet connection and run setup again." }
& $venvPy -m ipykernel install --sys-prefix --name python3 | Out-Null
New-Item -ItemType Directory -Force -Path workspace | Out-Null
Write-Host "Dependencies installed."
Write-Host ""

# ---- Remember previous answers ---------------------------------------------------
$old = @{}
if (Test-Path ".env") {
    foreach ($line in Get-Content ".env" -Encoding UTF8) {
        if ($line -match "^([A-Z_]+)=(.*)$") { $old[$Matches[1]] = $Matches[2].Trim("'") }
    }
    if ($old["TUTOR_LLM"]) { Write-Host "Current LLM setting: $($old['TUTOR_LLM'])" }
}

function Open-Url($url) { try { Start-Process $url } catch { } }

function Read-Secret($prompt) {
    $sec = Read-Host -Prompt $prompt -AsSecureString
    return [System.Net.NetworkCredential]::new("", $sec).Password.Trim()
}

function Ask-Key($label, $existing, $prefix) {
    if ($existing) {
        $keep = Read-Host "$label key already saved. Keep it? [Y/n]"
        if ($keep -notmatch "^[nN]") { return $existing }
    }
    while ($true) {
        $key = Read-Secret "Paste your $label API key (input hidden)"
        if (-not $key) { continue }
        if (-not $key.StartsWith($prefix)) {
            Write-Host "That doesn't look like an $label key (expected it to start with `"$prefix`")."
            $yn = Read-Host "Use it anyway? [y/N]"
            if ($yn -notmatch "^[yY]") { continue }
        }
        return $key
    }
}

$llm = ""; $anthropicKey = ""; $openaiKey = ""
while (-not $llm) {
    Write-Host "Which LLM should the tutor use?"
    Write-Host "  1) Anthropic  (API key)"
    Write-Host "  2) OpenAI     (API key)"
    Write-Host "  3) claude command line (uses your Claude Code login, no key needed)"
    $choice = Read-Host "Choose [1-3]"
    switch ($choice) {
        "1" {
            $llm = "anthropic"
            if (-not $old["ANTHROPIC_API_KEY"]) {
                Write-Host ""
                Write-Host "--- How to get an Anthropic API key ---"
                Write-Host "  1. Go to https://console.anthropic.com and sign in (or create an account)."
                Write-Host "  2. Add credits under Settings > Billing (pay-as-you-go; the tutor uses a few small requests per problem)."
                Write-Host "  3. Open Settings > API Keys and click 'Create Key'. Name it e.g. 'coding-tutor'."
                Write-Host "  4. Copy the key right away (it starts with 'sk-ant-'); it is only shown once."
                $o = Read-Host "Open the console in your browser now? [Y/n]"
                if ($o -notmatch "^[nN]") { Open-Url "https://console.anthropic.com/settings/keys" }
                Write-Host ""
            }
            $anthropicKey = Ask-Key "Anthropic" $old["ANTHROPIC_API_KEY"] "sk-ant-"
        }
        "2" {
            $llm = "openai"
            if (-not $old["OPENAI_API_KEY"]) {
                Write-Host ""
                Write-Host "--- How to get an OpenAI API key ---"
                Write-Host "  1. Go to https://platform.openai.com and sign in (or create an account)."
                Write-Host "     Note: a ChatGPT subscription does not include API access."
                Write-Host "  2. Add a payment method / credits under Settings > Billing."
                Write-Host "  3. Open https://platform.openai.com/api-keys and click 'Create new secret key'."
                Write-Host "  4. Copy the key right away (it starts with 'sk-'); it is only shown once."
                $o = Read-Host "Open the API keys page in your browser now? [Y/n]"
                if ($o -notmatch "^[nN]") { Open-Url "https://platform.openai.com/api-keys" }
                Write-Host ""
            }
            $openaiKey = Ask-Key "OpenAI" $old["OPENAI_API_KEY"] "sk-"
            Write-Host "Installing the openai package..."
            & $venvPy -m pip install -q openai
        }
        "3" {
            if (Get-Command claude -ErrorAction SilentlyContinue) { $llm = "claude-cli" }
            else {
                Write-Host "The 'claude' command was not found."
                Write-Host ""
                Write-Host "--- How to set up the claude command ---"
                Write-Host "  1. Install Claude Code: see https://docs.claude.com/en/docs/claude-code/setup"
                Write-Host "  2. Open a NEW terminal, run 'claude' once and sign in when prompted."
                Write-Host "  3. Confirm it works:  claude -p `"say hi`""
                Write-Host "  Then run setup again and choose this option."
                Read-Host "Press Enter to go back to the menu"
                Write-Host ""
            }
        }
        default { Write-Host "Please enter 1, 2 or 3."; Write-Host "" }
    }
}

$model = Read-Host "Model override (press Enter for the default)"

$lines = @("TUTOR_LLM=$llm")
if ($anthropicKey) { $lines += "ANTHROPIC_API_KEY='$anthropicKey'" }
if ($openaiKey)    { $lines += "OPENAI_API_KEY='$openaiKey'" }
if ($model)        { $lines += "TUTOR_MODEL='$model'" }
[System.IO.File]::WriteAllText((Join-Path $PSScriptRoot ".env"), (($lines -join "`n") + "`n"), (New-Object System.Text.UTF8Encoding $false))

Write-Host ""
Write-Host "Saved to .env. LLM: $llm"
Write-Host "Setup complete. Start the tutor by double-clicking launch.bat"
