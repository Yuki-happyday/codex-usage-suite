"""CDP 客户端（browser 级）：可 attach 到 webview / 多 page，执行 JS

Codex 桌面版的主界面渲染在 webview 里，/json/list 只给到外壳页，
所以必须走 browser endpoint + Target.attachToTarget(flatten=True)。
"""
import json
import urllib.request

import websocket

PORT = 9222


class Cdp:
    def __init__(self, port=PORT, timeout=20):
        v = json.load(urllib.request.urlopen(f'http://127.0.0.1:{port}/json/version', timeout=5))
        self.ws = websocket.create_connection(v['webSocketDebuggerUrl'], timeout=timeout,
                                             max_size=64 * 1024 * 1024)
        self._id = 0

    def send(self, method, params=None, session_id=None, timeout=None):
        self._id += 1
        mid = self._id
        msg = {'id': mid, 'method': method, 'params': params or {}}
        if session_id:
            msg['sessionId'] = session_id
        if timeout:
            self.ws.settimeout(timeout)
        self.ws.send(json.dumps(msg))
        while True:
            m = json.loads(self.ws.recv())
            if m.get('id') == mid:
                return m
            # 其它事件忽略

    def targets(self):
        r = self.send('Target.getTargets')
        return r.get('result', {}).get('targetInfos', [])

    def attach(self, target_id):
        r = self.send('Target.attachToTarget', {'targetId': target_id, 'flatten': True})
        return r.get('result', {}).get('sessionId')

    def js(self, expr, session_id=None, timeout=15):
        r = self.send('Runtime.evaluate',
                      {'expression': expr, 'returnByValue': True, 'awaitPromise': True},
                      session_id=session_id, timeout=timeout)
        res = r.get('result', {})
        if 'exceptionDetails' in res:
            return {'error': res['exceptionDetails'].get('text'),
                    'detail': json.dumps(res['exceptionDetails'], ensure_ascii=False)[:600]}
        return res.get('result', {}).get('value')

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def find_chat_target(c, probe='接下来四周'):
    """返回 (sessionId, targetInfo)：内容是对话正文的那个 page"""
    out = []
    for t in c.targets():
        if t.get('type') not in ('page', 'webview'):
            continue
        sid = c.attach(t['targetId'])
        if not sid:
            continue
        try:
            txt = c.js('(document.body?document.body.innerText:"")', sid) or ''
        except Exception:
            txt = ''
        out.append((sid, t, len(txt) if isinstance(txt, str) else 0, probe in str(txt)))
    for sid, t, n, hit in out:
        if hit:
            return sid, t
    out.sort(key=lambda x: -x[2])
    return (out[0][0], out[0][1]) if out else (None, None)


if __name__ == '__main__':
    import sys
    c = Cdp()
    sid, t = find_chat_target(c)
    print('target:', t and t.get('url'), '| title:', t and t.get('title'))
    if sid:
        print(c.js(sys.argv[1] if len(sys.argv) > 1 else 'document.body.innerText.length', sid))
    c.close()
