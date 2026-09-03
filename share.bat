@echo off
REM Start the Hardware Agent and expose it on the internet via Cloudflare.
REM Two windows open: the app, and the tunnel. The tunnel window prints the
REM public https:// address -- that is the link to share. Both must stay open;
REM closing either takes the link down.
cd /d "%~dp0"

set "CFD=%ProgramFiles(x86)%\cloudflared\cloudflared.exe"
if not exist "%CFD%" set "CFD=%ProgramFiles%\cloudflared\cloudflared.exe"
if not exist "%CFD%" (
  echo cloudflared is not installed. Install it with:
  echo     winget install --id Cloudflare.cloudflared
  pause
  exit /b 1
)

findstr /b /c:"APP_PASSWORD=" .env | findstr /v /b /c:"APP_PASSWORD=$" >nul
if errorlevel 1 (
  echo.
  echo   WARNING: APP_PASSWORD is empty in .env
  echo   The app is about to be reachable from the internet with no sign-in,
  echo   and anyone who finds the link can spend your distributor API quota.
  echo.
  choice /m "Continue anyway"
  if errorlevel 2 exit /b 1
)

start "Hardware Agent" cmd /k python app.py
timeout /t 3 /nobreak >nul
start "Public link (share the https address below)" cmd /k ""%CFD%" tunnel --url http://127.0.0.1:8080 --no-autoupdate"
echo.
echo   Two windows opened. The public https://...trycloudflare.com address
echo   appears in the tunnel window. It changes every time you restart.
echo.
pause
