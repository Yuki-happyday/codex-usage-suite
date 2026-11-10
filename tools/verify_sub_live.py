"""实机验证子菜单：右键 → 光标移到「外观」行 → 等定时器展开 → 截图。"""
import ctypes
import ctypes.wintypes as wt
import os
import sys
import time

from PIL import ImageGrab

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import menu_win as W

u = W.user32
P = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)


def find(cls_prefix):
    out = []

    def cb(h, lp):
        b = ctypes.create_unicode_buffer(128)
        u.GetClassNameW(h, b, 128)
        if b.value.startswith(cls_prefix) and u.IsWindowVisible(h):
            out.append(h)
        return True

    u.EnumWindows(P(cb), 0)
    return out


def rect(h):
    r = wt.RECT()
    u.GetWindowRect(h, ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def main():
    W.make_thread_dpi_aware()
    fl = find('PyFloat_')
    if not fl:
        print('no float')
        return
    x, y, w, h = rect(fl[0])
    u.SetCursorPos(x + w // 2, y + h // 2)
    time.sleep(0.3)
    u.PostMessageW(fl[0], 0x0205, 0, 0)
    time.sleep(1.4)

    ms = find('PyCodexMenu')
    if not ms:
        print('no menu')
        return
    mx, my, mw, mh = rect(ms[0])
    s = W.F.dpi_scale()
    print('menu', (mx, my, mw, mh), 'scale', s, 'count', len(ms))

    # 第三行「外观」：PAD_Y=7 + 行高 29 + 分隔 8 → 44~73，取中心
    tx = int(mx + (13 + 90) * s)
    ty = int(my + (13 + 58.5) * s)
    print('hover', (tx, ty))
    u.SetCursorPos(tx, ty)
    time.sleep(1.3)

    ms = find('PyCodexMenu')
    print('menus now', [(m, rect(m)) for m in ms])
    img = ImageGrab.grab()
    xs = [rect(m)[0] for m in ms]
    x0 = max(0, min(xs) - 30)
    y0 = max(0, my - 30)
    x1 = min(img.size[0], mx + mw + 30)
    y1 = min(img.size[1], my + mh + 30)
    img.crop((x0, y0, x1, y1)).save(os.path.join(ROOT, 'docs', 'screenshots', 'shot-live-sub.png'))
    print('shot', (x0, y0, x1, y1))
    for m in ms:
        u.PostMessageW(m, 0x0100, 0x1B, 0)


if __name__ == '__main__':
    main()
