"""极简 CDP 客户端：连 ChatGPT(Codex) 桌面版的调试端口，执行 JS"""
import json
import urllib.request

import websocket

PORT = 9222


def pages(port=PORT):
    try:
        d = json.load(urllib.request.urlopen(f'http://127.0.0.1:{port}/json/list', timeout=5))
    except Exception:
        return []
    return [t for t in d if t.get('type') == 'page' and t.get('webSocketDebuggerUrl')]


def _conn(port=PORT, timeout=15):
    ps = pages(port)
    if not ps:
        return None
    return websocket.create_connection(ps[0]['webSocketDebuggerUrl'], timeout=timeout,
                                       max_size=32 * 1024 * 1024)


def call(method, params=None, port=PORT, timeout=15):
    ws = _conn(port, timeout)
    if ws is None:
        return {'error': 'no page target'}
    try:
        ws.send(json.dumps({'id': 1, 'method': method, 'params': params or {}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get('id') == 1:
                return msg
    except Exception as e:
        return {'error': f'{type(e).__name__}: {e}'}
    finally:
        try:
            ws.close()
        except Exception:
            pass


def js(expr, port=PORT, timeout=15):
    r = call('Runtime.evaluate',
             {'expression': expr, 'returnByValue': True, 'awaitPromise': True},
             port=port, timeout=timeout)
    if 'error' in r and 'result' not in r:
        return {'error': r['error']}
    res = r.get('result', {})
    if 'exceptionDetails' in res:
        return {'error': res['exceptionDetails'].get('text'),
                'detail': json.dumps(res['exceptionDetails'], ensure_ascii=False)[:800]}
    return res.get('result', {}).get('value')


if __name__ == '__main__':
    import sys
    print(js(sys.argv[1] if len(sys.argv) > 1 else 'document.title'))
