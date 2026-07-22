@echo off
timeout /t 15 /nobreak >nul
"C:\Users\jhoar\AppData\Local\Microsoft\WinGet\Packages\Ngrok.Ngrok_Microsoft.Winget.Source_8wekyb3d8bbwe\ngrok.exe" http 5678 --domain=ferment-eccentric-convent.ngrok-free.dev >> "C:\Users\jhoar\Desktop\Bots\Gold-bot\tunnel.log" 2>&1
