#!/usr/bin/env python3
"""session_sync.py — 主服务器上运行，实时同步 Baileys creds 到备用服务器
Run on primary server: watches creds changes → rsync to standby
Startup: systemd or `nohup python3 session_sync.py &`
"""
import os, sys, time, subprocess, json, hashlib

SYNC_ROOT = "/opt/whatsapp-saas"
WATCH_DIRS = [
    "data/baileys",
    "baileys-engine/auth",
]
DB_FILE = "admin.db"
SYNC_INTERVAL = 5
DEBOUNCE_SECONDS = 2
HEARTBEAT_FILE = "/tmp/session_sync_heartbeat"

def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

def load_ha_config():
    import sqlite3
    try:
        conn = sqlite3.connect(os.path.join(SYNC_ROOT, DB_FILE))
        rows = conn.execute("SELECT config_key, config_value FROM ha_config").fetchall()
        conn.close()
        return {r[0]: r[1] for r in rows}
    except:
        return {}

def get_standby_info(cfg):
    host = cfg.get('standby_host', '')
    port = cfg.get('standby_port', '22')
    user = cfg.get('standby_user', 'root')
    pwd = cfg.get('standby_password', '')
    if not host or not pwd:
        return None
    return {'host': host, 'port': port, 'user': user, 'password': pwd}

def rsync_dir(standby, src_rel):
    src = os.path.join(SYNC_ROOT, src_rel)
    dst_host = f"{standby['user']}@{standby['host']}"
    dst_path = f"{dst_host}:{SYNC_ROOT}/{src_rel}"
    cmd = [
        "sshpass", "-p", standby['password'],
        "rsync", "-avz", "--delete",
        "-e", f"ssh -o StrictHostKeyChecking=no -p {standby['port']}",
        src + "/", dst_path + "/"
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return result.returncode == 0
    except:
        return False

def rsync_db(standby):
    src = os.path.join(SYNC_ROOT, DB_FILE)
    dst_host = f"{standby['user']}@{standby['host']}"
    dst_tmp = f"{dst_host}:/tmp/admin.db.syncing"
    cmd1 = [
        "sshpass", "-p", standby['password'],
        "rsync", "-az",
        "-e", f"ssh -o StrictHostKeyChecking=no -p {standby['port']}",
        src, dst_tmp
    ]
    cmd2 = [
        "sshpass", "-p", standby['password'],
        "ssh", "-o", "StrictHostKeyChecking=no", "-p", str(standby['port']),
        f"{standby['user']}@{standby['host']}",
        f"cp /tmp/admin.db.syncing {SYNC_ROOT}/{DB_FILE} && echo OK"
    ]
    try:
        r1 = subprocess.run(cmd1, capture_output=True, text=True, timeout=30)
        if r1.returncode != 0:
            return False
        r2 = subprocess.run(cmd2, capture_output=True, text=True, timeout=15)
        return r2.returncode == 0
    except:
        return False

def get_file_hashes(watch_dirs):
    hashes = {}
    for wd in watch_dirs:
        full = os.path.join(SYNC_ROOT, wd)
        if not os.path.exists(full):
            continue
        for root, dirs, files in os.walk(full):
            for f in files:
                if f.endswith(('.bak', '.pyc', '.tmp')):
                    continue
                fpath = os.path.join(root, f)
                try:
                    with open(fpath, 'rb') as fh:
                        hashes[fpath] = hashlib.md5(fh.read()).hexdigest()
                except:
                    pass
    return hashes

def main():
    log("Session sync daemon starting...")
    while not os.path.exists(os.path.join(SYNC_ROOT, DB_FILE)):
        log("Waiting for admin.db...")
        time.sleep(3)
    
    last_hashes = {}
    last_full_sync = 0
    last_db_sync = 0
    
    while True:
        try:
            cfg = load_ha_config()
            enabled = cfg.get('enabled', 'false')
            if enabled not in ('true', '1', True):
                time.sleep(10)
                with open(HEARTBEAT_FILE, 'w') as f:
                    f.write(str(time.time()))
                continue
            
            standby = get_standby_info(cfg)
            if not standby:
                log("No standby configured, sleeping")
                time.sleep(30)
                continue
            
            current_hashes = get_file_hashes(WATCH_DIRS)
            if current_hashes != last_hashes and last_hashes:
                changed = []
                for k in set(current_hashes) | set(last_hashes):
                    if current_hashes.get(k) != last_hashes.get(k):
                        changed.append(k)
                log(f"Detected {len(changed)} file changes, syncing...")
                for wd in WATCH_DIRS:
                    ok = rsync_dir(standby, wd)
                    log(f"  Sync {wd}: {'OK' if ok else 'FAILED'}")
            
            last_hashes = current_hashes
            now = time.time()
            if now - last_full_sync > SYNC_INTERVAL:
                log("Running periodic full sync...")
                for wd in WATCH_DIRS:
                    ok = rsync_dir(standby, wd)
                last_full_sync = now
            
            if now - last_db_sync > 30:
                rsync_db(standby)
                last_db_sync = now
            
            with open(HEARTBEAT_FILE, 'w') as f:
                f.write(str(time.time()))
            
            time.sleep(SYNC_INTERVAL)
        except Exception as e:
            log(f"Error: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()
