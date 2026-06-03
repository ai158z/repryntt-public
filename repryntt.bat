@echo off
REM ────────────────────────────────────────────────────────────────────
REM  repryntt.bat — Windows shim so you don't have to activate the venv.
REM
REM  Usage from the repo root:
REM      .\repryntt start
REM      .\repryntt status
REM      .\repryntt doctor
REM
REM  Finds the venv automatically; falls back to system `repryntt` on PATH.
REM ────────────────────────────────────────────────────────────────────
setlocal

REM Enable Python UTF-8 mode (PEP 540). On Windows, the default file
REM encoding is cp1252, which crashes when reading/writing emoji, ≥, ≤,
REM em-dashes, and other common Unicode the agent uses. UTF-8 mode makes
REM every open() default to encoding="utf-8" without us having to
REM annotate every open() call in the codebase.
set PYTHONUTF8=1

REM Look for the venv next to this file
set "VENV_REPRYNTT=%~dp0.venv\Scripts\repryntt.exe"

if exist "%VENV_REPRYNTT%" (
    "%VENV_REPRYNTT%" %*
    exit /b %ERRORLEVEL%
)

REM Fallback: maybe they installed globally (pip install --user, etc.)
where repryntt >nul 2>&1
if %ERRORLEVEL%==0 (
    repryntt %*
    exit /b %ERRORLEVEL%
)

echo.
echo [repryntt] No installation found.
echo.
echo Expected one of:
echo    %VENV_REPRYNTT%
echo    repryntt on system PATH
echo.
echo Run install.py first:
echo    python install.py
echo.
exit /b 1
