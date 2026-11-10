"""展示「外观 ▸」这一层的三级菜单摆位：主菜单 + 外观子菜单 + 颜色子弹窗。"""
import os
import sys

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import float_win32 as F
import menucanvas as M

SCALE = 1.75
BG_L, BG_D = (238, 240, 239, 255), (24, 27, 29, 255)


def level1():
    return [
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


def build(dark, theme):
    p1 = M.Painter(level1(), dark=dark, theme=theme, scale=SCALE)
    sub1 = p1.rows[2]['sub']                       # 外观下面的四项
    p2 = M.Painter(sub1, dark=dark, theme=theme, scale=SCALE)
    p3 = M.Painter(p2.rows[0]['sub'], dark=dark, theme=theme, scale=SCALE)   # 颜色

    # 内容框左上角坐标（逻辑 px）
    x1, y1 = 0.0, 0.0
    x2 = p1.W - 1.0
    y2 = p1.rows[2]['y0']
    x3 = x2 + p2.W - 1.0
    y3 = y2 + p2.rows[0]['y0']

    margin = 34
    tw = (x3 + p3.W) * SCALE + margin * 2
    th = max(p1.H, y3 + p3.H) * SCALE + margin * 2
    canvas = Image.new('RGBA', (int(tw), int(th)), BG_D if dark else BG_L)

    for p, (cx, cy), hover in ((p1, (x1, y1), 2), (p2, (x2, y2), 0), (p3, (x3, y3), 1)):
        img = p.image(hover=hover)
        px = int(margin + cx * SCALE - p.shw * SCALE)
        py = int(margin + cy * SCALE - p.shw * SCALE)
        canvas.alpha_composite(img, (px, py))
    return canvas


def main():
    outs = [build(False, 'soft'), build(True, 'soft')]
    W = max(i.size[0] for i in outs) + 60
    H = sum(i.size[1] for i in outs) + 60 * 3
    canvas = Image.new('RGBA', (int(W), int(H)), (236, 238, 237, 255))
    y = 60
    d = ImageDraw.Draw(canvas)
    lf = F._font(28, True)
    for img, label in ((outs[0], '浅色 · 主菜单 → 外观 → 颜色'),
                       (outs[1], '深色 · 同上')):
        d.text((60, y - 34), label, font=lf, fill=(33, 45, 38), anchor='ls')
        canvas.alpha_composite(img, (30, y))
        y += img.size[1] + 60
    out = os.path.join(ROOT, 'docs', 'screenshots', 'menu-sublevels.png')
    canvas.convert('RGB').save(out)
    print(out, canvas.size)


if __name__ == '__main__':
    main()
