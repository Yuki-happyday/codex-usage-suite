"""极简 Windows 托盘图标实现（纯 ctypes，不依赖 pystray）

为什么不用 pystray：实测 Shell_NotifyIcon 返回成功，但图标不出现在通知区。
这里自己创建顶层窗口 + HICON，直接走 NIM_ADD/NIM_MODIFY。
"""
import ctypes
import ctypes.wintypes as wt
import threading

user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_LBUTTONDBLCLK = 0x0203
WM_USER = 0x0400
CALLBACK_MSG = WM_USER + 20
# 菜单线程选完 / 关掉后投回来的两条消息（WM_APP+1 / +2，与 menu_win 里一致）
WM_MENU_PICK = 0x8001
WM_MENU_CLOSED = 0x8002

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x1, 0x2, 0x4
MF_STRING, MF_GRAYED, MF_SEPARATOR = 0x0, 0x1, 0x800
MF_CHECKED, MF_POPUP = 0x00000008, 0x00000010
TPM_RETURNCMD, TPM_NONOTIFY, TPM_RIGHTBUTTON = 0x100, 0x80, 0x2

WS_POPUP = 0x80000000
CS_VREDRAW, CS_HREDRAW = 0x0001, 0x0002
IDI_APPLICATION = 32512

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, ctypes.c_uint,
                             ctypes.c_size_t, ctypes.c_ssize_t)

user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.DefWindowProcW.argtypes = [wt.HWND, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]

SRCCOPY = 0x00CC0020


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [('biSize', wt.DWORD), ('biWidth', ctypes.c_long),
                ('biHeight', ctypes.c_long), ('biPlanes', wt.WORD),
                ('biBitCount', wt.WORD), ('biCompression', wt.DWORD),
                ('biSizeImage', wt.DWORD), ('biXPelsPerMeter', ctypes.c_long),
                ('biYPelsPerMeter', ctypes.c_long), ('biClrUsed', wt.DWORD),
                ('biClrImportant', wt.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [('bmiHeader', BITMAPINFOHEADER), ('bmiColors', wt.DWORD * 3)]


class ICONINFO(ctypes.Structure):
    _fields_ = [('fIcon', wt.BOOL), ('xHotspot', wt.DWORD), ('yHotspot', wt.DWORD),
                ('hbmMask', wt.HBITMAP), ('hbmColor', wt.HBITMAP)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [('cbSize', wt.DWORD), ('hWnd', wt.HWND), ('uID', wt.UINT),
                ('uFlags', wt.UINT), ('uCallbackMessage', wt.UINT),
                ('hIcon', wt.HICON), ('szTip', wt.WCHAR * 128),
                ('dwState', wt.DWORD), ('dwStateMask', wt.DWORD),
                ('szInfo', wt.WCHAR * 256), ('uVersion', wt.UINT),
                ('szInfoTitle', wt.WCHAR * 64), ('dwInfoFlags', wt.DWORD),
                ('guidItem', ctypes.c_byte * 16), ('hBalloonIcon', wt.HICON)]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [('style', ctypes.c_uint), ('lpfnWndProc', WNDPROC),
                ('cbClsExtra', ctypes.c_int), ('cbWndExtra', ctypes.c_int),
                ('hInstance', wt.HINSTANCE), ('hIcon', wt.HICON),
                ('hCursor', wt.HANDLE), ('hbrBackground', wt.HBRUSH),
                ('lpszMenuName', wt.LPCWSTR), ('lpszClassName', wt.LPCWSTR)]


def pil_to_hicon(img):
    """PIL RGBA 图 -> HICON"""
    if img.mode != 'RGBA':
        img = img.convert('RGBA')
    w, h = img.size
    data = img.tobytes('raw', 'BGRA')

    bi = BITMAPINFO()
    bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.bmiHeader.biWidth = w
    bi.bmiHeader.biHeight = -h          # 负值 = 自上而下
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = 32
    bi.bmiHeader.biCompression = 0      # BI_RGB

    hdc = user32.GetDC(None)
    bits = ctypes.c_void_p()
    hbm_color = gdi32.CreateDIBSection(hdc, ctypes.byref(bi), 0,
                                       ctypes.byref(bits), None, 0)
    ctypes.memmove(bits, data, len(data))
    user32.ReleaseDC(None, hdc)

    hbm_mask = gdi32.CreateBitmap(w, h, 1, 1, None)

    ii = ICONINFO()
    ii.fIcon = 1
    ii.hbmMask = hbm_mask
    ii.hbmColor = hbm_color
    hicon = user32.CreateIconIndirect(ctypes.byref(ii))

    gdi32.DeleteObject(hbm_color)
    gdi32.DeleteObject(hbm_mask)
    return hicon


class TrayIcon:
    def __init__(self, uid=7, tooltip='', menu_provider=None):
        self.uid = uid
        self.tooltip = tooltip
        self.menu_provider = menu_provider    # 返回 [(label, enabled, callable)]
        self._hicon = None
        self._hwnd = None
        self._thread = None
        self._proc = None
        self._nid = NOTIFYICONDATAW()
        self._ready = threading.Event()
        self._handlers = {}
        self._menu_open = False
        self.last_add_result = None
        self._wm_taskbarcreated = user32.RegisterWindowMessageW('TaskbarCreated')

    # ---------------- 内部 ----------------
    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_MENU_PICK or msg == WM_MENU_CLOSED:
                self._on_menu_msg(msg)
                return 0
            if msg == CALLBACK_MSG:
                if lparam in (WM_RBUTTONUP, WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    self._show_menu()
                return 0
            if msg == WM_COMMAND:
                fn = self._handlers.get(int(wparam) & 0xFFFF)
                if fn:
                    fn()
                return 0
            if msg == self._wm_taskbarcreated:
                self._add()
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            pass
        try:
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
        except Exception:
            return 0

    def _add(self):
        nid = self._nid
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = self.uid
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = CALLBACK_MSG
        nid.hIcon = self._hicon
        nid.szTip = self.tooltip[:127]
        ok = shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
        self.last_add_result = (ok, kernel32.GetLastError())
        return ok

    def _modify(self):
        nid = self._nid
        nid.uFlags = NIF_ICON | NIF_TIP
        nid.hIcon = self._hicon
        nid.szTip = self.tooltip[:127]
        return shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def _show_menu(self):
        """自绘菜单（menucanvas/menu_win）：和悬浮窗共用一套视觉。

        托盘图标自己是 DPI unaware 的，菜单线程内部会切到 per-monitor，
        所以坐标与位图都按物理像素走。

        这里同样不能等菜单关掉：这段跑在托盘窗口的过程里，一等就把托盘焊住，
        Windows 会给鼠标换上忙/加载光标。选完的动作由 WM_MENU_PICK 送回来。
        """
        if not self.menu_provider or self._menu_open:
            return
        items = self.menu_provider()
        self._menu_open = True
        ok = False
        try:
            import menu_win
            ok = menu_win.popup_async(items, self._hwnd, dark=menu_win.system_dark())
        except Exception as e:
            try:
                import menu_win as _mw
                _mw._err('tray popup', e)
            except Exception:
                pass
        if not ok:
            self._menu_open = False

    def _on_menu_msg(self, msg):
        act = None
        try:
            import menu_win
            if msg == WM_MENU_PICK:
                act = menu_win.take_pick(self._hwnd)
        except Exception:
            pass
        self._menu_open = False
        if act:
            try:
                act()
            except Exception:
                pass

    def _run(self):
        hinst = kernel32.GetModuleHandleW(None)
        cls_name = f'PyTray_{self.uid}_{id(self)}'
        self._proc = WNDPROC(self._wndproc)
        wc = WNDCLASSW()
        wc.style = CS_HREDRAW | CS_VREDRAW
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.lpszClassName = cls_name
        atom = user32.RegisterClassW(ctypes.byref(wc))
        if not atom:
            self._ready.set()
            return
        self._hwnd = user32.CreateWindowExW(
            0, cls_name, 'codex-usage', WS_POPUP, 0, 0, 0, 0,
            None, None, hinst, None)
        self._add()
        self._ready.set()

        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    # ---------------- 对外 ----------------
    def start(self, img, tooltip=None):
        if tooltip is not None:
            self.tooltip = tooltip
        self._hicon = pil_to_hicon(img)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(5)
        return bool(self._hwnd)

    def update(self, img=None, tooltip=None):
        changed = False
        if img is not None:
            old = self._hicon
            self._hicon = pil_to_hicon(img)
            if old:
                try:
                    user32.DestroyIcon(old)
                except Exception:
                    pass
            changed = True
        if tooltip is not None and tooltip != self.tooltip:
            self.tooltip = tooltip
            changed = True
        if changed and self._hwnd:
            self._modify()

    def hide(self):
        """把图标从通知区拿走，但保留窗口和消息循环，方便随时恢复"""
        try:
            if self._hwnd:
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
        except Exception:
            pass

    def show(self):
        if self._hwnd:
            self._add()

    def stop(self):
        try:
            if self._hwnd:
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
                user32.PostMessageW(self._hwnd, WM_DESTROY, 0, 0)
        except Exception:
            pass
