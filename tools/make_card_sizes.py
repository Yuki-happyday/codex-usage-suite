# -*- coding: utf-8 -*-
"""生成 docs/screenshots/card-3sizes.png：三档尺寸 × 浅色/深色。

直接调用 float_win32.render()，所以电池、字号、配色永远跟主程序一致——
不再手绘示意，避免"改了代码但样张没跟着变"。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from PIL import Image, ImageDraw, ImageFont
import float_win32 as F

OUT = os.path.join(ROOT, 'docs', 'screenshots', 'card-3sizes.png')

# 样张内容：左节 5h 重置钟点、右节 week 重置日期（与主程序同格式，不补零）
DATA = {'reset5': '4:44', 'date': '11/9', 'p1': 99, 'p2': 100}

R = 3.0                 # 渲染倍率（相对逻辑像素），只为看清细节
DPI = 1.75              # 175% 缩放下的物理像素，用来标注
CELL_W = 580
PAD_X = 40
TOP_PAD = 52            # 每块色带里，卡片区上方留给「浅色 / 深色」标签的高度
LBL_GAP = 20            # 卡片底边到尺寸文字的间距
BOT_PAD = 46
FOOT_H = 54

LIGHT_BG = (241, 243, 244)
DARK_BG = (22, 24, 26)
LIGHT_TXT = (88, 98, 92)
DARK_TXT = (156, 168, 161)
FOOT_TXT = (128, 138, 132)

FONT = 'C:/Windows/Fonts/msyh.ttc'
FONT_BD = 'C:/Windows/Fonts/msyhbd.ttc'


def font(px, bold=False):
    try:
        return ImageFont.truetype(FONT_BD if bold else FONT, px)
    except Exception:
        return ImageFont.truetype('C:/Windows/Fonts/simhei.ttf', px)


cards = []          # [(名称, size, 浅色图, 深色图)]
for name, size in F.CARD_SIZES:
    light = F.render(DATA, scale=R, dark=False, size=size)
    dark = F.render(DATA, scale=R, dark=True, size=size)
    cards.append((name, size, light, dark))

max_h = max(c[2].size[1] for c in cards)
band_h = TOP_PAD + max_h + LBL_GAP + 40 + BOT_PAD
W = PAD_X * 2 + CELL_W * len(cards)
H = band_h * 2 + FOOT_H

img = Image.new('RGB', (W, H), LIGHT_BG)
d = ImageDraw.Draw(img)

# 上：浅色带；下：深色带
d.rectangle([0, band_h, W, band_h * 2], fill=DARK_BG)
d.rectangle([0, band_h * 2, W, H], fill=LIGHT_BG)

f_band = font(26, True)
f_name = font(30, True)
f_dim = font(24)
f_foot = font(21)

for row, (title, band_y, text_col, images) in enumerate([
        ('浅色外观', 0, LIGHT_TXT, [c[2] for c in cards]),
        ('深色外观', band_h, DARK_TXT, [c[3] for c in cards])]):
    d.text((PAD_X, band_y + 20), title, font=f_band, fill=text_col)

    base = band_y + TOP_PAD + max_h          # 三张卡底边对齐
    for i, (name, size, _l, _d) in enumerate(cards):
        im = images[i]
        cx = PAD_X + CELL_W * i + CELL_W // 2
        img.paste(im, (cx - im.size[0] // 2, base - im.size[1]), im)

        py = base + LBL_GAP
        phys = (int(round(size[0] * DPI)), int(round(size[1] * DPI)))
        d.text((cx, py), name, font=f_name, fill=text_col, anchor='mt')
        d.text((cx, py + 40), '逻辑 %d×%d  ·  物理 %d×%d' % (
            size[0], size[1], phys[0], phys[1]),
            font=f_dim, fill=text_col, anchor='mt')

d.text((PAD_X, H - 38),
       '图为逻辑像素的 3 倍渲染（非 1:1 截图）；物理尺寸指 175% 缩放下的实际像素',
       font=f_foot, fill=FOOT_TXT)

os.makedirs(os.path.dirname(OUT), exist_ok=True)
img.save(OUT)
print('saved', OUT, img.size)
