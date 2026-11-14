# One-command setup for the Roblox sniper bot (Windows PowerShell).
#
#   .\setup.ps1            install, configure, verify, start
#   .\setup.ps1 -NoRun     install and configure only
#
# Safe to re-run: an existing .env is never overwritten.
# If PowerShell blocks the script:  powershell -ExecutionPolicy Bypass -File .\setup.ps1

param([switch]$NoRun)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# ------------------------------------------------------------------- python
$PythonCmd = $null
foreach ($candidate in @("python", "python3", "py")) {
    try {
        $null = & $candidate --version 2>&1
        $PythonCmd = $candidate
        break
    } catch { }
}
if (-not $PythonCmd) {
    Write-Host "X Python 3 not found. Install it from https://python.org (3.10+), then re-run." -ForegroundColor Red
    exit 1
}
$version = (& $PythonCmd --version 2>&1) -join " "
Write-Host "OK $version" -ForegroundColor Green

# --------------------------------------------------------------- virtualenv
if (-not (Test-Path ".venv")) {
    Write-Host "-> Creating .venv"
    & $PythonCmd -m venv .venv
}
$PyBin = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $PyBin)) {
    Write-Host "X Could not create a virtualenv at .venv" -ForegroundColor Red
    exit 1
}

Write-Host "-> Installing dependencies"
& $PyBin -m pip install --quiet --upgrade pip
& $PyBin -m pip install --quiet -r requirements.txt
Write-Host "OK Dependencies installed" -ForegroundColor Green

# --------------------------------------------------------------------- .env
if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env" }

function Read-Secret([string]$Prompt) {
    $secure = Read-Host -AsSecureString $Prompt
    if (-not $secure) { return "" }
    return [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    ).Trim()
}

function Get-EnvValue([string[]]$Lines, [string]$Key) {
    foreach ($line in $Lines) {
        if ($line -match "^$Key=(.*)$") { return $Matches[1].Trim() }
    }
    return ""
}

# Rebuilt line-by-line rather than with regex replacement: tokens and cookies can
# contain characters that -replace would treat as substitutions.
function Set-EnvValue([string[]]$Lines, [string]$Key, [string]$Value) {
    $found = $false
    for ($i = 0; $i -lt $Lines.Count; $i++) {
        if ($Lines[$i] -match "^$Key=") { $Lines[$i] = "$Key=$Value"; $found = $true }
    }
    if (-not $found) { $Lines = $Lines + "$Key=$Value" }
    return $Lines
}

$lines = @(Get-Content ".env")
$changed = @()

if ((Get-EnvValue $lines "DISCORD_TOKEN") -eq "" -and (Get-EnvValue $lines "TOKEN") -eq "") {
    $token = Read-Secret "Paste your Discord bot token (input hidden)"
    if ($token) {
        $lines = Set-EnvValue $lines "DISCORD_TOKEN" $token
        $changed += "DISCORD_TOKEN"
    } else {
        Write-Host "! No token entered - set DISCORD_TOKEN in .env later" -ForegroundColor Yellow
    }
}

if ((Get-EnvValue $lines "ROBLOX_COOKIE") -eq "") {
    $cookie = Read-Secret "Paste the .ROBLOSECURITY value of the throwaway account (hidden)"
    if ($cookie) {
        $lines = Set-EnvValue $lines "ROBLOX_COOKIE" $cookie
        $changed += "ROBLOX_COOKIE"
    } else {
        Write-Host "! No cookie entered - /snipe doctor will report a dead cookie until you add one" -ForegroundColor Yellow
    }
}

if ((Get-EnvValue $lines "GUILD_ID") -eq "") {
    $guild = (Read-Host "Your Discord server id (optional, Enter to skip)").Trim()
    if ($guild -match '^\d+$') {
        $lines = Set-EnvValue $lines "GUILD_ID" $guild
        $changed += "GUILD_ID"
    }
}

Set-Content -Path ".env" -Value $lines
if ($changed.Count -gt 0) {
    Write-Host ("OK .env updated (" + ($changed -join ", ") + ")") -ForegroundColor Green
} else {
    Write-Host "OK .env unchanged" -ForegroundColor Green
}

# ------------------------------------------------------------------- verify
Write-Host "-> Running offline self-test"
& $PyBin selftest.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "X Self-test failed - fix the above before running the bot." -ForegroundColor Red
    exit 1
}

# -------------------------------------------------------------------- start
if ($NoRun) {
    Write-Host ""
    Write-Host "Setup complete. Start with:  .\.venv\Scripts\python.exe bot.py"
    exit 0
}

Write-Host ""
Write-Host "-> Starting the bot (Ctrl-C to stop)"
& $PyBin bot.py
