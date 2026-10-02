@echo off
rem Starts NextPull (no console window). Extra arguments are passed on, e.g.:  Launch-NextPull.bat --background
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
  echo Python 3 was not found. Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
  pause
  exit /b 1
)

py -3 -c "import webview, pystray, PIL" >nul 2>nul
if errorlevel 1 (
  echo Installing the required Python packages ^(first run only^)...
  py -3 -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Installing the packages failed. Check your internet connection and try again.
    pause
    exit /b 1
  )
)

if not exist "tools\rclone\rclone.exe" (
  where rclone >nul 2>nul
  if errorlevel 1 (
    echo NOTE: rclone.exe was not found. Run  powershell -ExecutionPolicy Bypass -File tools\Fetch-rclone.ps1  or copy rclone.exe to tools\rclone\
    echo       NextPull starts anyway, but downloads need rclone.
    timeout /t 6 >nul
  )
)

where pyw >nul 2>nul
if errorlevel 1 (
  start "" py -3 "%~dp0nextpull.py" %*
) else (
  start "" pyw -3 "%~dp0nextpull.py" %*
)
endlocal
