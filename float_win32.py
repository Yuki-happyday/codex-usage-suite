"""桌面悬浮窗：电池样式的额度显示（纯 ctypes + PIL，不依赖 PyQt）

视觉参考 codex-quota-float 的胶囊窗：白底圆角、两节横向电池、
上方灰色重置时间（左 5h 钟点、右 week 日期），下方黑色百分比。
这里是自己重画的几何图形，
不复用该项目的任何代码或图标文件。

贴边收起：窗口拖到屏幕某条边附近松手就吸附过去，滑出屏幕外只留一条
           6px 的刻度细条（按额度等级色分三段）；鼠标碰到细条立刻滑回来，
           移开 0.5 秒再滑出去。四边都支持。

对外接口：
    FloatWindow(menu_provider, on_move, on_dock, handle_provider)
        -> start(img, pos=None, dock=None) / update(img) / show() / hide() / stop()
        -> expand() / collapse() / snap_now() / undock()
    render(data, scale, dark, size=None, handle_edge=None) -> PIL.Image
"""
import ctypes
import ctypes.wintypes as wt
import threading
import time

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

# ---------------------------------------------------------------- Win32 常量
WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000
WS_EX_LAYERED = 0x00080000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080

WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_SETCURSOR = 0x0020
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_NCHITTEST = 0x0084
WM_NCLBUTTONDOWN = 0x00A1
WM_EXITSIZEMOVE = 0x0232
HTCAPTION = 2
HTCLIENT = 1
IDC_SIZEALL = 32646        # 四向移动光标，提示「这块能拖」

# 菜单线程选完 / 关掉后投回来的两条消息（WM_APP+1 / +2，与 menu_win 里一致）
WM_MENU_PICK = 0x8001
WM_MENU_CLOSED = 0x8002

MF_STRING, MF_GRAYED, MF_SEPARATOR = 0x0, 0x1, 0x800
MF_CHECKED, MF_POPUP = 0x00000008, 0x00000010
TPM_RETURNCMD, TPM_NONOTIFY, TPM_RIGHTBUTTON = 0x100, 0x80, 0x2
SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0001, 0x0004, 0x0010
HWND_TOPMOST = -1

AC_SRC_OVER, AC_SRC_ALPHA, ULW_ALPHA = 0, 1, 0x00000002
CS_VREDRAW, CS_HREDRAW = 0x0001, 0x0002

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, ctypes.c_uint,
                             ctypes.c_size_t, ctypes.c_ssize_t)
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.DefWindowProcW.argtypes = [wt.HWND, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
# 显式声明签名：64 位下句柄超过 2^31 时按默认 c_int 传会被截断
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.SendMessageW.argtypes = [wt.HWND, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
user32.SendMessageW.restype = ctypes.c_ssize_t
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.SetCapture.argtypes = [wt.HWND]
user32.ReleaseCapture.argtypes = []
# 菜单相关同理：句柄按 c_int 传会被截断，TrackPopupMenu 直接失效（菜单闪一下就没有）
user32.CreatePopupMenu.restype = wt.HMENU
user32.AppendMenuW.argtypes = [wt.HMENU, ctypes.c_uint, ctypes.c_size_t, wt.LPCWSTR]
user32.TrackPopupMenu.argtypes = [wt.HMENU, ctypes.c_uint, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, wt.HWND, ctypes.c_void_p]
user32.TrackPopupMenu.restype = ctypes.c_int
user32.DestroyMenu.argtypes = [wt.HMENU]
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.PostMessageW.argtypes = [wt.HWND, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
user32.LoadCursorW.argtypes = [wt.HINSTANCE, ctypes.c_void_p]
user32.LoadCursorW.restype = wt.HANDLE
user32.SetCursor.argtypes = [wt.HANDLE]
# 抓屏排除自身：句柄按默认 c_int 传会被截断，调用直接返回 0（失败但不报错）
user32.SetWindowDisplayAffinity.argtypes = [wt.HWND, wt.DWORD]
user32.SetWindowDisplayAffinity.restype = ctypes.c_bool


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [('BlendOp', ctypes.c_ubyte), ('BlendFlags', ctypes.c_ubyte),
                ('SourceConstantAlpha', ctypes.c_ubyte), ('AlphaFormat', ctypes.c_ubyte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [('biSize', wt.DWORD), ('biWidth', ctypes.c_long),
                ('biHeight', ctypes.c_long), ('biPlanes', wt.WORD),
                ('biBitCount', wt.WORD), ('biCompression', wt.DWORD),
                ('biSizeImage', wt.DWORD), ('biXPelsPerMeter', ctypes.c_long),
                ('biYPelsPerMeter', ctypes.c_long), ('biClrUsed', wt.DWORD),
                ('biClrImportant', wt.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [('bmiHeader', BITMAPINFOHEADER), ('bmiColors', wt.DWORD * 3)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [('cbSize', wt.DWORD),
                ('rcMonitor', wt.RECT),
                ('rcWork', wt.RECT),
                ('dwFlags', wt.DWORD)]


MONITOR_DEFAULTTONEAREST = 2


class WNDCLASSW(ctypes.Structure):
    _fields_ = [('style', ctypes.c_uint), ('lpfnWndProc', WNDPROC),
                ('cbClsExtra', ctypes.c_int), ('cbWndExtra', ctypes.c_int),
                ('hInstance', wt.HINSTANCE), ('hIcon', wt.HICON),
                ('hCursor', wt.HANDLE), ('hbrBackground', wt.HBRUSH),
                ('lpszMenuName', wt.LPCWSTR), ('lpszClassName', wt.LPCWSTR)]


def dpi_scale():
    try:
        dc = user32.GetDC(None)
        dpi = gdi32.GetDeviceCaps(dc, 88)          # LOGPIXELSX
        user32.ReleaseDC(None, dc)
        if dpi:
            return max(1.0, dpi / 96.0)
    except Exception:
        pass
    return 1.0


# ---------------------------------------------------------------- 绘制
NORMAL = {
    'bg': (252, 253, 252, 255),         # #fcfdfc
    'border': (221, 227, 223, 255),     # #dde3df
    'divider': (233, 237, 234, 255),    # #e9edea
    'label': (107, 119, 111, 255),      # #6b776f
    'pct': (37, 55, 44, 255),           # #25372c
}
DARK = {
    'bg': (38, 41, 44, 255),            # #26292c
    'border': (70, 75, 79, 255),        # #464b4f
    'divider': (68, 73, 76, 255),       # #44494c
    'label': (191, 200, 194, 255),      # #bfc8c2
    'pct': (237, 242, 238, 255),        # #edf2ee
}
COLOR_GREEN = (36, 133, 102)            # #248566
COLOR_AMBER = (194, 138, 34)            # #c28a22
COLOR_RED = (224, 90, 85)               # #e05a55
GRAY = (152, 162, 173)                  # #98a2ad
COLOR_GREEN_D = (76, 175, 142)
COLOR_AMBER_D = (239, 159, 39)
COLOR_RED_D = (226, 90, 85)

# ---------- 半透明材质 ----------
# 两款：原始 = 纯平涂；磨砂 = 平涂 + 顶部往下衰减的高光（模拟受光面，多一点质感）。
# 不透明度统一由 render(opacity=...) 控制，100 = 完全不透明、数值越小越透，
# 桌面通过分层窗口的 alpha 自然透出来，不需要抓屏。
# （原先抓背后屏幕做高斯模糊的「玻璃」款已按需求移除）
FROST = {
    False: dict(tint=(255, 255, 255), sheen=0.38, border=(255, 255, 255, 198)),
    True: dict(tint=(34, 37, 41), sheen=0.16, border=(255, 255, 255, 58)),
}
STYLES = ('plain', 'frost')

# 逻辑尺寸（会乘 dpi_scale）。横版：两节并排，左 5h、右 week。
# 基准画布 124x74，所有坐标都是这个基准下的绝对值，再乘 k 等比缩放，
# 所以改 DEFAULT_SIZE 只改大小、不改比例。
DEFAULT_SIZE = (124, 74)
BASE_W = 124.0
BASE_H = 74.0

# 卡片尺寸档位（逻辑像素，宽高比固定 124:74；乘 dpi 后即物理像素）
CARD_SIZES = [('小', (104, 62)), ('中', (124, 74)), ('大', (152, 90))]

# 卡片位图的实际画布比"看得见的外框"大一圈：可见外框 112x62 画在 124x74 的画布上，
# 上下各留 5、左右各留 6 的透明边（给描边/投影的余量用的）。
CARD_BODY_H_RATIO = 62.0 / 74.0
DEFAULT_CARD_IDX = 1
SS = 4                                  # 超采样倍数，缩回后线条和字才有抗锯齿

BODY = (6, 5, 112, 62)                  # x, y, w, h
BODY_R = 26
# 两节等宽 56，节中心分别在 body 内 28 / 84（绝对 34 / 90），分隔线正好在界上
CELL_W = 56.0
CELL_DX = (0, 56)
TOP = 12                                # 每节内容的纵向起点
DIV_X, DIV_Y0, DIV_Y1 = 62, 18, 50

# 电池图形（逻辑 px，相对节中心与 TOP 定位）
# 长度 30 → 25：原来 30x11 比例 2.7 显得细长，25x11 ≈ 2.3 更接近真电池
BAT_W, BAT_H = 25.0, 11.0
BAT_LW = 1.2                            # 外壳描边
BAT_INSET = 2.0                         # 填充与外壳的内缩
BAT_GAP, BAT_ELEC_W = 1.2, 2.0          # 电极与外壳的缝 / 电极宽度
BAT_GROUP_W = BAT_W + BAT_GAP + BAT_ELEC_W   # 含电极的总宽，用来居中
BAT_IN = BAT_W - BAT_INSET * 2          # 填充可用宽度
BAT_Y = 17.0                            # 电池顶部相对 TOP 的偏移

# ---------- 贴边收起：屏幕内只留一条细条（逻辑像素，物理值 = ×dpi_scale）
TIP_W = 6.0                             # 露出屏幕的细条宽度
TIP_SEGS = 3                            # 细条被两道缝切成几段
TIP_GAP = 1.5                           # 缝宽（透明，透出桌面）
SNAP_LOGICAL = 26.0                     # 松手时距边小于这个值就吸附
LEAVE_DELAY = 0.5                       # 鼠标移开后多久收回（秒）
ANIM_SEC = 0.18                         # 滑动动画时长（秒）
EDGES = ('left', 'right', 'top', 'bottom')


def level_color(pct, dark):
    if pct is None:
        return GRAY
    if pct < 20:
        return COLOR_RED_D if dark else COLOR_RED
    if pct <= 50:
        return COLOR_AMBER_D if dark else COLOR_AMBER
    return COLOR_GREEN_D if dark else COLOR_GREEN


def _worst(pcts):
    """细条颜色取两个窗口里更紧张的那个"""
    vals = [p for p in pcts if p is not None]
    return min(vals) if vals else None


def _font(size_px, bold):
    from PIL import ImageFont
    path = 'C:/Windows/Fonts/msyhbd.ttc' if bold else 'C:/Windows/Fonts/msyh.ttc'
    px = max(1, int(round(size_px)))
    try:
        return ImageFont.truetype(path, px)
    except Exception:
        return ImageFont.truetype('C:/Windows/Fonts/simhei.ttf', px)


def _fit_font(d, text, base_px, max_w, bold=False):
    """顶部那行字按格子宽度自动缩字号。

    格子只有 56 逻辑 px，「明01:30」这类更长的串直接按 10px 画会溢出到
    隔壁格、甚至压到卡片圆角上。先量宽度，放不下就按 6% 步长降字号，
    最低到 60%，保证任何串都留在自己那一格里。
    """
    px = base_px
    floor = base_px * 0.6
    for _ in range(12):
        f = _font(px, bold)
        try:
            if d.textlength(text, font=f) <= max_w:
                return f
        except Exception:
            return f
        if px <= floor:
            break
        px = max(floor, px - base_px * 0.06)
    return _font(px, bold)


def _tip_rect(edge, W, H):
    """收起态那条细条在画布里的矩形（逻辑 px）。

    贴边收起时窗口整体滑出屏幕，屏内只剩画布**朝屏内那一侧**的一小段，
    所以细条要画在画布贴着屏幕边界的那一端：
        右边贴屏 → 窗口左边露在屏内，条在画布最左（x 从 0 起）
        左边贴屏 → 条在画布最右
        下边贴屏 → 条在画布最上；上边贴屏 → 条在画布最下
    条的长度与卡片可见外框对齐（上下各缩 5/74、左右各缩 6/124），
    所以展开/收起切换时它跟卡片边缘是齐的。
    """
    k = W / BASE_W
    bx, by, bw, bh = (v * k for v in BODY)
    if edge == 'left':
        return (W - TIP_W, by, float(W), by + bh)
    if edge == 'right':
        return (0.0, by, TIP_W, by + bh)
    if edge == 'top':
        return (bx, H - TIP_W, bx + bw, float(H))
    return (bx, 0.0, bx + bw, TIP_W)


def _seg_box(box, i, n, gap, vert):
    """把细条沿长度方向切成 n 段，返回第 i 段的矩形"""
    x0, y0, x1, y1 = box
    length = (y1 - y0) if vert else (x1 - x0)
    seg = (length - gap * (n - 1)) / float(n)
    a = (y0 if vert else x0) + i * (seg + gap)
    b = a + seg
    return (x0, a, x1, b) if vert else (a, y0, b, y1)


def _edge_alpha(col, op):
    """描边的不透明度：跟着卡片降，但保底 35%，透明卡也需要一圈轮廓"""
    return tuple(col[:3]) + (int(round(col[3] * (0.35 + 0.65 * op))),)


def _frost_plate(scale, size, cfg, op):
    """磨砂底板（物理像素，卡片外全透明）：平涂底色 + 顶部高光。

    op 是 0~1 的不透明度，直接作用在底色与高光上——分层窗口是 per-pixel alpha，
    填多少 alpha 就透出多少桌面，不需要抓屏也不需要模糊。
    高光与底色都按卡片圆角裁掉，否则会糊到透明留白里。
    """
    from PIL import Image, ImageDraw

    W, H = size
    px = (max(1, int(round(W * scale))), max(1, int(round(H * scale))))
    k = W / BASE_W
    s = k * scale
    bx, by, bw, bh = (v * s for v in BODY)
    radius = max(1.0, BODY_R * s)

    plate = Image.new('RGBA', px, (0, 0, 0, 0))

    tint = Image.new('RGBA', px, tuple(cfg['tint']) + (255,))
    ta = Image.new('L', px, 0)
    ImageDraw.Draw(ta).rounded_rectangle([bx, by, bx + bw, by + bh],
                                         radius=radius, fill=int(round(255 * op)))
    tint.putalpha(ta)
    plate = Image.alpha_composite(plate, tint)

    if cfg['sheen'] and op > 0.05:
        # 顶部往下衰减的白色高光，模拟受光面；浓度跟着不透明度一起降
        gh = max(2, int(round(bh * 0.62)))
        grad = Image.new('L', (1, gh))
        for y in range(gh):
            t = y / max(1, gh - 1)
            grad.putpixel((0, y), int(255 * cfg['sheen'] * op * (1 - t) ** 1.5))
        full = Image.new('L', px, 0)
        full.paste(grad.resize((px[0], gh)), (0, int(round(by))))
        mask = Image.new('L', px, 0)
        ImageDraw.Draw(mask).rounded_rectangle([bx, by, bx + bw, by + bh],
                                               radius=radius, fill=255)
        sheen = Image.new('RGBA', px, (255, 255, 255, 0))
        sheen.putalpha(Image.composite(full, Image.new('L', px, 0), mask))
        plate = Image.alpha_composite(plate, sheen)
    return plate


def render(data, scale=1.0, dark=False, size=None, handle_edge=None,
           style=None, opacity=100):
    """data = {'reset5': '19:30', 'date': '11/9', 'p1': 68, 'p2': 80}

    横版：左边一节 5 小时窗口（上方重置钟点），右边一节每周窗口（上方重置日期）。
    电池、描边、填充的内缩全部按 k 等比，改大小不会把电池拉成一根细条。

    handle_edge 不为空时画的是「收起态」：整张画布只有贴边那条细条，
    其余全透明。画布尺寸与展开态完全一致，所以窗口尺寸不用变、滑动动画
    只挪位置，不会闪。

    贴边收起时画的是「刻度细条」：一条 6 逻辑 px 宽、与卡片等高的小条，
    用两道透明缝切成三段，像仪表上的刻度。颜色取两个窗口里更紧张的那个。
    """
    from PIL import Image, ImageDraw

    c = dict(DARK if dark else NORMAL)
    W, H = size or DEFAULT_SIZE
    k = W / BASE_W
    S = scale * SS
    img = Image.new('RGBA', (int(round(W * S)), int(round(H * S))), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 不透明度：100 = 完全不透明，越小越透（桌面从分层窗口的 alpha 里透出来）
    try:
        op = max(0.0, min(1.0, float(opacity) / 100.0))
    except Exception:
        op = 1.0
    frost_cfg = FROST.get(bool(dark)) if style == 'frost' else None

    def sc(v):
        return v * k * S

    def ln(v):
        return max(1, int(round(v)))

    if handle_edge in EDGES:
        pct = _worst([data.get('p1'), data.get('p2')])
        col = level_color(pct, dark) + (255,)
        box = _tip_rect(handle_edge, W, H)
        vert = handle_edge in ('left', 'right')
        thick = (box[2] - box[0]) if vert else (box[3] - box[1])
        r = max(1.0, thick * S / 2.0)          # 整条是个胶囊
        # 先画整条胶囊，再用"擦除"把两道缝挖成透明（缝里透出桌面，
        # 而不是把条切成一截截独立的小胶囊——那样缝看起来太宽）
        x0, y0, x1, y1 = (v * S for v in box)
        d.rounded_rectangle([x0, y0, x1, y1], radius=r, fill=col)
        gw = max(1, int(round(TIP_GAP * S)))
        for i in range(1, TIP_SEGS):
            sb = _seg_box(box, i, TIP_SEGS, 0.0, vert)
            p = (sb[1] if vert else sb[0]) * S
            if vert:
                d.rectangle([x0, p - gw / 2.0, x1, p + gw / 2.0], fill=(0, 0, 0, 0))
            else:
                d.rectangle([p - gw / 2.0, y0, p + gw / 2.0, y1], fill=(0, 0, 0, 0))
        return img.resize((int(round(W * scale)), int(round(H * scale))), Image.LANCZOS)

    # 四边留 6px 透明边给柔光投影
    bx, by, bw, bh = (sc(v) for v in BODY)
    for spread, alpha in ((4, 3), (3, 5), (2, 7), (1, 9)):
        s = sc(spread)
        d.rounded_rectangle([bx - s, by - s + sc(2), bx + bw + s, by + bh + s + sc(2)],
                            radius=sc(BODY_R) + s, fill=(0, 0, 0, alpha))

    if frost_cfg:
        # 字色跟的是"外观"（浅色外观=深字、深色外观=白字），不是材质本身：
        # 浅色外观配白字会在白底页面上直接看不见，这是之前踩过的坑。
        if dark:
            c['label'] = (255, 255, 255, 198)
            c['pct'] = (255, 255, 255, 255)
            c['divider'] = (255, 255, 255, 46)
            bat_dark = True
        else:
            c['label'] = (74, 86, 78, 255)
            c['pct'] = (30, 42, 35, 255)
            c['divider'] = (0, 0, 0, 24)
            bat_dark = False
        plate = _frost_plate(scale, (W, H), frost_cfg, op)
        img.alpha_composite(plate.resize(img.size, Image.BICUBIC))
        edge_col = _edge_alpha(frost_cfg['border'], op)
        div_col = c['divider']
    else:
        # 原始款：不透明度直接作用在底色上；描边只跟着降到 35%，透一点也有轮廓
        bat_dark = dark
        bg = tuple(c['bg'][:3]) + (int(round(255 * op)),)
        d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=sc(BODY_R), fill=bg)
        edge_col = _edge_alpha(c['border'], op)
        div_col = tuple(c['divider'][:3]) + (int(round(c['divider'][3] * op)),)

    # 描边、分隔线、电池、文字先画在一张透明覆盖层上，最后整体 alpha_composite。
    # 不能直接往卡片上画：ImageDraw 对 RGBA 图是"写像素"而不是"混合"，
    # 带 alpha 的颜色会把底色连 alpha 一起覆盖掉——半透明卡片上分隔线会变成
    # 一条亮白线，中间还会露出桌面。
    over = Image.new('RGBA', img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=sc(BODY_R),
                        outline=edge_col, width=ln(sc(1)))
    d.line([(sc(DIV_X), sc(DIV_Y0)), (sc(DIV_X), sc(DIV_Y1))],
           fill=div_col, width=ln(sc(1)))

    def cell(dx, label, pct):
        col = level_color(pct, bat_dark)
        cx = BODY[0] + dx + CELL_W / 2.0      # 节中心（两节等宽 56，中轴 34 / 90）

        # 电池组（外壳 + 电极）整体以 cx 为轴居中。
        # 以前按"节左边界 + 固定偏移"画，含电极后中轴比文字偏右 6.5px，
        # 而且右列电极只剩 3px 就顶到卡片边——现在全部相对 cx 算。
        xb = cx - BAT_GROUP_W / 2.0
        d.rounded_rectangle([sc(xb), sc(TOP + BAT_Y), sc(xb + BAT_W), sc(TOP + BAT_Y + BAT_H)],
                            radius=sc(2.5), outline=col, width=ln(sc(BAT_LW)))
        d.rounded_rectangle([sc(xb + BAT_W + BAT_GAP), sc(TOP + BAT_Y + (BAT_H - 5) / 2.0),
                             sc(xb + BAT_GROUP_W), sc(TOP + BAT_Y + (BAT_H + 5) / 2.0)],
                            radius=sc(1), fill=col)
        if pct is not None and pct > 0:
            fwd = sc(BAT_IN) * min(100.0, max(0.0, pct)) / 100.0
            d.rounded_rectangle([sc(xb + BAT_INSET), sc(TOP + BAT_Y + BAT_INSET),
                                 sc(xb + BAT_INSET) + fwd, sc(TOP + BAT_Y + BAT_H - BAT_INSET)],
                                radius=sc(1), fill=col)
        elif pct is None:
            d.line([(sc(cx - 4), sc(TOP + BAT_Y + BAT_H / 2.0)),
                    (sc(cx + 4), sc(TOP + BAT_Y + BAT_H / 2.0))],
                   fill=col, width=ln(sc(1)))

        # 顶部那行统一是「重置时间」：左节 5h 的钟点、右节 week 的日期。
        # 以前左节放的是倒计时（还剩几小时几分），和右节语义不一致，
        # 容易被读成"左边也是重置时间"，反而算错窗口。倒计时挪到右键明细。
        label = str(label)
        d.text((sc(cx), sc(TOP + 7)), label,
               font=_fit_font(d, label, sc(10), sc(CELL_W - 10), False),
               fill=c['label'], anchor='mm')
        d.text((sc(cx), sc(TOP + 38.5)), '—' if pct is None else f'{pct:.0f}%',
               font=_font(sc(12), True), fill=c['pct'], anchor='mm')

    cell(CELL_DX[0], data.get('reset5') or '--:--', data.get('p1'))
    cell(CELL_DX[1], data.get('date') or '--/--', data.get('p2'))

    # 透明度低于 100% 时，文字/电池/描边也一起淡下去（保底 55%）。
    # 只让卡片表面变透明的话，白色卡片在白色页面上永远看不出变化——
    # 白色的半透膜叠在白纸上混出来仍是白，实际观感就是"调了没反应"。
    # 整卡一起淡，白底上也能明显看出变轻；最低档仍保留七成以上的实心度，够读。
    if op < 0.999:
        fade = 0.55 + 0.45 * op
        over.putalpha(over.getchannel('A').point(
            lambda v: int(round(v * fade))))

    img.alpha_composite(over)
    return img.resize((int(round(W * scale)), int(round(H * scale))), Image.LANCZOS)


def _inside(pt, rect, pad=0):
    if not rect:
        return False
    return (rect[0] - pad <= pt[0] <= rect[2] + pad
            and rect[1] - pad <= pt[1] <= rect[3] + pad)


# ---------------------------------------------------------------- 窗口
class FloatWindow:
    def __init__(self, menu_provider=None, on_move=None, on_dock=None,
                 handle_provider=None, size=None, theme_provider=None):
        self.menu_provider = menu_provider
        self.theme_provider = theme_provider      # () -> bool 深色，菜单跟着卡片外观走
        self.on_move = on_move
        self.on_dock = on_dock
        self.handle_provider = handle_provider
        self.size = size or DEFAULT_SIZE
        self.scale = dpi_scale()
        self.pos = None
        self._hwnd = None
        self._thread = None
        self._proc = None
        self._ready = threading.Event()
        self._img = None            # 当前真正显示在窗口上的图
        self._card_img = None       # 展开态（卡片）的图，watcher 持续更新
        self._handle_img = None     # 收起态（细条）的图
        self._moving = False        # 系统原生拖动中
        self._animating = False
        self._anim_gen = 0
        self._menu_open = False
        self._handlers = {}
        self._visible = False
        self._alive = False
        self._hover_thread = None
        self._leave_at = None
        # 拖动收起后光标常常正停在细条上，若立刻允许悬停唤出会出现
        # 「松手→收起→马上又弹回来」。这里要求光标先离开一次才允许唤出。
        self._hover_armed = True

        self.dock_edge = None
        self.dock_offset = 0
        self.expanded = True
        self._expanded_pos_cache = None

        self._margin = max(2, int(round(6 * self.scale)))

    # ---------- 几何 ----------
    def _px(self):
        return (int(round(self.size[0] * self.scale)),
                int(round(self.size[1] * self.scale)))

    def _work_rect(self, cx=None, cy=None):
        """窗口所在显示器的可用区域（已扣掉任务栏）"""
        w, h = self._px()
        if cx is None:
            cx = (self.pos[0] if self.pos else 0) + w // 2
            cy = (self.pos[1] if self.pos else 0) + h // 2
        try:
            pt = wt.POINT(int(cx), int(cy))
            mon = user32.MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST)
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            if mon and user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
                r = mi.rcWork
                return (int(r.left), int(r.top), int(r.right), int(r.bottom))
        except Exception:
            pass
        return (0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))

    def _body_insets(self):
        """卡片可见外框相对窗口左上角的物理偏移：(左, 上, 右, 下)。

        画布比可见外框大一圈（BODY 是 6,5,112,62 放在 124x74 的画布上），
        而且这圈留白会随卡片档位按 k 缩放。以前用固定的 `6*dpi` 当留白，
        在大档位下卡片右缘就会多探出屏幕 1~2px，边框被屏幕边切掉一条。
        """
        k = self.size[0] / BASE_W
        s = k * self.scale
        return (BODY[0] * s, BODY[1] * s,
                (BODY[0] + BODY[2]) * s, (BODY[1] + BODY[3]) * s)

    def _expanded_pos(self, edge, offset):
        """贴边展开时窗口应处的位置：让卡片主体（不含投影留白）正好挨着边"""
        l, t, r, b = self._work_rect()
        li, ti, ri, bi = self._body_insets()
        if edge == 'left':
            return (l - int(round(li)), int(offset))
        if edge == 'right':
            return (r - int(round(ri)), int(offset))
        if edge == 'top':
            return (int(offset), t - int(round(ti)))
        return (int(offset), b - int(round(bi)))

    def _collapsed_pos(self, edge, expanded):
        """收起时把窗口大半推到屏幕外，只留细条那一段在屏内"""
        l, t, r, b = self._work_rect()
        w, h = self._px()
        T = self._handle_t()
        if edge == 'left':
            return (l - (w - T), int(expanded[1]))
        if edge == 'right':
            return (r - T, int(expanded[1]))
        if edge == 'top':
            return (int(expanded[0]), t - (h - T))
        return (int(expanded[0]), b - T)

    def _handle_t(self):
        """收起后屏幕内露出的宽度（物理像素）= 细条宽度。

        细条画在画布坐标里，所以要按 k×dpi 换算，不能直接用 dpi：
        用 dpi 会算成 10px，而细条实际只有 8.8px，末段 1px 是透明的，
        贴边时看起来就有一条缝。
        """
        k = self.size[0] / BASE_W
        return max(4, int(round(TIP_W * k * self.scale)))

    def _tip_rect_screen(self):
        """细条在屏幕上的矩形（用于悬停判定）。

        别用「窗口位置 + 画布内偏移」去算：收起时窗口大半在屏幕外，
        直接按「贴着哪条边、露出多宽」推更稳。
        """
        if not self.dock_edge or not self.pos:
            return None
        l, t, r, b = self._work_rect()
        T = self._handle_t()
        k = self.size[0] / BASE_W
        bh = BODY[3] * k * self.scale
        by = BODY[1] * k * self.scale
        x, y = self.pos
        if self.dock_edge == 'left':
            return (l, int(y + by), l + T, int(y + by + bh))
        if self.dock_edge == 'right':
            return (r - T, int(y + by), r, int(y + by + bh))
        bw = BODY[2] * k * self.scale
        bx = BODY[0] * k * self.scale
        # 贴上面：窗口往上滑出，屏内剩画布**底部**那一条 → 屏幕顶部
        # 贴下面：窗口往下滑出，屏内剩画布**顶部**那一条 → 屏幕底部
        if self.dock_edge == 'top':
            return (int(x + bx), t, int(x + bx + bw), t + T)
        return (int(x + bx), b - T, int(x + bx + bw), b)

    def _card_rect(self):
        """卡片主体（去掉投影留白）在屏幕上的矩形"""
        if not self.pos:
            return None
        li, ti, ri, bi = self._body_insets()
        return (self.pos[0] + int(round(li)), self.pos[1] + int(round(ti)),
                self.pos[0] + int(round(ri)), self.pos[1] + int(round(bi)))

    def set_sizes(self, size=None, card_img=None):
        """切换卡片档位。

        分层窗口的物理尺寸由 UpdateLayeredWindow 的位图尺寸决定，所以换档位
        必须同时递进一张**新尺寸**的卡片图（card_img）：只改 self.size 而拿
        旧图去 blit，窗口会被旧图拉回旧尺寸，看起来就是"尺寸抽了一下"。
        只想要标签更新、尺寸没变时 card_img 传 None 即可。
        """
        old = tuple(self.size)
        if size:
            self.size = (int(size[0]), int(size[1]))
        if card_img is not None:
            self._card_img = card_img
        changed = tuple(self.size) != old
        if not changed and card_img is None:
            return
        self._margin = max(2, int(round(6 * self.scale)))
        if changed:
            # 尺寸变了：先打断进行中的滑动动画，否则它下一帧还会拿旧图 blit
            self._anim_gen += 1
            self._animating = False
            self._handle_img = None      # 细条要按新画布重画
        if self.dock_edge:
            self._expanded_pos_cache = self._expanded_pos(self.dock_edge, self.dock_offset)
            self.pos = (self._expanded_pos_cache if self.expanded
                        else self._collapsed_pos(self.dock_edge, self._expanded_pos_cache))
        elif self.pos:
            self.ensure_reachable()
        if self._hwnd and self.pos:
            w, h = self._px()
            user32.SetWindowPos(self._hwnd, HWND_TOPMOST,
                                int(self.pos[0]), int(self.pos[1]), w, h,
                                SWP_NOZORDER | SWP_NOACTIVATE)
        img = self._card_img if self.expanded else self._handle_image(fresh=True)
        # 位图尺寸决定窗口尺寸：对不上就宁可不画，等主循环送新图来
        if img is not None and img.size == self._px():
            self._img = img
            try:
                self._blit(img)
            except Exception:
                pass

    # ---------- 图片 ----------
    def _card_image(self):
        return self._card_img

    def _handle_image(self, fresh=False):
        if fresh or self._handle_img is None:
            if self.handle_provider:
                try:
                    self._handle_img = self.handle_provider(self.dock_edge)
                except Exception:
                    self._handle_img = None
        return self._handle_img

    # ---------- 贴边 ----------
    def _maybe_dock(self):
        """松手 / 手动贴边时判断：距某条边够近就吸附过去"""
        if not self.pos:
            return False
        w, h = self._px()
        x, y = self.pos
        l, t, r, b = self._work_rect(x + w // 2, y + h // 2)
        snap = int(round(SNAP_LOGICAL * self.scale))
        gaps = ((x - l, 'left'), (r - (x + w), 'right'),
                (y - t, 'top'), (b - (y + h), 'bottom'))
        gap, edge = min(gaps, key=lambda p: p[0])
        if gap > snap:
            return False
        offset = y if edge in ('left', 'right') else x
        if edge in ('left', 'right'):
            offset = min(max(offset, t), max(t, b - h))
        else:
            offset = min(max(offset, l), max(l, r - w))
        self.dock_edge = edge
        self.dock_offset = offset
        self._expanded_pos_cache = self._expanded_pos(edge, offset)
        return True

    def snap_now(self):
        """菜单项：立刻贴到最近的边并收起"""
        if not self._hwnd:
            return
        if self.dock_edge and not self.expanded:
            self.pos = self._expanded_pos_cache or self.pos
            self.expanded = True
            if self._card_img is not None:
                self._img = self._card_img
                self._blit(self._card_img)
        if self._maybe_dock():
            self.collapse(notify=True, arm=False)

    def undock(self):
        """菜单项：取消贴边，停在原地当自由悬浮窗"""
        if not self._hwnd:
            return
        target = self._expanded_pos_cache if self.dock_edge else self.pos
        self.dock_edge = None
        self.expanded = True
        self._leave_at = None
        if target:
            self.pos = target
        if self._card_img is not None:
            self._img = self._card_img
            self._blit(self._card_img)
        self.ensure_reachable()
        if self.on_move:
            try:
                self.on_move(self.pos)
            except Exception:
                pass
        self._notify_dock(None)

    def _notify_dock(self, payload):
        if self.on_dock:
            try:
                self.on_dock(payload)
            except Exception:
                pass

    def expand(self):
        if not self.dock_edge or self.expanded:
            return
        self.expanded = True
        self._leave_at = None
        self._hover_armed = True
        target = self._expanded_pos_cache or self._expanded_pos(self.dock_edge, self.dock_offset)
        img = self._card_image()
        self._animate(target, start_img=img, end_img=None)

    def collapse(self, notify=False, arm=True):
        if not self.dock_edge or not self.expanded:
            return
        base = self._expanded_pos_cache or self.pos
        self.expanded = False
        self._leave_at = None
        self._hover_armed = arm
        target = self._collapsed_pos(self.dock_edge, base)
        end = self._handle_image(fresh=True)
        self._animate(target, start_img=None, end_img=end)
        if notify:
            self._notify_dock({'edge': self.dock_edge, 'offset': self.dock_offset})

    # ---------- 动画 ----------
    def _move_to(self, x, y):
        if not self._hwnd:
            return
        x, y = int(round(x)), int(round(y))
        user32.SetWindowPos(self._hwnd, HWND_TOPMOST, x, y, 0, 0,
                            SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
        self.pos = (x, y)

    def _settle_dock(self):
        """弹菜单会打断滑动动画，窗口可能停在半路。菜单关掉后把它补到位。"""
        if not self.dock_edge or not self._hwnd:
            return
        if self.expanded:
            target = self._expanded_pos_cache or self.pos
            img = self._card_img
        else:
            target = self._collapsed_pos(self.dock_edge,
                                         self._expanded_pos_cache or self.pos)
            img = self._handle_image(fresh=True)
        if not target:
            return
        if not self.pos or (int(self.pos[0]), int(self.pos[1])) != (int(target[0]), int(target[1])):
            self._move_to(*target)
            if img is not None:
                self._img = img
                self._blit(img)

    def _animate(self, target, start_img=None, end_img=None, dur=ANIM_SEC):
        """只挪窗口位置，不重画位图（重画才慢）。缓出曲线，跟手且不弹。"""
        self._anim_gen += 1
        gen = self._anim_gen
        self._animating = True
        start = tuple(self.pos or target)
        frames = max(2, int(round(dur / 0.012)))

        def run():
            try:
                if start_img is not None:
                    self.pos = start
                    self._img = start_img
                    self._blit(start_img)
                for i in range(1, frames + 1):
                    if gen != self._anim_gen or not self._hwnd:
                        return
                    p = i / frames
                    e = 1 - (1 - p) ** 3
                    self._move_to(start[0] + (target[0] - start[0]) * e,
                                  start[1] + (target[1] - start[1]) * e)
                    time.sleep(dur / frames)
                if gen != self._anim_gen:
                    return
                self._move_to(*target)
                if end_img is not None:
                    self._img = end_img
                    self._blit(end_img)
            except Exception:
                pass
            finally:
                if gen == self._anim_gen:
                    self._animating = False

        threading.Thread(target=run, daemon=True).start()

    # ---------- 悬停唤出 / 移开收回 ----------
    def _hover_loop(self):
        """窗口滑到屏幕外就收不到 WM_MOUSEMOVE 了，只能主动轮询光标。
        80ms 一次，只比对矩形，开销可以忽略。"""
        pad = max(6, int(round(10 * self.scale)))
        while self._alive:
            time.sleep(0.08)
            try:
                if (not self._visible or not self._hwnd or not self.dock_edge
                        or self._moving or self._menu_open or self._animating):
                    continue
                pt = wt.POINT()
                if not user32.GetCursorPos(ctypes.byref(pt)):
                    continue
                cur = (pt.x, pt.y)
                if not self.expanded:
                    if not _inside(cur, self._tip_rect_screen(), pad):
                        self._hover_armed = True      # 真的离开了，才允许下次唤出
                    elif self._hover_armed:
                        self.expand()
                    continue
                if _inside(cur, self._card_rect()):
                    self._leave_at = None
                else:
                    now = time.time()
                    if self._leave_at is None:
                        self._leave_at = now
                    elif now - self._leave_at >= LEAVE_DELAY:
                        self.collapse()
            except Exception:
                pass

    # ---------- 拖动 ----------
    def _native_drag(self, hwnd):
        """把拖动整段交给系统：SendMessage 内部跑的是 Windows 自己的移动循环，
        拖动期间一次都不回 Python，所以不会因为 GIL 排队而掉帧、也就不再"拖不动"。
        代价是拖动中不响应右键（松手后照常）。"""
        self._moving = True
        try:
            user32.ReleaseCapture()
            user32.SendMessageW(hwnd, WM_NCLBUTTONDOWN, HTCAPTION, 0)
        except Exception:
            pass
        finally:
            self._moving = False
        self._settle_after_drag()

    def _settle_after_drag(self):
        """松手后：读回真实位置 → 贴边就收起，否则拉回屏幕内并记住位置"""
        if not self._hwnd:
            return
        try:
            r = wt.RECT()
            if user32.GetWindowRect(self._hwnd, ctypes.byref(r)):
                self.pos = (int(r.left), int(r.top))
        except Exception:
            pass
        if self._maybe_dock():
            self.expanded = True          # 先当作展开态，再滑出去收起
            self._img = self._card_img or self._img
            self.collapse(notify=True, arm=False)
            return
        self.dock_edge = None
        self.expanded = True
        self.ensure_reachable()
        if self._img is not None:
            try:
                self._blit(self._img)
            except Exception:
                pass
        if self.on_move and self.pos:
            try:
                self.on_move(self.pos)
            except Exception:
                pass
        self._notify_dock(None)

    # ---------- 内部 ----------
    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_LBUTTONDOWN:
                if not self._moving:
                    if self.dock_edge and not self.expanded:
                        self.expand()          # 收起时点细条 = 唤出，不拖动
                    else:
                        self._native_drag(hwnd)
                return 0
            if msg == WM_RBUTTONUP:
                self._show_menu()
                return 0
            if msg == WM_MENU_PICK or msg == WM_MENU_CLOSED:
                self._on_menu_msg(msg)
                return 0
            if msg == WM_SETCURSOR:
                # 悬停时显示四向箭头，暗示这块可以拖
                hcur = user32.LoadCursorW(None, ctypes.c_void_p(IDC_SIZEALL))
                if hcur:
                    user32.SetCursor(hcur)
                    return 1
            if msg == WM_COMMAND:
                fn = self._handlers.get(int(wparam) & 0xFFFF)
                if fn:
                    fn()
                return 0
            if msg == WM_NCHITTEST:
                return HTCLIENT
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            pass
        try:
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
        except Exception:
            return 0

    def _show_menu(self):
        """右键菜单交给自绘实现（menucanvas 排版 + menu_win 窗口）。

        关键：**不能在这里等菜单关闭**。这段代码跑在悬浮窗的窗口过程里，
        一旦阻塞，整个窗口在菜单开着期间都「无响应」——Windows 会把鼠标换成
        忙/加载光标，卡片也拖不动、不刷新。所以用 popup_async：立刻返回，
        选完的动作由 menu_win 发 WM_MENU_PICK 回到这里执行。
        """
        if not self.menu_provider:
            return
        items = self.menu_provider()
        dark = False
        try:
            if self.theme_provider:
                dark = bool(self.theme_provider())
        except Exception:
            pass
        self._menu_open = True
        self._anim_gen += 1          # 弹菜单时打断正在跑的滑动
        self._animating = False
        ok = False
        try:
            import menu_win
            if menu_win.on_error is None:
                menu_win.on_error = lambda m: None
            ok = menu_win.popup_async(items, self._hwnd, dark=dark)
        except Exception as e:
            try:
                import menu_win
                menu_win._err('float popup', e)
            except Exception:
                pass
        if not ok:
            self._menu_open = False
            self._leave_at = None
            self._settle_dock()

    def _on_menu_msg(self, msg):
        """菜单线程收工时回到这里：先复位状态，再执行选中的动作。

        动作在当前线程（悬浮窗 UI 线程）执行，和以前一样，不存在跨线程动窗口。
        """
        act = None
        try:
            import menu_win
            if msg == WM_MENU_PICK:
                act = menu_win.take_pick(self._hwnd)
        except Exception:
            pass
        self._menu_open = False
        self._leave_at = None
        self._settle_dock()
        if act:
            try:
                act()
            except Exception:
                pass

    @staticmethod
    def _premultiply(img):
        """把直通 alpha 转成预乘 alpha，UpdateLayeredWindow 唯一接受的格式。

        Windows 拿到位图后直接按「已乘过 alpha」去做 SRC_OVER 相加：
            屏幕 = 源RGB + 目标RGB × (1 - 源A)
        如果源 RGB 没乘 alpha，半透明白 (255,255,255,128) 会被当成
        「亮度 255、覆盖度 50%」叠上去，混出来的值远大于正确结果并被截断到 255，
        于是白卡在彩色壁纸上依然是一块死白 —— 看起来就像透明度根本没生效。
        黑色元素（投影 0,0,0 与文字）乘不乘都是 0，所以这个 bug 只在
        白色/浅色卡片上肉眼可见，这也是之前一直没被发现的原因。
        """
        from PIL import Image, ImageChops

        img = img.convert('RGBA')
        a = img.getchannel('A')
        lo, hi = a.getextrema()
        if lo == 255:                      # 全不透明，无需处理
            return img
        rgb = img.convert('RGB')
        mul = Image.merge('RGB', (a, a, a))
        rgb = ImageChops.multiply(rgb, mul)
        return Image.merge('RGBA', (*rgb.split(), a))

    def _blit(self, img):
        if not self._hwnd:
            return
        w, h = img.size
        hdc_screen = user32.GetDC(None)
        hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
        bi = BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = w
        bi.bmiHeader.biHeight = -h
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = 0
        bits = ctypes.c_void_p()
        bmp = gdi32.CreateDIBSection(hdc_screen, ctypes.byref(bi), 0,
                                     ctypes.byref(bits), None, 0)
        data = self._premultiply(img).tobytes('raw', 'BGRA')
        ctypes.memmove(bits, data, len(data))
        old = gdi32.SelectObject(hdc_mem, bmp)

        pos = wt.POINT(self.pos[0], self.pos[1])
        size = wt.SIZE(w, h)
        src = wt.POINT(0, 0)
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        user32.UpdateLayeredWindow(self._hwnd, hdc_screen, ctypes.byref(pos),
                                   ctypes.byref(size), hdc_mem, ctypes.byref(src),
                                   0, ctypes.byref(blend), ULW_ALPHA)

        gdi32.SelectObject(hdc_mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(None, hdc_screen)

    def default_pos(self):
        sw = user32.GetSystemMetrics(0)
        sh = user32.GetSystemMetrics(1)
        w, h = self._px()
        margin = int(24 * self.scale)
        bottom = int(64 * self.scale)
        return (max(0, sw - w - margin), max(0, sh - h - bottom))

    def clamp_to_workarea(self, x, y, w, h):
        """把窗口位置限制在所在显示器的可用区域内（多显示器 / 换分辨率后旧坐标会跑到屏幕外）"""
        try:
            pt = wt.POINT(int(x + w // 2), int(y + h // 2))
            mon = user32.MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST)
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            if mon and user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
                r = mi.rcWork
                x = min(max(int(x), r.left), max(r.left, r.right - w))
                y = min(max(int(y), r.top), max(r.top, r.bottom - h))
        except Exception:
            pass
        return (int(x), int(y))

    def ensure_reachable(self):
        """松手时把窗口整块收回屏幕内（允许压住任务栏、可以贴四边），
        这样"想放哪放哪"，同时不会出现窗口大半跑到屏幕外、再也抓不回来的情况。
        用 self.pos 而不是 GetWindowRect —— 拖动刚结束时后者常返回旧值。
        贴边状态下不做处理：那时候位置本来就是负的。"""
        if not self._hwnd or not self.pos or self.dock_edge:
            return
        try:
            sw = user32.GetSystemMetrics(0)
            sh = user32.GetSystemMetrics(1)
            w, h = self._px()
            x = min(max(int(self.pos[0]), 0), max(0, sw - w))
            y = min(max(int(self.pos[1]), 0), max(0, sh - h))
            if (x, y) != (int(self.pos[0]), int(self.pos[1])):
                self.pos = (x, y)
                user32.SetWindowPos(self._hwnd, HWND_TOPMOST, x, y, 0, 0,
                                    SWP_NOSIZE | SWP_NOZORDER)
        except Exception:
            pass

    def _run(self):
        hinst = kernel32.GetModuleHandleW(None)
        cls = f'PyFloat_{id(self)}'
        self._proc = WNDPROC(self._wndproc)
        wc = WNDCLASSW()
        wc.style = CS_HREDRAW | CS_VREDRAW
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.lpszClassName = cls
        if not user32.RegisterClassW(ctypes.byref(wc)):
            self._ready.set()
            return
        w, h = self._px()
        if not self.pos:
            self.pos = self.default_pos()
        if not self.dock_edge:
            self.pos = self.clamp_to_workarea(self.pos[0], self.pos[1], w, h)
        self._hwnd = user32.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW,
            cls, 'codex-quota-float', WS_POPUP | WS_VISIBLE,
            self.pos[0], self.pos[1], w, h, None, None, hinst, None)
        if self._hwnd and self._img is not None:
            self._blit(self._img)
        self._ready.set()

        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    # ---------- 对外 ----------
    def start(self, img, pos=None, dock=None):
        """dock = {'edge': 'left'|'right'|'top'|'bottom', 'offset': int} 时直接以收起态启动"""
        self._card_img = img
        self._img = img
        if isinstance(dock, dict) and dock.get('edge') in EDGES:
            self.dock_edge = dock['edge']
            self.dock_offset = int(dock.get('offset') or 0)
            self._expanded_pos_cache = self._expanded_pos(self.dock_edge, self.dock_offset)
            self.expanded = False
            self.pos = self._collapsed_pos(self.dock_edge, self._expanded_pos_cache)
            self._img = self._handle_image(fresh=True) or img
        elif pos:
            self.pos = (int(pos[0]), int(pos[1]))
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(5)
        self._visible = bool(self._hwnd)
        if self._visible and not self._alive:
            self._alive = True
            self._hover_thread = threading.Thread(target=self._hover_loop, daemon=True)
            self._hover_thread.start()
        return self._visible

    def update(self, img):
        self._card_img = img
        # 拖动 / 滑动期间不碰窗口：UpdateLayeredWindow 会带旧坐标把窗口拽回去
        if not self._hwnd or self._moving or self._animating:
            return
        try:
            if self.dock_edge and not self.expanded:
                # 收起态：只换细条颜色，位置不动
                h = self._handle_image(fresh=True)
                if h is not None:
                    self._img = h
                    self._blit(h)
                return
            self._img = img
            self._blit(img)
        except Exception:
            pass

    def show(self):
        if self._hwnd:
            user32.ShowWindow(self._hwnd, 5)     # SW_SHOW
            if self._img is not None:
                self._blit(self._img)
            self._visible = True

    def hide(self):
        if self._hwnd:
            user32.ShowWindow(self._hwnd, 0)     # SW_HIDE
            self._visible = False

    def stop(self):
        self._alive = False
        self._anim_gen += 1
        try:
            if self._hwnd:
                user32.DestroyWindow(self._hwnd)
        except Exception:
            pass
