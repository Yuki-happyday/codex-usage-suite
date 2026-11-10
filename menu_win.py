"""自绘右键菜单的窗口与交互层。

和 menucanvas（只负责画）分开，是因为渲染要能脱离窗口单独出图做预览。

设计上的一个关键取舍：**鼠标捕获只挂在根面板上**。
如果让每个子菜单窗口各自 SetCapture，光标从子菜单移回父菜单时，父窗口收不到
任何鼠标消息，子菜单就会「粘」在屏幕上关不掉。所以这里让根面板独占捕获，
所有鼠标事件都用 GetCursorPos 取屏幕坐标，再按「光标落在哪一层」派发下去，
父子之间的切换就干净了。

另一个关键是**子菜单窗口带 WS_EX_NOACTIVATE**：它只负责显示，不抢焦点。
焦点始终留在根面板上，键盘（Esc / ↑↓ / ←→ / 回车）才不会因为划过一个子菜单
就失效。

对外只有 popup()：
    popup(items, owner, dark, theme, scale) -> callable | None
        items 结构与 TrackPopupMenu 那套兼容，见 menucanvas 顶部说明
        返回选中项的 action（未选返回 None），由调用方执行
"""
import ctypes
import ctypes.wintypes as wt
import threading

import menucanvas as M
import float_win32 as F

user32 = F.user32
gdi32 = F.gdi32
kernel32 = F.kernel32

WS_POPUP = F.WS_POPUP
WS_EX_LAYERED = F.WS_EX_LAYERED
WS_EX_TOPMOST = F.WS_EX_TOPMOST
WS_EX_TOOLWINDOW = F.WS_EX_TOOLWINDOW
WS_EX_NOACTIVATE = 0x08000000
SW_SHOWNOACTIVATE = 4

WM_NULL = 0x0000
WM_DESTROY = 0x0002
WM_PAINT = 0x000F
WM_ERASEBKGND = 0x0014
WM_ACTIVATEAPP = 0x001C
WM_MOUSEACTIVATE = 0x0021
WM_KEYDOWN = 0x0100
WM_TIMER = 0x0113
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_RBUTTONDOWN = 0x0204
WM_NCHITTEST = 0x0084
WM_SETCURSOR = 0x0020
WM_CAPTURECHANGED = 0x0215

VK_ESCAPE, VK_UP, VK_DOWN, VK_RETURN, VK_LEFT, VK_RIGHT = 0x1B, 0x26, 0x28, 0x0D, 0x25, 0x27
HTCLIENT = 1
MA_ACTIVATE = 1
TID_SUB = 1
SUB_DELAY_MS = 180

IDC_ARROW = 32512
IDC_HAND = 32649

# 菜单选完 / 关闭后，往宿主窗口投的两条消息（WM_APP+1 与 WM_APP+2）
WM_MENU_PICK = 0x8001
WM_MENU_CLOSED = 0x8002

CLASS_NAME = 'PyCodexMenu'
_PANELS = {}                # hwnd(int) -> Panel，wndproc 靠这个找实例
_CLASS_READY = [False]
on_error = None             # 外部可注入 log 函数；出错时静默不弹窗，但能留下痕迹


def _err(where, e):
    if on_error:
        try:
            on_error(f'menu {where}: {e!r}')
        except Exception:
            pass


user32.SetTimer.argtypes = [wt.HWND, ctypes.c_size_t, ctypes.c_uint, ctypes.c_void_p]
user32.SetTimer.restype = ctypes.c_size_t
user32.KillTimer.argtypes = [wt.HWND, ctypes.c_size_t]
user32.DestroyWindow.argtypes = [wt.HWND]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.SetFocus.argtypes = [wt.HWND]
user32.SetCapture.argtypes = [wt.HWND]
user32.GetCapture.restype = wt.HWND
user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
user32.GetMonitorInfoW.argtypes = [wt.HANDLE, ctypes.POINTER(F.MONITORINFO)]
user32.MonitorFromPoint.argtypes = [wt.POINT, wt.DWORD]
user32.MonitorFromPoint.restype = wt.HANDLE
user32.CreateWindowExW.restype = wt.HWND
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE,
                                   ctypes.c_void_p]
# 注意：不要给 RegisterClassW 设 argtypes —— tray_win32 用它自己的 WNDCLASSW 结构，
# 一旦把参数类型固定成 float_win32.WNDCLASSW，托盘线程注册窗口类会直接抛 ArgumentError


# ---------------------------------------------------------------- 位图上传
def _blit_layered(hwnd, img, pos):
    """把 RGBA 图贴到分层窗口。必须先预乘 alpha，否则半透明会被截断成死白。"""
    w, h = img.size
    hdc_screen = user32.GetDC(None)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    bi = F.BITMAPINFO()
    bi.bmiHeader.biSize = ctypes.sizeof(F.BITMAPINFOHEADER)
    bi.bmiHeader.biWidth = w
    bi.bmiHeader.biHeight = -h
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = 32
    bi.bmiHeader.biCompression = 0
    bits = ctypes.c_void_p()
    bmp = gdi32.CreateDIBSection(hdc_screen, ctypes.byref(bi), 0,
                                 ctypes.byref(bits), None, 0)
    data = F.FloatWindow._premultiply(img).tobytes('raw', 'BGRA')
    ctypes.memmove(bits, data, len(data))
    old = gdi32.SelectObject(hdc_mem, bmp)

    pt = wt.POINT(int(pos[0]), int(pos[1]))
    size = wt.SIZE(w, h)
    src = wt.POINT(0, 0)
    blend = F.BLENDFUNCTION(F.AC_SRC_OVER, 0, 255, F.AC_SRC_ALPHA)
    user32.UpdateLayeredWindow(hwnd, hdc_screen, ctypes.byref(pt),
                               ctypes.byref(size), hdc_mem, ctypes.byref(src),
                               0, ctypes.byref(blend), F.ULW_ALPHA)
    gdi32.SelectObject(hdc_mem, old)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(None, hdc_screen)


def _cursor():
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


_CURS = {}                  # 'arrow'/'hand' -> HCURSOR，装载一次反复用


def cursor_handle(hand=False):
    """普通箭头 / 手型。

    必须显式给窗口设光标：窗口类 hCursor 为空时，DefWindowProc 收到
    WM_SETCURSOR 什么也不做，光标会停在「上一个窗口留下的那个」——如果那时
    正好是系统的忙/加载光标，就会一直转圈，看着像程序卡死。
    """
    key = 'hand' if hand else 'arrow'
    h = _CURS.get(key)
    if h is None:
        h = user32.LoadCursorW(None, ctypes.c_void_p(IDC_HAND if hand else IDC_ARROW))
        _CURS[key] = h
    return h


def _work_area(x, y):
    mi = F.MONITORINFO()
    mi.cbSize = ctypes.sizeof(F.MONITORINFO)
    try:
        mon = user32.MonitorFromPoint(wt.POINT(int(x), int(y)), F.MONITOR_DEFAULTTONEAREST)
        if mon and user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
            return mi.rcWork
    except Exception:
        pass
    return wt.RECT(0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))


# ---------------------------------------------------------------- 单个面板
class Panel:
    def __init__(self, session, items, owner=None, parent=None, parent_row=None,
                 anchor=None):
        self.session = session
        self.parent = parent
        self.parent_row = parent_row
        self.is_root = parent is None
        self.hwnd = None
        self.hover = None
        self.sub_row = None
        self._pending_sub = None
        self.painter = M.Painter(items, dark=session.dark, theme=session.theme,
                                 scale=session.scale)
        scale = session.scale
        shw = self.painter.shw
        pw, ph = self.painter.pw, self.painter.ph
        self.pw, self.ph, self.shw = pw, ph, shw
        cx, cy = anchor or window_anchor()
        self.pos = self._fit(cx - shw * scale, cy - shw * scale)
        self._create(owner if self.is_root else session.root.hwnd)

    # ---------- 几何
    def _fit(self, x, y):
        """窗口左上角夹进所在屏幕的工作区，别让菜单跑到屏幕外"""
        r = _work_area(x + self.pw // 2, y + self.ph // 2)
        x = min(max(int(round(x)), r.left), max(r.left, r.right - self.pw))
        y = min(max(int(round(y)), r.top), max(r.top, r.bottom - self.ph))
        return (x, y)

    def contains(self, sx, sy):
        return (self.pos[0] <= sx < self.pos[0] + self.pw and
                self.pos[1] <= sy < self.pos[1] + self.ph)

    def content_origin(self):
        """内容框（不含阴影）左上角的屏幕坐标"""
        d = self.shw * self.session.scale
        return self.pos[0] + d, self.pos[1] + d

    def hit_index(self, sx, sy):
        cx, cy = self.content_origin()
        lx = (sx - cx) / self.session.scale
        ly = (sy - cy) / self.session.scale
        return self.painter.hit_index(lx, ly)

    # ---------- 窗口
    def _create(self, owner):
        _ensure_class()
        ex = WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW
        if not self.is_root:
            ex |= WS_EX_NOACTIVATE
        hinst = kernel32.GetModuleHandleW(None)
        self.hwnd = user32.CreateWindowExW(
            ex, CLASS_NAME, 'codex-menu', WS_POPUP,
            self.pos[0], self.pos[1], self.pw, self.ph,
            owner if owner else None, None, hinst, None)
        if not self.hwnd:
            return
        _PANELS[int(self.hwnd)] = self
        self.paint()
        user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)

    def paint(self):
        if not self.hwnd:
            return
        try:
            _blit_layered(self.hwnd, self.painter.image(self.hover, self.sub_row), self.pos)
        except Exception:
            pass

    def destroy(self):
        try:
            user32.KillTimer(self.hwnd, TID_SUB)
        except Exception:
            pass
        if self.hwnd:
            _PANELS.pop(int(self.hwnd), None)
            try:
                user32.DestroyWindow(self.hwnd)
            except Exception:
                pass
            self.hwnd = None

    def focus(self):
        if not self.is_root or not self.hwnd:
            return
        try:
            user32.SetForegroundWindow(self.hwnd)
            user32.SetFocus(self.hwnd)
            user32.SetCapture(self.hwnd)
        except Exception:
            pass

    # ---------- 悬停 / 子菜单定时器
    def set_hover(self, sx, sy):
        idx = self.hit_index(sx, sy)
        if idx == self.hover:
            return
        self.hover = idx
        self.paint()
        self._arm_sub(idx)

    def _arm_sub(self, idx):
        if idx is None or idx >= len(self.painter.rows):
            self._cancel_timer()
            return
        row = self.painter.rows[idx]
        if not row.get('sub'):
            self._cancel_timer()
            return
        self._pending_sub = idx
        # 已经展开了同一个就不必再计时
        if self.sub_row == idx:
            return
        try:
            user32.SetTimer(self.hwnd, TID_SUB, SUB_DELAY_MS, None)
        except Exception:
            pass

    def _cancel_timer(self):
        self._pending_sub = None
        try:
            user32.KillTimer(self.hwnd, TID_SUB)
        except Exception:
            pass

    def _on_timer(self):
        idx = self._pending_sub
        self._pending_sub = None
        try:
            user32.KillTimer(self.hwnd, TID_SUB)
        except Exception:
            pass
        if idx is None:
            return
        sx, sy = _cursor()
        if self.hit_index(sx, sy) == idx:
            self.session.open_sub(self, idx)

    # ---------- 键盘
    def _on_key(self, vk):
        s = self.session
        if vk == VK_ESCAPE:
            s.close()
            return
        if vk == VK_LEFT:
            if not self.is_root:
                s.pop_above(s.stack.index(self.parent))
                self.parent.hover = self.parent_row
                self.parent.paint()
            return
        if vk == VK_RIGHT:
            if self.hover is not None and self.painter.rows[self.hover].get('sub'):
                s.open_sub(self, self.hover)
            return
        if vk in (VK_UP, VK_DOWN):
            self._step(-1 if vk == VK_UP else 1)
            return
        if vk == VK_RETURN and self.hover is not None:
            row = self.painter.rows[self.hover]
            if row.get('sub'):
                s.open_sub(self, self.hover)
            elif row.get('action') and row['enabled']:
                s.finish(row['action'])

    def _step(self, d):
        rows = self.painter.rows
        if not rows:
            return
        cur = self.hover if self.hover is not None else (-1 if d > 0 else len(rows))
        i = cur
        for _ in range(len(rows)):
            i = (i + d) % len(rows)
            if rows[i]['kind'] in ('item', 'sec'):
                self.hover = i
                self.paint()
                self._arm_sub(i)
                return

    # ---------- 消息
    def on_message(self, hwnd, msg, wparam, lparam):
        if msg == WM_PAINT:
            return 0
        if msg == WM_ERASEBKGND:
            return 1
        # 定时器只给「自己按下的那个面板」处理：子菜单也会用它延迟展开下一层，
        # 所以不能像鼠标/键盘那样只让根面板响应
        if msg == WM_TIMER:
            self._on_timer()
            return 0
        if msg == WM_SETCURSOR:
            # 捕获挂在根面板上，所以光标在任何一层移动时这个窗口都会收到
            # WM_SETCURSOR；由它统一按「光标下面是什么行」决定箭头还是手型
            if (int(lparam) & 0xFFFF) != HTCLIENT:
                return None
            self.session.update_cursor()
            return 1
        if msg == WM_CAPTURECHANGED:
            # 被别的窗口抢走捕获就再也收不到点击了，留着只会是一块关不掉的浮层
            if self.is_root and self.session.alive:
                self.session.close()
            return 0
        if not self.is_root:
            return None
        if msg == WM_MOUSEMOVE:
            sx, sy = _cursor()
            self.session.dispatch_move(sx, sy)
            # 不能指望 WM_SETCURSOR：这类「无激活 + 分层 + 独占捕获」的窗口实测
            # 一条都收不到，光标会停在上一层留下的状态（比如系统的忙光标）。
            # 鼠标消息是稳的，就跟着鼠标自己设。
            self.session.update_cursor()
            return 0
        if msg == WM_LBUTTONDOWN:
            sx, sy = _cursor()
            self.session.dispatch_click(sx, sy)
            return 0
        if msg == WM_RBUTTONDOWN:
            self.session.close()
            return 0
        if msg == WM_KEYDOWN:
            self._on_key(int(wparam) & 0xFFFF)
            return 0
        if msg == WM_MOUSEACTIVATE:
            return MA_ACTIVATE
        if msg == WM_ACTIVATEAPP:
            if int(wparam) == 0:
                self.session.close()
            return 0
        return None


def _ensure_class():
    if _CLASS_READY[0]:
        return
    wc = F.WNDCLASSW()
    wc.style = 0
    wc.lpfnWndProc = _PROC
    wc.hInstance = kernel32.GetModuleHandleW(None)
    wc.hCursor = cursor_handle(False)
    wc.lpszClassName = CLASS_NAME
    ok = user32.RegisterClassW(ctypes.byref(wc))
    # 返回 0 多半是类已经注册过（重复注册会报 1410），CreateWindowEx 照样能用；
    # 真失败时窗口创建会返回 0，上层据此判定弹出失败
    _CLASS_READY[0] = True or bool(ok)


def _wndproc(hwnd, msg, wparam, lparam):
    p = _PANELS.get(int(hwnd or 0))
    if p is not None:
        try:
            r = p.on_message(hwnd, msg, wparam, lparam)
            if r is not None:
                return r
        except Exception:
            pass
    try:
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
    except Exception:
        return 0


_PROC = F.WNDPROC(_wndproc)
_WNDPROC_REF = _PROC          # 别让回调被 GC 掉，窗口类还指着它


# ---------------------------------------------------------------- 会话
class Session:
    def __init__(self, dark, theme, scale):
        self.dark = dark
        self.theme = theme
        self.scale = scale
        self.stack = []
        self.root = None
        self.result = None
        self.alive = True
        self.done = threading.Event()

    # ---------- 打开 / 关闭
    def open_root(self, items, owner, anchor):
        p = Panel(self, items, owner=owner, anchor=anchor)
        if not p.hwnd:
            self.alive = False
            return False
        self.stack.append(p)
        self.root = p
        p.focus()
        return True

    def open_sub(self, parent, idx):
        row = parent.painter.rows[idx]
        if not row.get('sub'):
            return
        i = self.stack.index(parent)
        if len(self.stack) > i + 1:
            sub = self.stack[i + 1]
            if sub.parent is parent and sub.parent_row == idx:
                return
            self.pop_above(i)
        cx, cy = parent.content_origin()
        # 注意 cx 是物理像素，painter.W 是逻辑宽度，必须乘 scale 才能对上
        ax = cx + parent.painter.W * self.scale
        # 右边缘放不下就翻到父面板左侧
        r = _work_area(cx, cy)
        sub_w = M.Painter(row['sub'], dark=self.dark, theme=self.theme,
                          scale=self.scale, defer=True).W * self.scale
        if ax + sub_w > r.right:
            ax = cx - sub_w
        ay = cy + row['y0'] * self.scale
        p = Panel(self, row['sub'], parent=parent, parent_row=idx, anchor=(ax, ay))
        self.stack.append(p)
        parent.sub_row = idx
        parent.hover = idx
        parent._cancel_timer()
        parent.paint()

    def pop_above(self, i):
        while len(self.stack) > i + 1:
            self.stack.pop().destroy()
        if len(self.stack) > i:
            self.stack[i].sub_row = None

    # ---------- 鼠标派发
    def _target(self, sx, sy):
        for p in reversed(self.stack):
            if p.contains(sx, sy):
                return p
        return None

    def update_cursor(self):
        """按光标下面那一行决定光标形状：可点/能展开的用小手，其余用箭头。

        用屏幕坐标从「最上层面板」开始找，所以光标压在子菜单上时也算子菜单那行。
        """
        pt = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        t = self._target(pt.x, pt.y)
        hand = False
        if t is not None:
            i = t.hit_index(pt.x, pt.y)
            if i is not None and 0 <= i < len(t.painter.rows):
                r = t.painter.rows[i]
                hand = bool(r.get('kind') == 'item' and r.get('enabled')
                            and (r.get('sub') or r.get('action')))
        user32.SetCursor(cursor_handle(hand))

    def dispatch_move(self, sx, sy):
        t = self._target(sx, sy)
        if t is None:
            # 完全移出菜单树：收起子菜单，但主菜单留着（和系统菜单一致）
            self.pop_above(0)
            for p in self.stack:
                p.sub_row = None
                if p.hover is not None:
                    p.hover = None
                    p.paint()
            return
        i = self.stack.index(t)
        if len(self.stack) > i + 1:
            sub = self.stack[i + 1]
            # 光标停在「刚展开子菜单的那一行」时不要收起子菜单，否则会来回闪
            if not (sub.parent is t and t.hit_index(sx, sy) == sub.parent_row):
                self.pop_above(i)
                t.sub_row = None
        t.set_hover(sx, sy)

    def dispatch_click(self, sx, sy):
        t = self._target(sx, sy)
        if t is None:
            self.close()
            return
        idx = t.hit_index(sx, sy)
        if idx is None:
            return                       # 点在阴影或行间留白，不理会
        row = t.painter.rows[idx]
        if row.get('sub'):
            self.open_sub(t, idx)
            return
        if row['kind'] == 'item' and row['enabled'] and row.get('action'):
            self.finish(row['action'])

    # ---------- 结束
    def finish(self, action):
        self.result = action
        self.close()

    def close(self):
        if not self.alive:
            return
        self.alive = False
        if self.root and self.root.hwnd:
            user32.PostMessageW(self.root.hwnd, WM_NULL, 0, 0)

    def destroy_all(self):
        try:
            user32.ReleaseCapture()
        except Exception:
            pass
        while self.stack:
            self.stack.pop().destroy()

    def run(self):
        msg = wt.MSG()
        while self.alive:
            r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r == 0:
                user32.PostQuitMessage(0)     # 收到 WM_QUIT，交还给外层循环
                break
            if r == -1:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        self.destroy_all()
        return self.result


def window_anchor():
    """菜单内容框左上角就落在光标处（阴影会自然溢出到左上外侧），与系统菜单一致"""
    return _cursor()


def system_dark():
    """系统「应用」是否深色。托盘菜单没有卡片可跟随，只能跟系统。"""
    try:
        import winreg
        k = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize')
        v, _ = winreg.QueryValueEx(k, 'AppsUseLightTheme')
        winreg.CloseKey(k)
        return int(v) == 0
    except Exception:
        return False


def make_thread_dpi_aware():
    """把**当前线程**切成 per-monitor v2。

    主程序和悬浮窗都是 DPI unaware 的（系统缩放 175%，进程只看得到 1646x1029，
    位图会被系统拉伸 1.75 倍，边界发糊）。悬浮窗字少还能糊过去，菜单里全是
    12px 中文，拉伸后一眼就能看出模糊。

    这里只切菜单线程，不动进程的 DPI 感知，所以主程序其它窗口不受影响。
    切换后 GetCursorPos / CreateWindowExW / GetSystemMetrics 全部按物理像素算，
    我们按 1.75 渲染的位图就是 1:1 上屏，边缘是干净的。
    """
    try:
        user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))   # PER_MONITOR_AWARE_V2
        return True
    except Exception:
        return False


_PICKS = {}                 # hwnd(int) -> 待执行的 action，等宿主线程来取
_PICK_LOCK = threading.Lock()


def popup_async(items, hwnd, msg_pick=None, msg_closed=None,
                dark=False, theme=None, scale=None, anchor=None):
    """弹出菜单，但**立刻返回**，不阻塞调用线程。

    这是「右键后鼠标停在菜单上一直显示加载」的根因修复：以前 popup() 在调用
    线程里死等菜单关闭，而调用者是悬浮窗/托盘的窗口过程，于是那个窗口在菜单
    开着期间一直「无响应」——Windows 会把鼠标换成忙/加载光标，悬浮窗也整个
    僵死（拖不动、不刷新、贴边逻辑停摆）。

    现在选完的动作不在这里执行，而是挂在宿主 hwnd 名下、发消息让宿主自己取。
    动作仍然跑在原来的 UI 线程上，不存在跨线程动窗口的问题。
    """
    if not items:
        return False
    msg_pick = WM_MENU_PICK if msg_pick is None else msg_pick
    msg_closed = WM_MENU_CLOSED if msg_closed is None else msg_closed
    s = Session(bool(dark), theme or M.DEFAULT_THEME, float(scale or F.dpi_scale()))

    def worker():
        try:
            make_thread_dpi_aware()
            s.scale = float(scale or F.dpi_scale())
            if s.open_root(items, None, anchor or window_anchor()):
                s.result = s.run()
        except Exception as e:
            _err('popup', e)
        finally:
            # 无论选没选、成没成，都要告诉宿主「菜单已经没了」，
            # 否则宿主那边的「菜单打开中」标志会一直挂着，卡片再也收不回去
            try:
                if hwnd:
                    if s.result is not None:
                        with _PICK_LOCK:
                            _PICKS[int(hwnd)] = s.result
                        user32.PostMessageW(hwnd, msg_pick, 0, 0)
                    else:
                        user32.PostMessageW(hwnd, msg_closed, 0, 0)
            except Exception as e:
                _err('post', e)

    threading.Thread(target=worker, daemon=True).start()
    return True


def take_pick(hwnd):
    """取走投给这个窗口的动作（取完即删，不会被执行两次）"""
    with _PICK_LOCK:
        return _PICKS.pop(int(hwnd or 0), None)


def popup(items, owner=None, dark=False, theme=M.DEFAULT_THEME, scale=None, anchor=None):
    """阻塞版，只给测试脚本 / 一次性预览用。

    正式界面请用 popup_async —— 在窗口过程里调这个会把窗口焊住 180 秒。
    """
    if not items:
        return None
    s = Session(bool(dark), theme, float(scale or F.dpi_scale()))

    def worker():
        try:
            make_thread_dpi_aware()
            s.scale = float(scale or F.dpi_scale())
            if s.open_root(items, None, anchor or window_anchor()):
                s.result = s.run()
        except Exception as e:
            _err('popup', e)
        finally:
            s.done.set()

    threading.Thread(target=worker, daemon=True).start()
    s.done.wait(180)          # 兜底，菜单卡死也不会把调用线程永远焊住
    return s.result
