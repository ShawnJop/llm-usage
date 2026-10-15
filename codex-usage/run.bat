@echo off
setlocal
cd /d "%~dp0"
if exist "%LocalAppData%\Programs\Python\Python312\pythonw.exe" (
  start "Codex Usage Cat" /b "%LocalAppData%\Programs\Python\Python312\pythonw.exe" "%~dp0app.py" >nul 2>&1
  exit /b
)
where pyw >nul 2>&1
if not errorlevel 1 (
  start "Codex Usage Cat" /b pyw -3.12 "%~dp0app.py" >nul 2>&1
  exit /b
)
start "Codex Usage Cat" /b pythonw "%~dp0app.py" >nul 2>&1
