@echo off
title School Schedule - Launcher
cd /d "%~dp0"

echo ==============================
echo   School Schedule
echo ==============================
echo.

if not exist "python\python.exe" (
    echo ERROR: portable Python not found in "python" folder.
    echo Make sure python.exe is at School_Schedule\python\
    pause
    exit /b 1
)

if not exist "data\app.py" (
    echo ERROR: file data\app.py not found.
    pause
    exit /b 1
)

echo Starting server in a new window...
start "School Schedule Server" cmd /k "cd /d "%~dp0" && set PATH=%CD%\python;%CD%\python\Scripts;%PATH% && python data\app.py"

echo Waiting for server to start...
python\python.exe data\chekaty_server.py
if errorlevel 1 goto NE_DOCHEKALYS

echo Opening browser...
start "" "http://127.0.0.1:5000"

echo.
echo Done. Server is running in "School Schedule Server" window.
echo This window will close automatically.
echo.
timeout /t 4 /nobreak >nul
exit

:NE_DOCHEKALYS
echo.
echo ERROR: server did not answer on http://127.0.0.1:5000
echo Look at the "School Schedule Server" window - the reason is printed there.
echo Most common: port 5000 is busy, or antivirus blocked python.exe
echo.
pause
exit /b 1
