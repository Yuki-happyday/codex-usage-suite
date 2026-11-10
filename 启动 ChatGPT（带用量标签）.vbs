Option Explicit
' 一键启动：先确保 ChatGPT 以 --remote-debugging-port=9222 启动（对话页的「本轮消耗」标签
' 要靠这个调试端口注入），端口就绪后再拉起监听程序。
Const PYW = "pythonw.exe"

Dim sh, fso, pyw, watch, alias, http, ok, svc, procs, running, msg
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

pyw   = PYW
watch = fso.BuildPath(fso.GetParentFolderName(WScript.ScriptFullName), "codex_usage_watch.py")
alias = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Microsoft\WindowsApps\chatgpt-desktop.exe"

If Not fso.FileExists(watch) Then
  MsgBox "没找到 codex_usage_watch.py，请把本启动器放在脚本同一目录下。", 16, "Codex 用量标签"
  WScript.Quit 1
End If

ok = False
On Error Resume Next
Set http = CreateObject("MSXML2.XMLHTTP")
http.Open "GET", "http://127.0.0.1:9222/json/version", False
http.Send
If Err.Number = 0 Then
  If http.Status = 200 Then ok = True
End If
Err.Clear
On Error GoTo 0

If Not ok Then
  running = False
  On Error Resume Next
  Set svc = GetObject("winmgmts:\\.\root\cimv2")
  Set procs = svc.ExecQuery("SELECT ProcessId FROM Win32_Process WHERE Name='ChatGPT.exe'")
  If Err.Number = 0 Then
    If procs.Count > 0 Then running = True
  End If
  Err.Clear
  On Error GoTo 0

  If running Then
    msg = "ChatGPT 正在运行，但没有开用量标签所需的调试端口。" & vbCrLf & vbCrLf & _
          "继续会先关闭 ChatGPT 再重开（输入框里没发送的内容会丢）。要继续吗？"
    If MsgBox(msg, 4 + 48, "Codex 用量标签") <> 6 Then
      sh.Run """" & pyw & """ """ & watch & """", 0, False
      WScript.Quit
    End If
    sh.Run "taskkill /IM ChatGPT.exe /F", 0, True
    WScript.Sleep 1500
  End If
  sh.Run """" & alias & """ --remote-debugging-port=9222", 0, False
  WScript.Sleep 8000
End If

sh.Run """" & pyw & """ """ & watch & """", 0, False