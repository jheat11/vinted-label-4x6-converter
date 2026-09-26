@echo off
REM Run from source on Windows (first run sets up a local .venv)
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
if not exist .venv (
  %PY% -m venv .venv || goto :fail
  .venv\Scripts\python -m pip install --upgrade pip -q
  .venv\Scripts\python -m pip install -r requirements.txt -q || goto :fail
)
start "" .venv\Scripts\pythonw VintedLabel4x6.py %*
exit /b 0
:fail
echo Setup failed - is Python 3.9+ installed from python.org?
pause
