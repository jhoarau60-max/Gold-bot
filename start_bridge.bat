@echo off
set MT5_BRIDGE_TOKEN=RsrBmgFo-RZH216bRdIQ_n4F1porlmgV
cd /d C:\Users\jhoar\Desktop\Bots\Gold-bot
echo MARKER_START %date% %time% >> bridge.log
C:\Python314\python.exe mt5_bridge.py >> bridge.log 2>&1
echo MARKER_END %date% %time% exitcode=%errorlevel% >> bridge.log
