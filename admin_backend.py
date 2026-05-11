#!/usr/bin/env python3
"""WhatsApp SaaS 管理后台 — Python stdlib only (sqlite3)"""
import os, json, hashlib, secrets, time, subprocess, sqlite3, re
import http.server
import socketserver
import urllib.request
import urllib.error
from translation_engine import TranslationManager, ENGINE_LABELS, ENGINE_REGISTRY

# ============ 配置 ============
DB_PATH = '/opt/whatsapp-saas/admin.db'
JWT_SECRET = secrets.token_hex(32)
TOKEN_EXPIRY = 24  # 小时
WAHA_DEFAULT_KEY = '669317c2d2f44cefab5b730b25472ba3'
WAHA_PORT_START = 3100
WAHA_PORT_END = 3199

# ============ 数据库初始化 ============
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS admin_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT DEFAULT 'admin',
                created_at TEXT DEFAULT (datetime('now')),
                last_login TEXT
            );
            CREATE TABLE IF NOT EXISTS tenant_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_name TEXT NOT NULL,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                email TEXT DEFAULT '',
                is_active INTEGER DEFAULT 1,
                whatsapp_number TEXT DEFAULT '',
                whatsapp_connected INTEGER DEFAULT 0,
                container_name TEXT UNIQUE,
                api_key TEXT UNIQUE,
                waha_port INTEGER DEFAULT 0,
                max_sessions INTEGER DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS system_settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS message_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id INTEGER,
                direction TEXT,
                remote_jid TEXT,
                message_type TEXT,
                content TEXT,
                status TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS tenant_contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id INTEGER NOT NULL,
                jid TEXT NOT NULL,
                display_name TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                UNIQUE(tenant_id, jid)
            );
CREATE TABLE IF NOT EXISTS tenant_account (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id INTEGER NOT NULL,
                session_name TEXT NOT NULL DEFAULT 'default',
                waha_port INTEGER DEFAULT 0,
                container_name TEXT,
                whatsapp_number TEXT,
                whatsapp_name TEXT,
                waha_status TEXT DEFAULT 'none',
                waha_picture TEXT,
                is_primary INTEGER DEFAULT 0,
                config_json TEXT DEFAULT '{}',
                created_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (tenant_id) REFERENCES tenant_users(id),
                UNIQUE(tenant_id, session_name)
            );
            CREATE TABLE IF NOT EXISTS translation_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_text_hash TEXT NOT NULL,
                source_text TEXT NOT NULL,
                source_lang TEXT DEFAULT 'auto',
                target_lang TEXT NOT NULL,
                translated_text TEXT NOT NULL,
                engine TEXT DEFAULT 'google',
                created_at TEXT DEFAULT (datetime('now')),
                UNIQUE(source_text_hash)
            );
            CREATE TABLE IF NOT EXISTS translation_config (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id INTEGER NOT NULL,
                engine TEXT NOT NULL,
                api_key TEXT,
                api_secret TEXT,
                enabled INTEGER DEFAULT 1,
                priority INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (tenant_id) REFERENCES tenant_users(id),
                UNIQUE(tenant_id, engine)
            );
            CREATE TABLE IF NOT EXISTS tenant_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id INTEGER NOT NULL UNIQUE,
                translate_target_lang TEXT DEFAULT 'zh',
                translate_engine TEXT DEFAULT 'google',
                translate_fallback_order TEXT DEFAULT 'google,baidu,deepseek,openai',
                llm_provider TEXT DEFAULT 'deepseek',
                llm_model TEXT DEFAULT 'deepseek-chat',
                llm_api_key TEXT,
                llm_api_base TEXT,
                ui_language TEXT DEFAULT 'zh-CN',
                timezone TEXT DEFAULT 'Asia/Shanghai',
                updated_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (tenant_id) REFERENCES tenant_users(id)
            );
            CREATE TABLE IF NOT EXISTS customer (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id INTEGER NOT NULL,
                whatsapp_jid TEXT NOT NULL,
                display_name TEXT,
                country TEXT,
                country_flag TEXT,
                phone TEXT,
                email TEXT,
                company TEXT,
                tags TEXT DEFAULT '[]',
                notes TEXT,
                status TEXT DEFAULT 'new',
                first_contact_at TEXT,
                last_contact_at TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (tenant_id) REFERENCES tenant_users(id),
                UNIQUE(tenant_id, whatsapp_jid)
            );
            CREATE TABLE IF NOT EXISTS customer_interaction (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL,
                tenant_id INTEGER NOT NULL,
                direction TEXT NOT NULL,
                message_type TEXT DEFAULT 'text',
                content TEXT,
                translated_content TEXT,
                waha_message_id TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (customer_id) REFERENCES customer(id),
                FOREIGN KEY (tenant_id) REFERENCES tenant_users(id)
            );
            CREATE TABLE IF NOT EXISTS customer_reminder (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL,
                tenant_id INTEGER NOT NULL,
                reminder_text TEXT NOT NULL,
                remind_at TEXT,
                is_done INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (customer_id) REFERENCES customer(id),
                FOREIGN KEY (tenant_id) REFERENCES tenant_users(id)
            );
        """)
        # 自动迁移：添加 waha_port 列（如果不存在）
        try:
            conn.execute("ALTER TABLE tenant_users ADD COLUMN waha_port INTEGER DEFAULT 0")
        except: pass
        # 默认管理员 & 设置
        conn.execute("""
            INSERT OR IGNORE INTO admin_users (username, password_hash, role) 
            VALUES ('admin', ?, 'super_admin')
        """, (hash_password('admin123'),))
        defaults = [
            ('proxy_host', '43.162.127.33'),
            ('proxy_port', '57891'),
            ('proxy_uuid', '6f659ace-69b1-4454-95bf-12f7b09c999c'),
            ('proxy_network', 'tcp'),
            ('proxy_tls', 'none'),
            ('waha_memory_limit_mb', '512'),
            ('waha_engine', 'NOWEB'),
        ]
        for k, v in defaults:
            conn.execute("INSERT OR IGNORE INTO system_settings (key,value) VALUES (?,?)", (k, v))
        conn.commit()

# ============ 工具函数 ============
def hash_password(password):
    return hashlib.sha256(f"{password}_whatsapp_saas_salt".encode()).hexdigest()

def generate_token(username):
    payload = f"{username}|{time.time() + TOKEN_EXPIRY * 3600}"
    sig = hashlib.sha256(f"{payload}{JWT_SECRET}".encode()).hexdigest()
    return f"{payload}|{sig}"

def generate_tenant_token(username):
    payload = f"tenant:{username}|{time.time() + TOKEN_EXPIRY * 3600}"
    sig = hashlib.sha256(f"{payload}{JWT_SECRET}".encode()).hexdigest()
    return f"{payload}|{sig}"

def verify_tenant_token(token):
    try:
        parts = token.split('|')
        if len(parts) != 3:
            return None
        prefixed, expiry, sig = parts
        if not prefixed.startswith('tenant:'):
            return None
        username = prefixed[7:]
        expected = hashlib.sha256(f"{prefixed}|{expiry}{JWT_SECRET}".encode()).hexdigest()
        if sig != expected or float(expiry) < time.time():
            return None
        return username
    except:
        return None

def require_tenant_auth(handler):
    auth = handler.headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        handler.send_json(401, {'error': '未登录'})
        return (None, None)
    username = verify_tenant_token(auth[7:])
    if not username:
        handler.send_json(401, {'error': '登录已过期，请重新登录'})
        return (None, None)
    with get_db() as conn:
        row = conn.execute("SELECT * FROM tenant_users WHERE username=?", (username,)).fetchone()
    if not row:
        handler.send_json(401, {'error': '租户不存在'})
        return (None, None)
    if not row['is_active']:
        handler.send_json(403, {'error': '账户已被禁用'})
        return (None, None)
    return (username, dict(row))

def verify_token(token):
    try:
        parts = token.split('|')
        if len(parts) != 3:
            return None
        username, expiry, sig = parts
        expected = hashlib.sha256(f"{username}|{expiry}{JWT_SECRET}".encode()).hexdigest()
        if sig != expected or float(expiry) < time.time():
            return None
        return username
    except:
        return None

def require_auth(handler):
    auth = handler.headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        handler.send_json(401, {'error': '未登录'})
        return None
    username = verify_token(auth[7:])
    if not username:
        handler.send_json(401, {'error': '登录已过期，请重新登录'})
        return None
    return username

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def get_settings():
    with get_db() as conn:
        rows = conn.execute("SELECT key, value FROM system_settings").fetchall()
    return {r['key']: r['value'] for r in rows}

def assign_port():
    """分配可用端口（3100-3199范围）"""
    used = set()
    with get_db() as conn:
        rows = conn.execute("SELECT waha_port FROM tenant_users WHERE waha_port > 0").fetchall()
        used = {r['waha_port'] for r in rows}
    # 检查 docker 实际占用的端口
    try:
        netstat = subprocess.run(
            "ss -tlnp 2>/dev/null | awk '{print $4}' | grep -oP ':\\d+' | tr -d ':' | sort -nu",
            shell=True, capture_output=True, text=True
        )
        for p in netstat.stdout.strip().split('\n'):
            try:
                used.add(int(p))
            except: pass
    except: pass
    for port in range(WAHA_PORT_START, WAHA_PORT_END + 1):
        if port not in used:
            return port
    return 0

def query_tenant_waha(api_key, port, tenant_id, tenant_whatsapp):
    """查询租户 WAHA 容器状态，返回 {waha_status, waha_number, waha_name, waha_picture}"""
    result = {'waha_status': 'offline', 'waha_number': tenant_whatsapp or '', 'waha_name': '', 'waha_picture': ''}
    if not api_key or not port:
        result['waha_status'] = 'none'
        return result
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/sessions/default",
            headers={"X-Api-Key": api_key}
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=2).read())
        me = resp.get('me') or {}
        wa_id = me.get('id', '') or ''
        # "8613172190207@c.us" → "8613172190207"
        if '@' in wa_id:
            wa_id = wa_id.split('@')[0]
        wa_name = me.get('pushName', '')
        status = resp.get('status', 'unknown')
        result['waha_status'] = status
        result['waha_number'] = wa_id or tenant_whatsapp or ''
        result['waha_name'] = wa_name
        # 如果检测到号码，更新数据库
        if wa_id and wa_id != tenant_whatsapp:
            with get_db() as conn:
                conn.execute(
                    "UPDATE tenant_users SET whatsapp_number=?, whatsapp_connected=1 WHERE id=?",
                    (wa_id, tenant_id)
                )
                conn.commit()
        # 获取自己的头像
        if status in ('WORKING', 'CONNECTED'):
            try:
                preq = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/default/profile",
                    headers={"X-Api-Key": api_key}
                )
                profile = json.loads(urllib.request.urlopen(preq, timeout=2).read())
                result['waha_picture'] = profile.get('picture', '')
            except:
                pass
    except:
        pass
    return result

def get_tenant(tenant_id):
    with get_db() as conn:
        return conn.execute("SELECT * FROM tenant_users WHERE id=?", (tenant_id,)).fetchone()

# ============ HTTP 处理 ============
class AdminHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass


    def query_params(self):
        """Parse query string from URL path"""
        if '?' in self.path:
            qs = self.path.split('?', 1)[1]
            from urllib.parse import parse_qs
            return {k: v[0] for k, v in parse_qs(qs).items()}
        return {}

    def send_json(self, status, data):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False, default=str).encode())

    def read_body(self):
        length = int(self.headers.get('Content-Length', 0))
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except:
            return {}

    def do_OPTIONS(self):
        self.send_json(200, {})

    # ===== 登录 =====
    def handle_login(self):
        data = self.read_body()
        user = data.get('username', '')
        pw = data.get('password', '')
        with get_db() as conn:
            row = conn.execute("SELECT * FROM admin_users WHERE username=?", (user,)).fetchone()
            if row and row['password_hash'] == hash_password(pw):
                token = generate_token(user)
                conn.execute("UPDATE admin_users SET last_login=datetime('now') WHERE id=?", (row['id'],))
                conn.commit()
                self.send_json(200, {'token': token, 'user': {'id': row['id'], 'username': row['username'], 'role': row['role']}})
            else:
                self.send_json(401, {'error': '用户名或密码错误'})

    # ===== 租户自助登录 =====
    def handle_tenant_login(self):
        data = self.read_body()
        username = data.get('username', '')
        password = data.get('password', '')
        with get_db() as conn:
            row = conn.execute(
                "SELECT * FROM tenant_users WHERE username=? AND is_active=1",
                (username,)
            ).fetchone()
            if row and row['password_hash'] == hash_password(password):
                token = generate_tenant_token(username)
                self.send_json(200, {
                    'token': token,
                    'tenant': {
                        'id': row['id'],
                        'company_name': row['company_name'],
                        'username': row['username'],
                        'email': row['email'] or '',
                        'whatsapp_number': row['whatsapp_number'] or '',
                        'waha_port': row['waha_port'] or 0,
                        'api_key': row['api_key'] or '',
                    }
                })
            else:
                self.send_json(401, {'error': '用户名或密码错误，或账户已被禁用'})

    # ===== 租户自助信息 =====
    def handle_tenant_me(self):
        username, tenant = require_tenant_auth(self)
        if not username: return
        wa_info = query_tenant_waha(
            tenant.get('api_key', ''),
            tenant.get('waha_port', 0),
            tenant['id'],
            tenant.get('whatsapp_number', '')
        )
        self.send_json(200, {
            'tenant': {
                'id': tenant['id'],
                'company_name': tenant['company_name'],
                'username': tenant['username'],
                'email': tenant['email'] or '',
                'waha_status': wa_info['waha_status'],
                'waha_number': wa_info['waha_number'],
                'waha_name': wa_info['waha_name'],
                'waha_picture': wa_info.get('waha_picture', ''),
                'waha_port': tenant.get('waha_port', 0),
                'api_key': tenant['api_key'] or '',
            }
        })

    # ===== 租户自助 QR =====
    def handle_tenant_self_session_get(self):
        username, tenant = require_tenant_auth(self)
        if not username: return
        wa_info = query_tenant_waha(
            tenant.get('api_key', ''),
            tenant.get('waha_port', 0),
            tenant['id'],
            tenant.get('whatsapp_number', '')
        )
        # Map to frontend-expected field names
        self.send_json(200, {
            'status': wa_info.get('waha_status', 'offline'),
            'whatsapp_number': wa_info.get('waha_number') or tenant.get('whatsapp_number', ''),
            'whatsapp_name': wa_info.get('waha_name', ''),
            'waha_picture': wa_info.get('waha_picture', ''),
        })

    def handle_tenant_self_qr(self):
        username, tenant = require_tenant_auth(self)
        if not username: return
        port = tenant.get('waha_port', 0)
        api_key = tenant.get('api_key', '')
        if not port or not api_key:
            self.send_json(400, {'error': '容器未启动，请联系管理员'})
            return
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/screenshot?session=default",
                headers={"X-Api-Key": api_key}
            )
            resp = urllib.request.urlopen(req, timeout=10)
            self.send_response(200)
            self.send_header('Content-Type', 'image/png')
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(resp.read())
        except Exception as e:
            self.send_json(500, {'error': f'获取二维码失败: {str(e)}'})

    # ===== 租户自助会话控制 =====
    def handle_tenant_self_session(self, action):
        username, tenant = require_tenant_auth(self)
        if not username: return
        port = tenant.get('waha_port', 0)
        api_key = tenant.get('api_key', '')
        if not port or not api_key:
            self.send_json(400, {'error': '容器未启动，请联系管理员'})
            return
        try:
            if action == 'restart':
                urllib.request.urlopen(urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/sessions/default/stop",
                    data=b'', headers={"X-Api-Key": api_key}, method='POST'
                ), timeout=5)
                time.sleep(1)
                urllib.request.urlopen(urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/sessions/default/start",
                    data=b'{}', headers={"X-Api-Key": api_key, "Content-Type": "application/json"}, method='POST'
                ), timeout=5)
                self.send_json(200, {'message': '会话已重启'})
            elif action == 'logout':
                urllib.request.urlopen(urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/sessions/default/logout",
                    data=b'', headers={"X-Api-Key": api_key}, method='POST'
                ), timeout=5)
                with get_db() as conn:
                    conn.execute("UPDATE tenant_users SET whatsapp_number='', whatsapp_connected=0 WHERE id=?", 
                                 (tenant['id'],))
                    conn.commit()
                self.send_json(200, {'message': '已登出'})
            elif action == 'stop':
                urllib.request.urlopen(urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/sessions/default/stop",
                    data=b'', headers={"X-Api-Key": api_key}, method='POST'
                ), timeout=5)
                self.send_json(200, {'message': '会话已停止'})
            elif action == 'start':
                urllib.request.urlopen(urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/sessions/default/start",
                    data=b'{}', headers={"X-Api-Key": api_key, "Content-Type": "application/json"}, method='POST'
                ), timeout=5)
                self.send_json(200, {'message': '会话已启动'})
            else:
                self.send_json(400, {'error': f'未知操作: {action}'})
        except Exception as e:
            self.send_json(500, {'error': str(e)})

    # ===== 租户列表 =====
    def handle_tenants(self, method):
        username = require_auth(self)
        if not username: return

        if method == 'GET':
            with get_db() as conn:
                rows = conn.execute("""
                    SELECT id, company_name, username, email, is_active, whatsapp_number, 
                           whatsapp_connected, container_name, api_key, waha_port, created_at, updated_at
                    FROM tenant_users ORDER BY created_at DESC
                """).fetchall()
            tenants = [dict(r) for r in rows]
            for t in tenants:
                if t.get('api_key') and t.get('is_active') and t.get('waha_port'):
                    wa_info = query_tenant_waha(t['api_key'], t['waha_port'], t['id'], t.get('whatsapp_number', ''))
                    t['waha_status'] = wa_info['waha_status']
                    t['waha_number'] = wa_info['waha_number']
                    t['waha_name'] = wa_info['waha_name']
                else:
                    t['waha_status'] = 'none'
                    t['waha_number'] = t.get('whatsapp_number', '')
                    t['waha_name'] = ''
            self.send_json(200, {'tenants': tenants})

        elif method == 'POST':
            data = self.read_body()
            company = data.get('company_name', '').strip()
            tenant_user = data.get('username', '').strip()
            password = data.get('password', '').strip()
            email = data.get('email', '').strip()

            if not company or not tenant_user or not password:
                self.send_json(400, {'error': '公司名称、用户名、密码为必填项'})
                return

            api_key = secrets.token_hex(16)
            container_name = f"waha-{tenant_user[:20].lower()}"

            try:
                with get_db() as conn:
                    cur = conn.execute("""
                        INSERT INTO tenant_users (company_name, username, password_hash, email, api_key, container_name)
                        VALUES (?,?,?,?,?,?)
                    """, (company, tenant_user, hash_password(password), email, api_key, container_name))
                    conn.commit()
                    tid = cur.lastrowid
                self.send_json(201, {'id': tid, 'api_key': api_key, 'container_name': container_name})
            except sqlite3.IntegrityError:
                self.send_json(400, {'error': '用户名已存在'})
            except Exception as e:
                self.send_json(500, {'error': str(e)})
        else:
            self.send_json(405, {'error': '不支持的方法'})

    # ===== 单个租户 =====
    def handle_tenant_detail(self, tenant_id, method):
        username = require_auth(self)
        if not username: return

        if method == 'GET':
            row = get_tenant(tenant_id)
            if row:
                d = dict(row)
                d.pop('password_hash', None)
                # 补充 WAHA 状态
                if d.get('api_key') and d.get('waha_port'):
                    wa = query_tenant_waha(d['api_key'], d['waha_port'], d['id'], d.get('whatsapp_number', ''))
                    d['waha_status'] = wa['waha_status']
                    d['waha_number'] = wa['waha_number']
                    d['waha_name'] = wa['waha_name']
                self.send_json(200, {'tenant': d})
            else:
                self.send_json(404, {'error': '租户不存在'})

        elif method == 'PUT':
            data = self.read_body()
            with get_db() as conn:
                for field in ['company_name', 'email', 'is_active']:
                    if field in data:
                        conn.execute(f"UPDATE tenant_users SET {field}=?, updated_at=datetime('now') WHERE id=?",
                                     (data[field], tenant_id))
                if 'password' in data and data['password']:
                    conn.execute("UPDATE tenant_users SET password_hash=?, updated_at=datetime('now') WHERE id=?",
                                 (hash_password(data['password']), tenant_id))
                conn.commit()
            self.send_json(200, {'message': '更新成功'})

        elif method == 'DELETE':
            with get_db() as conn:
                row = conn.execute("SELECT container_name FROM tenant_users WHERE id=?", (tenant_id,)).fetchone()
                if row and row['container_name']:
                    subprocess.run(f"docker rm -f {row['container_name']} 2>/dev/null", shell=True)
                conn.execute("DELETE FROM tenant_users WHERE id=?", (tenant_id,))
                conn.commit()
            self.send_json(200, {'message': '已删除'})
        else:
            self.send_json(405, {})

    # ===== 租户容器管理 =====
    def handle_tenant_container(self, tenant_id, action):
        username = require_auth(self)
        if not username: return

        tenant = get_tenant(tenant_id)
        if not tenant:
            self.send_json(404, {'error': '租户不存在'})
            return

        settings = get_settings()
        container_name = tenant['container_name']
        api_key = tenant['api_key']
        waha_memory = settings.get('waha_memory_limit_mb', '512')

        if action in ('start', 'create'):
            # 分配端口
            port = tenant['waha_port']
            if not port or port == 0:
                port = assign_port()
                if port == 0:
                    self.send_json(500, {'error': '无可用端口'})
                    return
                with get_db() as conn:
                    conn.execute("UPDATE tenant_users SET waha_port=? WHERE id=?", (port, tenant_id))
                    conn.commit()

            # 清理旧容器
            subprocess.run(f"docker rm -f {container_name} 2>/dev/null", shell=True)
            time.sleep(0.5)

            # 使用 bridge 网络 + 端口映射，proxy 通过 host.docker.internal
            docker_cmd = (
                f"docker run -d --name {container_name} "
                f"--network saas-network "
                f"-p {port}:3000 "
                f"--add-host host.docker.internal:host-gateway "
                f"--memory={waha_memory}m --memory-swap={int(waha_memory)*2}m "
                f"-e WHATSAPP_DEFAULT_ENGINE={settings.get('waha_engine','NOWEB')} "
                f"-e WAHA_API_KEY={api_key} "
                f"-v waha_{tenant['username']}_data:/app/data "
                f"devlikeapro/waha:latest"
            )
            result = subprocess.run(docker_cmd, shell=True, capture_output=True, text=True)
            container_id = result.stdout.strip()

            if result.returncode == 0:
                time.sleep(4)  # 等容器启动
                proxy_server = "host.docker.internal:10808"
                # 配置代理 + 启动会话（与之前类似，但用 host 127.0.0.1:{port} 访问）
                for attempt in range(2):
                    try:
                        urllib.request.urlopen(urllib.request.Request(
                            f"http://127.0.0.1:{port}/api/sessions/default/stop",
                            data=b'', headers={"X-Api-Key": api_key}, method='POST'
                        ), timeout=5)
                        time.sleep(0.5)
                    except: pass
                    try:
                        body = json.dumps({"name":"default","config":{"proxy":{"server":proxy_server,"type":"socks5"},"noweb":{"store":{"enabled":True,"fullSync":True}}}}).encode()
                        urllib.request.urlopen(urllib.request.Request(
                            f"http://127.0.0.1:{port}/api/sessions/default",
                            data=body,
                            headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
                            method='PUT'
                        ), timeout=5)
                        time.sleep(0.5)
                    except: pass
                    try:
                        urllib.request.urlopen(urllib.request.Request(
                            f"http://127.0.0.1:{port}/api/sessions/default/start",
                            data=b'{}',
                            headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
                            method='POST'
                        ), timeout=5)
                        break
                    except:
                        time.sleep(2)

                self.send_json(200, {
                    'message': '容器已创建并启动',
                    'container_id': container_id,
                    'container_name': container_name,
                    'api_key': api_key,
                    'port': port
                })
            else:
                self.send_json(500, {'error': result.stderr or '容器创建失败'})

        elif action == 'stop':
            subprocess.run(f"docker stop {container_name} 2>/dev/null", shell=True)
            self.send_json(200, {'message': '已停止'})

        elif action == 'status':
            result = subprocess.run(
                f"docker inspect {container_name} --format '{{{{json .State}}}}' 2>/dev/null",
                shell=True, capture_output=True, text=True
            )
            if result.stdout.strip():
                state = json.loads(result.stdout)
                waha_status = "unknown"
                port = tenant['waha_port']
                try:
                    if port:
                        req = urllib.request.Request(
                            f"http://127.0.0.1:{port}/api/sessions/default",
                            headers={"X-Api-Key": api_key}
                        )
                        resp = json.loads(urllib.request.urlopen(req, timeout=3).read())
                        waha_status = resp.get('status', 'unknown')
                except: pass
                self.send_json(200, {
                    'docker_status': state.get('Status', 'unknown'),
                    'running': state.get('Running', False),
                    'waha_status': waha_status,
                    'port': port
                })
            else:
                self.send_json(200, {'docker_status': 'not_found', 'running': False, 'waha_status': 'none'})
        else:
            self.send_json(400, {'error': f'未知操作: {action}'})

    # ===== QR 码 =====
    def handle_tenant_qr(self, tenant_id):
        username = require_auth(self)
        if not username: return

        tenant = get_tenant(tenant_id)
        if not tenant:
            self.send_json(404, {'error': '租户不存在'})
            return
        port = tenant['waha_port']
        api_key = tenant['api_key']
        if not port or not api_key:
            self.send_json(400, {'error': '租户容器未启动，请先点击启动'})
            return

        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/screenshot?session=default",
                headers={"X-Api-Key": api_key}
            )
            resp = urllib.request.urlopen(req, timeout=10)
            self.send_response(200)
            self.send_header('Content-Type', 'image/png')
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(resp.read())
        except Exception as e:
            self.send_json(500, {'error': f'获取二维码失败: {str(e)}'})

    # ===== 会话控制 =====
    def handle_tenant_session(self, tenant_id, action):
        username = require_auth(self)
        if not username: return

        tenant = get_tenant(tenant_id)
        if not tenant:
            self.send_json(404, {'error': '租户不存在'})
            return
        port = tenant['waha_port']
        api_key = tenant['api_key']
        if not port or not api_key:
            self.send_json(400, {'error': '租户容器未启动'})
            return

        try:
            if action == 'restart':
                # stop → start
                urllib.request.urlopen(urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/sessions/default/stop",
                    data=b'', headers={"X-Api-Key": api_key}, method='POST'
                ), timeout=5)
                time.sleep(1)
                urllib.request.urlopen(urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/sessions/default/start",
                    data=b'{}', headers={"X-Api-Key": api_key, "Content-Type": "application/json"}, method='POST'
                ), timeout=5)
                self.send_json(200, {'message': '会话已重启'})
            elif action == 'logout':
                urllib.request.urlopen(urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/sessions/default/logout",
                    data=b'', headers={"X-Api-Key": api_key}, method='POST'
                ), timeout=5)
                with get_db() as conn:
                    conn.execute("UPDATE tenant_users SET whatsapp_number='', whatsapp_connected=0 WHERE id=?",
                                 (tenant_id,))
                    conn.commit()
                self.send_json(200, {'message': '已登出，需要重新扫码连接'})
            elif action == 'stop':
                urllib.request.urlopen(urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/sessions/default/stop",
                    data=b'', headers={"X-Api-Key": api_key}, method='POST'
                ), timeout=5)
                self.send_json(200, {'message': '会话已停止'})
            elif action == 'start':
                urllib.request.urlopen(urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/sessions/default/start",
                    data=b'{}', headers={"X-Api-Key": api_key, "Content-Type": "application/json"}, method='POST'
                ), timeout=5)
                self.send_json(200, {'message': '会话已启动'})
            else:
                self.send_json(400, {'error': f'未知操作: {action}'})
        except Exception as e:
            self.send_json(500, {'error': str(e)})

    # ===== 服务器状态（仅系统资源，不含 WAHA 会话） =====
    def handle_status(self):
        username = require_auth(self)
        if not username: return

        try:
            mem_out = subprocess.run("free -m | awk '/Mem:/{print $2,$3,$4}'", 
                                     shell=True, capture_output=True, text=True).stdout.strip().split()
            disk_out = subprocess.run("df -h / | awk 'NR==2{print $2,$3,$4,$5}'", 
                                      shell=True, capture_output=True, text=True).stdout.strip().split()
            nproc = int(subprocess.run('nproc', shell=True, capture_output=True, text=True).stdout.strip() or 1)
            load_raw = subprocess.run("cat /proc/loadavg | awk '{print $1,$2,$3}'", shell=True, capture_output=True, text=True).stdout.strip()
            load_parts = load_raw.split()
            load_1m = float(load_parts[0]) if len(load_parts) > 0 else 0
            load_5m = float(load_parts[1]) if len(load_parts) > 1 else 0
            load_15m = float(load_parts[2]) if len(load_parts) > 2 else 0
            load_pct = round(load_1m / nproc * 100, 1)
            containers = subprocess.run(
                "docker ps --format '{{.Names}}|{{.Status}}' --no-trunc 2>/dev/null",
                shell=True, capture_output=True, text=True
            ).stdout.strip().split('\n')
            container_list = []
            for c in containers:
                if '|' in c:
                    name, status = c.split('|', 1)
                    container_list.append({'name': name, 'status': status})

            xray = subprocess.run("systemctl is-active xray 2>/dev/null", 
                                  shell=True, capture_output=True, text=True).stdout.strip()

            self.send_json(200, {
                'system': {
                    'memory_total_mb': int(mem_out[0]) if len(mem_out) > 0 else 0,
                    'memory_used_mb': int(mem_out[1]) if len(mem_out) > 1 else 0,
                    'memory_free_mb': int(mem_out[2]) if len(mem_out) > 2 else 0,
                    'disk_total': disk_out[0] if len(disk_out) > 0 else '',
                    'disk_used': disk_out[1] if len(disk_out) > 1 else '',
                    'disk_free': disk_out[2] if len(disk_out) > 2 else '',
                    'disk_usage_pct': disk_out[3] if len(disk_out) > 3 else '',
                    'load_1m': load_1m,
                    'load_5m': load_5m,
                    'load_15m': load_15m,
                    'cpu_cores': nproc,
                    'load_pct': load_pct,
                },
                'containers': container_list,
                'xray': xray if xray else 'inactive',
            })
        except Exception as e:
            self.send_json(500, {'error': str(e)})

    # ===== 代理配置 =====
    def handle_proxy(self, method):
        username = require_auth(self)
        if not username: return

        if method == 'GET':
            settings = get_settings()
            proxy = {k: v for k, v in settings.items() if k.startswith('proxy_') or k.startswith('waha_')}
            self.send_json(200, {'proxy': proxy})

        elif method == 'PUT':
            data = self.read_body()
            with get_db() as conn:
                for k, v in data.items():
                    conn.execute("""
                        INSERT OR REPLACE INTO system_settings (key, value, updated_at) 
                        VALUES (?, ?, datetime('now'))
                    """, (k, str(v)))
                conn.commit()

            ph, pp, pu = data.get('proxy_host'), data.get('proxy_port'), data.get('proxy_uuid')
            if ph and pp and pu:
                xray_config = {
                    "log": {"loglevel": "warning"},
                    "inbounds": [
                        {"port": 10808, "protocol": "socks", "listen": "127.0.0.1",
                         "settings": {"auth": "noauth", "udp": True},
                         "sniffing": {"enabled": True, "destOverride": ["http", "tls"]}},
                        {"port": 10809, "protocol": "http", "listen": "127.0.0.1", "settings": {}}
                    ],
                    "outbounds": [{
                        "protocol": "vmess",
                        "settings": {"vnext": [{
                            "address": ph, "port": int(pp),
                            "users": [{"id": pu, "alterId": 0, "security": "auto"}]
                        }]},
                        "streamSettings": {"network": data.get('proxy_network', 'tcp')}
                    }]
                }
                with open('/tmp/new_xray.json', 'w') as f:
                    json.dump(xray_config, f, indent=2)
                subprocess.run(
                    "echo 'zfb0411!' | sudo -S cp /tmp/new_xray.json /usr/local/etc/xray/config.json && "
                    "echo 'zfb0411!' | sudo -S systemctl restart xray",
                    shell=True, capture_output=True
                )
            self.send_json(200, {'message': '代理配置已更新'})
        else:
            self.send_json(405, {})

    # ===== 路由 =====

    # ===== 租户自助聊天 API =====
    def handle_tenant_chats(self):
        """Get chat list from local message_logs (NOWEB engine doesn't have /api/chats)"""
        username, tenant = require_tenant_auth(self)
        if not username: return
        tenant_id = tenant.get('id', 0)
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute("""
                    SELECT remote_jid as id,
                           MAX(created_at) as last_message_at,
                           COUNT(*) as message_count,
                           (SELECT content FROM message_logs m2 
                            WHERE m2.remote_jid = m1.remote_jid AND m2.tenant_id = ? 
                            ORDER BY m2.id DESC LIMIT 1) as last_message
                    FROM message_logs m1
                    WHERE tenant_id = ?
                    GROUP BY remote_jid
                    ORDER BY MAX(id) DESC
                """, (tenant_id, tenant_id)).fetchall()
                # Merge contact names
                contacts = conn.execute(
                    "SELECT jid, display_name FROM tenant_contacts WHERE tenant_id=?",
                    (tenant_id,)
                ).fetchall()
            name_map = {c[0]: c[1] for c in contacts if c[1]}
            chats = []
            for r in rows:
                chat = dict(r)
                jid = chat['id']
                if jid in name_map:
                    chat['name'] = name_map[jid]
                else:
                    phone = jid.split('@')[0] if '@' in jid else jid
                    chat['name'] = phone
                chats.append(chat)
            self.send_json(200, chats)
        except Exception as e:
            self.send_json(500, {'error': f'获取对话列表失败: {str(e)}'})

    def handle_tenant_contacts(self):
        username, tenant = require_tenant_auth(self)
        if not username: return
        tenant_id = tenant.get('id', 0)
        if not tenant_id:
            self.send_json(400, {'error': '无效租户'})
            return
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                "SELECT jid, display_name FROM tenant_contacts WHERE tenant_id=? ORDER BY display_name",
                (tenant_id,)
            ).fetchall()
        self.send_json(200, [{'jid': r[0], 'display_name': r[1]} for r in rows])

    def handle_tenant_contacts_update(self):
        username, tenant = require_tenant_auth(self)
        if not username: return
        tenant_id = tenant.get('id', 0)
        body = self.read_body()
        jid = body.get('jid', '').strip()
        display_name = body.get('display_name', '').strip()
        if not jid:
            self.send_json(400, {'error': '缺少 jid'})
            return
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO tenant_contacts (tenant_id, jid, display_name, updated_at) VALUES (?,?,?,datetime('now')) "
                "ON CONFLICT(tenant_id, jid) DO UPDATE SET display_name=excluded.display_name, updated_at=excluded.updated_at",
                (tenant_id, jid, display_name)
            )
            conn.commit()
        self.send_json(200, {'ok': True, 'jid': jid, 'display_name': display_name})

    def handle_tenant_pushnames(self):
        """批量获取联系人的 WA pushname"""
        username, tenant = require_tenant_auth(self)
        if not username: return
        port = tenant.get('waha_port', 0)
        api_key = tenant.get('api_key', '')
        if not port or not api_key:
            self.send_json(400, {'error': '容器未启动'})
            return
        jids_str = self.query_params().get('jids', '')
        if not jids_str:
            self.send_json(200, {})
            return
        jids = [j.strip() for j in jids_str.split(',') if j.strip()][:30]
        result = {}
        for jid in jids:
            try:
                # Convert @s.whatsapp.net → @c.us for contacts API
                cjid = jid.replace('@s.whatsapp.net', '@c.us').replace('@lid', '@c.us')
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/default/contacts/{cjid}",
                    headers={"X-Api-Key": api_key}
                )
                resp = urllib.request.urlopen(req, timeout=2)
                data = json.loads(resp.read())
                pn = data.get('pushname', '') or ''
                if pn:
                    result[jid] = pn
                    # Auto-save to contacts DB
                    with sqlite3.connect(DB_PATH) as conn:
                        conn.execute(
                            "INSERT INTO tenant_contacts (tenant_id, jid, display_name, updated_at) "
                            "VALUES (?,?,?,datetime('now')) "
                            "ON CONFLICT(tenant_id, jid) DO UPDATE "
                            "SET display_name=CASE WHEN display_name='' THEN excluded.display_name ELSE display_name END, "
                            "updated_at=datetime('now')",
                            (tenant.get('id', 0), jid, pn)
                        )
                        conn.commit()
            except:
                pass
        self.send_json(200, result)

    def handle_tenant_chat_messages(self, chat_id):
        """Get messages from local message_logs (NOWEB compatible)"""
        username, tenant = require_tenant_auth(self)
        if not username: return
        tenant_id = tenant.get('id', 0)
        try:
            limit = int(self.query_params().get('limit', '50'))
            with sqlite3.connect(DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM message_logs WHERE tenant_id=? AND remote_jid=? ORDER BY id DESC LIMIT ?",
                    (tenant_id, chat_id, limit)
                ).fetchall()
            # Reverse to chronological order (oldest first)
            messages = [dict(r) for r in reversed(rows)]
            self.send_json(200, messages)
        except Exception as e:
            self.send_json(500, {'error': f'获取消息失败: {str(e)}'})

    def handle_tenant_send(self):
        username, tenant = require_tenant_auth(self)
        if not username: return
        port = tenant.get('waha_port', 0)
        api_key = tenant.get('api_key', '')
        tenant_id = tenant.get('id', 0)
        if not port or not api_key:
            self.send_json(400, {'error': '容器未启动，请联系管理员'})
            return
        data = self.read_body()
        chat_id = data.get('chatId', '')
        text = data.get('text', '')
        if not chat_id or not text:
            self.send_json(400, {'error': '缺少 chatId 或 text'})
            return
        # Normalize chatId: add @c.us suffix if plain phone number
        if '@' not in chat_id and chat_id.isdigit():
            chat_id = chat_id + '@c.us'
        try:
            body = json.dumps({"chatId": chat_id, "text": text, "session": "default"}).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/sendText",
                data=body,
                headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
                method='POST'
            )
            resp = urllib.request.urlopen(req, timeout=15)
            result = json.loads(resp.read())
            # Store sent message in local DB
            try:
                with sqlite3.connect(DB_PATH) as conn:
                    conn.execute(
                        "INSERT INTO message_logs (tenant_id, direction, remote_jid, message_type, content, status) VALUES (?, 'out', ?, 'text', ?, ?)",
                        (tenant_id, chat_id, text, result.get('status', 'sent'))
                    )
                    conn.commit()
            except Exception:
                pass  # Don't fail the request if DB write fails
            self.send_json(200, result)
        except urllib.error.HTTPError as e:
            msg = json.loads(e.read()) if e.fp else {}
            self.send_json(e.code, {'error': msg.get('message', msg.get('error', str(e)))})
        except Exception as e:
            self.send_json(500, {'error': f'发送失败: {str(e)}'})

    def handle_tenant_translate(self):
        """翻译单条文本 (多引擎 + fallback + 缓存)"""
        username, tenant = require_tenant_auth(self)
        if not username: return
        try:
            body = self.read_body()
            text = body.get('text', '').strip()
            target_lang = body.get('target_lang')
            source_lang = body.get('source_lang', 'auto')
            engine = body.get('engine', 'auto')
            direction = body.get('direction')  # 'in' (receive) or 'out' (send)

            if not text:
                self.send_json(400, {'error': 'text required'})
                return

            tm = TranslationManager(tenant['id'])
            result = tm.translate(text, target_lang, source_lang,
                                  None if engine == 'auto' else engine,
                                  direction=direction)
            self.send_json(200, result)
        except Exception as e:
            self.send_json(500, {'error': str(e)})

    def handle_tenant_translate_batch(self):
        username, tenant = require_tenant_auth(self)
        if not username: return
        try:
            body = self.read_body()
            texts = body.get('texts', [])
            target_lang = body.get('target_lang')
            source_lang = body.get('source_lang', 'auto')
            direction = body.get('direction')  # 'in' or 'out'

            if not texts:
                self.send_json(400, {'error': 'texts required'})
                return

            tm = TranslationManager(tenant['id'])
            results = tm.batch_translate(texts, target_lang, source_lang, direction=direction)
            self.send_json(200, {'results': results})
        except Exception as e:
            self.send_json(500, {'error': str(e)})

    def handle_tenant_translate_engines(self):
        username, tenant = require_tenant_auth(self)
        if not username: return
        try:
            engines = []
            for name, label in ENGINE_LABELS.items():
                engines.append({
                    'name': name,
                    'label': label,
                    'is_free': name == 'google',
                    'needs_key': name != 'google'
                })
            self.send_json(200, {'engines': engines})
        except Exception as e:
            self.send_json(500, {'error': str(e)})

    def handle_tenant_translate_config_get(self):
        username, tenant = require_tenant_auth(self)
        if not username: return
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM translation_config WHERE tenant_id=? ORDER BY priority",
                    (tenant['id'],)
                ).fetchall()
                engine_configs = {}
                for r in rows:
                    engine_configs[r['engine']] = {
                        'enabled': bool(r['enabled']),
                        'priority': r['priority'],
                        'has_key': bool(r['api_key'])
                    }

                settings = conn.execute(
                    "SELECT * FROM tenant_settings WHERE tenant_id=?",
                    (tenant['id'],)
                ).fetchone()

            def gs(key, default):
                return settings[key] if settings else default

            result = {
                # Receive (incoming) translation
                'receive_enabled': bool(gs('receive_enabled', 1)),
                'receive_target_lang': gs('receive_target_lang', 'zh'),
                'receive_engine': gs('receive_engine', 'google'),
                # Send (outgoing) translation
                'send_enabled': bool(gs('send_enabled', 1)),
                'send_target_lang': gs('send_target_lang', 'zh'),
                'send_engine': gs('send_engine', 'google'),
                # General
                'fallback_order': (gs('translate_fallback_order', 'google,baidu,deepseek,openai')).split(','),
                'engines': engine_configs,
                # AI translation detailed config
                'llm_provider': gs('llm_provider', 'deepseek'),
                'llm_model': gs('llm_model', 'deepseek-chat'),
                'llm_api_key': gs('llm_api_key', ''),
                'llm_api_endpoint': gs('llm_api_endpoint', ''),
                'llm_system_prompt': gs('llm_system_prompt', 'You are a professional translator. Translate the following text accurately.'),
                'llm_temperature': float(gs('llm_temperature', 0.3)),
                'llm_max_tokens': int(gs('llm_max_tokens', 1024)),
            }
            self.send_json(200, result)
        except Exception as e:
            self.send_json(500, {'error': str(e)})

    def handle_tenant_translate_config_put(self):
        username, tenant = require_tenant_auth(self)
        if not username: return
        try:
            body = self.read_body()

            with sqlite3.connect(DB_PATH) as conn:
                # Upsert tenant_settings
                conn.execute("""
                    INSERT INTO tenant_settings (tenant_id, receive_enabled, send_enabled,
                        receive_target_lang, send_target_lang, receive_engine, send_engine,
                        translate_target_lang, translate_engine,
                        translate_fallback_order, llm_provider, llm_model, llm_api_key,
                        llm_api_endpoint, llm_system_prompt, llm_temperature, llm_max_tokens,
                        updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                    ON CONFLICT(tenant_id) DO UPDATE SET
                        receive_enabled=excluded.receive_enabled,
                        send_enabled=excluded.send_enabled,
                        receive_target_lang=excluded.receive_target_lang,
                        send_target_lang=excluded.send_target_lang,
                        receive_engine=excluded.receive_engine,
                        send_engine=excluded.send_engine,
                        translate_target_lang=excluded.translate_target_lang,
                        translate_engine=excluded.translate_engine,
                        translate_fallback_order=excluded.translate_fallback_order,
                        llm_provider=excluded.llm_provider,
                        llm_model=excluded.llm_model,
                        llm_api_key=excluded.llm_api_key,
                        llm_api_endpoint=excluded.llm_api_endpoint,
                        llm_system_prompt=excluded.llm_system_prompt,
                        llm_temperature=excluded.llm_temperature,
                        llm_max_tokens=excluded.llm_max_tokens,
                        updated_at=datetime('now')
                """, (
                    tenant['id'],
                    body.get('receive_enabled', True),
                    body.get('send_enabled', True),
                    body.get('receive_target_lang', 'zh'),
                    body.get('send_target_lang', 'zh'),
                    body.get('receive_engine', 'google'),
                    body.get('send_engine', 'google'),
                    body.get('receive_target_lang', 'zh'),  # legacy translate_target_lang
                    body.get('receive_engine', 'google'),   # legacy translate_engine
                    ','.join(body.get('fallback_order', [])) if isinstance(body.get('fallback_order'), list) else body.get('fallback_order', 'google,baidu,deepseek,openai'),
                    body.get('llm_provider', 'deepseek'),
                    body.get('llm_model', 'deepseek-chat'),
                    body.get('llm_api_key', ''),
                    body.get('llm_api_endpoint', ''),
                    body.get('llm_system_prompt', 'You are a professional translator. Translate the following text accurately.'),
                    body.get('llm_temperature', 0.3),
                    body.get('llm_max_tokens', 1024),
                ))

                # Update engine API keys
                engine_key = body.get('engine_key')
                if engine_key and isinstance(engine_key, dict):
                    for eng_name, key_info in engine_key.items():
                        api_key = key_info.get('api_key', '') if isinstance(key_info, dict) else ''
                        enabled = key_info.get('enabled', True) if isinstance(key_info, dict) else True
                        conn.execute("""
                            INSERT INTO translation_config (tenant_id, engine, api_key, enabled, priority)
                            VALUES (?, ?, ?, ?, 0)
                            ON CONFLICT(tenant_id, engine) DO UPDATE SET
                                api_key=excluded.api_key,
                                enabled=excluded.enabled
                        """, (tenant['id'], eng_name, api_key, 1 if enabled else 0))

                conn.commit()

            self.send_json(200, {'success': True, 'message': 'Translation config updated'})
        except Exception as e:
            self.send_json(500, {'error': str(e)})


    # ========== CRM API ==========

    def handle_tenant_customers_list(self):
        """GET /api/tenant/customers — 客户列表"""
        username, tenant = require_tenant_auth(self)
        if not username: return
        try:
            params = self.query_params()
            search = params.get('search', '')
            tag = params.get('tag', '')
            status = params.get('status', '')

            with sqlite3.connect(DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                sql = "SELECT * FROM customer WHERE tenant_id=?"
                args = [tenant['id']]

                if search:
                    sql += " AND (display_name LIKE ? OR whatsapp_jid LIKE ? OR company LIKE ? OR phone LIKE ?)"
                    like = f"%{search}%"
                    args.extend([like, like, like, like])
                if tag:
                    sql += " AND tags LIKE ?"
                    args.append(f"%{tag}%")
                if status:
                    sql += " AND status=?"
                    args.append(status)

                sql += " ORDER BY last_contact_at DESC NULLS LAST, updated_at DESC LIMIT 100"
                rows = conn.execute(sql, args).fetchall()

                # Get pending reminder counts per customer
                cids = [r['id'] for r in rows]
                reminder_counts = {}
                if cids:
                    placeholders = ','.join(['?'] * len(cids))
                    rem_rows = conn.execute(
                        f"SELECT customer_id, COUNT(*) as cnt FROM customer_reminder WHERE customer_id IN ({placeholders}) AND is_done=0 GROUP BY customer_id",
                        cids
                    ).fetchall()
                    for r in rem_rows:
                        reminder_counts[r['customer_id']] = r['cnt']

                # Get interaction counts
                msg_counts = {}
                if cids:
                    placeholders = ','.join(['?'] * len(cids))
                    msg_rows = conn.execute(
                        f"SELECT customer_id, COUNT(*) as cnt FROM customer_interaction WHERE customer_id IN ({placeholders}) GROUP BY customer_id",
                        cids
                    ).fetchall()
                    for r in msg_rows:
                        msg_counts[r['customer_id']] = r['cnt']

            customers = []
            for r in rows:
                c = dict(r)
                c['tags'] = json.loads(c.get('tags', '[]'))
                c['pending_reminders'] = reminder_counts.get(c['id'], 0)
                c['message_count'] = msg_counts.get(c['id'], 0)
                customers.append(c)

            self.send_json(200, {'customers': customers, 'total': len(customers)})
        except Exception as e:
            self.send_json(500, {'error': f'获取客户列表失败: {str(e)}'})

    def handle_tenant_customers_create(self):
        """POST /api/tenant/customers — 创建或更新客户"""
        username, tenant = require_tenant_auth(self)
        if not username: return
        try:
            body = self.read_body()
            jid = body.get('whatsapp_jid', '').strip()
            if not jid:
                self.send_json(400, {'error': 'whatsapp_jid required'})
                return

            with sqlite3.connect(DB_PATH) as conn:
                # Check if exists
                existing = conn.execute(
                    "SELECT id FROM customer WHERE tenant_id=? AND whatsapp_jid=?",
                    (tenant['id'], jid)
                ).fetchone()

                tags = json.dumps(body.get('tags', []))

                if existing:
                    conn.execute("""
                        UPDATE customer SET
                            display_name=?, country=?, country_flag=?, phone=?, email=?,
                            company=?, tags=?, notes=?, status=?, updated_at=datetime('now')
                        WHERE id=?
                    """, (
                        body.get('display_name', ''), body.get('country', ''),
                        body.get('country_flag', ''), body.get('phone', ''),
                        body.get('email', ''), body.get('company', ''),
                        tags, body.get('notes', ''),
                        body.get('status', 'new'), existing[0]
                    ))
                    cid = existing[0]
                else:
                    cursor = conn.execute("""
                        INSERT INTO customer (tenant_id, whatsapp_jid, display_name, country, country_flag, phone, email, company, tags, notes, status, first_contact_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                    """, (
                        tenant['id'], jid,
                        body.get('display_name', ''), body.get('country', ''),
                        body.get('country_flag', ''), body.get('phone', ''),
                        body.get('email', ''), body.get('company', ''),
                        tags, body.get('notes', ''),
                        body.get('status', 'new')
                    ))
                    cid = cursor.lastrowid

                conn.commit()

            # Return created customer
            with sqlite3.connect(DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT * FROM customer WHERE id=?", (cid,)).fetchone()
                result = dict(row)
                result['tags'] = json.loads(result.get('tags', '[]'))

            self.send_json(200, {'customer': result, 'created': not bool(existing)})
        except Exception as e:
            self.send_json(500, {'error': f'创建客户失败: {str(e)}'})

    def handle_tenant_customers_detail(self, customer_id):
        """GET /api/tenant/customers/{id} — 客户详情 + 交互历史"""
        username, tenant = require_tenant_auth(self)
        if not username: return
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                customer = conn.execute(
                    "SELECT * FROM customer WHERE id=? AND tenant_id=?",
                    (customer_id, tenant['id'])
                ).fetchone()

                if not customer:
                    self.send_json(404, {'error': '客户不存在'})
                    return

                # Interactions (last 50)
                interactions = conn.execute(
                    "SELECT * FROM customer_interaction WHERE customer_id=? ORDER BY created_at DESC LIMIT 50",
                    (customer_id,)
                ).fetchall()

                # Reminders
                reminders = conn.execute(
                    "SELECT * FROM customer_reminder WHERE customer_id=? ORDER BY remind_at DESC",
                    (customer_id,)
                ).fetchall()

            result = dict(customer)
            result['tags'] = json.loads(result.get('tags', '[]'))
            result['interactions'] = [dict(r) for r in reversed(interactions)]
            result['reminders'] = [dict(r) for r in reminders]

            self.send_json(200, {'customer': result})
        except Exception as e:
            self.send_json(500, {'error': f'获取客户详情失败: {str(e)}'})

    def handle_tenant_customers_update(self, customer_id):
        """PUT /api/tenant/customers/{id} — 更新客户"""
        username, tenant = require_tenant_auth(self)
        if not username: return
        try:
            body = self.read_body()

            with sqlite3.connect(DB_PATH) as conn:
                existing = conn.execute(
                    "SELECT id FROM customer WHERE id=? AND tenant_id=?",
                    (customer_id, tenant['id'])
                ).fetchone()
                if not existing:
                    self.send_json(404, {'error': '客户不存在'})
                    return

                updates = []
                args = []
                fields = ['display_name', 'country', 'country_flag', 'phone', 'email',
                         'company', 'notes', 'status']
                for f in fields:
                    if f in body:
                        updates.append(f"{f}=?")
                        args.append(body[f])
                if 'tags' in body:
                    updates.append("tags=?")
                    args.append(json.dumps(body['tags']))

                if updates:
                    updates.append("updated_at=datetime('now')")
                    args.append(customer_id)
                    conn.execute(
                        f"UPDATE customer SET {', '.join(updates)} WHERE id=?",
                        args
                    )
                    conn.commit()

            self.send_json(200, {'success': True})
        except Exception as e:
            self.send_json(500, {'error': f'更新客户失败: {str(e)}'})

    def handle_tenant_customers_delete(self, customer_id):
        """DELETE /api/tenant/customers/{id} — 删除客户"""
        username, tenant = require_tenant_auth(self)
        if not username: return
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute(
                    "DELETE FROM customer WHERE id=? AND tenant_id=?",
                    (customer_id, tenant['id'])
                )
                conn.execute(
                    "DELETE FROM customer_interaction WHERE customer_id=? AND tenant_id=?",
                    (customer_id, tenant['id'])
                )
                conn.execute(
                    "DELETE FROM customer_reminder WHERE customer_id=? AND tenant_id=?",
                    (customer_id, tenant['id'])
                )
                conn.commit()
            self.send_json(200, {'success': True})
        except Exception as e:
            self.send_json(500, {'error': f'删除客户失败: {str(e)}'})

    def handle_tenant_reminders_create(self, customer_id):
        """POST /api/tenant/customers/{id}/reminders — 添加提醒"""
        username, tenant = require_tenant_auth(self)
        if not username: return
        try:
            body = self.read_body()
            text = body.get('reminder_text', '').strip()
            if not text:
                self.send_json(400, {'error': 'reminder_text required'})
                return

            with sqlite3.connect(DB_PATH) as conn:
                cursor = conn.execute(
                    "INSERT INTO customer_reminder (customer_id, tenant_id, reminder_text, remind_at) VALUES (?, ?, ?, ?)",
                    (customer_id, tenant['id'], text, body.get('remind_at', None))
                )
                conn.commit()
                rid = cursor.lastrowid

            self.send_json(200, {'success': True, 'reminder_id': rid})
        except Exception as e:
            self.send_json(500, {'error': f'创建提醒失败: {str(e)}'})

    def handle_tenant_reminders_complete(self, customer_id, reminder_id):
        """PUT /api/tenant/customers/{id}/reminders/{rid} — 完成提醒"""
        username, tenant = require_tenant_auth(self)
        if not username: return
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute(
                    "UPDATE customer_reminder SET is_done=1 WHERE id=? AND customer_id=? AND tenant_id=?",
                    (reminder_id, customer_id, tenant['id'])
                )
                conn.commit()
            self.send_json(200, {'success': True})
        except Exception as e:
            self.send_json(500, {'error': f'更新提醒失败: {str(e)}'})

    def handle_tenant_customers_timeline(self, customer_id):
        """GET /api/tenant/customers/{id}/timeline — 交互时间线"""
        username, tenant = require_tenant_auth(self)
        if not username: return
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                interactions = conn.execute(
                    "SELECT * FROM customer_interaction WHERE customer_id=? AND tenant_id=? ORDER BY created_at DESC LIMIT 200",
                    (customer_id, tenant['id'])
                ).fetchall()

            self.send_json(200, {
                'timeline': [dict(r) for r in reversed(interactions)]
            })
        except Exception as e:
            self.send_json(500, {'error': f'获取时间线失败: {str(e)}'})


    def do_GET(self):
        path = self.path.split('?')[0]

        if path == '/api/tenant/me':
            self.handle_tenant_me()
        elif path == '/api/tenant/self/session':
            self.handle_tenant_self_session_get()
        elif path == '/api/tenant/qr':
            self.handle_tenant_self_qr()
        elif path.startswith('/api/tenant/chats/') and '/messages' in path:
            # /api/tenant/chats/{chatId}/messages
            parts = path.split('/')
            chat_id = parts[4]  # /api/tenant/chats/{chatId}/messages
            self.handle_tenant_chat_messages(chat_id)
        elif path == '/api/tenant/chats':
            self.handle_tenant_chats()
        elif path == '/api/tenant/contacts':
            self.handle_tenant_contacts()
        elif path == '/api/tenant/translate/engines':
            self.handle_tenant_translate_engines()
        elif path == '/api/tenant/translate/config':
            self.handle_tenant_translate_config_get()
        elif path == '/api/tenant/customers':
            self.handle_tenant_customers_list()
        elif path.startswith('/api/tenant/customers/') and '/timeline' in path:
            # /api/tenant/customers/{id}/timeline
            cid = path.split('/')[4]
            self.handle_tenant_customers_timeline(cid)
        elif path.startswith('/api/tenant/customers/') and '/reminders' in path:
            # /api/tenant/customers/{id}/reminders — handled in do_POST
            pass
        elif path.startswith('/api/tenant/customers/'):
            # /api/tenant/customers/{id}
            cid = path.split('/')[4]
            self.handle_tenant_customers_detail(cid)
        elif path == '/api/tenant/pushnames':
            self.handle_tenant_pushnames()
        elif path == '/api/admin/status':
            self.handle_status()
        elif path == '/api/admin/tenants':
            self.handle_tenants('GET')
        elif path == '/api/admin/proxy':
            self.handle_proxy('GET')
        elif path.startswith('/api/admin/tenants/') and '/qr' in path:
            try:
                self.handle_tenant_qr(int(path.split('/')[4]))
            except: self.send_json(404, {})
        elif path.startswith('/api/admin/tenants/') and '/container/' in path:
            parts = path.split('/')
            self.handle_tenant_container(int(parts[4]), parts[6] if len(parts) > 6 else 'status')
        elif path.startswith('/api/admin/tenants/'):
            try:
                self.handle_tenant_detail(int(path.split('/')[4]), 'GET')
            except: self.send_json(404, {})
        elif path == '/' or path == '/admin':
            self.serve_file('/opt/whatsapp-saas/admin/index.html', 'text/html')
        elif path == '/tenant' or path == '/tenant/':
            self.serve_file('/opt/whatsapp-saas/tenant/index.html', 'text/html')
        elif path == '/tenant/quotation' or path == '/tenant/quotation/':
            self.serve_file('/opt/whatsapp-saas/tenant/quotation.html', 'text/html')
        elif path.startswith('/assets/') or path.startswith('/tenant/'):
            self.serve_static(path)
        else:
            self.send_json(404, {})

    def do_POST(self):
        path = self.path.split('?')[0]
        if path == '/api/tenant/login':
            self.handle_tenant_login()
        elif path == '/api/tenant/translate':
            self.handle_tenant_translate()
        elif path == '/api/tenant/send':
            self.handle_tenant_send()
        elif path.startswith('/api/tenant/self/session/'):
            self.handle_tenant_self_session(path.split('/')[-1])
        elif path == '/api/admin/login':
            self.handle_login()
        elif path == '/api/admin/tenants':
            self.handle_tenants('POST')
        elif path.startswith('/api/admin/tenants/') and '/session/' in path:
            parts = path.split('/')
            try:
                self.handle_tenant_session(int(parts[4]), parts[6] if len(parts) > 6 else 'start')
            except: self.send_json(404, {})
        elif path.startswith('/api/admin/tenants/') and '/container/' in path:
            parts = path.split('/')
            self.handle_tenant_container(int(parts[4]), parts[6] if len(parts) > 6 else 'start')
        elif path == '/api/tenant/customers':
            # POST create customer — same as PUT for upsert
            self.handle_tenant_customers_create()
        elif path.startswith('/api/tenant/customers/') and path.endswith('/reminders'):
            cid = path.split('/')[4]
            self.handle_tenant_reminders_create(cid)
        elif path.startswith('/api/tenant/customers/') and '/reminders/' in path:
            parts = path.split('/')
            cid = parts[4]
            rid = parts[-1]
            self.handle_tenant_reminders_complete(cid, rid)
        else:
            self.send_json(404, {})

    def do_PUT(self):
        path = self.path.split('?')[0]
        if path == '/api/admin/proxy':
            self.handle_proxy('PUT')
        elif path == '/api/tenant/contacts':
            self.handle_tenant_contacts_update()
        elif path == '/api/tenant/translate/batch':
            self.handle_tenant_translate_batch()
        elif path == '/api/tenant/translate/config':
            self.handle_tenant_translate_config_put()
        elif path.startswith('/api/tenant/customers/'):
            # PUT /api/tenant/customers/{id}
            cid = path.split('/')[4]
            self.handle_tenant_customers_update(cid)
        elif path.startswith('/api/admin/tenants/'):
            try:
                self.handle_tenant_detail(int(path.split('/')[4]), 'PUT')
            except: self.send_json(404, {})
        else:
            self.send_json(404, {})

    def do_DELETE(self):
        path = self.path.split('?')[0]
        if path.startswith('/api/admin/tenants/'):
            try:
                self.handle_tenant_detail(int(path.split('/')[4]), 'DELETE')
            except: self.send_json(404, {})
        else:
            self.send_json(404, {})

    # ===== 静态文件 =====
    def serve_file(self, filepath, content_type):
        if os.path.exists(filepath):
            self.send_response(200)
            self.send_header('Content-Type', f'{content_type}; charset=utf-8')
            self.end_headers()
            with open(filepath, 'r' if 'text' in content_type else 'rb') as f:
                self.wfile.write(f.read() if 'text' not in content_type else f.read().encode())
        else:
            self.send_json(404, {})

    def serve_static(self, path):
        if path.startswith('/tenant/'):
            filepath = os.path.join('/opt/whatsapp-saas/tenant', path[len('/tenant/'):])
        else:
            filepath = os.path.join('/opt/whatsapp-saas/admin', path.lstrip('/'))
        if os.path.exists(filepath):
            ext = os.path.splitext(filepath)[1]
            ct = {'.css': 'text/css', '.js': 'application/javascript', 
                  '.png': 'image/png', '.svg': 'image/svg+xml',
                  '.html': 'text/html', '.ico': 'image/x-icon'}
            self.serve_file(filepath, ct.get(ext, 'application/octet-stream'))
        else:
            self.send_json(404, {})

def run(port=8080):
    init_db()
    class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    server = ThreadingHTTPServer(('0.0.0.0', port), AdminHandler)
    print(f"管理后台运行在 0.0.0.0:{port}")
    server.serve_forever()

if __name__ == '__main__':
    import sys
    run(int(sys.argv[1]) if len(sys.argv) > 1 else 8080)
