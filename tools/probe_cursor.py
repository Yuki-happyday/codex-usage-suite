"""只读诊断：右键悬浮窗后，鼠标停在菜单上时系统实际用的光标是什么。

不改任何文件、不改程序逻辑，只发消息 + 读 GetCursorInfo。
"""
import ctypes
import ctypes.wintypes as wt
import time

u = ctypes.windll.user32

IDC_ARROW = 32512
IDC_IBEAM = 32513
IDC_WAIT = 32514
IDC_CROSS = 32515
IDC_SIZEALL = 32646
IDC_APPSTARTING = 32650
IDC_HAND = 32649
NAMES = {IDC_ARROW: 'IDC_ARROW 普通箭头', IDC_IBEAM: 'IDC_IBEAM 文本',
         IDC_WAIT: 'IDC_WAIT 沙漏（忙）', IDC_CROSS: 'IDC_CROSS 十字',
         IDC_SIZEALL: 'IDC_SIZEALL 四向箭头（拖动）',
         IDC_APPSTARTING: 'IDC_APPSTARTING 箭头+转圈（加载中）',
         IDC_HAND: 'IDC_HAND 手型'}


class CURSORINFO(ctypes.Structure):
    _fields_ = [('cbSize', wt.DWORD), ('flags', wt.DWORD),
                ('hCursor', wt.HANDLE), ('ptScreenPos', wt.POINT)]


u.GetCursorInfo.argtypes = [ctypes.POINTER(CURSORINFO)]
u.LoadCursorW.argtypes = [wt.HINSTANCE, ctypes.c_void_p]
u.LoadCursorW.restype = wt.HANDLE
u.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]


def dpi_aware():
    try:
        u.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        u.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        u.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass


def cur():
    ci = CURSORINFO()
    ci.cbSize = ctypes.sizeof(CURSORINFO)
    u.GetCursorInfo(ctypes.byref(ci))
    return ci.hCursor, (ci.ptScreenPos.x, ci.ptScreenPos.y), ci.flags


def name_of(h):
    for v, n in NAMES.items():
        if u.LoadCursorW(None, ctypes.c_void_p(v)) == h:
            return n
    return f'未知光标句柄 {h}'


def find(prefix):
    P = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    out = []

    def cb(h, l):
        b = ctypes.create_unicode_buffer(128)
        u.GetClassNameW(h, b, 128)
        if b.value.startswith(prefix) and u.IsWindowVisible(h):
            r = wt.RECT()
            u.GetWindowRect(h, ctypes.byref(r))
            out.append((h, r))
        return True

    u.EnumWindows(P(cb), 0)
    return out


def main():
    dpi_aware()
    fl = find('PyFloat_')
    if not fl:
        print('悬浮窗没在跑')
        return
    hwnd, r = fl[0]
    print('悬浮窗', hwnd, (r.left, r.top, r.right, r.bottom))

    cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
    u.SetCursorPos(cx, cy)
    time.sleep(0.3)
    print('移到卡片上：', name_of(cur()[0]))

    u.PostMessageW(hwnd, 0x0205, 0, 0)      # WM_RBUTTONUP
    time.sleep(1.2)
    menus = find('PyCodexMenu')
    print('菜单窗口:', [(h, (x.left, x.top)) for h, x in menus])
    if not menus:
        print('菜单没弹出来')
        return

    h, mr = menus[0]
    # 挪到菜单内容区中部
    u.SetCursorPos(mr.left + 80, mr.top + 60)
    print('\n--- 鼠标停在菜单上，每 0.5s 采样一次光标 ---')
    seen = []
    for i in range(16):
        # 抖动 2px：确保系统真的产生鼠标移动，而不是"光标一开始就在那"
        u.SetCursorPos(mr.left + 80 + (1 if i % 2 else -1), mr.top + 60)
        time.sleep(0.5)
        hc, pos, flags = cur()
        n = name_of(hc)
        seen.append(n)
        print(f'{i * 0.5 + 0.5:4.1f}s  flags=0x{flags:X}  {n}')
    print('\n出现过的光标：', sorted(set(seen)))

    for h2, _ in find('PyCodexMenu'):
        u.PostMessageW(h2, 0x0100, 0x1B, 0)    # Esc
    print('已关闭菜单')


main()
