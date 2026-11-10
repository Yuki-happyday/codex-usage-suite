 ' Codex 用量提示 —— 启动器（双击即可，不弹黑框）
' 也可把这个文件放进「启动」文件夹（shell:startup）实现开机自动运行
'
' 关于 pythonw 路径：默认用 PATH 里的 pythonw.exe（无控制台窗口）。
' 如果你的 pythonw 不在 PATH 里，把下面 Const PYW 改成绝对路径，例如：
'     Const PYW = "C:\Python313\pythonw.exe"
' Windows 11 的托盘可见性（NotifyIconSettings.IsPromoted）是按 exe 路径 + 图标身份
' 记录的，一旦换了解释器路径，托盘图标可能重新掉进折叠区，所以选定后尽量别改。
Option Explicit
Const PYW = "pythonw.exe"

Dim sh, fso, script
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
script = fso.BuildPath(fso.GetParentFolderName(WScript.ScriptFullName), "codex_usage_watch.py")

If Not fso.FileExists(script) Then
  MsgBox "没找到 codex_usage_watch.py，请把本启动器放在脚本同一目录下。", 16, "Codex 用量提示"
  WScript.Quit 1
End If

sh.Run """" & PYW & """ """ & script & """", 0, False