"""实机验证：光标形状 + 点选后动作是否真的执行（异步消息链路）。

用一个零风险的可逆动作做验证：「自动接管任务栏启动」开/关，
state.json 里的 takeover 会翻转，点两次就回到原值。
"""
import ctypes
import ctypes.wintypes as wt
import json
import time

u = ctypes.windll.user32
u.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
u.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
u.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
u.LoadCursorW.argtypes = [wt.HINSTANCE, ctypes.c_void_p]
u.LoadCursorW.restype = wt.HANDLE
u.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
u.mouse_event.argtypes = [wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_size_t]

STATE = r'D:\AppData\codex\usage\state.json'
SHW, SCALE = 13.0, 1.75
PAD_Y, ROW_H = 6, 29


def dpi():
    pass


class CI(ctypes.Structure):
    _fields_ = [('cbSize', wt.DWORD), ('flags', wt.DWORD),
                ('hCursor', wt.HANDLE), ('pt', wt.POINT)]


def cur_name():
    ci = CI()
    ci.cbSize = ctypes.sizeof(CI)
    u.GetCursorInfo(ctypes.byref(ci))
    for v, n in ((32512, '箭头'), (32649, '手型'), (32514, '沙漏'),
                 (32646, '四向'), (32650, '箭头+转圈')):
        if u.LoadCursorW(None, ctypes.c_void_p(v)) == ci.hCursor:
            return n
    return f'?{ci.hCursor}'


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


def rect(h):
    r = wt.RECT()
    u.GetWindowRect(h, ctypes.byref(r))
    return r


def click(x, y):
    u.SetCursorPos(x, y)
    time.sleep(0.25)
    u.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(0.05)
    u.mouse_event(0x0004, 0, 0, 0, 0)


def takeover():
    return json.load(open(STATE, encoding='utf-8')).get('takeover')


def open_menu():
    fw, fr = find('PyFloat_')[0]
    u.SetCursorPos((fr.left + fr.right) // 2, (fr.top + fr.bottom) // 2)
    time.sleep(0.3)
    u.PostMessageW(fw, 0x0205, 0, 0)
    time.sleep(1.0)
    ms = find('PyCodexMenu')
    if not ms:
        return None
    return ms[0][0], rect(ms[0][0])


def row_xy(mr, from_bottom_rows, frac=0.55):
    """from_bottom_rows：这一行往上数第几行（0 = 最后一行「退出」）"""
    y = mr.bottom - int(round((SHW + PAD_Y + ROW_H / 2.0) * SCALE)) \
        - int(round(from_bottom_rows * ROW_H * SCALE))
    x = mr.left + int((mr.right - mr.left) * frac)
    return x, y


def main():
    print('初始 takeover =', takeover())

    # 1) 光标：非可点行（Codex 用量分组标题）应为箭头
    got = open_menu()
    assert got, '菜单没弹出来'
    hwnd, mr = got
    x, y = row_xy(mr, 5)          # 自动接管(1) 退出(0) …… 往上数 5 行 ≈ Codex 用量标题
    u.SetCursorPos(x, y)
    time.sleep(0.5)
    print('分组标题行光标 =', cur_name())

    # 2) 光标：可点行应为手型
    x, y = row_xy(mr, 1)          # 自动接管
    u.SetCursorPos(x, y)
    time.sleep(0.5)
    print('可点行光标     =', cur_name())

    # 3) 点它：动作应该真的执行（异步消息回来后在悬浮窗线程跑）
    before = takeover()
    click(x, y)
    time.sleep(1.2)
    after = takeover()
    print(f'点击后 takeover: {before} -> {after}   菜单是否已关: {not find("PyCodexMenu")}')

    # 4) 再点一次还原
    got = open_menu()
    if got:
        hwnd, mr = got
        x, y = row_xy(mr, 1)
        click(x, y)
        time.sleep(1.2)
        print('还原后 takeover =', takeover(), ' 菜单是否已关:', not find('PyCodexMenu'))
    for h, _ in find('PyCodexMenu'):
        u.PostMessageW(h, 0x0100, 0x1B, 0)


main()
