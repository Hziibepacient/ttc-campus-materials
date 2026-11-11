# EventLens installer for Windows (PowerShell). Safe to run again.
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User") + ";" +
                (Join-Path $env:USERPROFILE ".local\bin")
}
function Have($name) { [bool](Get-Command $name -ErrorAction SilentlyContinue) }

Refresh-Path
if (-not (Have "uv")) {
    Write-Host "[1/5] Installing uv (Python manager)..."
    winget install --id astral-sh.uv -e --accept-source-agreements --accept-package-agreements
    Refresh-Path
} else { Write-Host "[1/5] uv already installed" }

if (-not (Have "ffmpeg") -or -not (Have "ffprobe")) {
    Write-Host "[2/5] Installing FFmpeg..."
    winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
    Refresh-Path
} else { Write-Host "[2/5] FFmpeg already installed" }

$wheel = Get-ChildItem -Path $here -Filter "eventlens-*.whl" | Select-Object -First 1
if (-not $wheel) { throw "eventlens-*.whl not found next to install.ps1" }
Write-Host "[3/5] Installing EventLens from $($wheel.Name)..."
uv tool install --python 3.12 --force $wheel.FullName
uv tool update-shell | Out-Null
Refresh-Path

Write-Host "[4/5] Downloading the detection model (YOLOX-S, ~36 MB, sha256 verified)..."
eventlens models fetch

Write-Host "[5/5] Installing the AI-assistant skill for Codex and Claude Code..."
eventlens skill install --force

Write-Host ""
eventlens doctor
Write-Host ""
Write-Host "Done. Open a NEW terminal / restart Codex or Claude Code so it picks up the skill."
