@echo off
cd /d "%~dp0"
where pyw >nul 2>nul && (start "" pyw -3 ffmon.py run --open & exit /b 0)
start "" pythonw ffmon.py run --open
