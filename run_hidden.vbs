Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "C:\Users\jhoar\Desktop\Bots\Gold-bot"
sh.Run "cmd /c start_bridge.bat", 0, False
WScript.Sleep 5000
sh.Run "cmd /c start_tunnel.bat", 0, False
