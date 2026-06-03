# ────────────────────────────────────────────────────────────────────
#  repryntt.ps1 — PowerShell shim so you don't have to activate the venv.
#
#  Usage from the repo root:
#      .\repryntt.ps1 start
#      .\repryntt.ps1 status
#      .\repryntt.ps1 doctor
#
#  Finds the venv automatically; falls back to system `repryntt` on PATH.
#
#  If you hit "running scripts is disabled on this system", run once:
#      Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
# ────────────────────────────────────────────────────────────────────

$ErrorActionPreference = "Stop"

# Enable Python UTF-8 mode (PEP 540). On Windows, the default file
# encoding is cp1252, which crashes when reading/writing emoji, ≥, ≤,
# em-dashes, and other common Unicode the agent uses. UTF-8 mode makes
# every open() default to encoding="utf-8" without us having to annotate
# every open() call in the codebase.
$env:PYTHONUTF8 = "1"

$VenvRepryntt = Join-Path $PSScriptRoot ".venv\Scripts\repryntt.exe"

if (Test-Path $VenvRepryntt) {
    & $VenvRepryntt @args
    exit $LASTEXITCODE
}

# Fallback to a global install on PATH
$global = Get-Command repryntt -ErrorAction SilentlyContinue
if ($global) {
    & repryntt @args
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "[repryntt] No installation found." -ForegroundColor Yellow
Write-Host ""
Write-Host "Expected one of:" -ForegroundColor Yellow
Write-Host "    $VenvRepryntt"
Write-Host "    repryntt on system PATH"
Write-Host ""
Write-Host "Run install.py first:" -ForegroundColor Yellow
Write-Host "    python install.py"
Write-Host ""
exit 1
