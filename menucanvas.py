"""自绘右键菜单的渲染层：圆角面板、悬停高亮、勾选标记、子菜单箭头。

原生 TrackPopupMenu 的圆角、留白、行高、悬停色、字号全部由 Windows 决定，
跟卡片那套「浅绿白 / 深灰」完全脱节，右键一出来就是一块系统灰。这里改成
分层窗口 + PIL 超采样自绘，视觉上和卡片是一套东西。

本文件只负责「画」：items -> PIL.Image。窗口与交互见同目录的 menu_win.py。
items 结构与原来 TrackPopupMenu 那套完全兼容，老数据不用改：
    (label, enabled, action)                 普通项
    (label, False, None)                     只读文本
    ('-', False, None)                       分隔线
    (label, [sub_items])                     子菜单（可以再嵌套）
    (label, True, action, flags)             flags：MENU_CHECK 打勾 / MENU_SECTION 小标题

为什么用超采样再缩：PIL 的圆角、1px 描边、勾号在目标尺寸上直接画会有明显锯齿，
按 SS 倍画完再 LANCZOS 缩回去，边缘才是干净的。
"""
import ctypes
import ctypes.wintypes as wt

from PIL import Image, ImageDraw, ImageFilter

import float_win32 as F

user32 = F.user32

# ---------------------------------------------------------------- items 标志位
MENU_CHECK = 0x8                # 与 MF_CHECKED 同值，老数据直接可用
MENU_SECTION = 0x100            # 本模块自定义：分组小标题（不响应点击）

# ---------------------------------------------------------------- 版面常量（逻辑 px）
PAD_X, PAD_Y = 11, 7
ROW_H, SEC_H, SEP_H = 29, 24, 8
CHECK_W, ARROW_W = 22, 16
TEXT_PX, TEXT_PX_SMALL = 12.5, 11.5
MIN_W = 152
SS = 2                          # 超采样倍数。菜单较大，取 2 已足够锐；4 会让每次重绘翻倍

# ---------------------------------------------------------------- 配色
BASE_LIGHT = {
    'bg': (252, 253, 252), 'bg_a': 253,
    'border': (223, 229, 225),
    'line': (234, 238, 235),
    'fg': (37, 55, 44),
    'fg2': (129, 141, 133),
    'accent': (36, 133, 102),
}
BASE_DARK = {
    'bg': (38, 41, 44), 'bg_a': 252,
    'border': (71, 76, 80),
    'line': (58, 62, 66),
    'fg': (237, 242, 238),
    'fg2': (150, 161, 155),
    'accent': (76, 175, 142),
}

# 三款只差「悬停观感 + 分隔线 + 投影」，底板圆角与字号基本一致，
# 免得换一款像换了个软件。
#   radius    底板圆角
#   hl_radius 悬停块圆角（越大越像胶囊）
#   hl_inset  悬停块左右内缩
#   shadow    (alpha, 模糊半径, 下移, 外扩) —— 外扩决定窗口比内容大多少
#   sep_line  是否画分隔线；draw_sep=False 时靠留白分组，sep_h 放大
THEMES = {
    'soft': dict(
        name='柔和卡片', radius=10, hl_radius=7, hl_inset=5,
        shadow=(70, 7.0, 3.0, 13.0),
        sep_line=True, sep_h=8,
        hi_light=(238, 242, 239), hi_dark=(50, 54, 58),
    ),
    'hairline': dict(
        name='极简描边', radius=6, hl_radius=5, hl_inset=1,
        shadow=(14, 3.0, 1.0, 6.0),
        sep_line=True, sep_h=8,
        border=((206, 212, 208), (86, 92, 96)),
        hl_bar=2.6,
        hi_light=(243, 246, 244), hi_dark=(52, 56, 60),
    ),
    'pill': dict(
        name='强调胶囊', radius=12, hl_radius=15, hl_inset=6,
        shadow=(52, 8.0, 4.0, 11.0),
        sep_line=False, sep_h=18,
        hi_light=(228, 242, 236), hi_dark=(44, 64, 56),
    ),
}
THEME_ORDER = ['soft', 'hairline', 'pill']
DEFAULT_THEME = 'soft'


# ---------------------------------------------------------------- 行解析
def parse_rows(items):
    """把外部 items 归一化成内部行，保持顺序。"""
    rows = []
    for it in items or []:
        if not it:
            continue
        label = it[0]
        if label == '-':
            rows.append(dict(kind='sep', label='', y0=0.0, y1=0.0))
            continue
        second = it[1] if len(it) > 1 else None
        if isinstance(second, list):          # (label, [sub_items]) = 子菜单
            flags = it[3] if len(it) > 3 else 0
            rows.append(dict(kind='item', label=label, enabled=True, action=None,
                             flags=flags, sub=second, y0=0.0, y1=0.0))
            continue
        action = it[2] if len(it) > 2 else None
        flags = it[3] if len(it) > 3 else 0
        if flags & MENU_SECTION:
            kind = 'sec'
        elif second:
            kind = 'item'
        else:
            kind = 'label'
        rows.append(dict(kind=kind, label=label, enabled=bool(second),
                         action=action, flags=flags, sub=None, y0=0.0, y1=0.0))
    return rows


# ---------------------------------------------------------------- 渲染
class Painter:
    """排好版面并画成位图。窗口和预览脚本共用，保证看到的就是弹出的。"""

    def __init__(self, items, dark=False, theme=None, scale=1.0, defer=False):
        self.dark = bool(dark)
        self.theme = theme if theme in THEMES else DEFAULT_THEME
        cfg = THEMES[self.theme]
        self.cfg = cfg
        self.pal = dict(BASE_DARK if self.dark else BASE_LIGHT)
        self.pal['hi'] = cfg['hi_dark' if self.dark else 'hi_light']
        bd = cfg.get('border')
        if bd:
            self.pal['border'] = bd[1 if self.dark else 0]
        self.scale = max(0.5, float(scale or 1.0))
        self.k = self.scale * SS
        self.rows = parse_rows(items)
        self._dummy = ImageDraw.Draw(Image.new('L', (1, 1)))
        self._fonts = {}
        self._hlc = {}
        self._measure()
        self._static_bg = None
        self._static_text = None
        if not defer:            # defer=True 时只量版面，真正画图留到 image()
            self._ensure_static()

    def _ensure_static(self):
        if self._static_bg is None:
            self._build_static()

    # ---------- 坐标换算
    def _o(self, v):
        return v * self.k

    def _font(self, px, bold):
        key = (round(px, 1), bool(bold))
        f = self._fonts.get(key)
        if f is None:
            f = F._font(px, bold)
            self._fonts[key] = f
        return f

    def _font_for(self, row):
        if row['kind'] == 'sec':
            return self._font(TEXT_PX_SMALL * self.k, True)
        if row['kind'] == 'label':
            return self._font(TEXT_PX_SMALL * self.k, False)
        return self._font(TEXT_PX * self.k, False)

    # ---------- 版面
    def _h_of(self, row):
        if row['kind'] == 'sep':
            return self.cfg['sep_h']
        return SEC_H if row['kind'] == 'sec' else ROW_H

    def _measure(self):
        maxw = 0.0
        has_sub = False
        has_check = False
        for r in self.rows:
            if r['kind'] == 'sep':
                continue
            f = self._font_for(r)
            r['font'] = f
            try:
                w = self._dummy.textlength(r['label'], font=f) / self.k
            except Exception:
                w = len(r['label']) * TEXT_PX
            r['tw'] = w
            maxw = max(maxw, w)
            if r.get('sub'):
                has_sub = True
            if r['flags'] & MENU_CHECK:
                has_check = True
        # 整个菜单都没有勾选项时不留那一列空位，否则文字左边会空出一块，
        # 显得菜单「缩在右边」。（主菜单就是这种：勾都在第二、三层里）
        self.check_w = CHECK_W if has_check else 0
        self.text_w = maxw
        self.has_sub = has_sub
        self.W = max(MIN_W, int(round(PAD_X * 2 + self.check_w + maxw +
                                      (ARROW_W if has_sub else 0))))
        y = float(PAD_Y)
        for r in self.rows:
            r['y0'] = y
            y += self._h_of(r)
            r['y1'] = y
        self.H = int(round(y + PAD_Y))
        shw = self.cfg['shadow'][3]
        self.shw = shw
        self.pw = max(1, int(round((self.W + shw * 2) * self.scale)))
        self.ph = max(1, int(round((self.H + shw * 2) * self.scale)))

    # ---------- 静态层（底板 + 文字），只在打开时算一次
    def _build_static(self):
        k = self.k
        shw = self.shw
        pal = self.pal
        alpha, blur, dy = self.cfg['shadow'][:3]

        big = Image.new('RGBA', (self.pw * SS, self.ph * SS), (0, 0, 0, 0))
        # 投影：整块圆角矩形模糊后当阴影，比一圈圈描边自然
        sh = Image.new('RGBA', big.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(sh)
        d.rounded_rectangle(
            [self._o(shw), self._o(shw + dy),
             self._o(shw + self.W), self._o(shw + dy + self.H)],
            radius=self.cfg['radius'] * k, fill=(0, 0, 0, alpha))
        sh = sh.filter(ImageFilter.GaussianBlur(blur * k))
        big.alpha_composite(sh)

        d = ImageDraw.Draw(big)
        d.rounded_rectangle(
            [self._o(shw), self._o(shw),
             self._o(shw + self.W), self._o(shw + self.H)],
            radius=self.cfg['radius'] * k,
            fill=pal['bg'] + (pal['bg_a'],),
            outline=pal['border'] + (255,),
            width=max(1, int(round(k))))

        if self.cfg['sep_line']:
            # 分隔线只画到内边距里，顶到圆角会显得很硬
            for r in self.rows:
                if r['kind'] != 'sep':
                    continue
                y = self._o(shw + (r['y0'] + r['y1']) / 2.0)
                d.line([self._o(shw + PAD_X + 2), y, self._o(shw + self.W - PAD_X - 2), y],
                       fill=pal['line'] + (255,), width=max(1, int(round(k))))
        self._static_bg = big.resize((self.pw, self.ph), Image.LANCZOS)

        txt = Image.new('RGBA', (self.pw * SS, self.ph * SS), (0, 0, 0, 0))
        d = ImageDraw.Draw(txt)
        cx_check = shw + PAD_X + self.check_w * 0.5
        for r in self.rows:
            if r['kind'] == 'sep':
                continue
            cy = shw + (r['y0'] + r['y1']) / 2.0
            if r['kind'] == 'sec':
                # 标题不缩进，和下面条目拉开层级
                d.text((self._o(shw + PAD_X), self._o(cy)), r['label'],
                       font=r['font'], fill=pal['fg2'] + (255,), anchor='lm')
            else:
                col = pal['fg'] if (r['kind'] == 'item' and r['enabled']) else pal['fg2']
                d.text((self._o(shw + PAD_X + self.check_w), self._o(cy)), r['label'],
                       font=r['font'], fill=col + (255,), anchor='lm')
            if r['flags'] & MENU_CHECK:
                self._draw_check(d, self._o(cx_check), self._o(cy))
            if r.get('sub'):
                self._draw_arrow(d, self._o(shw + self.W - PAD_X - ARROW_W * 0.5),
                                 self._o(cy))
        self._static_text = txt.resize((self.pw, self.ph), Image.LANCZOS)

    def _draw_check(self, d, cx, cy):
        k = self.k
        pts = [(cx - 3.6 * k, cy + 0.1 * k),
               (cx - 1.2 * k, cy + 2.6 * k),
               (cx + 3.7 * k, cy - 2.7 * k)]
        d.line(pts, fill=self.pal['accent'] + (255,),
               width=max(1, int(round(1.7 * k))), joint='curve')

    def _draw_arrow(self, d, cx, cy):
        k = self.k
        d.polygon([(cx - 1.7 * k, cy - 2.7 * k), (cx + 1.8 * k, cy),
                   (cx - 1.7 * k, cy + 2.7 * k)],
                  fill=self.pal['fg2'] + (255,))

    # ---------- 悬停层（按行懒生成并缓存，同一行反复划过不重画）
    def _hl_layer(self, idx):
        img = self._hlc.get(idx)
        if img is not None:
            return img
        k = self.k
        r = self.rows[idx]
        ins = self.cfg['hl_inset']
        img = Image.new('RGBA', (self.pw * SS, self.ph * SS), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle(
            [self._o(self.shw + ins), self._o(self.shw + r['y0'] + 1.5),
             self._o(self.shw + self.W - ins), self._o(self.shw + r['y1'] - 1.5)],
            radius=self.cfg['hl_radius'] * k,
            fill=self.pal['hi'] + (255,))
        bar = self.cfg.get('hl_bar')
        if bar:
            # 左侧强调竖条：极简款靠这一条指示位置，比整块底色更轻
            bx = self.shw + 2.2
            d.rounded_rectangle(
                [self._o(bx), self._o(self.shw + r['y0'] + 6),
                 self._o(bx + bar), self._o(self.shw + r['y1'] - 6)],
                radius=bar * 0.5 * k, fill=self.pal['accent'] + (205,))
        img = img.resize((self.pw, self.ph), Image.LANCZOS)
        self._hlc[idx] = img
        return img

    def image(self, hover=None, sub_row=None):
        """hover = 鼠标所在行；sub_row = 已展开子菜单的行（保持高亮，不跟随鼠标）"""
        self._ensure_static()
        img = self._static_bg.copy()
        hl = hover if hover is not None else sub_row
        if hl is not None and 0 <= hl < len(self.rows):
            if self.rows[hl]['kind'] in ('item', 'sec'):
                img.alpha_composite(self._hl_layer(hl))
        img.alpha_composite(self._static_text)
        return img

    # ---------- 命中
    def hit_index(self, lx, ly):
        """lx/ly 相对内容框左上角，逻辑坐标"""
        if not (0 <= lx <= self.W and 0 <= ly <= self.H):
            return None
        for i, r in enumerate(self.rows):
            if r['kind'] == 'sep':
                continue
            if r['y0'] <= ly < r['y1']:
                return i
        return None


def render_menu(items, dark=False, theme=None, hover=None, scale=1.0, sub_row=None):
    return Painter(items, dark=dark, theme=theme, scale=scale).image(hover, sub_row)
