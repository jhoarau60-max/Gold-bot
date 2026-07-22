@echo off
cd /d "%~dp0"
echo ==============================
echo   DEPLOIEMENT GOLD BOT
echo ==============================
echo.
git push origin main
if errorlevel 1 (
  echo.
  echo [ECHEC] Push echoue - envoie une capture a Claude
) else (
  echo.
  echo [OK] Push reussi - Railway redeploie le bot dans 2-3 min
)
echo.
pause
