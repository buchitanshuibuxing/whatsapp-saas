#!/usr/bin/env python3
"""failover_daemon.py — 备用服务器上运行，健康检查主服务器，自动故障切换
Run on standby server: health-checks primary → triggers failover when primary is down
Startup: systemd or `nohup python3 failover_daemon.py &`
"""
import os, sys, time, subprocess, json, socket, urllib.request, ssl

PROJECT_ROOT = "/opt/whatsapp-saas"
HEARTBEAT_FILE = "/tmp/failover_daemon_heartbeat"
STATE_FILE = "/tmp/failover_state.json"

def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except:
            pass
    return {"role": "standby", "failover_count": 0, "last_failover": None}

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f)

def load_ha_config():
    import sqlite3
    try:
        conn = sqlite3.connect(os.path.join(PROJECT_ROOT, "admin.db"))
        rows = conn.execute("SELECT config_key, config_value FROM ha_config").fetchall()
        conn.close()
        return {r[0]: r[1] for r in rows}
    except:
        return {}

def check_primary_health(primary_host, port=7080):
    url = f"http://{primary_host}:{port}/api/admin/health"
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(url)
        resp = urllib.request.urlopen(req, timeout=5)
        return resp.status == 200
    except:
        pass
    try:
        s = socket.socket()
        s.settimeout(3)
        s.connect((primary_host, 7022))
        s.close()
        return True
    except:
        return False

def execute_failover(cfg):
    log("=== FAILOVER TRIGGERED ===")
    creds_root = os.path.join(PROJECT_ROOT, "data", "baileys")
    creds_exist = False
    if os.path.exists(creds_root):
        for root, dirs, files in os.walk(creds_root):
            for f in files:
                if f == "creds.json" and os.path.getsize(os.path.join(root, f)) > 100:
                    creds_exist = True
                    break
    if not creds_exist:
        log("FAIL: No synced creds found")
        return False

    log("Starting Baileys engine on standby...")
    try:
        subprocess.run(["docker", "start", "baileys-engine"], capture_output=True, timeout=30)
        time.sleep(3)
    except Exception as e:
        log(f"Baileys start warning: {e}")

    try:
        subprocess.run(["systemctl", "restart", "wa-admin"], timeout=30)
    except:
        pass

    state = load_state()
    state["role"] = "primary"
    state["failover_count"] += 1
    state["last_failover"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_state(state)
    log(f"=== FAILOVER COMPLETE (count: {state['failover_count']}) ===")
    return True

def check_and_promote():
    cfg = load_ha_config()
    enabled = cfg.get('enabled', 'false') in ('true', '1')
    if not enabled:
        return
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Status}}", "baileys-engine"],
            capture_output=True, text=True, timeout=5
        )
        baileys_running = "running" in (result.stdout or "")
    except:
        baileys_running = False

    if baileys_running:
        return

    creds_count = 0
    creds_root = os.path.join(PROJECT_ROOT, "data", "baileys")
    if os.path.exists(creds_root):
        for root, dirs, files in os.walk(creds_root):
            creds_count += files.count("creds.json")

    if creds_count > 0:
        log(f"Baileys not running but {creds_count} creds synced — starting...")
        try:
            subprocess.run(["docker", "start", "baileys-engine"], timeout=30)
        except:
            pass

def main():
    log("Failover daemon starting on STANDBY server...")
    state = load_state()
    log(f"Initial state: role={state['role']}, failover_count={state['failover_count']}")
    check_and_promote()

    consecutive_failures = 0
    failover_timeout = 60

    while True:
        try:
            cfg = load_ha_config()
            enabled = cfg.get('enabled', 'false') in ('true', '1')
            if not enabled:
                time.sleep(10)
                continue

            primary_host = cfg.get('primary_host', '')
            failover_timeout = int(cfg.get('failover_timeout_seconds', 60))
            health_interval = int(cfg.get('health_check_interval', 10))
            auto_failover = cfg.get('auto_failover', 'false') in ('true', '1')

            if not primary_host:
                time.sleep(health_interval)
                continue

            healthy = check_primary_health(primary_host)

            if healthy:
                if consecutive_failures > 0:
                    log(f"Primary back online after {consecutive_failures} failures")
                consecutive_failures = 0
                state = load_state()
                if state.get('role') == 'primary':
                    log("Primary recovered, demoting back to standby")
                    try:
                        subprocess.run(["docker", "stop", "baileys-engine"], timeout=30)
                    except:
                        pass
                    state['role'] = 'standby'
                    save_state(state)
            else:
                consecutive_failures += 1
                elapsed = consecutive_failures * health_interval
                log(f"Primary DOWN ({consecutive_failures}/{failover_timeout // health_interval}), elapsed={elapsed}s")

                if auto_failover and elapsed >= failover_timeout:
                    log("Failover threshold reached!")
                    success = execute_failover(cfg)
                    if success:
                        consecutive_failures = 0

            check_and_promote()

            with open(HEARTBEAT_FILE, 'w') as f:
                f.write(str(time.time()))

            time.sleep(health_interval)
        except Exception as e:
            log(f"Error: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()
