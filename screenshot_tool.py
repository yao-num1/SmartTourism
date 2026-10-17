# -*- coding: utf-8 -*-
"""
真实产品截屏工具（纯标准库，无需 playwright/selenium）。

原理：以远程调试模式启动本机 Edge/Chrome，通过 Chrome DevTools Protocol(CDP)
逐页导航、执行 JS（切换标签/打开弹窗/等待渲染）、抓取 PNG 截屏。
可交互、可等待异步渲染，得到的是真实产品界面截图。

用法：
    1) 先启动业务服务： python server/app.py
    2) python screenshot_tool.py [输出目录]
       默认输出到 参赛完善包/screenshots/

说明：会以可见窗口打开浏览器（截屏需要真实渲染），结束后自动关闭调试实例。
"""
import base64
import json
import os
import socket
import struct
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(HERE, '参赛完善包', 'screenshots')
PORT = 9222
PROFILE = 'D:/_edge_cdp_profile'

EDGE_CANDIDATES = [
    r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
    r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
    r'C:\Program Files\Google\Chrome\Application\chrome.exe',
    r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
]

BASE = 'http://127.0.0.1:5050'

# (文件名, 路径/哈希, 前置JS, 等待毫秒)
SHOTS = [
    ('01_home',      '/',                        None, 2500),
    ('02_chat',      '/#conv=0b338bb5-c998-47e9-9608-07224e2bd5c6',    None, 4000),
    ('03_plan',      '/#plan=ITI20260920230201220', None, 13000),
    ('04_marketing', '/#page=marketing',        None, 4000),
    ('05_auth',      '/#auth',                  None, 2000),
    ('06_card_preview', '/#card=c91d54abf52',   None, 3000),
    ('07_profile',   '/#page=profile',          None, 2500),
    ('08_digital_card', '/static/digital-card.html?id=c91d54abf52', None, 3500),
]


# ---------- 极简 WebSocket 客户端（标准库实现） ----------
class WS:
    def __init__(self, url):
        # ws://host:port/path
        rest = url.split('://', 1)[1]
        hostport, path = rest.split('/', 1)
        host, port = hostport.split(':')
        self.sock = socket.create_connection((host, int(port)), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            'GET /%s HTTP/1.1\r\n' % path
            + 'Host: %s\r\n' % hostport
            + 'Upgrade: websocket\r\nConnection: Upgrade\r\n'
            + 'Sec-WebSocket-Key: %s\r\n' % key
            + 'Sec-WebSocket-Version: 13\r\n\r\n'
        )
        self.sock.sendall(req.encode())
        resp = b''
        while b'\r\n\r\n' not in resp:
            resp += self.sock.recv(4096)
        if b'101' not in resp.split(b'\r\n')[0]:
            raise RuntimeError('websocket handshake failed: %r' % resp.split(b'\r\n')[0])

    def _recv_exact(self, n):
        buf = b''
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise RuntimeError('ws closed')
            buf += chunk
        return buf

    def send(self, text):
        payload = text.encode()
        mask = os.urandom(4)
        header = bytearray([0x81])
        L = len(payload)
        if L < 126:
            header.append(0x80 | L)
        elif L < 65536:
            header.append(0x80 | 126); header += struct.pack('>H', L)
        else:
            header.append(0x80 | 127); header += struct.pack('>Q', L)
        header += mask
        masked = bytearray(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + bytes(masked))

    def recv(self):
        # 返回一条文本消息（跳过 ping/pong/continuation 简化处理）
        while True:
            b1, b2 = self._recv_exact(2)
            opcode = b1 & 0x0F
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack('>H', self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack('>Q', self._recv_exact(8))[0]
            data = self._recv_exact(length) if length else b''
            if opcode == 0x1:
                return data.decode('utf-8', 'ignore')
            if opcode == 0x8:
                raise RuntimeError('ws closed by server')
            # 0x9 ping / 0xA pong / 0x0 cont 忽略继续


class CDP:
    def __init__(self, ws):
        self.ws = ws
        self.id = 0

    def call(self, method, params=None, timeout=30):
        self.id += 1
        msg = {'id': self.id, 'method': method}
        if params:
            msg['params'] = params
        self.ws.send(json.dumps(msg))
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = self.ws.recv()
            d = json.loads(raw)
            if d.get('id') == self.id:
                if 'error' in d:
                    raise RuntimeError('%s error: %s' % (method, d['error']))
                return d.get('result', {})
        raise RuntimeError('timeout: %s' % method)


def http_json(url, method='GET'):
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def main():
    os.makedirs(OUT, exist_ok=True)
    exe = next((p for p in EDGE_CANDIDATES if os.path.exists(p)), None)
    if not exe:
        print('[!] 未找到 Edge/Chrome，无法截屏'); sys.exit(1)
    proc = subprocess.Popen([
        exe, '--remote-debugging-port=%d' % PORT,
        '--user-data-dir=%s' % PROFILE,
        '--window-size=1600,1000', '--no-first-run', '--no-default-browser-check',
        'about:blank',
    ])
    try:
        # 等待调试端口
        for _ in range(40):
            try:
                http_json('http://127.0.0.1:%d/json/version' % PORT); break
            except Exception:
                time.sleep(0.5)
        else:
            print('[!] 调试端口未就绪'); sys.exit(1)

        for name, path, js, wait in SHOTS:
            url = BASE + path
            try:
                target = http_json('http://127.0.0.1:%d/json/new?%s' % (PORT, url), method='PUT')
            except Exception:
                target = http_json('http://127.0.0.1:%d/json/new?%s' % (PORT, url))
            ws = WS(target['webSocketDebuggerUrl'])
            cdp = CDP(ws)
            cdp.call('Page.enable')
            cdp.call('Page.navigate', {'url': url})
            time.sleep(wait / 1000.0)
            if js:
                cdp.call('Runtime.evaluate', {'expression': js})
                time.sleep(1.0)
            shot = cdp.call('Page.captureScreenshot', {'format': 'png'}, timeout=30)
            png = base64.b64decode(shot['data'])
            outp = os.path.join(OUT, name + '.png')
            with open(outp, 'wb') as f:
                f.write(png)
            print('[OK] %s (%d bytes)' % (outp, len(png)))
            # 关闭该标签
            try:
                urllib.request.urlopen('http://127.0.0.1:%d/json/close/%s' % (PORT, target['id']), timeout=5)
            except Exception:
                pass
            ws.sock.close()
    finally:
        proc.terminate()
    print('[DONE] 截屏完成，输出目录:', OUT)


if __name__ == '__main__':
    main()
