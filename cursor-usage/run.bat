@echo off
setlocal
cd /d "%~dp0"
if exist "%LocalAppData%\Programs\Python\Python312\pythonw.exe" (
  start "Cursor Usage" "%LocalAppData%\Programs\Python\Python312\pythonw.exe" "%~dp0app.py" >nul 2>&1
  exit /b
)
where pyw >nul 2>&1
if not errorlevel 1 (
  start "Cursor Usage" pyw -3.12 "%~dp0app.py" >nul 2>&1
  exit /b
)
start "Cursor Usage" pythonw "%~dp0app.py" >nul 2>&1
