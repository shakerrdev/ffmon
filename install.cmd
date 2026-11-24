@echo off
setlocal
cd /d "%~dp0"
set PY=python
where py >nul 2>nul && set PY=py -3
%PY% -m pip install --quiet -r requirements.txt || goto :error
echo Hooking the collector into Firefox (Windows will ask for administrator rights)...
powershell -NoProfile -Command "Start-Process cmd -Verb RunAs -Wait -ArgumentList '/c cd /d \"%~dp0\" && %PY% ffmon.py install && pause'"
echo.
echo Close every Firefox window so it restarts with the collector, then press a key to open the dashboard.
pause >nul
call "%~dp0start.cmd"
exit /b 0
:error
echo Installing psutil failed. Is Python 3.9+ installed and on PATH?
pause
exit /b 1
