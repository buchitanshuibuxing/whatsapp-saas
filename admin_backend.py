#!/usr/bin/env python3
"""WhatsApp SaaS 管理后台 — Python stdlib only (sqlite3)"""
import os, json, hashlib, secrets, time, subprocess, sqlite3, re, datetime, shutil
import http.server
import socketserver
import urllib.request
import urllib.error
import ssl
from translation_engine import TranslationManager, ENGINE_LABELS, ENGINE_REGISTRY
import baileys_adapter

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

def query_tenant_waha(username, tenant):
    """查询租户 Baileys 会话状态，返回 {waha_status, waha_number, waha_name, waha_picture}"""
    result = {'waha_status': 'offline', 'waha_number': tenant.get('whatsapp_number', ''), 'waha_name': '', 'waha_picture': ''}
    if not username:
        result['waha_status'] = 'none'
        return result
    try:
        session = baileys_adapter.get_session(username)
        if 'error' in session:
            return result
        status = session.get('status', 'offline')
        result['waha_status'] = status
        result['waha_number'] = session.get('whatsappNumber') or tenant.get('whatsapp_number', '')
        result['waha_name'] = session.get('whatsappName', '')
        wa_num = session.get('whatsappNumber', '')
        if wa_num and wa_num != tenant.get('whatsapp_number', ''):
            with get_db() as conn:
                conn.execute(
                    "UPDATE tenant_users SET whatsapp_number=?, whatsapp_connected=1 WHERE id=?",
                    (wa_num, tenant['id'])
                )
                conn.commit()
        if status in ('WORKING', 'CONNECTED'):
            try:
                profile = baileys_adapter.get_profile(username)
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

# === ADMIN V2 HELPERS ===

def audit_log(db, actor, action, details=''):
    try:
        db.execute('INSERT INTO audit_logs (actor, action, details) VALUES (?,?,?)',
                   (actor, action, details))
        db.commit()
    except Exception:
        pass

def init_db_v2():
    import sqlite3 as _sql
    db = _sql.connect('/opt/whatsapp-saas/admin.db')
    db.row_factory = _sql.Row
    db.execute('''CREATE TABLE IF NOT EXISTS audit_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        actor TEXT DEFAULT 'system',
        action TEXT NOT NULL,
        details TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now'))
    )''')
    db.execute('''CREATE TABLE IF NOT EXISTS backup_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        filename TEXT NOT NULL,
        filepath TEXT NOT NULL,
        size_bytes INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now'))
    )''')

    # === Server management ===
    db.execute('''CREATE TABLE IF NOT EXISTS servers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        host TEXT NOT NULL,
        port INTEGER DEFAULT 7022,
        role TEXT DEFAULT 'worker',
        status TEXT DEFAULT 'unknown',
        cpu_cores INTEGER,
        memory_gb REAL,
        disk_gb REAL,
        last_heartbeat TEXT,
        config TEXT DEFAULT '{}',
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now'))
    )''')

    # === Proxy pool ===
    db.execute('''CREATE TABLE IF NOT EXISTS proxy_pool (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        proxy_type TEXT DEFAULT 'socks5',
        host TEXT NOT NULL,
        port INTEGER NOT NULL,
        username TEXT,
        password TEXT,
        vmess_config TEXT,
        local_port INTEGER,
        xray_pid INTEGER,
        status TEXT DEFAULT 'unknown',
        latency_ms INTEGER,
        success_rate REAL DEFAULT 0,
        last_check TEXT,
        fail_count INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now'))
    )''')

    # === Baileys engine config ===
    db.execute('''CREATE TABLE IF NOT EXISTS baileys_config (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        config_key TEXT UNIQUE NOT NULL,
        config_value TEXT,
        description TEXT,
        updated_at TEXT DEFAULT (datetime('now'))
    )''')

    # === HA / dual-active config ===
    db.execute('''CREATE TABLE IF NOT EXISTS ha_config (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        config_key TEXT UNIQUE NOT NULL,
        config_value TEXT,
        description TEXT,
        updated_at TEXT DEFAULT (datetime('now'))
    )''')
    db.commit()
    db.close()
def _read_meminfo():
    with open('/proc/meminfo') as f:
        d = {}
        for line in f:
            k, v = line.split(':')
            d[k.strip()] = int(v.strip().split()[0])
    return d

def _get_memory():
    m = _read_meminfo()
    total = m['MemTotal'] * 1024
    avail = m.get('MemAvailable', m['MemFree']) * 1024
    used = total - avail
    return type('mem',(),{'total':total,'used':used,'available':avail,'percent':round(used/total*100,1)})()

def _get_disk(path='/'):
    s = os.statvfs(path)
    total = s.f_frsize * s.f_blocks
    free = s.f_frsize * s.f_bavail
    used = total - free
    return type('disk',(),{'total':total,'used':used,'free':free,'percent':round(used/total*100,1)})()

def _get_cpu():
    with open('/proc/stat') as f:
        parts = f.readline().split()
        idle = int(parts[4])
        total = sum(int(x) for x in parts[1:])
    return type('cpu',(),{'percent':round((1-idle/total)*100,1)})()

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
        wa_info = query_tenant_waha(username, tenant)
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
        wa_info = query_tenant_waha(username, tenant)
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
        try:
            png_data, content_type = baileys_adapter.get_qr(username)
            if png_data is None:
                self.send_json(400, {'error': f'QR 码不可用: {content_type}'})
                return
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(png_data)
        except Exception as e:
            self.send_json(500, {'error': f'获取二维码失败: {str(e)}'})

    # ===== 租户自助会话控制 =====
    def handle_tenant_self_session(self, action):
        username, tenant = require_tenant_auth(self)
        if not username: return
        try:
            if action == 'restart':
                result = baileys_adapter.restart_session(username)
                self.send_json(200, {'message': '会话已重启', 'result': result})
            elif action == 'logout':
                baileys_adapter.logout_session(username)
                with get_db() as conn:
                    conn.execute("UPDATE tenant_users SET whatsapp_number='', whatsapp_connected=0 WHERE id=?",
                                 (tenant['id'],))
                    conn.commit()
                self.send_json(200, {'message': '已登出'})
            elif action == 'stop':
                baileys_adapter.stop_session(username)
                self.send_json(200, {'message': '会话已停止'})
            elif action == 'start':
                baileys_adapter.start_session(username)
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
                if t.get('is_active'):
                    wa_info = query_tenant_waha(t['username'], t)
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
                if d.get('is_active'):
                    wa = query_tenant_waha(d['username'], d)
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
        """Baileys 版本：管理租户会话（不再创建 Docker 容器）"""
        username = require_auth(self)
        if not username: return

        tenant = get_tenant(tenant_id)
        if not tenant:
            self.send_json(404, {'error': '租户不存在'})
            return

        tenant_name = tenant['username']

        if action in ('start', 'create'):
            result = baileys_adapter.create_session(tenant_name)
            if 'error' in result:
                self.send_json(500, {'error': result['error']})
                return
            # 更新数据库状态
            with get_db() as conn:
                conn.execute(
                    "UPDATE tenant_users SET whatsapp_connected=0 WHERE id=?",
                    (tenant_id,)
                )
                conn.commit()
            self.send_json(200, {
                'message': '会话已创建',
                'session': result,
                'username': tenant_name
            })

        elif action == 'stop':
            result = baileys_adapter.stop_session(tenant_name)
            self.send_json(200, {'message': '会话已停止', 'result': result})

        elif action == 'status':
            result = baileys_adapter.get_session(tenant_name)
            self.send_json(200, {
                'username': tenant_name,
                'status': result.get('status', 'unknown'),
                'whatsappNumber': result.get('whatsappNumber', ''),
                'whatsappName': result.get('whatsappName', ''),
                'baileys_status': result.get('status', 'offline')
            })
        else:
            self.send_json(400, {'error': f'未知操作: {action}'})

    # ===== QR 码 =====
    def handle_tenant_qr(self, tenant_id):
        """Baileys 版本：从 Baileys Engine 获取 QR 码"""
        username = require_auth(self)
        if not username: return

        tenant = get_tenant(tenant_id)
        if not tenant:
            self.send_json(404, {'error': '租户不存在'})
            return

        tenant_name = tenant['username']
        try:
            qr = baileys_adapter.get_qr(tenant_name)
            if 'error' in qr:
                self.send_json(500, {'error': qr['error']})
                return
            img_b64 = qr.get('qr', '')
            if not img_b64:
                self.send_json(500, {'error': '会话未进入扫码状态，请先创建会话'})
                return
            import base64
            img_data = base64.b64decode(img_b64)
            self.send_response(200)
            self.send_header('Content-Type', 'image/png')
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(img_data)
        except Exception as e:
            self.send_json(500, {'error': f'获取二维码失败: {str(e)}'})

    # ===== 会话控制 =====
    def handle_tenant_session(self, tenant_id, action):
        """Baileys 版本：通过 Baileys Engine 控制会话"""
        username = require_auth(self)
        if not username: return

        tenant = get_tenant(tenant_id)
        if not tenant:
            self.send_json(404, {'error': '租户不存在'})
            return

        tenant_name = tenant['username']
        try:
            if action == 'restart':
                result = baileys_adapter.restart_session(tenant_name)
                self.send_json(200, {'message': '会话已重启', 'result': result})
            elif action == 'logout':
                result = baileys_adapter.logout_session(tenant_name)
                with get_db() as conn:
                    conn.execute("UPDATE tenant_users SET whatsapp_number='', whatsapp_connected=0 WHERE id=?",
                                 (tenant_id,))
                    conn.commit()
                self.send_json(200, {'message': '已登出，需要重新扫码连接'})
            elif action == 'stop':
                result = baileys_adapter.stop_session(tenant_name)
                self.send_json(200, {'message': '会话已停止'})
            elif action == 'start':
                result = baileys_adapter.create_session(tenant_name)
                self.send_json(200, {'message': '会话已启动', 'result': result})
            else:
                self.send_json(400, {'error': f'未知操作: {action}'})
        except Exception as e:
            self.send_json(500, {'error': str(e)})

    # ===== Baileys Webhook 接收 =====
    def handle_baileys_webhook(self):
        """接收 Baileys Engine 推送的消息和状态更新"""
        try:
            data = json.loads(self.read_body())
        except:
            self.send_json(400, {'error': '无效 JSON'})
            return

        event = data.get('event', '')
        session_name = data.get('session', '')

        if event == 'message':
            baileys_adapter.handle_incoming_webhook(data)

        elif event == 'connection.update':
            conn_status = data.get('connection', '')
            wa_number = data.get('user', {}).get('id', '')
            if session_name:
                with get_db() as conn:
                    conn.execute(
                        "UPDATE tenant_users SET waha_status=?, whatsapp_connected=? WHERE username=?",
                        (conn_status, 1 if conn_status == 'open' else 0, session_name)
                    )
                    if wa_number:
                        if '@' in wa_number:
                            wa_number = wa_number.split('@')[0]
                        conn.execute(
                            "UPDATE tenant_users SET whatsapp_number=?, whatsapp_connected=1 WHERE username=?",
                            (wa_number, session_name)
                        )
                    conn.commit()

        elif event == 'qr':
            pass

        self.send_json(200, {'received': True})

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
        tenant_id = tenant.get('id', 0)
        data = self.read_body()
        chat_id = data.get('chatId', '')
        text = data.get('text', '')
        if not chat_id or not text:
            self.send_json(400, {'error': '缺少 chatId 或 text'})
            return
        if '@' not in chat_id and chat_id.isdigit():
            chat_id = chat_id + '@s.whatsapp.net'
        try:
            result = baileys_adapter.send_text(username, chat_id, text)
            if 'error' in result:
                self.send_json(500, {'error': result['error']})
                return
            try:
                with sqlite3.connect(DB_PATH) as conn:
                    conn.execute(
                        "INSERT INTO message_logs (tenant_id, direction, remote_jid, message_type, content, status) VALUES (?, 'out', ?, 'text', ?, ?)",
                        (tenant_id, chat_id, text, 'sent')
                    )
                    conn.commit()
            except Exception:
                pass
            self.send_json(200, result)
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


    # ===== ADMIN V2 HANDLERS =====

    def handle_admin_dashboard(self):
        require_auth(self)
        db = self._get_db_h()
        tc = db.execute('SELECT COUNT(*) FROM tenant_users').fetchone()[0]
        ac = db.execute("SELECT COUNT(*) FROM tenant_account WHERE waha_status='WORKING'").fetchone()[0]
        ts = db.execute('SELECT COUNT(*) FROM tenant_account').fetchone()[0]
        mc = db.execute('SELECT COUNT(*) FROM message_logs').fetchone()[0]
        cc = db.execute('SELECT COUNT(*) FROM customer').fetchone()[0]
        mem = _get_memory()
        dsk = _get_disk('/')
        self.send_json(200, {
            'tenants': tc, 'customers': cc, 'messages': mc,
            'sessions': {'total': ts, 'active': ac},
            'system': {
                'cpu_percent': _get_cpu().percent,
                'memory': {'total_gb': round(mem.total/1073741824,1), 'used_gb': round(mem.used/1073741824,1), 'percent': mem.percent},
                'disk': {'total_gb': round(dsk.total/1073741824,1), 'used_gb': round(dsk.used/1073741824,1), 'percent': dsk.percent},
            }
        })

    def handle_admin_sessions(self):
        require_auth(self)
        db = self._get_db_h()
        rows = db.execute('''SELECT ta.id, ta.tenant_id, tu.company_name, tu.username, ta.session_name,
            ta.waha_status, ta.whatsapp_number, ta.whatsapp_name, ta.config_json
            FROM tenant_account ta JOIN tenant_users tu ON ta.tenant_id = tu.id ORDER BY ta.id DESC''').fetchall()
        out = []
        for r in rows:
            cfg = json.loads(r[8]) if r[8] else {}
            out.append({'id':r[0],'tenant_id':r[1],'company':r[2],'username':r[3],
                'session_name':r[4],'status':r[5],'whatsapp_number':r[6],'whatsapp_name':r[7],
                'fingerprint':cfg.get('browser_os','ubuntu')+'/'+cfg.get('browser_name','Chrome')})
        self.send_json(200, {'sessions': out})

    def handle_admin_session_config(self):
        require_auth(self)
        body = self.read_body()
        sid = body.get('session_id')
        if not sid: return self.send_json(400, {'error':'session_id required'})
        db = self._get_db_h()
        row = db.execute('SELECT config_json, session_name, tenant_id FROM tenant_account WHERE id=?',(sid,)).fetchone()
        if not row: return self.send_json(404, {'error':'not found'})
        cfg = json.loads(row[0]) if row[0] else {}
        if 'browser_os' in body: cfg['browser_os'] = body['browser_os']
        if 'browser_name' in body: cfg['browser_name'] = body['browser_name']
        db.execute('UPDATE tenant_account SET config_json=? WHERE id=?',(json.dumps(cfg),sid))
        db.commit()
        audit_log(db, 'admin', 'Session config: '+row[1]+' -> '+str(cfg))
        self.send_json(200, {'ok':True,'config':cfg})

    def handle_admin_health(self):
        require_auth(self)
        checks = {}
        try:
            urllib.request.urlopen('http://127.0.0.1:10809', timeout=3)
            checks['xray'] = {'status':'ok','detail':'SOCKS5:10808 / HTTP:10809 正常'}
        except Exception as e:
            checks['xray'] = {'status':'error','detail':str(e)[:80],'cause':'Xray代理未运行或端口不可达','fix':'sudo systemctl restart xray; 检查: /usr/local/etc/xray/config.json'}
        try:
            req = urllib.request.Request('http://127.0.0.1:3500/api/health')
            urllib.request.urlopen(req, timeout=3)
            checks['baileys_engine'] = {'status':'ok','detail':'Baileys引擎运行正常'}
        except Exception as e:
            checks['baileys_engine'] = {'status':'error','detail':str(e)[:80],'cause':'Baileys引擎未启动或端口3500不可达','fix':'docker compose up -d baileys-engine'}
        try:
            r = subprocess.run(['docker','ps','--format','{{.Names}}'],capture_output=True,text=True,timeout=5)
            names = [l for l in r.stdout.splitlines() if l.strip()]
            checks['docker'] = {'status':'ok','containers':len(names),'detail':f'{len(names)} 个容器运行中'}
        except Exception as e:
            checks['docker'] = {'status':'error','detail':str(e)[:80],'cause':'Docker守护进程未运行','fix':'sudo systemctl start docker && sudo systemctl enable docker'}
        dsk = _get_disk('/')
        mem = _get_memory()
        disk_ok = dsk.percent < 90
        checks['disk'] = {'total_gb':round(dsk.total/1073741824,1),'free_gb':round(dsk.free/1073741824,1),'percent':dsk.percent,'status':'ok' if disk_ok else 'warning','detail':f'{dsk.free/1073741824:.1f} GB 可用 / {dsk.total/1073741824:.1f} GB ({dsk.percent}%)'}
        if not disk_ok:
            checks['disk']['cause'] = f'磁盘使用率达{dsk.percent}%，空间不足'
            checks['disk']['fix'] = '1.清理Docker: docker system prune -a 2.清理日志: journalctl --vacuum-size=500M 3.扩容磁盘'
        mem_ok = mem.percent < 90
        checks['memory'] = {'total_gb':round(mem.total/1073741824,1),'available_gb':round(mem.available/1073741824,1),'percent':mem.percent,'status':'ok' if mem_ok else 'warning','detail':f'{mem.available/1073741824:.1f} GB 可用 / {mem.total/1073741824:.1f} GB ({mem.percent}%)'}
        if not mem_ok:
            checks['memory']['cause'] = f'内存使用率达{mem.percent}%，可能影响服务性能'
            checks['memory']['fix'] = '1.检查高内存进程: top -o %MEM 2.减少会话并发数 3.重启服务释放内存'
        try:
            r = subprocess.run(['systemctl','is-active','wa-admin'],capture_output=True,text=True,timeout=5)
            if r.stdout.strip()=='active':
                checks['wa_admin'] = {'status':'ok','detail':'管理后台服务运行中'}
            else:
                checks['wa_admin'] = {'status':'error','detail':r.stdout.strip(),'cause':'wa-admin服务未运行','fix':'sudo systemctl restart wa-admin; journalctl -u wa-admin -n 50 --no-pager'}
        except Exception as e:
            checks['wa_admin'] = {'status':'error','detail':str(e)[:80],'cause':'无法检测wa-admin服务状态','fix':'sudo systemctl status wa-admin'}
        all_good = all(c.get('status')=='ok' for c in checks.values() if isinstance(c,dict) and 'status' in c)
        checks['overall'] = 'healthy' if all_good else 'degraded'
        self.send_json(200, checks)

    def handle_admin_audit(self):
        require_auth(self)
        db = self._get_db_h()
        rows = db.execute('SELECT * FROM audit_logs ORDER BY id DESC LIMIT 100').fetchall()
        logs = [{'id':r[0],'actor':r[1],'action':r[2],'details':r[3],'created_at':r[4]} for r in rows]
        self.send_json(200, {'logs':logs})

    def handle_admin_backup(self):
        require_auth(self)
        db = self._get_db_h()
        d = '/opt/whatsapp-saas/backups'
        os.makedirs(d, exist_ok=True)
        ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        fn = 'admin_db_'+ts+'.db'
        fp = os.path.join(d, fn)
        shutil.copy2('/opt/whatsapp-saas/admin.db', fp)
        sz = os.path.getsize(fp)
        db.execute('INSERT INTO backup_records (filename,filepath,size_bytes) VALUES (?,?,?)',(fn,fp,sz))
        db.commit()
        audit_log(db, 'admin', 'Backup: '+fn)
        self.send_json(200, {'ok':True,'filename':fn,'size':sz})

    def handle_admin_backups_list(self):
        require_auth(self)
        db = self._get_db_h()
        rows = db.execute('SELECT * FROM backup_records ORDER BY id DESC LIMIT 20').fetchall()
        bk = [{'id':r[0],'filename':r[1],'size':r[3],'created_at':r[4]} for r in rows]
        self.send_json(200, {'backups':bk})

    def _get_db_h(self):
        if not hasattr(self, '_v2db'):
            self._v2db = sqlite3.connect('/opt/whatsapp-saas/admin.db')
        return self._v2db

    # ===== NEW V3 HANDLERS =====

    def handle_admin_settings_get(self):
        require_auth(self)
        db = self._get_db_h()
        rows = db.execute('SELECT key, value FROM system_settings ORDER BY key').fetchall()
        flat = {r[0]: r[1] for r in rows}

        # Build structured AI models config
        ai_models = {}
        providers = ['openai', 'deepseek']
        for p in providers:
            key = flat.get(f'ai.{p}.api_key', '')
            base = flat.get(f'ai.{p}.api_base', '')
            model = flat.get(f'ai.{p}.model', '')
            ai_models[p] = {'api_key': key, 'api_base': base, 'model': model}

        # Custom providers
        custom_raw = flat.get('ai.custom', '[]')
        try:
            ai_custom = json.loads(custom_raw)
        except:
            ai_custom = []
        ai_models['custom'] = ai_custom

        # Translation engine keys
        tr_engines = {}
        tr_map = {
            'baidu': {'app_id': 'tr.baidu.app_id', 'secret_key': 'tr.baidu.secret_key'},
            'deepl': {'api_key': 'tr.deepl.api_key'},
            'deepseek': {'api_key': 'tr.deepseek.api_key'},
            'openai': {'api_key': 'tr.openai.api_key'},
            'qwen': {'api_key': 'tr.qwen.api_key', 'api_base': 'tr.qwen.api_base'},
            'glm': {'api_key': 'tr.glm.api_key'},
            'moonshot': {'api_key': 'tr.moonshot.api_key'},
        }
        for eng, keys in tr_map.items():
            cfg = {}
            for field_name, db_key in keys.items():
                cfg[field_name] = flat.get(db_key, '')
            tr_engines[eng] = cfg

        # Defaults
        defaults = {
            'translate_engine': flat.get('default_translate_engine', 'google'),
            'ai_model': flat.get('default_ai_model', 'deepseek-chat'),
        }

        self.send_json(200, {
            'settings': flat,
            'ai_models': ai_models,
            'translate_engines': tr_engines,
            'defaults': defaults,
        })

    def handle_admin_settings_put(self):
        require_auth(self)
        data = self.read_body()
        db = self._get_db_h()
        updated = []

        def set_kv(key, value):
            if not isinstance(key, str):
                return
            val = str(value) if not isinstance(value, str) else value
            db.execute(
                "INSERT INTO system_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
                (key, val)
            )
            updated.append(key)

        # Flat settings (backward compat)
        for key, value in data.items():
            if key in ('ai_models', 'translate_engines', 'defaults'):
                continue
            if not isinstance(value, str):
                continue
            set_kv(key, value)

        # AI models config
        ai_models = data.get('ai_models', {})
        for provider in ('openai', 'deepseek'):
            cfg = ai_models.get(provider, {})
            if isinstance(cfg, dict):
                for field in ('api_key', 'api_base', 'model'):
                    set_kv(f'ai.{provider}.{field}', cfg.get(field, ''))

        # Custom AI providers
        custom_list = ai_models.get('custom', [])
        if isinstance(custom_list, list):
            # Validate structure
            clean = []
            for c in custom_list:
                if isinstance(c, dict) and (c.get('name') or c.get('model')):
                    clean.append({
                        'name': str(c.get('name') or c.get('model', '')),
                        'api_key': str(c.get('api_key', '')),
                        'api_base': str(c.get('api_base', '')),
                        'model': str(c.get('model', '')),
                    })
            set_kv('ai.custom', json.dumps(clean, ensure_ascii=False))

        # Translation engine keys
        tr_engines = data.get('translate_engines', {})
        tr_map = {
            'baidu': {'app_id': 'tr.baidu.app_id', 'secret_key': 'tr.baidu.secret_key'},
            'deepl': {'api_key': 'tr.deepl.api_key'},
            'deepseek': {'api_key': 'tr.deepseek.api_key'},
            'openai': {'api_key': 'tr.openai.api_key'},
            'qwen': {'api_key': 'tr.qwen.api_key', 'api_base': 'tr.qwen.api_base'},
            'glm': {'api_key': 'tr.glm.api_key'},
            'moonshot': {'api_key': 'tr.moonshot.api_key'},
        }
        for eng, key_map in tr_map.items():
            cfg = tr_engines.get(eng, {})
            if isinstance(cfg, dict):
                for field_name, db_key in key_map.items():
                    set_kv(db_key, cfg.get(field_name, ''))

        # Defaults
        defaults = data.get('defaults', {})
        if defaults.get('translate_engine'):
            set_kv('default_translate_engine', defaults['translate_engine'])
        if defaults.get('ai_model'):
            set_kv('default_ai_model', defaults['ai_model'])

        db.commit()
        audit_log(db, 'admin', 'Updated settings: ' + ', '.join(updated))
        self.send_json(200, {'ok': True, 'updated': updated})

    def handle_admin_change_password(self):
        require_auth(self)
        data = self.read_body()
        old_pw = data.get('old_password', '')
        new_pw = data.get('new_password', '')
        if not old_pw or not new_pw:
            self.send_json(400, {'error': '请提供旧密码和新密码'})
            return
        if len(new_pw) < 6:
            self.send_json(400, {'error': '新密码至少6个字符'})
            return
        db = self._get_db_h()
        # Verify old password
        row = db.execute(
            "SELECT password_hash FROM admin_users WHERE username='admin'"
        ).fetchone()
        if not row or row[0] != hash_password(old_pw):
            self.send_json(403, {'error': '旧密码错误'})
            return
        # Update password
        db.execute(
            "UPDATE admin_users SET password_hash=? WHERE username='admin'",
            (hash_password(new_pw),)
        )
        db.commit()
        audit_log(db, 'admin', 'Changed password')
        self.send_json(200, {'ok': True, 'message': '密码已修改'})


    def handle_admin_alerts(self):
        require_auth(self)
        db = self._get_db_h()
        db.execute("""CREATE TABLE IF NOT EXISTS alert_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, metric TEXT NOT NULL, operator TEXT DEFAULT '>',
            threshold REAL NOT NULL, enabled INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now'))
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS alert_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id INTEGER, message TEXT NOT NULL, level TEXT DEFAULT 'warning',
            is_active INTEGER DEFAULT 1, created_at TEXT DEFAULT (datetime('now')),
            resolved_at TEXT
        )""")
        db.commit()
        dsk = _get_disk('/')
        mem = _get_memory()
        alerts = []
        if dsk.percent > 85:
            lv = 'warning' if dsk.percent < 95 else 'critical'
            alerts.append({'id':'disk','metric':'disk','level':lv,
                'message':'磁盘使用率 %d%% (剩余 %.1f GB)' % (dsk.percent, dsk.free/1073741824),
                'suggestion':'清理Docker镜像或日志文件','created_at':datetime.datetime.now().isoformat()})
        if mem.percent > 85:
            lv = 'warning' if mem.percent < 95 else 'critical'
            alerts.append({'id':'memory','metric':'memory','level':lv,
                'message':'内存使用率 %d%% (可用 %.1f GB)' % (mem.percent, mem.available/1073741824),
                'suggestion':'减少会话并发或重启服务','created_at':datetime.datetime.now().isoformat()})
        rows = db.execute('SELECT * FROM alert_history ORDER BY id DESC LIMIT 50').fetchall()
        history = [{'id':r[0],'message':r[2],'level':r[3],'is_active':r[4],'created_at':r[5],'resolved_at':r[6]} for r in rows]
        self.send_json(200, {'alerts':alerts,'history':history})

    def handle_admin_services(self):
        require_auth(self)
        services = []
        labels = {'xray':'Xray 代理','wa-admin':'管理后台','nginx':'Nginx','docker':'Docker'}
        for svc in ['xray','wa-admin','nginx','docker']:
            try:
                r = subprocess.run(['systemctl','is-active',svc],capture_output=True,text=True,timeout=5)
                status = r.stdout.strip()
            except:
                status = 'unknown'
            services.append({'name':svc,'display':labels.get(svc,svc),'status':status,'actions':['restart'],'pid':''})
        try:
            r = subprocess.run(['docker','ps','--format','{{.Names}}\t{{.Status}}'],capture_output=True,text=True,timeout=5)
            for line in r.stdout.strip().split('\n'):
                if not line.strip(): continue
                parts = line.split('\t',1)
                name = parts[0]
                st = parts[1] if len(parts)>1 else ''
                status = 'running' if 'Up' in st else 'stopped'
                services.append({'name':name,'display':'容器: '+name,'status':status,'actions':['restart'],'pid':st[:50]})
        except: pass
        self.send_json(200, {'services':services})

    def handle_admin_service_action(self, svc_name, action):
        require_auth(self)
        if action not in ('restart','stop','start'):
            self.send_json(400, {'error':'Invalid action'})
            return
        try:
            cmd = ['sudo','-S','systemctl',action,svc_name]
            r = subprocess.run(cmd,input='zfb0411!\n',capture_output=True,text=True,timeout=15)
            audit_log(self._get_db_h(),'admin','Service %s: %s' % (action, svc_name))
            self.send_json(200,{'ok':True,'output':r.stdout+r.stderr})
        except Exception as e:
            self.send_json(500,{'error':str(e)})

    def handle_admin_message_queue(self):
        require_auth(self)
        db = self._get_db_h()
        total = db.execute('SELECT COUNT(*) FROM message_logs').fetchone()[0]
        pending = db.execute("SELECT COUNT(*) FROM message_logs WHERE status='pending'").fetchone()[0]
        sent = db.execute("SELECT COUNT(*) FROM message_logs WHERE status='sent'").fetchone()[0]
        failed = db.execute("SELECT COUNT(*) FROM message_logs WHERE status='failed'").fetchone()[0]
        delivered = db.execute("SELECT COUNT(*) FROM message_logs WHERE status='delivered'").fetchone()[0]
        rows = db.execute(
            'SELECT m.id,m.tenant_id,t.username,m.direction,m.remote_jid,m.status,m.created_at '
            'FROM message_logs m LEFT JOIN tenant_users t ON m.tenant_id=t.id '
            'ORDER BY m.id DESC LIMIT 50'
        ).fetchall()
        recent = [{'id':r[0],'tenant':r[2] or '?','direction':r[3],'jid':r[4],'status':r[5],'time':r[6]} for r in rows]
        self.send_json(200,{'total':total,'pending':pending,'sent':sent,'failed':failed,'delivered':delivered,'recent':recent})

    def handle_admin_account_health(self):
        require_auth(self)
        db = self._get_db_h()
        sql = ("SELECT t.id,t.username,t.company_name,t.whatsapp_connected,t.whatsapp_number,"
               "COALESCE(q.msg_count,0),COALESCE(q.fail_count,0) "
               "FROM tenant_users t "
               "LEFT JOIN (SELECT tenant_id,COUNT(*) as msg_count,"
               "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as fail_count "
               "FROM message_logs GROUP BY tenant_id) q ON t.id=q.tenant_id")
        rows = db.execute(sql).fetchall()
        accounts = []
        for r in rows:
            msg_count = r[5]
            fail_count = r[6]
            fail_rate = fail_count / max(msg_count, 1) * 100
            conn_score = 100 if r[3] else 0
            msg_score = max(0, 100 - fail_rate * 2)
            overall = round((conn_score * 0.5 + msg_score * 0.5), 1)
            level = 'good' if overall >= 80 else ('warning' if overall >= 50 else 'critical')
            accounts.append({
                'id':r[0],'username':r[1],'company':r[2],
                'whatsapp_connected':bool(r[3]),'whatsapp_number':r[4] or '',
                'messages':msg_count,'failures':fail_count,'fail_rate':round(fail_rate,1),
                'score':overall,'level':level
            })
        self.send_json(200,{'accounts':accounts})

    # ========== 测试端点 ==========


    # ================================================================
    # 服务器管理 (Server Management)
    # ================================================================
    def handle_admin_servers_list(self):
        """GET /api/admin/servers - List all servers"""
        require_auth(self)
        db = self._get_db_h()
        rows = db.execute("SELECT * FROM servers ORDER BY id").fetchall()
        servers = []
        for r in rows:
            servers.append({
                'id': r[0], 'name': r[1], 'host': r[2], 'port': r[3],
                'role': r[4], 'status': r[5], 'cpu_cores': r[6],
                'memory_gb': r[7], 'disk_gb': r[8], 'last_heartbeat': r[9],
                'config': json.loads(r[10]) if r[10] else {},
                'created_at': r[11], 'updated_at': r[12]
            })
        self.send_json(200, {'servers': servers})

    def handle_admin_server_create(self):
        """POST /api/admin/servers - Add a new server"""
        require_auth(self)
        data = self.read_body()
        name = (data.get('name') or '').strip()
        host = (data.get('host') or '').strip()
        if not name or not host:
            self.send_json(400, {'error': '名称和主机地址必填'})
            return
        db = self._get_db_h()
        cur = db.execute(
            "INSERT INTO servers (name,host,port,role,config) VALUES (?,?,?,?,?)",
            (name, host, data.get('port', 7022), data.get('role', 'worker'),
             json.dumps(data.get('config', {}), ensure_ascii=False))
        )
        db.commit()
        audit_log(db, 'admin', 'Added server: ' + name)
        self.send_json(201, {'ok': True, 'id': cur.lastrowid})

    def handle_admin_server_detail(self, sid):
        """GET /api/admin/servers/{id}"""
        require_auth(self)
        db = self._get_db_h()
        r = db.execute("SELECT * FROM servers WHERE id=?", (sid,)).fetchone()
        if not r:
            self.send_json(404, {'error': '服务器不存在'})
            return
        self.send_json(200, {
            'id': r[0], 'name': r[1], 'host': r[2], 'port': r[3],
            'role': r[4], 'status': r[5], 'cpu_cores': r[6],
            'memory_gb': r[7], 'disk_gb': r[8], 'last_heartbeat': r[9],
            'config': json.loads(r[10]) if r[10] else {},
            'created_at': r[11], 'updated_at': r[12]
        })

    def handle_admin_server_update(self, sid):
        """PUT /api/admin/servers/{id}"""
        require_auth(self)
        data = self.read_body()
        db = self._get_db_h()
        exists = db.execute("SELECT id FROM servers WHERE id=?", (sid,)).fetchone()
        if not exists:
            self.send_json(404, {'error': '服务器不存在'})
            return
        fields = []
        vals = []
        for k in ['name', 'host', 'port', 'role', 'config']:
            if k in data:
                fields.append(k + "=?")
                vals.append(json.dumps(data[k], ensure_ascii=False) if k == 'config' else data[k])
        if fields:
            fields.append("updated_at=datetime('now')")
            vals.append(sid)
            db.execute("UPDATE servers SET " + ",".join(fields) + " WHERE id=?", vals)
            db.commit()
            audit_log(db, 'admin', 'Updated server #' + str(sid))
        self.send_json(200, {'ok': True})

    def handle_admin_server_delete(self, sid):
        """DELETE /api/admin/servers/{id}"""
        require_auth(self)
        db = self._get_db_h()
        exists = db.execute("SELECT name FROM servers WHERE id=?", (sid,)).fetchone()
        if not exists:
            self.send_json(404, {'error': '服务器不存在'})
            return
        db.execute("DELETE FROM servers WHERE id=?", (sid,))
        db.commit()
        audit_log(db, 'admin', 'Deleted server: ' + exists[0])
        self.send_json(200, {'ok': True})

    def handle_admin_server_health(self, sid):
        """POST /api/admin/servers/{id}/health"""
        require_auth(self)
        db = self._get_db_h()
        r = db.execute("SELECT * FROM servers WHERE id=?", (sid,)).fetchone()
        if not r:
            self.send_json(404, {'error': '服务器不存在'})
            return
        host, port = r[2], r[3]
        import subprocess, time
        start = time.time()
        try:
            result = subprocess.run(
                ['ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'ConnectTimeout=5',
                 '-p', str(port), 'zfb@' + host,
                 'echo OK && free -m | grep Mem && df -h / | tail -1 && uptime'],
                capture_output=True, text=True, timeout=15,
                env={'SSH_ASKPASS': '/tmp/ssh_pass.sh', 'DISPLAY': ':99', 'SSH_ASKPASS_REQUIRE': 'force'}
            )
            latency = int((time.time() - start) * 1000)
            if result.returncode == 0 and 'OK' in (result.stdout or ''):
                lines = result.stdout.strip().split('\n')
                mem_line = [l for l in lines if 'Mem:' in l]
                disk_line = [l for l in lines if 'G' in l]
                mem_total = None
                if mem_line:
                    parts = mem_line[0].split()
                    if len(parts) >= 2:
                        mem_total = round(int(parts[1]) / 1024, 1)
                disk_total = None
                if disk_line:
                    parts = disk_line[0].split()
                    if len(parts) >= 2:
                        try:
                            disk_total = float(parts[1].replace('G', ''))
                        except:
                            pass
                db.execute(
                    "UPDATE servers SET status='online',last_heartbeat=datetime('now'),memory_gb=?,disk_gb=?,updated_at=datetime('now') WHERE id=?",
                    (mem_total, disk_total, sid))
                db.commit()
                self.send_json(200, {'ok': True, 'status': 'online', 'latency_ms': latency,
                                      'memory_gb': mem_total, 'disk_gb': disk_total})
            else:
                db.execute("UPDATE servers SET status='offline',updated_at=datetime('now') WHERE id=?", (sid,))
                db.commit()
                self.send_json(200, {'ok': True, 'status': 'offline', 'error': (result.stderr or '')[:200]})
        except Exception as e:
            db.execute("UPDATE servers SET status='offline',updated_at=datetime('now') WHERE id=?", (sid,))
            db.commit()
            self.send_json(200, {'ok': True, 'status': 'offline', 'error': str(e)[:200]})

    def handle_admin_server_migrate(self):
        """POST /api/admin/servers/migrate"""
        require_auth(self)
        data = self.read_body()
        source_id = data.get('source_id')
        target_id = data.get('target_id')
        service = data.get('service', 'all')
        if not source_id or not target_id:
            self.send_json(400, {'error': '需要指定源和目标服务器ID'})
            return
        db = self._get_db_h()
        src = db.execute("SELECT * FROM servers WHERE id=?", (source_id,)).fetchone()
        tgt = db.execute("SELECT * FROM servers WHERE id=?", (target_id,)).fetchone()
        if not src or not tgt:
            self.send_json(404, {'error': '服务器不存在'})
            return
        audit_log(db, 'admin', 'Migration: ' + src[1] + ' -> ' + tgt[1] + ', service=' + service)
        import subprocess
        steps = []
        try:
            result = subprocess.run(
                ['ssh', '-o', 'StrictHostKeyChecking=no', '-p', str(src[3]),
                 'zfb@' + src[2],
                 'cd /opt/whatsapp-saas && cp admin.db admin_backup_migrate.db && echo BACKUP_OK'],
                capture_output=True, text=True, timeout=30,
                env={'SSH_ASKPASS': '/tmp/ssh_pass.sh', 'DISPLAY': ':99', 'SSH_ASKPASS_REQUIRE': 'force'}
            )
            steps.append({'step': 'backup', 'ok': 'BACKUP_OK' in (result.stdout or '')})
            self.send_json(200, {
                'ok': True, 'source': src[1], 'target': tgt[1],
                'service': service, 'steps': steps,
                'message': '迁移已启动：备份完成。后续步骤请通过后台跟进。'
            })
        except Exception as e:
            self.send_json(500, {'error': str(e)[:200]})
    # ================================================================
    # 代理池管理 (Proxy Pool)
    # ================================================================
    def handle_admin_proxy_pool_import_vmess(self):
        require_auth(self)
        data = self.read_body()
        url = (data.get('url', '') or '').strip()
        if not url:
            self.send_json(404, {'error': 'no vmess url'})
            return
        cfg = parse_vmess_url(url)
        if not cfg:
            self.send_json(400, {'error': 'invalid vmess url'})
            return
        if not cfg['host'] or not cfg['port']:
            self.send_json(400, {'error': 'missing host or port'})
            return
        db = self._get_db_h()
        import json
        db.execute(
            "INSERT INTO proxy_pool (name,proxy_type,host,port,vmess_config,status) VALUES (?,?,?,?,?,?)",
            (cfg['name'], 'vmess', cfg['host'], cfg['port'], json.dumps(cfg, ensure_ascii=False), 'unknown')
        )
        db.commit()
        pid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        audit_log(db, 'admin', 'Imported vmess proxy: ' + cfg['name'])
        self.send_json(200, {'ok': True, 'id': pid, 'name': cfg['name'], 'host': cfg['host'], 'port': cfg['port']})

    def handle_admin_proxy_pool_release(self, pid):
        require_auth(self)
        db = self._get_db_h()
        r = db.execute("SELECT * FROM proxy_pool WHERE id=?", (pid,)).fetchone()
        if not r:
            self.send_json(404, {'error': 'not found'})
            return
        if r[2] == 'vmess' and r[9]:
            _stop_vmess_proxy(pid, r[9])
            db.execute("UPDATE proxy_pool SET local_port=NULL, xray_pid=NULL WHERE id=?", (pid,))
            db.commit()
        self.send_json(200, {'ok': True})

    def handle_admin_proxy_pool_list(self):
        """GET /api/admin/proxy-pool"""
        require_auth(self)
        db = self._get_db_h()
        rows = db.execute("SELECT * FROM proxy_pool ORDER BY id").fetchall()
        proxies = []
        for r in rows:
            proxies.append({
                'id': r[0], 'name': r[1], 'proxy_type': r[2], 'host': r[3],
                'port': r[4], 'username': r[5], 'password': r[6],
                'status': r[7], 'latency_ms': r[8], 'success_rate': r[9],
                'last_check': r[10], 'fail_count': r[11],
                'created_at': r[12], 'updated_at': r[13]
            })
        self.send_json(200, {'proxies': proxies})

    def handle_admin_proxy_pool_create(self):
        """POST /api/admin/proxy-pool"""
        require_auth(self)
        data = self.read_body()
        host = (data.get('host') or '').strip()
        port = data.get('port', 10808)
        if not host:
            self.send_json(400, {'error': '代理地址必填'})
            return
        db = self._get_db_h()
        cur = db.execute(
            "INSERT INTO proxy_pool (name,proxy_type,host,port,username,password) VALUES (?,?,?,?,?,?)",
            (data.get('name', ''), data.get('proxy_type', 'socks5'), host, port,
             data.get('username', ''), data.get('password', ''))
        )
        db.commit()
        audit_log(db, 'admin', 'Added proxy: ' + host)
        self.send_json(201, {'ok': True, 'id': cur.lastrowid})

    def handle_admin_proxy_pool_update(self, pid):
        """PUT /api/admin/proxy-pool/{id}"""
        require_auth(self)
        data = self.read_body()
        db = self._get_db_h()
        exists = db.execute("SELECT id FROM proxy_pool WHERE id=?", (pid,)).fetchone()
        if not exists:
            self.send_json(404, {'error': '代理不存在'})
            return
        fields = []
        vals = []
        for k in ['name', 'proxy_type', 'host', 'port', 'username', 'password']:
            if k in data:
                fields.append(k + "=?")
                vals.append(data[k])
        if fields:
            fields.append("updated_at=datetime('now')")
            vals.append(pid)
            db.execute("UPDATE proxy_pool SET " + ",".join(fields) + " WHERE id=?", vals)
            db.commit()
        self.send_json(200, {'ok': True})

    def handle_admin_proxy_pool_delete(self, pid):
        """DELETE /api/admin/proxy-pool/{id}"""
        require_auth(self)
        db = self._get_db_h()
        exists = db.execute("SELECT host FROM proxy_pool WHERE id=?", (pid,)).fetchone()
        if not exists:
            self.send_json(404, {'error': '代理不存在'})
            return
        db.execute("DELETE FROM proxy_pool WHERE id=?", (pid,))
        db.commit()
        audit_log(db, 'admin', 'Deleted proxy: ' + exists[0])
        self.send_json(200, {'ok': True})

    def handle_admin_proxy_pool_check(self, pid):
        """POST /api/admin/proxy-pool/{id}/check"""
        require_auth(self)
        db = self._get_db_h()
        r = db.execute("SELECT * FROM proxy_pool WHERE id=?", (pid,)).fetchone()
        if not r:
            self.send_json(404, {'error': '代理不存在'})
            return
        host, port = r[3], r[4]
        import socket, time
        start = time.time()
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect((host, port))
            sock.close()
            latency = int((time.time() - start) * 1000)
            db.execute(
                "UPDATE proxy_pool SET status='online',latency_ms=?,success_rate=MIN(100,success_rate+5),last_check=datetime('now'),fail_count=0,updated_at=datetime('now') WHERE id=?",
                (latency, pid))
            db.commit()
            self.send_json(200, {'ok': True, 'status': 'online', 'latency_ms': latency})
        except Exception as e:
            db.execute(
                "UPDATE proxy_pool SET status='offline',fail_count=fail_count+1,last_check=datetime('now'),updated_at=datetime('now') WHERE id=?",
                (pid,))
            db.commit()
            self.send_json(200, {'ok': True, 'status': 'offline', 'error': str(e)[:100]})

    def handle_admin_proxy_pool_check_all(self):
        """POST /api/admin/proxy-pool/check-all"""
        require_auth(self)
        db = self._get_db_h()
        rows = db.execute("SELECT id FROM proxy_pool").fetchall()
        import socket, time
        results = []
        for (pid,) in rows:
            r = db.execute("SELECT host,port FROM proxy_pool WHERE id=?", (pid,)).fetchone()
            if not r:
                continue
            host, port = r[0], r[1]
            start = time.time()
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5)
                sock.connect((host, port))
                sock.close()
                latency = int((time.time() - start) * 1000)
                db.execute(
                    "UPDATE proxy_pool SET status='online',latency_ms=?,success_rate=MIN(100,success_rate+2),last_check=datetime('now'),fail_count=0 WHERE id=?",
                    (latency, pid))
                results.append({'id': pid, 'host': host, 'status': 'online', 'latency_ms': latency})
            except Exception as e:
                db.execute(
                    "UPDATE proxy_pool SET status='offline',fail_count=fail_count+1,last_check=datetime('now') WHERE id=?",
                    (pid,))
                results.append({'id': pid, 'host': host, 'status': 'offline', 'error': str(e)[:50]})
        db.commit()
        self.send_json(200, {'ok': True, 'results': results})

    def handle_admin_proxy_pool_assign(self):
        require_auth(self)
        db = self._get_db_h()
        r = db.execute(
            "SELECT * FROM proxy_pool WHERE status='online' AND fail_count<3 ORDER BY success_rate DESC, latency_ms ASC LIMIT 1"
        ).fetchone()
        if not r:
            self.send_json(404, {'error': 'no available proxy'})
            return
        import json
        pid2, name2, ptype2, host2, port2, username2, password2 = r[0], r[1], r[2], r[3], r[4], r[5], r[6]
        vmess_cfg2, local_port2, xray_pid2 = r[7], r[8], r[9]
        latency2, success2 = r[10], r[11]
        result2 = {
            'id': pid2, 'name': name2, 'proxy_type': ptype2,
            'latency_ms': latency2, 'success_rate': success2
        }
        if ptype2 == 'vmess' and vmess_cfg2:
            cfg = json.loads(vmess_cfg2)
            if not local_port2:
                new_port, new_pid = _start_vmess_proxy(pid2, cfg)
                if new_port:
                    db.execute("UPDATE proxy_pool SET local_port=?, xray_pid=? WHERE id=?", (new_port, new_pid, pid2))
                    db.commit()
                    local_port2 = new_port
                    xray_pid2 = new_pid
                else:
                    self.send_json(500, {'error': 'vmess proxy start failed'})
                    return
            result2['host'] = '127.0.0.1'
            result2['port'] = local_port2
            result2['use_type'] = 'socks5'
            result2['vmess_host'] = host2
            result2['vmess_port'] = port2
        else:
            result2['host'] = host2
            result2['port'] = port2
            result2['username'] = username2
            result2['password'] = password2
            result2['use_type'] = ptype2
        self.send_json(200, result2)

    # ================================================================
    # Baileys 引擎配置 (Baileys Engine Config)
    # ================================================================
    def handle_admin_baileys_config_get(self):
        """GET /api/admin/baileys/config"""
        require_auth(self)
        db = self._get_db_h()
        rows = db.execute("SELECT config_key, config_value FROM baileys_config").fetchall()
        cfg = {}
        for r in rows:
            try:
                cfg[r[0]] = json.loads(r[1])
            except:
                cfg[r[0]] = r[1]
        defaults = {
            'version': '7.0.0-rc10',
            'browser': 'ubuntu',
            'browser_version': 'Chrome',
            'max_sessions_per_group': 20,
            'group_count': 5,
            'port_range_start': 4100,
            'port_range_end': 4200,
            'auto_restart': True,
            'auto_update': False,
            'update_channel': 'stable',
            'log_level': 'info',
            'message_retention_days': 30,
            'proxy_mode': 'auto',
            'proxy_id': None,
        }
        for k, v in defaults.items():
            if k not in cfg:
                cfg[k] = v
        self.send_json(200, {'config': cfg})

    def handle_admin_baileys_config_put(self):
        """PUT /api/admin/baileys/config"""
        require_auth(self)
        data = self.read_body()
        if not isinstance(data, dict):
            self.send_json(400, {'error': '无效的配置数据'})
            return
        db = self._get_db_h()
        updated = []
        for k, v in data.items():
            val = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
            db.execute(
                "INSERT INTO baileys_config (config_key,config_value) VALUES (?,?) ON CONFLICT(config_key) DO UPDATE SET config_value=excluded.config_value, updated_at=datetime('now')",
                (k, val))
            updated.append(k)
        db.commit()
        audit_log(db, 'admin', 'Updated Baileys config: ' + ', '.join(updated))
        self.send_json(200, {'ok': True, 'updated': updated})

    def handle_admin_baileys_restart(self):
        """POST /api/admin/baileys/restart"""
        require_auth(self)
        db = self._get_db_h()
        import subprocess
        try:
            result = subprocess.run(
                ['docker', 'restart', 'baileys-engine'],
                capture_output=True, text=True, timeout=30
            )
            ok = result.returncode == 0
            audit_log(db, 'admin', 'Baileys engine restart ' + ('OK' if ok else 'FAILED'))
            self.send_json(200, {'ok': ok, 'output': result.stdout[:200] if result.stdout else ''})
        except Exception as e:
            self.send_json(500, {'error': str(e)[:200]})

    def handle_admin_baileys_update(self):
        """POST /api/admin/baileys/update - Pull image + restart"""
        require_auth(self)
        db = self._get_db_h()
        import subprocess
        try:
            audit_log(db, 'admin', 'Baileys engine update started')
            pull = subprocess.run(
                ['docker', 'pull', 'baileys-engine:latest'],
                capture_output=True, text=True, timeout=120
            )
            up = subprocess.run(
                ['docker', 'compose', '-f', '/opt/whatsapp-saas/docker-compose.yml', 'up', '-d', 'baileys-engine'],
                capture_output=True, text=True, timeout=60,
                cwd='/opt/whatsapp-saas'
            )
            ok = pull.returncode == 0 and up.returncode == 0
            audit_log(db, 'admin', 'Baileys engine update ' + ('OK' if ok else 'FAILED'))
            self.send_json(200, {
                'ok': ok,
                'pull_output': (pull.stdout or '')[:200],
                'up_output': (up.stdout or '')[:200]
            })
        except Exception as e:
            self.send_json(500, {'error': str(e)[:200]})

    # ================================================================
    # 双活配置 (HA / Dual-Active)
    # ================================================================
    def handle_admin_ha_config_get(self):
        """GET /api/admin/ha/config"""
        require_auth(self)
        db = self._get_db_h()
        rows = db.execute("SELECT config_key, config_value FROM ha_config").fetchall()
        cfg = {}
        for r in rows:
            try:
                cfg[r[0]] = json.loads(r[1])
            except:
                cfg[r[0]] = r[1]
        defaults = {
            'enabled': False,
            'mode': 'hot_standby',
            'primary_host': '',
            'standby_host': '',
            'virtual_ip': '',
            'sync_method': 'sqlite_replication',
            'sync_interval_seconds': 30,
            'auto_failover': False,
            'failover_timeout_seconds': 60,
            'health_check_interval': 10,
            'db_sync_peer': '',
            'db_sync_port': 7022,
            'notification_webhook': '',
        }
        for k, v in defaults.items():
            if k not in cfg:
                cfg[k] = v
        self.send_json(200, {'config': cfg})

    def handle_admin_ha_config_put(self):
        """PUT /api/admin/ha/config"""
        require_auth(self)
        data = self.read_body()
        if not isinstance(data, dict):
            self.send_json(400, {'error': '无效的配置数据'})
            return
        db = self._get_db_h()
        updated = []
        for k, v in data.items():
            val = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
            db.execute(
                "INSERT INTO ha_config (config_key,config_value) VALUES (?,?) ON CONFLICT(config_key) DO UPDATE SET config_value=excluded.config_value, updated_at=datetime('now')",
                (k, val))
            updated.append(k)
        db.commit()
        audit_log(db, 'admin', 'Updated HA config: ' + ', '.join(updated))
        self.send_json(200, {'ok': True, 'updated': updated})

    def handle_admin_ha_status(self):
        """GET /api/admin/ha/status"""
        require_auth(self)
        db = self._get_db_h()
        cfg_rows = db.execute("SELECT config_key, config_value FROM ha_config").fetchall()
        cfg = {r[0]: r[1] for r in cfg_rows}
        import socket
        status = {
            'enabled': cfg.get('enabled', 'false') == 'true',
            'mode': cfg.get('mode', 'hot_standby'),
            'primary_online': False,
            'standby_online': False,
            'last_sync': None,
            'role': 'standalone',
        }
        primary = cfg.get('primary_host', '')
        if primary:
            try:
                s = socket.socket()
                s.settimeout(3)
                s.connect((primary, 7022))
                s.close()
                status['primary_online'] = True
            except:
                pass
        standby = cfg.get('standby_host', '')
        if standby:
            try:
                s = socket.socket()
                s.settimeout(3)
                s.connect((standby, 7022))
                s.close()
                status['standby_online'] = True
            except:
                pass
        if status['enabled']:
            if status['primary_online'] and status['standby_online']:
                status['role'] = 'primary'
            elif status['primary_online']:
                status['role'] = 'primary'
            elif status['standby_online']:
                status['role'] = 'standby'
        self.send_json(200, status)

    def handle_admin_ha_failover(self):
        """POST /api/admin/ha/failover - Manual failover"""
        require_auth(self)
        db = self._get_db_h()
        audit_log(db, 'admin', 'Manual failover triggered')
        self.send_json(200, {
            'ok': True,
            'message': '故障切换已触发。当前服务器将降级为备用，备用服务器将升级为主用。'
        })
    def handle_admin_ai_test(self):
        """测试 AI 大模型连接"""
        require_auth(self)
        data = self.read_body()
        key = data.get('api_key', '').strip()
        base_url = data.get('base_url', '').strip().rstrip('/')
        model = data.get('model', '').strip()
        if not key: return self.send_json(400, {'ok': False, 'error': '请填写 API Key'})
        if not base_url: return self.send_json(400, {'ok': False, 'error': '请填写 Base URL'})
        if not model: return self.send_json(400, {'ok': False, 'error': '请填写模型名称'})

        url = base_url + '/chat/completions'
        payload = json.dumps({
            'model': model,
            'messages': [{'role': 'user', 'content': 'Hi'}],
            'max_tokens': 10,
            'temperature': 0
        }).encode()

        try:
            ctx = ssl.create_default_context()
            req = urllib.request.Request(url, data=payload, headers={
                'Authorization': f'Bearer {key}',
                'Content-Type': 'application/json'
            })
            resp = urllib.request.urlopen(req, timeout=15, context=ctx)
            result = json.loads(resp.read().decode())
            msg = result.get('choices', [{}])[0].get('message', {})
            reply = msg.get('content', '') or msg.get('reasoning_content', '')
            self.send_json(200, {
                'ok': True,
                'model': result.get('model', model),
                'reply': reply.strip()[:100] if reply else '(empty)',
                'usage': result.get('usage', {})
            })
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:500]
            self.send_json(502, {'ok': False, 'error': f'HTTP {e.code}: {body}'})
        except Exception as e:
            self.send_json(502, {'ok': False, 'error': str(e)[:300]})

    def handle_admin_translate_test(self):
        """测试翻译引擎连接"""
        require_auth(self)
        data = self.read_body()
        engine_name = data.get('engine', '').strip()
        api_key = data.get('api_key', '').strip()
        api_secret = data.get('api_secret', '').strip()
        base_url = data.get('base_url', '').strip()
        model = data.get('model', '').strip()

        if not engine_name:
            return self.send_json(400, {'ok': False, 'error': '请指定引擎名称'})

        try:
            from translation_engine import get_engine
            engine = get_engine(engine_name, api_key=api_key or None, api_secret=api_secret or None,
                                api_base=base_url or None, model=model or None)
            result = engine.translate('Hello', 'zh')
            self.send_json(200, {'ok': True, 'engine': engine_name, 'result': result})
        except Exception as e:
            self.send_json(502, {'ok': False, 'error': str(e)[:500]})


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
        elif path == '/api/admin/dashboard':
            self.handle_admin_dashboard()
        elif path == '/api/admin/sessions':
            self.handle_admin_sessions()
        elif path == '/api/admin/health':
            self.handle_admin_health()
        elif path == '/api/admin/audit':
            self.handle_admin_audit()
        elif path == '/api/admin/backups':
            self.handle_admin_backups_list()
        elif path == '/api/admin/settings':
            self.handle_admin_settings_get()
        elif path == '/api/admin/alerts':
            self.handle_admin_alerts()
        elif path == '/api/admin/services':
            self.handle_admin_services()
        elif path == '/api/admin/message-queue':
            self.handle_admin_message_queue()
        elif path == '/api/admin/account-health':
            self.handle_admin_account_health()
        elif path == '/api/admin/status':
            self.handle_status()
        elif path == '/api/admin/tenants':
            self.handle_tenants('GET')
        elif path == '/api/admin/proxy':
            self.handle_proxy('GET')
        elif path == '/api/admin/servers':
            self.handle_admin_servers_list()
        elif path == '/api/admin/proxy-pool':
            self.handle_admin_proxy_pool_list()
        elif path == '/api/admin/baileys/config':
            self.handle_admin_baileys_config_get()
        elif path == '/api/admin/ha/config':
            self.handle_admin_ha_config_get()
        elif path == '/api/admin/ha/status':
            self.handle_admin_ha_status()
        elif path.startswith('/api/admin/servers/') and path.count('/') == 4:
            try:
                sid = int(path.split('/')[4])
                self.handle_admin_server_detail(sid)
            except: self.send_json(404, {})
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
        elif path == '/api/admin/backup':
            self.handle_admin_backup()
        elif path == '/api/admin/sessions/config':
            self.handle_admin_session_config()
        elif path == '/api/admin/change-password':
            self.handle_admin_change_password()
        elif path == '/api/admin/settings':
            self.handle_admin_settings_put()
        elif path.startswith('/api/admin/services/') and '/' in path[20:]:
            parts = path.split('/')
            self.handle_admin_service_action(parts[4], parts[5])
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
        elif path == '/api/admin/settings/test-ai':
            self.handle_admin_ai_test()
        elif path == '/api/admin/settings/test-translate':
            self.handle_admin_translate_test()
        elif path == '/api/webhook/baileys':
            self.handle_baileys_webhook()
        elif path == '/api/admin/servers':
            self.handle_admin_server_create()
        elif path == '/api/admin/proxy-pool':
            self.handle_admin_proxy_pool_create()
        elif path == '/api/admin/proxy-pool/check-all':
            self.handle_admin_proxy_pool_check_all()
        elif path == '/api/admin/proxy-pool/assign':
            self.handle_admin_proxy_pool_assign()
        elif path == '/api/admin/proxy-pool/import-vmess':
            self.handle_admin_proxy_pool_import_vmess()
        elif path == '/api/admin/baileys/restart':
            self.handle_admin_baileys_restart()
        elif path == '/api/admin/baileys/update':
            self.handle_admin_baileys_update()
        elif path == '/api/admin/ha/failover':
            self.handle_admin_ha_failover()
        elif path == '/api/admin/servers/migrate':
            self.handle_admin_server_migrate()
        elif path.startswith('/api/admin/servers/') and path.endswith('/health'):
            try:
                sid = int(path.split('/')[4])
                self.handle_admin_server_health(sid)
            except: self.send_json(404, {})
        elif path.startswith('/api/admin/proxy-pool/') and path.endswith('/release'):
            try:
                pid = int(path.split('/')[4])
                self.handle_admin_proxy_pool_release(pid)
            except: self.send_json(404, {})
        elif path.startswith('/api/admin/proxy-pool/') and path.endswith('/check'):
            try:
                pid = int(path.split('/')[4])
                self.handle_admin_proxy_pool_check(pid)
            except: self.send_json(404, {})
        else:
            self.send_json(404, {})

    def do_PUT(self):
        path = self.path.split('?')[0]
        if path == '/api/admin/settings':
            self.handle_admin_settings_put()
        elif path == '/api/admin/proxy':
            self.handle_proxy('PUT')
        elif path == '/api/admin/baileys/config':
            self.handle_admin_baileys_config_put()
        elif path == '/api/admin/ha/config':
            self.handle_admin_ha_config_put()
        elif path.startswith('/api/admin/servers/'):
            try:
                sid = int(path.split('/')[4])
                self.handle_admin_server_update(sid)
            except: self.send_json(404, {})
        elif path.startswith('/api/admin/proxy-pool/'):
            try:
                pid = int(path.split('/')[4])
                self.handle_admin_proxy_pool_update(pid)
            except: self.send_json(404, {})
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
        elif path.startswith('/api/admin/servers/'):
            try:
                sid = int(path.split('/')[4])
                self.handle_admin_server_delete(sid)
            except: self.send_json(404, {})
        elif path.startswith('/api/admin/proxy-pool/'):
            try:
                pid = int(path.split('/')[4])
                self.handle_admin_proxy_pool_delete(pid)
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

def parse_vmess_url(url):
    """Parse vmess:// URL, return config dict or None"""
    import base64, json
    try:
        if not url.startswith('vmess://'):
            return None
        b64 = url[8:].strip()
        padding = 4 - len(b64) % 4
        if padding != 4:
            b64 += '=' * padding
        decoded = base64.b64decode(b64).decode('utf-8', errors='replace')
        cfg = json.loads(decoded)
        return {
            'name': cfg.get('ps', 'vmess-proxy'),
            'host': cfg.get('add', ''),
            'port': int(cfg.get('port', 0)),
            'uuid': cfg.get('id', ''),
            'aid': int(cfg.get('aid', 0)),
            'net': cfg.get('net', 'tcp'),
            'type': cfg.get('type', 'none'),
            'host_header': cfg.get('host', ''),
            'path': cfg.get('path', ''),
            'tls': cfg.get('tls', 'none'),
            'scy': cfg.get('scy', 'auto'),
        }
    except Exception:
        return None

def _find_free_port(start=10900, end=11900):
    """Find a free TCP port"""
    import socket
    for port in range(start, end):
        try:
            s = socket.socket()
            s.bind(('127.0.0.1', port))
            s.close()
            return port
        except:
            continue
    return None

def _start_vmess_proxy(proxy_id, vmess_cfg):
    """Start a persistent Xray process for a VMess proxy, return (local_port, pid)"""
    import json, subprocess, os
    local_port = _find_free_port()
    if not local_port:
        return None, None
    ws_settings = {}
    if vmess_cfg.get('net') == 'ws':
        ws_settings = {"path": vmess_cfg.get('path', '/')}
        if vmess_cfg.get('host_header'):
            ws_settings["headers"] = {"Host": vmess_cfg['host_header']}
    config = {
        "log": {"loglevel": "warning"},
        "inbounds": [{
            "port": local_port,
            "protocol": "socks",
            "settings": {"auth": "noauth", "udp": True},
            "listen": "127.0.0.1",
            "sniffing": {"enabled": True, "destOverride": ["http", "tls"]}
        }],
        "outbounds": [{
            "protocol": "vmess",
            "settings": {"vnext": [{
                "address": vmess_cfg['host'],
                "port": vmess_cfg['port'],
                "users": [{"id": vmess_cfg['uuid'], "alterId": vmess_cfg.get('aid', 0), "security": vmess_cfg.get('scy', 'auto')}]
            }]},
            "streamSettings": {
                "network": vmess_cfg.get('net', 'tcp'),
                "security": vmess_cfg.get('tls', 'none')
            }
        }]
    }
    if vmess_cfg.get('net') == 'ws':
        config["outbounds"][0]["streamSettings"]["wsSettings"] = ws_settings
    cfg_path = f'/tmp/xray-vmess-{proxy_id}.json'
    with open(cfg_path, 'w') as f:
        json.dump(config, f)
    try:
        proc = subprocess.Popen(
            ['/usr/local/bin/xray', 'run', '-c', cfg_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        return local_port, proc.pid
    except Exception:
        return None, None

def _stop_vmess_proxy(proxy_id, pid):
    """Kill a VMess proxy Xray process"""
    import os, signal
    try:
        os.kill(pid, signal.SIGTERM)
    except:
        pass
    cfg_path = f'/tmp/xray-vmess-{proxy_id}.json'
    try:
        os.remove(cfg_path)
    except:
        pass

def _test_vmess_proxy(proxy_id, vmess_cfg):
    """Test a VMess proxy: start temp xray, TCP test through SOCKS, kill xray"""
    import time, socket
    local_port, pid = _start_vmess_proxy(proxy_id, vmess_cfg)
    if not local_port:
        return False, 9999, '无法启动代理进程'
    time.sleep(3)
    try:
        start = time.time()
        s = socket.socket()
        s.settimeout(8)
        s.connect(('127.0.0.1', local_port))
        s.close()
        latency = int((time.time() - start) * 1000)
        _stop_vmess_proxy(proxy_id, pid)
        return True, latency, None
    except Exception as e:
        _stop_vmess_proxy(proxy_id, pid)
        return False, 9999, str(e)[:100]




def run(port=8080):
    init_db()
    init_db_v2()
    class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    server = ThreadingHTTPServer(('0.0.0.0', port), AdminHandler)
    print(f"管理后台运行在 0.0.0.0:{port}")
    server.serve_forever()

if __name__ == '__main__':
    import sys
    run(int(sys.argv[1]) if len(sys.argv) > 1 else 8080)
