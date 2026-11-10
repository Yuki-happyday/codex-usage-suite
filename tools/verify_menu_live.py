"""实机验证：向运行中的悬浮窗发右键消息，截下自绘菜单，再发 Esc 关掉。"""
import ctypes
import ctypes.wintypes as wt
import os
import sys
import time

from PIL import ImageGrab

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import menu_win as W
import float_win32 as F

u = W.user32
EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)


def find_windows(prefix):
    out = []

    def cb(h, lp):
        b = ctypes.create_unicode_buffer(256)
        u.GetClassNameW(h, b, 256)
        if b.value.startswith(prefix):
            out.append((h, b.value, bool(u.IsWindowVisible(h))))
        return True

    u.EnumWindows(EnumProc(cb), 0)
    return out


def rect(h):
    r = wt.RECT()
    u.GetWindowRect(h, ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def main():
    W.make_thread_dpi_aware()
    floats = [f for f in find_windows('PyFloat_') if f[2]]
    if not floats:
        print('no float window')
        return
    fw = floats[0][0]
    x, y, w, h = rect(fw)
    print('float', fw, (x, y, w, h))
    u.SetCursorPos(x + w // 2, y + h // 2)
    time.sleep(0.35)
    u.PostMessageW(fw, 0x0205, 0, 0)          # WM_RBUTTONUP
    time.sleep(1.6)

    menus = [m for m in find_windows('PyCodexMenu') if m[2]]
    print('menu windows', [(m[0], rect(m[0])) for m in menus])
    img = ImageGrab.grab()
    if menus:
        mx, my, mw, mh = rect(menus[0][0])
        x0, y0 = max(0, mx - 40), max(0, my - 40)
        x1, y1 = min(img.size[0], mx + mw + 40), min(img.size[1], my + mh + 40)
    else:
        x0, y0 = max(0, x - 240), max(0, y - 60)
        x1, y1 = min(img.size[0], x0 + 900), min(img.size[1], y0 + 900)
    img.crop((x0, y0, x1, y1)).save(os.path.join(ROOT, 'docs', 'screenshots', 'shot-live.png'))
    print('shot', (x0, y0, x1, y1))

    for m in menus:
        u.PostMessageW(m[0], 0x0100, 0x1B, 0)   # VK_ESCAPE
    time.sleep(0.6)
    left = [m for m in find_windows('PyCodexMenu') if m[2]]
    print('after esc, menus left', len(left))


if __name__ == '__main__':
    main()
