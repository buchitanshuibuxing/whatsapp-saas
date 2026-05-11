#!/usr/bin/env python3
"""WhatsApp Management Web Server - proxies WAHA API and serves static UI"""
import http.server
import urllib.request
import urllib.error
import json
import os
import sys
import io
import base64

WAHA_URL = "http://127.0.0.1:3000"
API_KEY = "669317c2d2f44cefab5b730b25472ba3"

class WhatsAppProxy(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory="/opt/whatsapp-saas/uploads", **kwargs)

    def do_GET(self):
        if self.path.startswith("/proxy/"):
            self.proxy_request("GET")
        elif self.path == "/" or self.path == "/index.html":
            self.serve_index()
        elif self.path == "/status":
            self.get_status()
        elif self.path == "/qr":
            self.get_qr()
        else:
            super().do_GET()

    def do_POST(self):
        if self.path.startswith("/proxy/"):
            self.proxy_request("POST")
        elif self.path == "/connect":
            self.connect_whatsapp()
        else:
            self.send_error(404)

    def proxy_request(self, method):
        """Proxy to WAHA API"""
        wa_path = self.path.replace("/proxy", "/api", 1)
        wa_url = WAHA_URL + wa_path
        if self.path.count("/proxy/") > 0:
            wa_url = WAHA_URL + self.path.replace("/proxy", "", 1)

        try:
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len) if content_len > 0 else None
        except:
            body = None

        req = urllib.request.Request(wa_url, data=body, method=method)
        req.add_header("X-Api-Key", API_KEY)
        if body:
            req.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                self.send_response(resp.status)
                for k, v in resp.getheaders():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(resp.read())
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(e.read())
        except Exception as e:
            self.send_error(502, str(e))

    def serve_index(self):
        html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>WhatsApp 管理中心</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: linear-gradient(135deg, #075E54 0%, #128C7E 50%, #25D366 100%);
            min-height: 100vh; display: flex; align-items: center; justify-content: center;
        }
        .card {
            background: white; border-radius: 16px; padding: 40px;
            max-width: 480px; width: 90%; box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            text-align: center;
        }
        h1 { color: #075E54; font-size: 28px; margin-bottom: 8px; }
        .subtitle { color: #667781; font-size: 14px; margin-bottom: 32px; }
        .status-badge {
            display: inline-block; padding: 6px 16px; border-radius: 20px;
            font-size: 13px; font-weight: 600; margin-bottom: 24px;
        }
        .status-STOPPED { background: #f0f2f5; color: #667781; }
        .status-STARTING { background: #FFF3CD; color: #856404; }
        .status-SCAN_QR_CODE { background: #D4EDDA; color: #155724; }
        .status-WORKING { background: #D4EDDA; color: #155724; }
        .status-FAILED { background: #F8D7DA; color: #721C24; }
        .qr-container {
            background: white; padding: 16px; border-radius: 12px;
            border: 2px dashed #25D366; margin: 20px 0; display: none;
        }
        .qr-container img { max-width: 256px; width: 100%; }
        .qr-hint { color: #667781; font-size: 11px; margin-top: 8px; }
        button {
            background: #25D366; color: white; border: none;
            padding: 14px 32px; border-radius: 24px; font-size: 16px;
            font-weight: 600; cursor: pointer; width: 100%;
            transition: all 0.2s;
        }
        button:hover { background: #1ebe5b; transform: translateY(-1px); }
        button:disabled { background: #ccc; cursor: not-allowed; transform: none; }
        .info { color: #667781; font-size: 13px; margin-top: 16px; }
        .error { color: #721C24; background: #F8D7DA; padding: 12px; border-radius: 8px; margin: 12px 0; font-size: 13px; }
        .loading { display: inline-block; width: 20px; height: 20px; border: 3px solid #ddd; border-top-color: #25D366; border-radius: 50%; animation: spin 0.8s linear infinite; margin-left: 8px; vertical-align: middle; }
        @keyframes spin { to { transform: rotate(360deg); } }
    </style>
</head>
<body>
    <div class="card">
        <h1>WhatsApp</h1>
        <p class="subtitle">设备链接管理中心</p>

        <div id="statusBadge" class="status-badge status-STOPPED">未连接</div>

        <div id="qrContainer" class="qr-container">
            <img id="qrImage" src="" alt="QR Code">
            <p class="qr-hint">请用 WhatsApp 手机 App 扫描二维码</p>
            <p class="qr-hint" style="color: #dc3545;">二维码有效期仅 20 秒，请尽快扫描</p>
        </div>

        <div id="errorBox" class="error" style="display:none;"></div>

        <button id="connectBtn" onclick="connectWhatsApp()">
            <span id="btnText">连接 WhatsApp</span>
            <span id="btnLoading" class="loading" style="display:none;"></span>
        </button>

        <p id="infoText" class="info">点击按钮开始连接 WhatsApp 设备</p>
    </div>

    <script>
        let polling = null;

        async function connectWhatsApp() {
            const btn = document.getElementById('connectBtn');
            const btnText = document.getElementById('btnText');
            const btnLoading = document.getElementById('btnLoading');
            const errorBox = document.getElementById('errorBox');
            document.getElementById('qrContainer').style.display = 'none';

            btn.disabled = true;
            btnText.textContent = '正在连接...';
            btnLoading.style.display = 'inline-block';
            errorBox.style.display = 'none';
            updateStatus('STARTING', '连接中...');

            try {
                // Step 1: Start session (auto-creates)
                let resp = await fetch('/whatsapp/connect', { method: 'POST' });
                let data = await resp.json();

                if (!resp.ok && resp.status !== 201) {
                    throw new Error(data.message || '启动失败');
                }

                updateStatus('STARTING', '正在建立连接...');
                document.getElementById('infoText').textContent = '正在通过代理连接 WhatsApp 服务器...';

                // Step 2: Poll for QR code
                pollForQR();

            } catch (err) {
                errorBox.textContent = '错误: ' + err.message;
                errorBox.style.display = 'block';
                updateStatus('FAILED', '连接失败');
                btn.disabled = false;
                btnText.textContent = '重试连接';
                btnLoading.style.display = 'none';
            }
        }

        function pollForQR() {
            let attempts = 0;
            const maxAttempts = 120;

            polling = setInterval(async () => {
                attempts++;
                try {
                    let resp = await fetch('/whatsapp/status');
                    let data = await resp.json();

                    if (data.status === 'SCAN_QR_CODE') {
                        updateStatus('SCAN_QR_CODE', '等待扫码');
                        document.getElementById('infoText').textContent = '二维码已生成，请尽快扫描';
                        document.getElementById('qrImage').src = '/whatsapp/qr?t=' + Date.now();
                        document.getElementById('qrContainer').style.display = 'block';
                        document.getElementById('connectBtn').disabled = false;
                        document.getElementById('btnText').textContent = '获取新二维码';
                        document.getElementById('btnLoading').style.display = 'none';

                    } else if (data.status === 'WORKING') {
                        clearInterval(polling);
                        updateStatus('WORKING', '已连接');
                        document.getElementById('qrContainer').style.display = 'none';
                        document.getElementById('infoText').textContent = 'WhatsApp 已成功连接！';
                        document.getElementById('connectBtn').disabled = true;
                        document.getElementById('btnText').textContent = '已连接';
                        document.getElementById('btnLoading').style.display = 'none';

                    } else if (data.status === 'FAILED') {
                        clearInterval(polling);
                        updateStatus('FAILED', '连接失败');
                        document.getElementById('errorBox').textContent = '连接失败，请重试';
                        document.getElementById('errorBox').style.display = 'block';
                        document.getElementById('connectBtn').disabled = false;
                        document.getElementById('btnText').textContent = '重试连接';
                        document.getElementById('btnLoading').style.display = 'none';

                    } else if (attempts >= maxAttempts) {
                        clearInterval(polling);
                        updateStatus('FAILED', '超时');
                        document.getElementById('errorBox').textContent = '连接超时，请检查代理并重试';
                        document.getElementById('errorBox').style.display = 'block';
                        document.getElementById('connectBtn').disabled = false;
                        document.getElementById('btnText').textContent = '重试连接';
                        document.getElementById('btnLoading').style.display = 'none';
                    }

                } catch (err) {
                    console.error('Poll error:', err);
                }
            }, 1000);
        }

        function updateStatus(status, label) {
            const badge = document.getElementById('statusBadge');
            badge.textContent = label;
            badge.className = 'status-badge status-' + status;
        }

        // Check initial status
        (async () => {
            try {
                let resp = await fetch('/whatsapp/status');
                let data = await resp.json();
                if (data.status === 'WORKING') {
                    updateStatus('WORKING', '已连接');
                    document.getElementById('infoText').textContent = 'WhatsApp 已成功连接！';
                    document.getElementById('connectBtn').disabled = true;
                    document.getElementById('btnText').textContent = '已连接';
                } else if (data.status === 'SCAN_QR_CODE') {
                    updateStatus('SCAN_QR_CODE', '等待扫码');
                    document.getElementById('qrImage').src = '/whatsapp/qr';
                    document.getElementById('qrContainer').style.display = 'block';
                    document.getElementById('infoText').textContent = '二维码已存在，请尽快扫描';
                }
            } catch (e) {}
        })();
    </script>
</body>
</html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def get_status(self):
        """Get WAHA session status"""
        try:
            req = urllib.request.Request(f"{WAHA_URL}/api/sessions/default")
            req.add_header("X-Api-Key", API_KEY)
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"status": data.get("status", "UNKNOWN")}).encode())
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "STOPPED"}).encode())
        except Exception as e:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ERROR", "error": str(e)}).encode())

    def get_qr(self):
        """Get QR code image"""
        try:
            req = urllib.request.Request(f"{WAHA_URL}/api/screenshot?session=default")
            req.add_header("X-Api-Key", API_KEY)
            with urllib.request.urlopen(req, timeout=10) as resp:
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(resp.read())
        except Exception as e:
            self.send_error(502, str(e))

    def connect_whatsapp(self):
        """Full connect flow: stop, configure proxy, start"""
        try:
            # Stop if running
            req = urllib.request.Request(f"{WAHA_URL}/api/sessions/default/stop", method="POST")
            req.add_header("X-Api-Key", API_KEY)
            try: urllib.request.urlopen(req, timeout=10)
            except: pass

            import time
            time.sleep(1)

            # Configure proxy
            body = json.dumps({"config": {"proxy": {"server": "127.0.0.1:10808"}}}).encode()
            req = urllib.request.Request(f"{WAHA_URL}/api/sessions/default", data=body, method="PUT")
            req.add_header("X-Api-Key", API_KEY)
            req.add_header("Content-Type", "application/json")
            try: urllib.request.urlopen(req, timeout=10)
            except: pass

            time.sleep(1)

            # Start (auto-creates if needed)
            req = urllib.request.Request(f"{WAHA_URL}/api/sessions/default/start", method="POST")
            req.add_header("X-Api-Key", API_KEY)
            resp = urllib.request.urlopen(req, timeout=10)
            data = resp.read()

            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)

        except Exception as e:
            self.send_error(500, str(e))

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8800
    print(f"WhatsApp Management Server on port {port}")
    http.server.HTTPServer(("0.0.0.0", port), WhatsAppProxy).serve_forever()
