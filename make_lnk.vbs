Option Explicit
' 生成一个带 --remote-debugging-port=9222 的 ChatGPT 快捷方式，
' 之后直接双击这个快捷方式启动 ChatGPT，对话页的消耗标签才有注入目标。
Dim sh, fso, lnk, out, tgt, appdir
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

appdir = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Microsoft\WindowsApps"
tgt = appdir & "\chatgpt-desktop.exe"
out = fso.BuildPath(fso.GetParentFolderName(WScript.ScriptFullName), "ChatGPT（带用量标签）.lnk")

If Not fso.FileExists(tgt) Then
  MsgBox "没找到 ChatGPT 桌面版：" & vbCrLf & tgt & vbCrLf & vbCrLf & _
         "请先在 Microsoft Store 安装 ChatGPT，或把本脚本里的 appdir 改成实际安装目录。", 16, "Codex 用量标签"
  WScript.Quit 1
End If

Set lnk = sh.CreateShortcut(out)
lnk.TargetPath = tgt
lnk.Arguments = "--remote-debugging-port=9222"
lnk.WorkingDirectory = appdir
lnk.IconLocation = tgt & ",0"
lnk.Description = "ChatGPT with remote debugging for usage labels"
lnk.WindowStyle = 1
lnk.Save
WScript.Echo "saved: " & out