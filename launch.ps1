# Starts the tutor (and its JupyterLab) on Windows and opens the browser.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Fail($msg) { Write-Host ""; Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { Fail "Run setup.bat first." }

# Settings saved by setup (LLM choice, API keys). Variables already in your environment win.
if (Test-Path ".env") {
    foreach ($line in Get-Content ".env" -Encoding UTF8) {
        if ($line -match "^([A-Z_]+)=(.*)$") {
            $name = $Matches[1]; $val = $Matches[2].Trim("'")
            if (-not [Environment]::GetEnvironmentVariable($name)) { [Environment]::SetEnvironmentVariable($name, $val, "Process") }
        }
    }
}

$llm = $env:TUTOR_LLM
switch ($llm) {
    "anthropic"  { if (-not $env:ANTHROPIC_API_KEY) { Fail "ANTHROPIC_API_KEY missing. Run setup.bat" } }
    "openai"     { if (-not $env:OPENAI_API_KEY) { Fail "OPENAI_API_KEY missing. Run setup.bat" } }
    "llmgateway" { if (-not $env:LLMGATEWAY_API_KEY) { Fail "LLMGATEWAY_API_KEY missing. Run setup.bat" } }
    "openrouter" { if (-not $env:OPENROUTER_API_KEY) { Fail "OPENROUTER_API_KEY missing. Run setup.bat" } }
    "claude-cli" { if (-not (Get-Command claude -ErrorAction SilentlyContinue)) { Fail "'claude' command not found. Run setup.bat" } }
    default {
        if (-not ($env:LLMGATEWAY_API_KEY -or $env:ANTHROPIC_API_KEY -or $env:OPENAI_API_KEY -or $env:OPENROUTER_API_KEY -or (Get-Command claude -ErrorAction SilentlyContinue))) {
            Fail "No LLM configured. Run setup.bat"
        }
    }
}
if ($llm) { Write-Host "LLM: $llm" } else { Write-Host "LLM: auto-detect" }

if (-not $env:TUTOR_PORT) { $env:TUTOR_PORT = "8765" }
if (-not $env:TUTOR_JUPYTER_PORT) { $env:TUTOR_JUPYTER_PORT = "8766" }
foreach ($p in @($env:TUTOR_PORT, $env:TUTOR_JUPYTER_PORT)) {
    $busy = Get-NetTCPConnection -LocalPort ([int]$p) -State Listen -ErrorAction SilentlyContinue
    if ($busy) { Fail "Port $p is already in use. Close the other copy of the tutor (or set TUTOR_PORT / TUTOR_JUPYTER_PORT)." }
}

$url = "http://localhost:$($env:TUTOR_PORT)"
# Open the browser once the server answers.
Start-Job -ArgumentList $url -ScriptBlock {
    param($u)
    for ($i = 0; $i -lt 60; $i++) {
        try { Invoke-WebRequest -UseBasicParsing "$u/api/state" | Out-Null; Start-Process $u; break }
        catch { Start-Sleep -Seconds 1 }
    }
} | Out-Null

Write-Host ""
Write-Host "Tutor starting at $url  (press Ctrl+C in this window to stop it)"
& $venvPy app.py
