"""弹出真实的自绘菜单并截三张图：初始 / 悬停 / 展开子菜单。用假坐标派发，不动用户鼠标。"""
import ctypes
import os
import sys
import threading
import time

from PIL import ImageGrab

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import menu_win as W
import menucanvas as M

WM_KEYDOWN = 0x0100
VK_ESCAPE = 0x1B

ITEMS = [
    ('立即刷新', True, None),
    ('-', False, None),
    ('外观', [
        ('颜色：深色', [('跟随系统', True, None),
                        ('深色', True, None, M.MENU_CHECK), ('浅色', True, None)]),
        ('样式：磨砂', [('原始', True, None), ('磨砂', True, None, M.MENU_CHECK)]),
        ('透明度：90%', [(f'{v}%', True, None, M.MENU_CHECK if v == 90 else 0)
                         for v in (100, 90, 80, 70, 60, 50)]),
        ('卡片大小：中', [('小', True, None), ('中', True, None, M.MENU_CHECK),
                          ('大', True, None)]),
    ]),
    ('-', False, None),
    ('贴到最近的屏幕边缘', True, None),
    ('固定在任务栏', True, None),
    ('-', False, None),
    ('Codex 用量', False, None, M.MENU_SECTION),
    ('5小时：剩 87%  还剩 3小时12分  20:17 重置', False, None),
    ('每周：剩 99%  还剩 5天6小时  11-15 16:47 重置', False, None),
    ('余额：1,240 credits', False, None),
    ('数据 · 12 分钟前', False, None),
    ('-', False, None),
    ('自动接管任务栏启动：开', True, None),
    ('退出', True, None),
]


def crop_shot(s, tag):
    img = ImageGrab.grab()
    x0 = max(0, s.root.pos[0] - 30)
    y0 = max(0, s.root.pos[1] - 30)
    x1 = min(img.size[0], x0 + 1420)
    y1 = min(img.size[1], y0 + 900)
    img.crop((x0, y0, x1, y1)).save(os.path.join(ROOT, 'docs', 'screenshots', f'shot-{tag}.png'))
    print('shot', tag, (x0, y0, x1, y1), 'grab', img.size)


def row_center(p, idx, scale):
    cx, cy = p.content_origin()
    row = p.painter.rows[idx]
    return (int(cx + p.painter.W * 0.5 * scale),
            int(cy + (row['y0'] + row['y1']) / 2 * scale))


def driver(s):
    W.make_thread_dpi_aware()      # 线程 DPI 上下文不继承，SetCursorPos 必须按物理像素走
    time.sleep(0.7)
    crop_shot(s, 'idle')
    # 真事件才会走窗口过程里的悬停派发与定时器，所以这里移动真实光标
    W.user32.SetCursorPos(*row_center(s.root, 2, s.scale))     # 「外观」
    time.sleep(0.9)                                            # 悬停 + 定时器展开
    crop_shot(s, 'sub')
    if len(s.stack) > 1:
        W.user32.SetCursorPos(*row_center(s.stack[1], 0, s.scale))   # 「颜色」
        time.sleep(0.9)
        crop_shot(s, 'sub2')
    time.sleep(0.2)
    W.user32.PostMessageW(s.root.hwnd, WM_KEYDOWN, VK_ESCAPE, 0)


def main():
    W.make_thread_dpi_aware()
    s = W.Session(False, 'soft', W.F.dpi_scale())
    if not s.open_root(ITEMS, None, (820, 300)):
        print('popup failed')
        return
    print('dpi scale', s.scale, 'hwnd', s.root.hwnd,
          'pos', s.root.pos, 'bitmap', (s.root.painter.pw, s.root.painter.ph),
          'logic', (s.root.painter.W, s.root.painter.H))
    threading.Thread(target=driver, args=(s,), daemon=True).start()
    r = s.run()
    print('closed, result =', r)


if __name__ == '__main__':
    main()
