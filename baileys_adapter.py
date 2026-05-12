#!/usr/bin/env python3
"""
Baileys Engine Adapter — 封装 Baileys Engine REST API
替换所有 WAHA 直接调用，提供会话管理、消息发送、Webhook 处理
"""
import json
import urllib.request
import urllib.error
import sqlite3
import os
import time
from translation_engine import TranslationManager

# Baileys Engine 地址（内部服务，不走 Xray 代理）
BAILEYS_API = 'http://127.0.0.1:3500/api'
BAILEYS_KEY = 'baileys-engine-internal-key-2026'

DB_PATH = '/opt/whatsapp-saas/admin.db'


def _baileys_request(method, path, body=None, timeout=20):
    """统一 Baileys Engine HTTP 请求"""
    url = f'{BAILEYS_API}{path}'
    data = json.dumps(body).encode() if body else None
    headers = {
        'X-Api-Key': BAILEYS_KEY,
        'Content-Type': 'application/json',
    }
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        msg = {}
        try:
            msg = json.loads(e.read())
        except Exception:
            pass
        return {'error': msg.get('error', str(e)), 'status_code': e.code}
    except Exception as e:
        return {'error': str(e), 'status_code': 500}


def create_session(session_name, start=True, config=None):
    """创建会话"""
    return _baileys_request('POST', '/sessions', {
        'name': session_name,
        'start': start,
        'config': config or {},
    })


def get_session(session_name):
    """获取会话状态"""
    return _baileys_request('GET', f'/sessions/{session_name}')


def list_sessions():
    """列出所有会话"""
    return _baileys_request('GET', '/sessions')


def start_session(session_name):
    """启动会话"""
    return _baileys_request('POST', f'/sessions/{session_name}/start')


def stop_session(session_name):
    """停止会话"""
    return _baileys_request('POST', f'/sessions/{session_name}/stop')


def logout_session(session_name):
    """登出会话（清除认证）"""
    return _baileys_request('POST', f'/sessions/{session_name}/logout')


def restart_session(session_name):
    """重启会话"""
    return _baileys_request('POST', f'/sessions/{session_name}/restart')


def delete_session(session_name):
    """删除会话"""
    return _baileys_request('DELETE', f'/sessions/{session_name}')


def get_qr(session_name):
    """获取 QR 码 PNG 数据 — 返回 (bytes, content_type)"""
    url = f'{BAILEYS_API}/sessions/{session_name}/qr'
    headers = {'X-Api-Key': BAILEYS_KEY}
    req = urllib.request.Request(url, headers=headers, method='GET')
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        return resp.read(), resp.headers.get('Content-Type', 'image/png')
    except urllib.error.HTTPError as e:
        return None, str(e.code)
    except Exception as e:
        return None, str(e)


def send_text(session_name, chat_id, text):
    """发送文本消息"""
    return _baileys_request('POST', '/sendText', {
        'chatId': chat_id,
        'text': text,
        'session': session_name,
    })


def get_profile(session_name):
    """获取自己的资料"""
    return _baileys_request('GET', f'/{session_name}/profile')


def set_webhook(url, secret=None):
    """配置 webhook"""
    return _baileys_request('POST', '/webhook', {'url': url, 'secret': secret})


def get_webhook():
    """获取 webhook 配置"""
    return _baileys_request('GET', '/webhook')


def health_check():
    """健康检查"""
    url = f'{BAILEYS_API.replace("/api", "")}/health'
    try:
        resp = urllib.request.urlopen(url, timeout=5)
        return json.loads(resp.read())
    except Exception as e:
        return {'error': str(e)}


# ================================================================
# Webhook 处理 — 接收来自 Baileys Engine 的入站消息
# ================================================================

def handle_incoming_webhook(payload):
    """
    处理 Baileys Engine 推送的 webhook 消息。
    payload 格式:
    {
        "event": "message",
        "session": "tenant_username",
        "data": {
            "key": {remoteJid, fromMe, id, ...},
            "message": {conversation, extendedTextMessage, ...},
            "pushName": "...",
            "messageTimestamp": 1734567890
        }
    }
    """
    if payload.get('event') != 'message':
        return {'status': 'ignored', 'reason': 'not a message event'}

    session_name = payload.get('session', '')
    data = payload.get('data', {})
    msg_key = data.get('key', {})
    msg_content = data.get('message', {})
    push_name = data.get('pushName', '')

    remote_jid = msg_key.get('remoteJid', '')
    from_me = msg_key.get('fromMe', False)

    # 只处理入站消息（非自己发出的）
    if from_me:
        return {'status': 'ignored', 'reason': 'outgoing message'}

    # 提取文本内容
    text = ''
    if 'conversation' in msg_content:
        text = msg_content['conversation']
    elif 'extendedTextMessage' in msg_content:
        text = msg_content['extendedTextMessage'].get('text', '')
    elif 'imageMessage' in msg_content:
        text = msg_content['imageMessage'].get('caption', '[图片]') or '[图片]'
    elif 'videoMessage' in msg_content:
        text = msg_content['videoMessage'].get('caption', '[视频]') or '[视频]'
    elif 'audioMessage' in msg_content:
        text = '[语音]'
    elif 'documentMessage' in msg_content:
        text = msg_content['documentMessage'].get('fileName', '[文件]') or '[文件]'
    elif 'stickerMessage' in msg_content:
        text = '[贴纸]'
    else:
        text = '[消息]'

    if not remote_jid or not text:
        return {'status': 'ignored', 'reason': 'empty content'}

    # 查找对应的租户
    try:
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                "SELECT id, company_name FROM tenant_users WHERE username = ? AND is_active = 1",
                (session_name,)
            ).fetchone()

            if not row:
                return {'status': 'error', 'reason': f'tenant not found: {session_name}'}

            tenant_id = row['id']

            # 存储消息到本地 DB
            conn.execute(
                "INSERT INTO message_logs (tenant_id, direction, remote_jid, message_type, content, status) "
                "VALUES (?, 'in', ?, 'text', ?, 'received')",
                (tenant_id, remote_jid, text)
            )

            # 自动翻译入站消息
            translation = None
            try:
                tm = TranslationManager(tenant_id)
                # 使用接收方向翻译
                result = tm.translate(text, direction='in')
                translation = result.get('translated', '')
            except Exception:
                pass  # 翻译失败不阻塞

            conn.commit()

            return {
                'status': 'ok',
                'tenant_id': tenant_id,
                'session': session_name,
                'from': remote_jid,
                'text': text,
                'translation': translation,
                'push_name': push_name,
            }
    except Exception as e:
        return {'status': 'error', 'reason': str(e)}


# ================================================================
# 批量翻译 — 供前端刷新后翻译已有消息
# ================================================================

def translate_incoming_for_tenant(tenant_id, texts):
    """批量翻译入站文本"""
    tm = TranslationManager(tenant_id)
    results = []
    for text in texts:
        try:
            result = tm.translate(text, direction='in')
            results.append(result)
        except Exception:
            results.append({'original': text, 'translated': '', 'error': 'translation failed'})
    return results
