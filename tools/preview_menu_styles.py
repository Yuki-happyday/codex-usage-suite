"""生成三款菜单风格的效果对比图（浅色 + 深色各一排）。纯出图，不碰窗口。"""
import os
import sys

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import float_win32 as F
import menucanvas as M

SCALE = 1.75          # 和用户实际 DPI 一致，出来的字号就是真实观感
GAP = 46
PAD = 46
LABEL_H = 44
NOTE_H = 40
TITLE_H = 44
BLOCK_GAP = 40


def demo_items():
    def check(flag, on):
        return flag if on else 0

    return [
        ('立即刷新', True, None),
        ('-', False, None),
        ('外观', [
            ('颜色：深色', [('跟随系统', True, None),
                            ('深色', True, None, 0x8), ('浅色', True, None)]),
            ('样式：磨砂', [('原始', True, None), ('磨砂', True, None, 0x8)]),
            ('透明度：90%', [(f'{v}%', True, None, 0x8 if v == 90 else 0)
                             for v in (100, 90, 80, 70, 60, 50)]),
            ('卡片大小：中', [('小', True, None), ('中', True, None, 0x8),
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


def compose(dark):
    items = demo_items()
    imgs = []
    for t in M.THEME_ORDER:
        p = M.Painter(items, dark=dark, theme=t, scale=SCALE)
        imgs.append((t, p.image(hover=2)))
    return imgs


def main():
    rows = {False: compose(False), True: compose(True)}
    col_w = max(i.size[0] for k in rows for _, i in rows[k])
    row_h = max(i.size[1] for k in rows for _, i in rows[k])
    block_h = TITLE_H + LABEL_H + row_h + NOTE_H
    W = PAD * 2 + col_w * 3 + GAP * 2
    H = PAD * 2 + block_h * 2 + BLOCK_GAP
    canvas = Image.new('RGB', (W, H), (236, 238, 237))

    # 上下两块底色：浅色区 / 深色区，阴影才看得出来
    mid = PAD + block_h + BLOCK_GAP // 2
    canvas.paste(Image.new('RGB', (W, mid), (238, 240, 239)), (0, 0))
    canvas.paste(Image.new('RGB', (W, H - mid), (24, 27, 29)), (0, mid))
    d = ImageDraw.Draw(canvas)

    title_f = F._font(30, True)
    name_f = F._font(25, True)
    sub_f = F._font(21, False)
    notes = {'soft': '圆角胶囊悬停 · 柔投影 · 细线分组',
             'hairline': '通栏高亮 + 左侧竖条 · 几乎无投影',
             'pill': '全圆角胶囊 · 淡绿悬停 · 无线分组'}
    idx = {'soft': 'A', 'hairline': 'B', 'pill': 'C'}

    for block, dark in enumerate((False, True)):
        top = PAD + block * (block_h + BLOCK_GAP)
        fg = (245, 248, 246) if dark else (33, 45, 38)
        fg2 = (150, 162, 156) if dark else (118, 130, 122)
        d.text((PAD, top), '深色外观' if dark else '浅色外观',
               font=title_f, fill=fg, anchor='lt')
        y = top + TITLE_H + LABEL_H
        for ci, (t, img) in enumerate(rows[dark]):
            x = PAD + ci * (col_w + GAP)
            canvas.paste(img, (x + (col_w - img.size[0]) // 2, y), img)
            d.text((x + col_w // 2, y - 12),
                   f'{idx[t]}  {M.THEMES[t]["name"]}', font=name_f, fill=fg, anchor='ms')
            d.text((x + col_w // 2, y + img.size[1] + 6),
                   notes[t], font=sub_f, fill=fg2, anchor='mt')

    out = os.path.join(ROOT, 'docs', 'screenshots', 'menu-styles.png')
    canvas.save(out)
    print(out, canvas.size)


if __name__ == '__main__':
    main()
