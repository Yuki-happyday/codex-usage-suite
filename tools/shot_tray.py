"""截取 Windows 11 托盘折叠浮层（用于核对图标是否出现）"""
import ctypes
import ctypes.wintypes as wt
import time

from PIL import ImageGrab
import uiautomation as auto

u = ctypes.windll.user32
PROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
CLS = 'TopLevelWindowForOverflowXamlIsland'


def visible_rect():
    box = {}

    def cb(h, l):
        cls = ctypes.create_unicode_buffer(256)
        u.GetClassNameW(h, cls, 256)
        if cls.value == CLS and u.IsWindowVisible(h):
            r = wt.RECT()
            u.GetWindowRect(h, ctypes.byref(r))
            box['r'] = (r.left, r.top, r.right, r.bottom)
        return True

    u.EnumWindows(PROC(cb), 0)
    return box.get('r')


def close_flyout():
    if visible_rect():
        u.keybd_event(0x1B, 0, 0, 0)      # ESC
        u.keybd_event(0x1B, 0, 2, 0)
        time.sleep(0.6)


def open_flyout():
    for attempt in range(4):
        close_flyout()
        if visible_rect():
            time.sleep(0.5)
        btn = auto.ButtonControl(Name='显示隐藏的图标', ClassName='SystemTray.NormalButton')
        btn.Exists(2)
        r = btn.BoundingRectangle
        cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
        u.SetCursorPos(cx, cy)
        time.sleep(0.3)
        u.mouse_event(0x0002, 0, 0, 0, 0)
        time.sleep(0.08)
        u.mouse_event(0x0004, 0, 0, 0, 0)
        u.SetCursorPos(120, 120)             # 移开，避免悬停预览遮挡
        for _ in range(10):
            time.sleep(0.4)
            if visible_rect():
                time.sleep(0.8)
                return True
    return False


def shot(path, scale=3):
    open_flyout()
    r = visible_rect()
    if not r:
        print('flyout not visible')
        return None
    img = ImageGrab.grab(bbox=r)
    img = img.resize((img.width * scale, img.height * scale), 1)
    img.save(path)
    print('saved', path, img.size, 'rect', r)
    close_flyout()
    return path


if __name__ == '__main__':
    import sys
import os
import tempfile
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(tempfile.gettempdir(), 'flyout_now.png')
    shot(out)
