@echo off
cd /d "%~dp0"
echo Arret des anciennes instances (fenetres noires)...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :5678 ^| findstr LISTENING') do taskkill /f /pid %%a >nul 2>&1
taskkill /f /im ngrok.exe >nul 2>&1
timeout /t 2 /nobreak >nul
echo Installation du demarrage automatique...
copy /Y run_hidden.vbs "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\GoldBot.vbs" >nul
echo Lancement en arriere-plan (aucune fenetre)...
wscript "%~dp0run_hidden.vbs"
timeout /t 3 /nobreak >nul
echo.
echo =================================================
echo  OK ! Bridge + tunnel tournent en arriere-plan.
echo  Ils redemarreront seuls a chaque demarrage du PC.
echo  Tu peux fermer cette fenetre + les 2 fenetres noires.
echo =================================================
pause
