"""Codex 用量提示（通知 + 托盘常显）

监听 ~/.codex/sessions 下的会话日志：
  1. 每轮结束弹一条系统通知：本轮 tokens + 各窗口剩余百分比、本次消耗百分点；
  2. 系统托盘常驻一个图标，图标上直接显示剩余百分比，悬停/点开看明细。
只读本地日志，不联网、不读凭证。
"""
import glob
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime

SESSIONS = os.path.join(os.path.expanduser('~'), '.codex', 'sessions')
BASE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE, 'state.json')
LOG_FILE = os.path.join(BASE, 'watch.log')
CRASH_FILE = os.path.join(BASE, 'crash.log')
POLL = 1.5
TRAY_TICK = 5.0

# pythonw.exe 下 sys.stdout/sys.stderr 是 None：任何线程里没接住的异常，
# 在默认的 excepthook 打印时都会撞上「NoneType 没有 write」，结果就是
# 进程几秒后无声无息地消失，watch.log 里一个字都不留。这里把两者接到文件上，
# 崩溃才有痕迹可查。
if sys.stdout is None or sys.stderr is None:
    try:
        _crash = open(CRASH_FILE, 'a', encoding='utf-8', buffering=1)
        sys.stdout = sys.stdout or _crash
        sys.stderr = sys.stderr or _crash
    except Exception:
        pass

APP_ID = 'OpenAI.Codex_2p2nqsd0c76g0!App'

# ChatGPT 的稳定别名（不随版本目录变化），任务栏/开始菜单都解析到它
CHATGPT_EXE = os.path.join(os.environ.get('LOCALAPPDATA', ''),
                           'Microsoft', 'WindowsApps', 'chatgpt-desktop.exe')
TAKEOVER_MAX_AGE = 25      # 只接管「刚启动」的实例（秒），避免杀掉你正在写的内容
TAKEOVER_COOLDOWN = 90     # 两次接管之间的冷却（秒），防止自己重启自己造成死循环
TAKEOVER_TICK = 2.0

WINDOW_LABEL = [
    (360, '5小时'),
    (1500, '每日'),
    (11000, '每周'),
    (40000, '每月'),
]

LOCK = threading.Lock()


SHORT_LABEL = {'5小时': '5h', '每日': '日', '每周': '周', '每月': '月', '额度': ''}


def window_label(minutes):
    if minutes is None:
        return '额度'
    for limit, label in WINDOW_LABEL:
        if minutes <= limit:
            return label
    return '额度'


def short_label(label):
    return SHORT_LABEL.get(label, label[:1])


def log(msg):
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(f'{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n')
    except Exception:
        pass


# ---------------------------------------------------------------- 通知

def notify(title, lines):
    try:
        from winrt.windows.ui.notifications import (
            ToastNotification, ToastNotificationManager)
        from winrt.windows.data.xml.dom import XmlDocument
    except Exception as e:
        log(f'notify import failed: {e}')
        return False

    from xml.sax.saxutils import escape
    body = ''.join(f'<text>{escape(str(ln))}</text>' for ln in lines[:3])
    xml = (f'<toast activationType="foreground">'
           f'<visual><binding template="ToastGeneric">'
           f'<text>{escape(str(title))}</text>{body}'
           f'</binding></visual></toast>')
    try:
        doc = XmlDocument()
        doc.load_xml(xml)
        ToastNotificationManager.create_toast_notifier(APP_ID).show(ToastNotification(doc))
        return True
    except Exception as e:
        log(f'notify failed: {e}')
        return False


# ---------------------------------------------------------------- 格式化

def fmt_tokens(n):
    if n >= 1_000_000:
        return f'{n / 1_000_000:.2f}M'
    if n >= 1000:
        return f'{n / 1000:.1f}k'
    return str(n)


def fmt_reset(ts):
    try:
        t = datetime.fromtimestamp(int(ts))
    except Exception:
        return ''
    now = datetime.now()
    same_day = t.date() == now.date()
    return f'{t:%H:%M}' if same_day else f'{t:%m-%d %H:%M}'


def fmt_reset_short(ts):
    """悬浮窗左节用的紧凑重置时间：19:30 / 明01:30 / 11/11 19:30

    5h 窗口的重置点最多跨一个零点，所以「明」够用；万一拿到过期很久的
    数据才会走到带日期那支（画的时候会自动缩字号，不会溢出格子）。
    """
    try:
        t = datetime.fromtimestamp(float(ts))
    except Exception:
        return '--:--'
    hm = f'{t:%H:%M}'
    days = (t.date() - datetime.now().date()).days
    if days == 0:
        return hm
    if days == 1:
        return f'明{hm}'
    return f'{t.month}/{t.day} {hm}'


def fmt_left(ts):
    """还剩多久：6天3小时 / 4小时12分 / 37分钟"""
    try:
        left = max(0, int(float(ts) - time.time()))
    except Exception:
        return ''
    d, h, m = left // 86400, left % 86400 // 3600, left % 3600 // 60
    if d:
        return f'{d}天{h}小时'
    if h:
        return f'{h}小时{m:02d}分'
    if m:
        return f'{m}分钟'
    return '不到 1 分钟'


def window_expired(ts):
    """重置点已经过去 → 这个窗口早就重新计时了，日志里的 used_percent 不再代表当前。

    Codex 只在对话时把 rate_limits 写进日志，所以隔一晚再打开，state 里存的
    还是昨天那条。不判断就会把「昨天剩 85%」当成今天的数据，倒计时还会被
    max(0, …) 压成「不到 1 分钟」——三个错一起显示。
    """
    try:
        return float(ts) <= time.time()
    except (TypeError, ValueError):
        return True


def ago_text(ts):
    """数据时间：11-09 18:06（16 小时前）"""
    try:
        t = datetime.fromtimestamp(float(ts))
    except Exception:
        return ''
    mins = max(0, int((time.time() - float(ts)) / 60))
    if mins < 60:
        rel = f'{mins} 分钟前' if mins else '刚刚'
    elif mins < 1440:
        rel = f'{mins // 60} 小时前'
    else:
        rel = f'{mins // 1440} 天前'
    return f'{t:%m-%d %H:%M}（{rel}）'


def windows_snapshot(state):
    """统一界面和通知的额度快照。重置时间未知或已过时，剩余额度待刷新。"""
    rl = state.get('rate_limits') or {}
    out = []
    for key in ('primary', 'secondary'):
        w = rl.get(key)
        if not isinstance(w, dict) or w.get('used_percent') is None:
            continue
        used = float(w['used_percent'])
        reset_ts = w.get('resets_at')
        reset = fmt_reset(reset_ts)
        stale = not reset or window_expired(reset_ts)
        out.append({
            'key': key,
            'label': window_label(w.get('window_minutes')),
            'window_minutes': w.get('window_minutes'),
            'used': None if stale else used,
            'remain': None if stale else max(0.0, 100.0 - used),
            'reset': '' if stale else reset,
            'reset_ts': reset_ts,
            'stale': stale,
        })
    return out


def credits_text(state):
    rl = state.get('rate_limits') or {}
    c = rl.get('credits')
    if not isinstance(c, dict):
        return ''
    if c.get('has_credits') is False:
        return '无 credit 余额'
    bal = c.get('balance')
    if bal is None:
        return ''
    try:
        return f'{float(bal):,.0f} credits'
    except Exception:
        return f'{bal} credits'


# ---------------------------------------------------------------- 状态

def load_state():
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {'offsets': {}, 'last_used': {}}


def save_state(st):
    try:
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(st, f, ensure_ascii=False)
    except Exception as e:
        log(f'state save failed: {e}')


def _read_latest_limits(hours=6.0, limit=3):
    """从最近几个会话日志里取最后一条 rate_limits，返回 (limits, mtime) 或 None。
    hours=0 表示不限时间（手动刷新时用）。"""
    files = scan_files()
    if not files:
        return None
    now = time.time()
    try:
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    except OSError:
        return None
    for f in files[:limit]:
        try:
            mtime = os.path.getmtime(f)
            if hours and now - mtime > hours * 3600:
                continue
            size = os.path.getsize(f)
            with open(f, 'rb') as fh:
                if size > 3_000_000:
                    fh.seek(size - 3_000_000)
                raw = fh.read().decode('utf-8', 'replace')
        except OSError:
            continue
        found = None
        for line in raw.splitlines():
            if '"rate_limits"' not in line:
                continue
            try:
                p = json.loads(line).get('payload') or {}
            except Exception:
                continue
            if p.get('rate_limits'):
                found = p['rate_limits']
        if found:
            return found, mtime
    return None


def seed_from_logs(state):
    """启动时从**近 6 小时内**的会话日志回填 rate_limits / last_used。
    太旧的日志不回填——旧数据的窗口形状（例如此前的月度额度）会显示成错误的百分比。
    """
    if state.get('rate_limits'):
        return
    got = _read_latest_limits()
    if not got:
        return
    found, mtime = got
    state['rate_limits'] = found
    state['limits_ts'] = mtime
    if not state.get('last_used'):
        lu = {}
        for k in ('primary', 'secondary'):
            w = found.get(k)
            if isinstance(w, dict) and w.get('used_percent') is not None:
                lu[k] = float(w['used_percent'])
        state['last_used'] = lu
    log('seeded rate_limits from latest session log')


def refresh_rate_limits(state):
    """手动刷新：重扫会话日志最后一条 rate_limits 并覆盖 state。返回是否读到数据。"""
    got = _read_latest_limits(hours=0)
    if not got:
        return False
    found, mtime = got
    with LOCK:
        state['rate_limits'] = found
        state['limits_ts'] = max(float(state.get('limits_ts') or 0), mtime)
        lu = {}
        for k in ('primary', 'secondary'):
            w = found.get(k)
            if isinstance(w, dict) and w.get('used_percent') is not None:
                lu[k] = float(w['used_percent'])
        if lu:
            state['last_used'] = lu
        save_state(state)
    return True


def scan_files():
    out = []
    for pat in ('*/*/*/*.jsonl',):
        out.extend(glob.glob(os.path.join(SESSIONS, pat)))
    return out


def initial_log_offset(path):
    """已有日志跳过历史整行，末尾尚未写完的记录留给监听循环。"""
    with open(path, 'rb') as fh:
        end = fh.seek(0, os.SEEK_END)
        while end:
            start = max(0, end - 8192)
            fh.seek(start)
            last_newline = fh.read(end - start).rfind(b'\n')
            if last_newline >= 0:
                return start + last_newline + 1
            end = start
    return 0


class Turn:
    def __init__(self):
        self.reset()

    def reset(self):
        self.active = False
        self.inp = 0
        self.out = 0
        self.cached = 0
        self.reasoning = 0
        self.total = 0
        self.parts = 0


def handle_line(obj, state, turn):
    """返回 True 表示这一轮结束、需要输出通知"""
    if obj.get('type') != 'event_msg':
        return False
    p = obj.get('payload') or {}
    ev = p.get('type')

    if ev == 'task_started':
        turn.reset()
        turn.active = True
        return False

    if ev == 'token_count':
        info = p.get('info') or {}
        last = info.get('last_token_usage') or {}
        if last:
            turn.inp += last.get('input_tokens', 0)
            turn.out += last.get('output_tokens', 0)
            turn.cached += last.get('cached_input_tokens', 0)
            turn.reasoning += last.get('reasoning_output_tokens', 0)
            turn.total += last.get('total_tokens', 0)
            turn.parts += 1
            turn.active = True
        rl = p.get('rate_limits') or {}
        if rl:
            state['rate_limits'] = rl
            state['limits_ts'] = time.time()
        return False

    if ev == 'task_complete':
        return turn.active

    return False


NOTIFY_LABEL = {'5小时': '5h', '每日': 'day', '每周': 'week', '每月': 'month'}


def disp_width(s):
    """按终端显示宽度算：全角字符算 2，半角算 1（PIL 不可用时的兜底）"""
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in s)


_FONT_CACHE = {}


def _measure_font():
    """用雅黑测宽——Windows 通知里的中英文都走这个字体，比例字体下按字符数补空格是不准的"""
    if 'f' not in _FONT_CACHE:
        try:
            from PIL import ImageFont
            _FONT_CACHE['f'] = ImageFont.truetype(r'C:\Windows\Fonts\msyh.ttc', 24)
        except Exception:
            _FONT_CACHE['f'] = None
    return _FONT_CACHE['f']


def text_px(s):
    f = _measure_font()
    return f.getlength(s) if f is not None else disp_width(s) * 12


def pad_to_px(s, target):
    """补空格把 s 撑到 target 像素宽"""
    f = _measure_font()
    if f is None:
        return s
    space = f.getlength(' ')
    if space <= 0:
        return s
    n = int((target - text_px(s)) / space + 0.5)
    return s + ' ' * max(0, n)


def build_notification(turn, state):
    """返回 (title, lines)，两行上下对齐

    第一行：本轮 · 5h（-3%） · week（-<1%）   ← 本轮各自消耗的百分点
    第二行：剩余 · 5h（87%） · week（99%）     ← 各自剩余百分比
    同列按实测像素宽度补空格，保证「5h」「week」「%」上下对齐。
    重置时间与 credits 放托盘悬停，通知只保留这两行。
    """
    prev = state.get('last_used') or {}
    wins = windows_snapshot(state)

    delta_cells, remain_cells = [], []
    for w in wins:
        short = NOTIFY_LABEL.get(w['label'], w['label'])
        if w['stale']:
            delta_cells.append(f'{short}（—）')
            remain_cells.append(f'{short}（待刷新）')
            prev.pop(w['key'], None)
            continue
        used = w['used']
        delta = None
        if w['key'] in prev and prev[w['key']] is not None:
            d = used - float(prev[w['key']])
            if d >= 1:
                delta = f'-{d:.0f}%'
            elif d > 0:
                delta = '-<1%'
            else:
                delta = '0%'
        elif turn.total > 0 and used > 0:
            delta = '-<1%'
        delta_cells.append(f'{short}（{delta}）' if delta else f'{short}（—）')
        remain_cells.append(f'{short}（{w["remain"]:.0f}%）')
        prev[w['key']] = used

    if delta_cells:
        widths = [max(text_px(a), text_px(b))
                  for a, b in zip(delta_cells, remain_cells)]
        title = '本轮 · ' + ' · '.join(pad_to_px(a, w) for a, w in zip(delta_cells, widths))
        lines = ['剩余 · ' + ' · '.join(pad_to_px(b, w) for b, w in zip(remain_cells, widths))]
        app_line = '本轮 · ' + ' · '.join(delta_cells)     # 注入 App 用，不补齐
    else:
        title = '本轮 · 额度数据未返回'
        lines = ['剩余 · 额度数据未返回']
        app_line = ''

    state['last_used'] = prev
    return title, lines, app_line


# ---------------------------------------------------------------- 注入 App（CDP）

CDP_PORT = 9222
TAG_TICK = 5.0
PAGE_URLS = ('app://-/index.html', 'app://-/detached-window.html')
PROBE_JS = ("JSON.stringify({v:document.visibilityState,"
            "n:document.querySelectorAll('.turn-action-controls').length})")
TAG_JS = r"""
(() => {
  const text = __TEXT__;
  const all = [...document.querySelectorAll('.turn-action-controls')];
  // 虚拟滚动的会话里有些行是 0 高度的（未布局），必须挑真正可见的那一行
  const shown = all.filter(r => r.getBoundingClientRect().height > 0);
  const row = shown.length ? shown[shown.length - 1] : all[all.length - 1];
  if (!row) return 'no-rows';
  const more = row.querySelector('button[aria-label="更多操作"]')
            || [...row.querySelectorAll('button')].pop();
  const holder = (more && more.parentElement) || row.querySelector(':scope > div') || row;
  let s = holder.querySelector('[data-codex-usage="1"]');
  if (!s) {
    s = document.createElement('span');
    s.setAttribute('data-codex-usage', '1');
    s.style.cssText = 'margin-left:10px;font-size:11.5px;line-height:26px;'
                    + 'color:rgba(110,118,133,.95);white-space:nowrap;user-select:none;'
                    + 'font-variant-numeric:tabular-nums;';
    holder.appendChild(s);
  }
  if (s.textContent !== text) s.textContent = text;
  return 'ok';
})()
"""


def cdp_push(text):
    """把一行用量文字插到最新一条回答的动作行右侧；App 没开调试端口就静默跳过。

    注意：App 可能同时存在多个渲染进程（主窗口 / 分离窗口 / 隐藏的常驻窗口），
    只有一个是你正在看的。这里按「可见 + 有消息行」打分，挑最高的那个写，
    否则会像以前那样把标签全写进一个看不见的窗口里。
    """
    if not text:
        return False
    try:
        import cdp2
    except Exception as e:
        log(f'cdp import failed: {e}')
        return False
    try:
        c = cdp2.Cdp(port=CDP_PORT, timeout=6)
    except Exception:
        return False
    try:
        best = None
        for t in c.targets():
            url = t.get('url') or ''
            if t.get('type') != 'page' or 'avatar-overlay' in url:
                continue
            if not any(url.startswith(u) for u in PAGE_URLS):
                continue
            sid = c.attach(t['targetId'])
            if not sid:
                continue
            try:
                info = json.loads(c.js(PROBE_JS, sid, timeout=6) or '{}')
            except Exception:
                info = {}
            score = (1 if info.get('n') else 0,          # 有消息行最重要（空窗口写不进去）
                     1 if info.get('v') == 'visible' else 0,
                     int(info.get('n') or 0))
            if best is None or score > best[0]:
                best = (score, sid)
        if best is None:
            return False
        expr = TAG_JS.replace('__TEXT__', json.dumps(text, ensure_ascii=False))
        return c.js(expr, best[1], timeout=6) == 'ok'
    except Exception as e:
        log(f'cdp push error: {e}')
        return False
    finally:
        c.close()


def cdp_loop(state):
    """每 5 秒守一次：React 重渲染/切换会话会把标签冲掉，这里补回去"""
    fails = 0
    while True:
        time.sleep(TAG_TICK)
        with LOCK:
            text = state.get('cdp_line')
        if not text:
            continue
        if cdp_push(text):
            if fails >= 3:
                log('cdp tag recovered')
            fails = 0
        else:
            fails += 1
            if fails == 3:
                log('cdp tag failed 3x（App 可能没带 --remote-debugging-port 启动）')


# ------------------------------------------------ 自动接管（任务栏启动兜底）

def _port_alive(timeout=0.9):
    """调试端口是否已经在监听"""
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{CDP_PORT}/json/version',
                                    timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _proc_start_time(pid):
    import ctypes
    import ctypes.wintypes as wt

    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.OpenProcess.restype = ctypes.c_void_p
    h = k32.OpenProcess(0x1000, False, pid)          # QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        c, e, k, u = (wt.FILETIME() for _ in range(4))
        if not k32.GetProcessTimes(ctypes.c_void_p(h), ctypes.byref(c),
                                   ctypes.byref(e), ctypes.byref(k),
                                   ctypes.byref(u)):
            return None
        ticks = (c.dwHighDateTime << 32) | c.dwLowDateTime
        return ticks / 1e7 - 11644473600.0           # FILETIME → Unix 时间戳
    finally:
        k32.CloseHandle(ctypes.c_void_p(h))


def _chatgpt_procs():
    """返回 [(pid, 父 pid, 创建时间戳)]，不依赖第三方库"""
    import ctypes
    import ctypes.wintypes as wt

    class PE32W(ctypes.Structure):
        _fields_ = [('dwSize', wt.DWORD),
                    ('cntUsage', wt.DWORD),
                    ('th32ProcessID', wt.DWORD),
                    ('th32DefaultHeapID', ctypes.c_void_p),
                    ('th32ModuleID', wt.DWORD),
                    ('cntThreads', wt.DWORD),
                    ('th32ParentProcessID', wt.DWORD),
                    ('pcPriClassBase', ctypes.c_long),
                    ('dwFlags', wt.DWORD),
                    ('szExeFile', wt.WCHAR * 260)]

    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)     # TH32CS_SNAPPROCESS
    if not snap:
        return []
    out = []
    try:
        e = PE32W()
        e.dwSize = ctypes.sizeof(PE32W)
        ok = k32.Process32FirstW(ctypes.c_void_p(snap), ctypes.byref(e))
        while ok:
            if (e.szExeFile or '').lower() == 'chatgpt.exe':
                t = _proc_start_time(e.th32ProcessID)
                if t:
                    out.append((e.th32ProcessID, e.th32ParentProcessID, t))
            ok = k32.Process32NextW(ctypes.c_void_p(snap), ctypes.byref(e))
    finally:
        k32.CloseHandle(ctypes.c_void_p(snap))
    return out


def _chatgpt_mains(procs=None):
    """主进程 = 父进程不是 ChatGPT.exe 的那些。
    渲染/GPU/utility 子进程的父进程是主进程，用这个条件一刀切掉。"""
    procs = _chatgpt_procs() if procs is None else procs
    pids = {p for p, _, _ in procs}
    return [(p, t) for p, pp, t in procs if pp not in pids]


def _port_owner_pid(port=CDP_PORT):
    """谁在监听这个调试端口（GetExtendedTcpTable，不走子进程）"""
    import ctypes
    import ctypes.wintypes as wt

    class ROW(ctypes.Structure):
        _fields_ = [('dwState', wt.DWORD), ('dwLocalAddr', wt.DWORD),
                    ('dwLocalPort', wt.DWORD), ('dwRemoteAddr', wt.DWORD),
                    ('dwRemotePort', wt.DWORD), ('dwOwningPid', wt.DWORD)]

    iphlp = ctypes.WinDLL('iphlpapi')
    size = wt.DWORD(0)
    AF_INET, OWNER_PID_LISTENER = 2, 3
    iphlp.GetExtendedTcpTable(None, ctypes.byref(size), False, AF_INET,
                              OWNER_PID_LISTENER, 0)
    if not size.value:
        return None
    buf = ctypes.create_string_buffer(size.value)
    if iphlp.GetExtendedTcpTable(buf, ctypes.byref(size), False, AF_INET,
                                 OWNER_PID_LISTENER, 0) != 0:
        return None
    n = ctypes.cast(buf, ctypes.POINTER(wt.DWORD))[0]
    arr = ctypes.cast(ctypes.addressof(buf) + ctypes.sizeof(wt.DWORD),
                      ctypes.POINTER(ROW))
    for i in range(n):
        row = arr[i]
        p = ((row.dwLocalPort & 0xFF) << 8) | ((row.dwLocalPort >> 8) & 0xFF)
        if p == port:
            return int(row.dwOwningPid)
    return None


def _kill_pid(pid):
    try:
        subprocess.run(['taskkill', '/F', '/T', '/PID', str(int(pid))],
                       capture_output=True, timeout=15,
                       creationflags=0x08000000)          # CREATE_NO_WINDOW
    except Exception as e:
        log(f'takeover: kill {pid} failed: {e}')


def _kill_chatgpt():
    try:
        subprocess.run(['taskkill', '/F', '/T', '/IM', 'ChatGPT.exe'],
                       capture_output=True, timeout=25,
                       creationflags=0x08000000)          # CREATE_NO_WINDOW
    except Exception as e:
        log(f'takeover: taskkill failed: {e}')


def _launch_chatgpt():
    try:
        subprocess.Popen([CHATGPT_EXE, f'--remote-debugging-port={CDP_PORT}'],
                         close_fds=True, creationflags=0x00000008)   # DETACHED
        return True
    except Exception as e:
        log(f'takeover: launch failed: {e}')
        return False


def _focus_chatgpt(only_pid=None):
    """把 ChatGPT 主窗口提到前台，免得重开后窗口缩在后台/藏在别的窗口下面"""
    try:
        import ctypes
        import ctypes.wintypes as wt

        u = ctypes.windll.user32
        pids = ({only_pid} if only_pid
                else {p for p, _ in _chatgpt_mains()})
        if not pids:
            return
        found = []
        CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

        def cb(h, _):
            if u.GetWindowTextLengthW(h) > 0:
                pid = wt.DWORD()
                u.GetWindowThreadProcessId(h, ctypes.byref(pid))
                if pid.value in pids:
                    found.append(h)
            return True

        u.EnumWindows(CB(cb), 0)
        # 按「宽度优先」挑：聊天主窗口比头像浮层宽（1141 vs 793），
        # 这样才不会把透明浮层当成主窗口提上来
        best, best_key = None, None
        for h in found:
            r = wt.RECT()
            u.GetWindowRect(h, ctypes.byref(r))
            w, hh = r.right - r.left, r.bottom - r.top
            if w < 200 or hh < 200:
                continue
            k = (w, w * hh)
            if best_key is None or k > best_key:
                best, best_key = h, k
        if best:
            u.ShowWindow(best, 9 if u.IsIconic(best) else 5)   # SW_RESTORE / SW_SHOW
            u.SwitchToThisWindow(best, True)
            if u.IsWindowVisible(best):
                return True
        return False
    except Exception:
        return False


def _recycle(why):
    """全关 → 用带调试端口的方式重开一次。

    App 不认「任务栏那条捷径」和「安装目录里的 exe」是同一个应用，
    于是会并存两个实例：你要用的那个没端口，有端口的那个窗口是藏着的。
    与其在两者之间来回搬窗口，不如整体重开成一个「有端口 + 有窗口」的实例。
    """
    log(f'takeover: {why} → 全部重开（带端口）')
    _kill_chatgpt()
    time.sleep(1.5)
    if not _launch_chatgpt():
        return False
    for _ in range(30):
        time.sleep(0.5)
        if _port_alive():
            log('takeover: 端口就绪')
            _focus_chatgpt()
            return True
    log('takeover: 端口超时未就绪')
    return False


def takeover_loop(state):
    """任务栏那个图标是按「应用身份」固定的，点击时不带命令行参数，
    所以冷启动的实例没有调试端口、App 里就长不出用量标签。
    这里趁它还没加载完（你还没来得及输入）用带端口的方式重开一次。
    另外：任务栏和开始菜单指向的 exe 路径不同，Windows 不认它们是同一个应用，
    于是会并存两个主进程——带端口的那个常被挤到后面，标签就全写进了看不见的窗口。
    所以还要持续收敛成「一个实例、且这个实例有端口」。"""
    last_act = 0.0
    last_dedup = 0.0
    told = False
    while True:
        time.sleep(TAKEOVER_TICK)
        try:
            with LOCK:
                enabled = takeover_enabled(state)
            if not enabled:
                continue
            procs = _chatgpt_procs()
            mains = _chatgpt_mains(procs)

            # ① 多个主进程：端口在哪个身上不好保证，直接整体重开最稳
            if len(mains) >= 2:
                now = time.time()
                if now - last_dedup < 20:
                    continue
                last_dedup = now
                _recycle(f'发现 {len(mains)} 个实例并存')
                told = False
                continue

            # ② 只有一个主进程：没有端口就重开
            if _port_alive():
                told = False
                continue
            if not mains:
                told = False
                continue
            age = time.time() - min(t for _, t in mains)
            if age > TAKEOVER_MAX_AGE:
                if not told:
                    log(f'takeover: 无端口实例已运行 {age:.0f}s，跳过不打断')
                    told = True
                continue
            now = time.time()
            if now - last_act < TAKEOVER_COOLDOWN:
                continue
            last_act = now
            now = time.time()
            if now - last_act < TAKEOVER_COOLDOWN:
                continue
            last_act = now
            _recycle(f'无端口实例（启动 {age:.1f}s）')
        except Exception as e:
            log(f'takeover error: {e}')


def takeover_enabled(state):
    """仅在用户通过菜单显式开启后接管。"""
    return state.get('takeover') is True


def _toggle_takeover(state):
    with LOCK:
        state['takeover'] = not takeover_enabled(state)
        v = state['takeover']
        save_state(state)
    log(f'takeover toggled -> {v}')


def rebuild_cdp_line(state):
    """App 被重开过（自动接管/手动关闭）后，内存里「本轮」那行就没了。
    用当前额度快照先拼一行占位，保证你打开历史对话时标签立刻在，
    下一轮真实消耗覆盖它。"""
    if state.get('cdp_line'):
        return
    wins = windows_snapshot(state)
    if not wins:
        return
    cells = []
    for w in wins:
        short = NOTIFY_LABEL.get(w['label'], w['label'])
        delta = '—' if w['stale'] else '0%'
        cells.append(f'{short}（{delta}）')
    state['cdp_line'] = '本轮 · ' + ' · '.join(cells)
    save_state(state)
    log('cdp_line rebuilt: ' + state['cdp_line'])


# ---------------------------------------------------------------- 托盘

def make_icon(text, color, sub=None):
    from PIL import Image, ImageDraw, ImageFont
    size = 64
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([1, 1, size - 2, size - 2], radius=15, fill=color)
    font_path = r'C:\Windows\Fonts\seguisb.ttf'
    n = len(text)
    fsize = 40 if n <= 2 else (30 if n == 3 else 24)
    try:
        font = ImageFont.truetype(font_path, fsize)
    except Exception:
        font = ImageFont.load_default()
    bbox = d.textbbox((0, 0), text, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    y = (size - h) / 2 - bbox[1] - (4 if sub else 0)
    d.text(((size - w) / 2 - bbox[0], y), text, font=font, fill='white')
    if sub:
        try:
            f2 = ImageFont.truetype(r'C:\Windows\Fonts\msyhbd.ttc', 16)
        except Exception:
            f2 = font
        b2 = d.textbbox((0, 0), sub, font=f2)
        d.text(((size - (b2[2] - b2[0])) / 2 - b2[0], size - 24), sub, font=f2, fill='white')
    return img


def color_for(remain):
    if remain >= 50:
        return (22, 163, 74)      # 绿
    if remain >= 20:
        return (217, 119, 6)      # 琥珀
    return (220, 38, 38)          # 红


def is_stale(state):
    """数据超过 90 分钟没刷新（说明这一轮没用 Codex），图标转灰，避免旧百分比误导。"""
    ts = state.get('limits_ts')
    return bool(ts) and (time.time() - float(ts)) > 90 * 60


def tray_text(state):
    """图标只放一个数字（剩余最少的那个窗口），副标在 16px 下看不清，已去掉"""
    wins = windows_snapshot(state)
    if not wins or any(w['stale'] for w in wins):
        return '--', (100, 116, 139), None
    main = min(wins, key=lambda w: w['remain'])
    color = (148, 163, 184) if is_stale(state) else color_for(main['remain'])
    return f'{main["remain"]:.0f}', color, None


def tooltip(state):
    wins = windows_snapshot(state)
    if not wins:
        return 'Codex 用量：等待数据（下一轮对话后刷新）'
    parts = []
    for w in wins:
        if w['stale']:
            parts.append(f'{w["label"]}待刷新')
        else:
            parts.append(f'{w["label"]}剩{w["remain"]:.0f}%（{w["reset"]}重置）')
    tip = 'Codex · ' + ' · '.join(parts)
    if is_stale(state):
        ts = state.get('limits_ts')
        try:
            tip += f'（数据停在 {datetime.fromtimestamp(float(ts)):%m-%d %H:%M}）'
        except Exception:
            pass
    return tip[:120]


THEME_ORDER = ['auto', 'dark', 'light']
THEME_NAME = {'auto': '跟随系统', 'dark': '深色', 'light': '浅色'}

# 款式（材质）：外观只决定字色与染色方向，款式决定表面做成什么
STYLE_ORDER = ['plain', 'frost']
STYLE_NAME = {'plain': '原始', 'frost': '磨砂'}

# 不透明度档位（%）。100 = 完全不透明，越小越透；桌面从分层窗口的 alpha 里透出来。
OPACITY_LEVELS = [100, 90, 80, 70, 60, 50]


def cur_style(state=None):
    v = (state or {}).get('style') or 'plain'
    # 旧版本存过 glass（抓屏模糊款），已移除，回落到磨砂
    if v == 'glass':
        return 'frost'
    return v if v in STYLE_ORDER else 'plain'


def cur_opacity(state=None):
    v = (state or {}).get('opacity')
    try:
        v = int(v)
    except Exception:
        return 100
    return v if v in OPACITY_LEVELS else 100


def next_theme(cur):
    """外观循环：跟随系统 → 深色 → 浅色 → 跟随系统"""
    return (THEME_ORDER[(THEME_ORDER.index(cur) + 1) % len(THEME_ORDER)]
            if cur in THEME_ORDER else 'auto')


def is_dark_mode(state=None):
    """外观：state['theme'] = dark / light 时用指定值，auto 才跟随系统「应用」深色设置"""
    theme = (state or {}).get('theme') or 'auto'
    if theme == 'dark':
        return True
    if theme == 'light':
        return False
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize')
        v, _ = winreg.QueryValueEx(k, 'AppsUseLightTheme')
        winreg.CloseKey(k)
        return int(v) == 0
    except Exception:
        return False


def float_payload(state):
    """悬浮窗的重置时间、剩余百分比和待刷新提示。"""
    p = {'reset5': '--:--', 'date': '--/--', 'p1': None, 'p2': None}
    for w in windows_snapshot(state):
        mins = w['window_minutes']
        ts = w['reset_ts']
        if mins and mins <= 360:
            p['p1'] = w['remain']
            p['reset5'] = '待刷新' if w['stale'] else fmt_reset_short(ts)
        else:
            p['p2'] = w['remain']
            if w['stale']:
                p['date'] = '待刷新'
            else:
                dt = datetime.fromtimestamp(float(ts))
                p['date'] = f'{dt.month}/{dt.day}'
    return p


def _set_mode(state, mode):
    with LOCK:
        state['display_mode'] = mode
        save_state(state)
    log(f'display mode -> {mode}')


def run_ui(state):
    """托盘 / 桌面悬浮窗 二选一的显示控制中心"""
    try:
        from tray_win32 import TrayIcon
    except Exception as e:
        log(f'tray import failed: {e}')
        return
    float_win32 = None
    try:
        import float_win32 as _fw
        float_win32 = _fw
    except Exception as e:
        log(f'float import failed: {e}')

    # 自绘菜单：分组小标题标记值从渲染模块取，避免两边各写一个常量
    menu_sec = 0x100
    try:
        import menucanvas as _mc
        menu_sec = _mc.MENU_SECTION
    except Exception as e:
        log(f'menucanvas import failed: {e}')
    try:
        import menu_win as _mw
        _mw.on_error = log
    except Exception as e:
        log(f'menu_win import failed: {e}')

    def snapshot():
        with LOCK:
            return {'rate_limits': state.get('rate_limits'),
                    'last_turn': state.get('last_turn'),
                    'limits_ts': state.get('limits_ts')}

    def detail_rows(st, with_mode):
        items = []
        wins = windows_snapshot(st)
        if not wins:
            items.append(('等待数据 · 下一轮对话后刷新', False, None))
        for w in wins:
            if w['stale']:
                items.append((f'{w["label"]}：待刷新', False, None))
                continue
            parts = [f'{w["label"]}：剩 {w["remain"]:.0f}%']
            left = fmt_left(w['reset_ts'])
            if left:
                parts.append(f'还剩 {left}')
            parts.append(f'{w["reset"]} 重置')
            items.append(('  '.join(parts), False, None))
        c = credits_text(st)
        if c:
            items.append((f'余额：{c}', False, None))
        if st.get('last_turn'):
            items.append((f'上一轮：{fmt_tokens(st["last_turn"])} tokens', False, None))
        # 数据来自日志文件，没有新对话就不会变。把"最后一次读到数据的时间"
        # 放在最后一行，用户看到数值不动时能立刻明白是数据源没更新。
        lt = st.get('limits_ts')
        if lt:
            items.append((f'数据 · {ago_text(lt)}', False, None))
        return items

    fw = None

    def style_now():
        with LOCK:
            return cur_style(state)

    def opacity_now():
        with LOCK:
            return cur_opacity(state)

    def draw(p, size=None):
        """按当前外观 + 款式 + 不透明度渲染卡片"""
        size = size or fw.size
        return float_win32.render(p, fw.scale, is_dark_mode(state), size=size,
                                  style=style_now(), opacity=opacity_now())

    def card_idx():
        with LOCK:
            v = state.get('card_size')
        if isinstance(v, int) and 0 <= v < len(float_win32.CARD_SIZES):
            return v
        return float_win32.DEFAULT_CARD_IDX

    def redraw_float():
        """立刻按当前 state 重画悬浮窗（手动刷新、换外观/款式后调用）"""
        if fw is None or not float_win32:
            return
        try:
            fw.update(draw(float_payload(snapshot())))
            last['float'] = None
        except Exception as e:
            log(f'redraw failed: {e}')

    def do_refresh():
        ok = refresh_rate_limits(state)
        redraw_float()
        log(f'manual refresh {"ok" if ok else "no new data in logs"}')

    def set_theme(name):
        with LOCK:
            state['theme'] = name
            save_state(state)
        log(f'theme -> {name}')
        redraw_float()

    def set_style(name):
        with LOCK:
            state['style'] = name
            save_state(state)
        log(f'style -> {name}')
        redraw_float()

    def set_opacity(level):
        with LOCK:
            state['opacity'] = int(level)
            save_state(state)
        log(f'opacity -> {level}%')
        redraw_float()

    def menu_rows(group):
        """外观 / 样式 / 透明度 / 卡片大小 四个子菜单，当前项打勾（MF_CHECKED = 0x8）"""
        with LOCK:
            cur_t = state.get('theme') or 'auto'
            cur_s = cur_style(state)
            cur_o = cur_opacity(state)
            cur_z = state.get('card_size')
        check = 0x8
        if group == 'theme':
            return [(f'{THEME_NAME[k]}', True, (lambda k=k: set_theme(k)),
                     check if k == cur_t else 0) for k in THEME_ORDER]
        if group == 'opacity':
            return [(f'{v}%', True, (lambda k=v: set_opacity(k)),
                     check if v == cur_o else 0) for v in OPACITY_LEVELS]
        if group == 'size':
            n = len(float_win32.CARD_SIZES)
            if not isinstance(cur_z, int) or not 0 <= cur_z < n:
                cur_z = float_win32.DEFAULT_CARD_IDX
            return [(f'{name}', True, (lambda k=i: set_card_size(k)),
                     check if i == cur_z else 0)
                    for i, (name, _) in enumerate(float_win32.CARD_SIZES)]
        return [(f'{STYLE_NAME[k]}', True, (lambda k=k: set_style(k)),
                 check if k == cur_s else 0) for k in STYLE_ORDER]

    def display_items(with_size=True):
        """显示设置统一收进「外观 ▸」一个入口。

        原来 外观/样式/透明度/卡片大小 四个子菜单平铺在主菜单上，加上用量明细
        一共 18 行、约 450 逻辑 px，175% 缩放下要吃掉近 2/3 屏高，右键一弹出来
        就是一大片灰字。现在主菜单只剩一行「外观」，点开才是这四项。

        托盘模式下没有卡片可调，with_size=False 就不列卡片大小。
        """
        with LOCK:
            cur = state.get('theme') or 'auto'
            sty = cur_style(state)
            op = cur_opacity(state)
            cur_z = state.get('card_size')
        rows = [(f'颜色：{THEME_NAME.get(cur, "跟随系统")}', menu_rows('theme')),
                (f'样式：{STYLE_NAME.get(sty, "原始")}', menu_rows('style')),
                (f'透明度：{op}%', menu_rows('opacity'))]
        if with_size and float_win32:
            n = len(float_win32.CARD_SIZES)
            if not isinstance(cur_z, int) or not 0 <= cur_z < n:
                cur_z = float_win32.DEFAULT_CARD_IDX
            rows.append((f'卡片大小：{float_win32.CARD_SIZES[cur_z][0]}',
                         menu_rows('size')))
        return [('外观', rows)]

    def set_card_size(idx):
        """切到指定档位（0 小 / 1 中 / 2 大）。菜单子菜单和循环调用都走这里。"""
        n = len(float_win32.CARD_SIZES)
        if not isinstance(idx, int) or not 0 <= idx < n:
            return
        with LOCK:
            state['card_size'] = idx
            state.pop('ball_size', None)      # 旧版本留下的键，别再用
            save_state(state)
        if fw is not None and float_win32:
            try:
                size = float_win32.CARD_SIZES[idx][1]
                fw.set_sizes(size, card_img=draw(float_payload(snapshot()), size))
            except Exception as e:
                log(f'resize failed: {e}')
        log(f'card_size -> {idx}')

    def cycle_size():
        """卡片大小 3 档循环：小 → 中 → 大 → 小。"""
        n = len(float_win32.CARD_SIZES)
        with LOCK:
            cur = state.get('card_size')
        if not isinstance(cur, int) or not 0 <= cur < n:
            cur = float_win32.DEFAULT_CARD_IDX
        set_card_size((cur + 1) % n)

    def menu_tray():
        """托盘菜单：信息在上（一眼看到额度），操作在下。托盘没有卡片可调，不列大小。"""
        st = snapshot()
        items = [('Codex 用量', False, None, menu_sec)] + detail_rows(st, True)
        items.append(('-', False, None))
        items.append(('立即刷新', True, do_refresh))
        items.extend(display_items(with_size=False))
        items.append(('-', False, None))
        if float_win32:
            items.append(('切到桌面悬浮窗', True, lambda: _set_mode(state, 'float')))
        with LOCK:
            on = takeover_enabled(state)
        items.append((f'自动接管任务栏启动：{"开" if on else "关"}', True,
                      lambda: _toggle_takeover(state)))
        items.append(('退出', True, lambda: os._exit(0)))
        return items

    def undock_float():
        if fw is not None:
            fw.undock()

    def menu_float():
        """悬浮窗菜单，按「操作 → 显示 → 位置 → 信息 → 其他」分段。

        以前把用量明细夹在中间，右键时一大块灰字顶着卡片，既挡视线又不好扫。
        现在操作项全在上半，明细沉到最下面，用分隔线隔开。
        """
        st = snapshot()
        docked = bool(fw is not None and fw.dock_edge)
        items = [('立即刷新', True, do_refresh), ('-', False, None)]
        items.extend(display_items())
        items.append(('-', False, None))
        # 贴边是拖到边缘自动发生的，不再单列「贴到最近的屏幕边缘」；
        # 只有已经贴边时才给一条退回自由浮动的出口。
        if docked:
            items.append(('取消贴边，改为自由浮动', True, undock_float))
        items.append(('固定在任务栏', True, lambda: _set_mode(state, 'tray')))
        items.append(('-', False, None))
        items.append(('Codex 用量', False, None, menu_sec))
        items.extend(detail_rows(st, False))
        items.append(('-', False, None))
        with LOCK:
            on = takeover_enabled(state)
        items.append((f'自动接管任务栏启动：{"开" if on else "关"}', True,
                      lambda: _toggle_takeover(state)))
        items.append(('退出', True, lambda: os._exit(0)))
        return items

    txt, color, sub = tray_text(state)
    tray = TrayIcon(uid=7311, tooltip=tooltip(state), menu_provider=menu_tray)
    if not tray.start(make_icon(txt, color, sub)):
        log('tray start failed')
        return
    log(f'tray started, Shell_NotifyIcon={tray.last_add_result}')

    fw = None
    mode = state.get('display_mode', 'tray')
    if mode == 'float' and not float_win32:
        mode = 'tray'
        state['display_mode'] = 'tray'
    if mode == 'float':
        tray.hide()

    def on_move(pos):
        with LOCK:
            state['float_pos'] = [int(pos[0]), int(pos[1])]
            save_state(state)

    def on_dock(payload):
        """贴边状态变化：贴边时记住边与沿边偏移，取消贴边时记回自由坐标"""
        with LOCK:
            state['float_dock'] = payload
            if not payload and fw is not None and fw.pos:
                state['float_pos'] = [int(fw.pos[0]), int(fw.pos[1])]
            save_state(state)
        log('dock -> ' + (f'{payload["edge"]}@{payload["offset"]}' if payload else 'free'))

    def handle_img(edge):
        return float_win32.render(float_payload(snapshot()), fw.scale,
                                  is_dark_mode(state), size=fw.size,
                                  handle_edge=edge)

    def ensure_float():
        nonlocal fw
        if fw is None:
            fw = float_win32.FloatWindow(menu_provider=menu_float, on_move=on_move,
                                         on_dock=on_dock, handle_provider=handle_img,
                                         size=float_win32.CARD_SIZES[card_idx()][1],
                                         theme_provider=lambda: is_dark_mode(state))
            pos = state.get('float_pos')
            dock = state.get('float_dock')
            img = float_win32.render(float_payload(snapshot()), fw.scale,
                                     is_dark_mode(state), size=fw.size,
                                     style=style_now(), opacity=opacity_now())
            if not fw.start(img, pos=pos, dock=dock):
                log('float start failed')
                return False
            if dock:
                log(f'float docked {dock["edge"]}@{dock["offset"]}')
            elif pos and tuple(pos) != tuple(fw.pos):
                # 旧坐标被拉回屏幕内了，写回，免得每次启动都纠一次
                with LOCK:
                    state['float_pos'] = [int(fw.pos[0]), int(fw.pos[1])]
                    save_state(state)
            log(f'float started at {fw.pos} (scale={fw.scale})')
        return True

    last = {'tray': (txt, color, sub), 'float': None}
    tick = 0
    while True:
        time.sleep(2.0)
        try:
            cur = state.get('display_mode', 'tray')
            if cur != mode:
                if cur == 'float' and float_win32:
                    if ensure_float():
                        fw.show()
                        tray.hide()
                        mode = 'float'
                        last['float'] = None
                        log('switch -> float')
                    else:
                        state['display_mode'] = 'tray'
                else:
                    if fw:
                        fw.hide()
                    tray.show()
                    mode = 'tray'
                    log('switch -> tray')

            st = snapshot()
            if mode == 'float':
                if fw is None and not ensure_float():
                    continue
                # 尺寸档位可能在菜单里被改过（也可能是我手改 state.json 测试），
                # 每轮对齐一次：不一致就重算窗口尺寸与贴边坐标
                want = float_win32.CARD_SIZES[card_idx()][1]
                p = float_payload(st)
                if tuple(fw.size) != want:
                    # 换卡片尺寸必须先渲染新图一起递进去，否则旧图会把窗口
                    # 拉回旧尺寸（表现为"尺寸抽一下"）
                    img = draw(p, want)
                    fw.set_sizes(want, card_img=img)
                    last['float'] = None
                key = (p['reset5'], p['date'],
                       None if p['p1'] is None else round(p['p1']),
                       None if p['p2'] is None else round(p['p2']))
                # 顶部已经没有倒计时，不必按分钟重画；数值或重置时间变了才画，
                # 另外每 30 秒兜底刷一次（数据源卡住或跨零点也能自己纠正）
                if key != last['float'] or tick % 15 == 0:
                    fw.update(draw(p))
                    last['float'] = key
            else:
                txt, color, sub = tray_text(st)
                key = (txt, color, sub)
                img = make_icon(txt, color, sub) if key != last['tray'] else None
                tray.update(img, tooltip(st))
                last['tray'] = key
            tick += 1
        except Exception as e:
            log(f'ui loop failed: {e}')


# ---------------------------------------------------------------- 主循环

def main():
    # 悬浮窗要按物理像素绘制才不糊；必须在创建任何窗口之前设置
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)      # PER_MONITOR_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    # 单实例：避免开机自启 + 手动双击 时出现两个图标
    try:
        import ctypes
        ctypes.windll.kernel32.CreateMutexW(None, False, 'CodexUsageWatch_SingleInstance')
        if ctypes.windll.kernel32.GetLastError() == 183:   # ERROR_ALREADY_EXISTS
            return
    except Exception:
        pass

    os.makedirs(BASE, exist_ok=True)
    state = load_state()
    seed_from_logs(state)
    rebuild_cdp_line(state)
    offsets = state.setdefault('offsets', {})
    turns = {}

    files = scan_files()
    for f in files:
        if f not in offsets:
            try:
                offsets[f] = initial_log_offset(f)
            except OSError:
                continue
    save_state(state)
    log(f'watcher started, tracking {len(files)} files')

    if '--no-tray' not in sys.argv:
        threading.Thread(target=run_ui, args=(state,), daemon=True).start()
    threading.Thread(target=cdp_loop, args=(state,), daemon=True).start()
    if '--test' not in sys.argv and '--no-takeover' not in sys.argv:
        threading.Thread(target=takeover_loop, args=(state,), daemon=True).start()

    while True:
        try:
            for f in scan_files():
                saved_offset = offsets.get(f, 0)
                try:
                    size = os.path.getsize(f)
                    if size == saved_offset:
                        continue
                    off = saved_offset
                    if size < off:
                        turns.pop(f, None)
                        off = 0
                    with open(f, 'rb') as fh:
                        fh.seek(off)
                        chunk = fh.read()
                except OSError:
                    continue
                # JSONL 写入可能跨越两次轮询。偏移只提交到最后一个换行。
                complete = chunk.rfind(b'\n') + 1
                next_offset = off + complete
                if not complete and next_offset == saved_offset:
                    continue
                turn = turns.setdefault(f, Turn())
                for line in chunk[:complete].split(b'\n'):
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line.decode('utf-8', errors='replace'))
                    except ValueError:
                        continue
                    try:
                        with LOCK:
                            if not handle_line(obj, state, turn):
                                continue
                            state['last_turn'] = turn.total
                            title, lines, app_line = build_notification(turn, state)
                            if app_line:
                                state['cdp_line'] = app_line
                            turn.reset()
                        ok = notify(title, lines)
                        if app_line:
                            cdp_push(app_line)
                        log(('notified' if ok else 'notify-failed') + ' | ' + ' / '.join(lines))
                    except Exception as e:
                        log(f'handle error: {e}')
                if not turn.active:
                    turns.pop(f, None)
                with LOCK:
                    offsets[f] = next_offset
                    save_state(state)
            time.sleep(POLL)
        except KeyboardInterrupt:
            break
        except Exception as e:
            log(f'loop error: {e}')
            time.sleep(3)


if __name__ == '__main__':
    if '--test' in sys.argv:
        t = Turn()
        t.active = True
        t.inp, t.out, t.total = 11020, 1460, 12480
        st = {'rate_limits': {'primary': {'used_percent': 13.0, 'window_minutes': 300,
                                          'resets_at': int(time.time()) + 4 * 3600},
                              'secondary': {'used_percent': 1.4, 'window_minutes': 10080,
                                            'resets_at': int(time.time()) + 6 * 86400},
                              'credits': {'has_credits': True, 'balance': None}},
              'last_used': {'primary': 10.0, 'secondary': 1.0}}
        title, lines, app_line = build_notification(t, st)
        print(title)
        print('\n'.join(lines))
        print('app  ->', app_line)
        print('notify ->', notify(title, lines))
        print('inject ->', cdp_push(app_line))
    elif '--preview' in sys.argv:
        # 生成托盘图标预览图
        sizes = [('87', (22, 163, 74), '周99'), ('42', (217, 119, 6), '周99'),
                 ('12', (220, 38, 38), '周99'), ('--', (100, 116, 139), None)]
        canvas_w, canvas_h = 64 * len(sizes) + 20 * (len(sizes) + 1), 104
        canvas = __import__('PIL.Image', fromlist=['x']).new('RGBA', (canvas_w, canvas_h), (32, 33, 36, 255))
        x = 20
        for txt, col, sub in sizes:
            canvas.paste(make_icon(txt, col, sub), (x, 20), make_icon(txt, col, sub))
            x += 84
        out = os.path.join(BASE, 'tray_preview.png')
        canvas.save(out)
        print('saved', out)
    else:
        main()
