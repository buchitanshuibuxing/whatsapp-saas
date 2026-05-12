// ============================================================
// Baileys Engine v1.1 — Multi-tenant WhatsApp Service
// ============================================================
// Baileys 7.x ESM | HTTP Proxy | Multi-Session | Webhook
// ============================================================

import { createRequire } from 'module';
const require = createRequire(import.meta.url);

// ─── Proxy Agent ─────────────────────────────────────────────
// Baileys 7.x natively supports `agent` in config — no monkey-patch needed
import { HttpsProxyAgent } from 'https-proxy-agent';

const PROXY_URL = process.env.HTTP_PROXY || process.env.SOCKS5_PROXY || 'http://127.0.0.1:10809';
const proxyAgent = new HttpsProxyAgent(PROXY_URL);

console.log('[BaileysEngine] HTTP proxy configured:', PROXY_URL);

// ─── Dependencies ────────────────────────────────────────────
import express from 'express';
import path from 'path';
import fs from 'fs';
import crypto from 'crypto';
import QRCode from 'qrcode';
import pino from 'pino';

import makeWASocket, {
    useMultiFileAuthState,
    DisconnectReason,
    fetchLatestBaileysVersion,
    Browsers,
    delay,
} from '@whiskeysockets/baileys';

// ─── Constants ───────────────────────────────────────────────
const PORT = parseInt(process.env.PORT || '3500', 10);
const AUTH_DIR = process.env.AUTH_DIR || '/app/data/auth';
const API_KEY = process.env.API_KEY || 'baileys-engine-internal-key-2026';
const WEBHOOK_RETRIES = 3;
const WEBHOOK_TIMEOUT = 5000;

// ─── Logging ─────────────────────────────────────────────────
const logger = pino({
    level: process.env.LOG_LEVEL || 'info',
    transport: process.env.NODE_ENV !== 'production' ? { target: 'pino-pretty' } : undefined,
});

// ─── Session Store ───────────────────────────────────────────
const sessions = new Map();
const META_FILE = path.join(AUTH_DIR, 'sessions.json');

function ensureDataDirs() {
    fs.mkdirSync(AUTH_DIR, { recursive: true });
}

function loadMeta() {
    try {
        if (fs.existsSync(META_FILE)) {
            return JSON.parse(fs.readFileSync(META_FILE, 'utf8'));
        }
    } catch (e) {
        logger.warn('Failed to load sessions.json, starting fresh');
    }
    return {};
}

function saveMeta(meta) {
    fs.writeFileSync(META_FILE, JSON.stringify(meta, null, 2), 'utf8');
}

function genId() {
    return crypto.randomBytes(8).toString('hex');
}

// ─── Webhook Dispatch ────────────────────────────────────────
async function dispatchWebhook(sessionName, event) {
    const meta = loadMeta();
    const cfg = meta[sessionName]?.config || {};
    const url = cfg.webhookUrl;
    if (!url) return;

    const payload = {
        session: sessionName,
        timestamp: new Date().toISOString(),
        ...event,
    };

    for (let i = 0; i < WEBHOOK_RETRIES; i++) {
        try {
            const controller = new AbortController();
            const timeout = setTimeout(() => controller.abort(), WEBHOOK_TIMEOUT);
            const resp = await fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
                signal: controller.signal,
            });
            clearTimeout(timeout);
            if (resp.ok) {
                logger.debug({ session: sessionName, url, status: resp.status }, 'Webhook dispatched');
                return;
            }
            logger.warn({ session: sessionName, url, status: resp.status }, 'Webhook failed');
        } catch (err) {
            logger.warn({ session: sessionName, url, error: err.message }, 'Webhook error, retrying...');
        }
        await delay(500 * (i + 1));
    }
    logger.error({ session: sessionName, url }, 'Webhook exhausted retries');
}

// ─── Baileys Session Lifecycle ───────────────────────────────
async function createSession(sessionName, config = {}) {
    if (sessions.has(sessionName)) {
        const existing = sessions.get(sessionName);
        if (existing.status === 'WORKING' || existing.status === 'STARTING' || existing.status === 'SCAN_QR_CODE') {
            return { error: 'Session already active', status: existing.status };
        }
        await destroySession(sessionName);
    }

    const meta = loadMeta();
    const sid = meta[sessionName]?.id || genId();
    meta[sessionName] = { id: sid, name: sessionName, createdAt: new Date().toISOString(), config };
    saveMeta(meta);

    const authDir = path.join(AUTH_DIR, sessionName);
    fs.mkdirSync(authDir, { recursive: true });

    const { state, saveCreds } = await useMultiFileAuthState(authDir);

    // Baileys 7.x: fetchLatestBaileysVersion still works
    const { version, isLatest } = await fetchLatestBaileysVersion().catch(() => ({
        version: [2, 3000, 1035194821],
        isLatest: true,
    }));

    const sessionState = {
        sock: null,
        status: 'STARTING',
        qr: null,
        qrTimestamp: 0,
        createdAt: new Date().toISOString(),
        config,
        startTime: Date.now(),
        authDir,
        saveCreds,
        whatsappNumber: null,
        whatsappName: null,
    };
    sessions.set(sessionName, sessionState);

    const sock = makeWASocket({
        version,
        auth: {
            creds: state.creds,
            keys: state.keys,
        },
        browser: Browsers.macOS('Safari'),
        markOnlineOnConnect: true,
        generateHighQualityLinkPreview: true,
        connectTimeoutMs: 60_000,
        keepAliveIntervalMs: 30_000,
        logger: logger.child({ session: sessionName, level: 'warn' }),
        defaultQueryTimeoutMs: 60_000,
        agent: proxyAgent,
        fetchAgent: proxyAgent,
        qrTimeout: 25_000,
    });

    sessionState.sock = sock;

    // ─── Event: Credentials Update ───────────────────────────
    sock.ev.on('creds.update', saveCreds);

    // ─── Event: Connection Update ────────────────────────────
    sock.ev.on('connection.update', async (update) => {
        const { connection, lastDisconnect, qr, isNewLogin } = update;

        if (qr) {
            sessionState.qr = qr;
            sessionState.qrTimestamp = Date.now();
            sessionState.status = 'SCAN_QR_CODE';
            logger.info({ session: sessionName }, 'QR code ready for scanning');
        }

        if (connection === 'open') {
            sessionState.status = 'WORKING';
            sessionState.qr = null;
            const me = sock.user?.id;
            sessionState.whatsappNumber = me ? me.split(':')[0] : null;
            sessionState.whatsappName = sock.user?.name || null;
            logger.info({ session: sessionName, phone: sessionState.whatsappNumber }, 'WhatsApp connected');

            const m = loadMeta();
            if (m[sessionName]) {
                m[sessionName].whatsappNumber = sessionState.whatsappNumber;
                m[sessionName].whatsappName = sessionState.whatsappName;
                saveMeta(m);
            }

            await dispatchWebhook(sessionName, {
                event: 'connection.open',
                whatsappNumber: sessionState.whatsappNumber,
                whatsappName: sessionState.whatsappName,
            });
        }

        if (connection === 'close') {
            const statusCode = lastDisconnect?.error?.output?.statusCode;
            const reason = lastDisconnect?.error?.message || 'unknown';

            logger.warn({ session: sessionName, statusCode, reason }, 'WhatsApp disconnected');

            sessionState.status = 'DISCONNECTED';
            sessionState.qr = null;

            await dispatchWebhook(sessionName, {
                event: 'connection.close',
                statusCode,
                reason,
            });

            const shouldReconnect =
                statusCode !== DisconnectReason.loggedOut &&
                statusCode !== DisconnectReason.forbidden &&
                statusCode !== DisconnectReason.restartRequired;

            if (shouldReconnect) {
                sessionState.status = 'RECONNECTING';
                logger.info({ session: sessionName }, 'Attempting reconnection in 3s...');
                await delay(3000);
                try {
                    await createSession(sessionName, config);
                } catch (err) {
                    logger.error({ session: sessionName, error: err.message }, 'Reconnection failed');
                    sessionState.status = 'FAILED';
                }
            } else if (statusCode === DisconnectReason.loggedOut) {
                sessionState.status = 'LOGGED_OUT';
                logger.info({ session: sessionName }, 'Session logged out');
            } else {
                sessionState.status = 'FAILED';
            }
        }
    });

    // ─── Event: Messages Received ────────────────────────────
    sock.ev.on('messages.upsert', async (m) => {
        for (const msg of m.messages) {
            if (msg.key.fromMe) continue;
            if (m.type === 'notify') {
                const payload = {
                    event: 'message.received',
                    message: msg,
                    pushName: msg.pushName,
                    timestamp: msg.messageTimestamp,
                };
                await dispatchWebhook(sessionName, payload);
            }
        }
    });

    // ─── Event: Contacts Update ──────────────────────────────
    sock.ev.on('contacts.update', async (contacts) => {
        await dispatchWebhook(sessionName, {
            event: 'contacts.update',
            contacts: contacts.map(c => ({
                jid: c.id,
                name: c.name || c.notify,
            })),
        });
    });

    // ─── Event: Group Updates ────────────────────────────────
    sock.ev.on('groups.update', async (groups) => {
        await dispatchWebhook(sessionName, {
            event: 'groups.update',
            groups: groups.map(g => ({
                jid: g.id,
                subject: g.subject,
            })),
        });
    });

    // Return initial status
    if (sessionState.status === 'STARTING') {
        // Wait briefly for QR or connection open
        await delay(2000);
    }

    return {
        name: sessionName,
        status: sessionState.status,
        hasQr: sessionState.qr !== null,
        qrTimestamp: sessionState.qrTimestamp,
        whatsappNumber: sessionState.whatsappNumber,
        whatsappName: sessionState.whatsappName,
        createdAt: sessionState.createdAt,
        config: sessionState.config,
    };
}

async function destroySession(sessionName) {
    const session = sessions.get(sessionName);
    if (!session) return { error: 'Session not found' };

    try {
        if (session.sock) {
            session.sock.ev.removeAllListeners();
            session.sock.ws?.close();
            session.sock.end?.(undefined).catch(() => {});
        }
    } catch (e) {
        logger.warn({ session: sessionName, error: e.message }, 'Error during cleanup');
    }

    sessions.delete(sessionName);
    logger.info({ session: sessionName }, 'Session destroyed');
    return { success: true };
}

// ─── Express App ─────────────────────────────────────────────
const app = express();
app.use(express.json());

// Auth middleware
function authMiddleware(req, res, next) {
    const key = req.headers['x-api-key'];
    if (key !== API_KEY) {
        return res.status(401).json({ error: 'Unauthorized' });
    }
    next();
}

// GET /api/health
app.get('/api/health', (_req, res) => {
    res.json({ status: 'ok', uptime: process.uptime() });
});

// POST /api/sessions — create or start a session
app.post('/api/sessions', authMiddleware, async (req, res) => {
    const { name, config = {} } = req.body;
    if (!name) return res.status(400).json({ error: 'Session name required' });

    try {
        const result = await createSession(name, config);
        res.json(result);
    } catch (err) {
        logger.error({ session: name, error: err.message }, 'Failed to create session');
        res.status(500).json({ error: err.message });
    }
});

// GET /api/sessions — list all sessions
app.get('/api/sessions', authMiddleware, (_req, res) => {
    const meta = loadMeta();
    const list = [];
    for (const [name, info] of Object.entries(meta)) {
        const session = sessions.get(name);
        list.push({
            name,
            status: session?.status || 'STOPPED',
            hasQr: session?.qr !== null || false,
            qrTimestamp: session?.qrTimestamp || 0,
            whatsappNumber: session?.whatsappNumber || info.whatsappNumber || null,
            whatsappName: session?.whatsappName || info.whatsappName || null,
            createdAt: info.createdAt,
            config: info.config || {},
        });
    }
    res.json(list);
});

// GET /api/sessions/:name — get session status
app.get('/api/sessions/:name', authMiddleware, (req, res) => {
    const { name } = req.params;
    const session = sessions.get(name);
    const meta = loadMeta();

    if (!session && !meta[name]) {
        return res.status(404).json({ error: 'Session not found' });
    }

    res.json({
        name,
        status: session?.status || 'STOPPED',
        hasQr: session?.qr !== null || false,
        qrTimestamp: session?.qrTimestamp || 0,
        whatsappNumber: session?.whatsappNumber || meta[name]?.whatsappNumber || null,
        whatsappName: session?.whatsappName || meta[name]?.whatsappName || null,
        createdAt: meta[name]?.createdAt || session?.createdAt || null,
        config: meta[name]?.config || session?.config || {},
    });
});

// GET /api/sessions/:name/qr — get QR code as PNG
app.get('/api/sessions/:name/qr', authMiddleware, async (req, res) => {
    const { name } = req.params;
    const session = sessions.get(name);

    if (!session || !session.qr) {
        return res.status(404).json({ error: 'No QR code available' });
    }

    try {
        const pngBuffer = await QRCode.toBuffer(session.qr, { width: 400 });
        res.setHeader('Content-Type', 'image/png');
        res.send(pngBuffer);
    } catch (err) {
        res.status(500).json({ error: 'Failed to generate QR' });
    }
});

// DELETE /api/sessions/:name — stop and destroy session
app.delete('/api/sessions/:name', authMiddleware, async (req, res) => {
    const { name } = req.params;
    try {
        const result = await destroySession(name);
        res.json(result);
    } catch (err) {
        res.status(500).json({ error: err.message });
    }
});

// POST /api/sessions/:name/logout — logout from WhatsApp
app.post('/api/sessions/:name/logout', authMiddleware, async (req, res) => {
    const { name } = req.params;
    const session = sessions.get(name);

    if (!session || !session.sock) {
        return res.status(404).json({ error: 'Session not found' });
    }

    try {
        await session.sock.logout();
        await destroySession(name);
        res.json({ success: true, message: 'Logged out' });
    } catch (err) {
        res.status(500).json({ error: err.message });
    }
});

// POST /api/sessions/:name/send — send a WhatsApp message
app.post('/api/sessions/:name/send', authMiddleware, async (req, res) => {
    const { name } = req.params;
    const { jid, text } = req.body;

    if (!jid || !text) {
        return res.status(400).json({ error: 'jid and text required' });
    }

    const session = sessions.get(name);
    if (!session || !session.sock) {
        return res.status(404).json({ error: 'Session not connected' });
    }

    try {
        const result = await session.sock.sendMessage(jid, { text });
        res.json({ success: true, messageId: result?.key?.id });
    } catch (err) {
        res.status(500).json({ error: err.message });
    }
});

// POST /api/sessions/:name/config — update session config (webhook, etc.)
app.post('/api/sessions/:name/config', authMiddleware, (req, res) => {
    const { name } = req.params;
    const meta = loadMeta();

    if (!meta[name]) {
        return res.status(404).json({ error: 'Session not found' });
    }

    meta[name].config = { ...meta[name].config, ...req.body };
    saveMeta(meta);

    // Update live session config
    const session = sessions.get(name);
    if (session) {
        session.config = meta[name].config;
    }

    res.json({ success: true, config: meta[name].config });
});

// ─── Startup ─────────────────────────────────────────────────
ensureDataDirs();

// Load existing sessions from metadata
const meta = loadMeta();
logger.info({ activeSessions: Object.keys(meta) }, 'Found saved sessions');

app.listen(PORT, '0.0.0.0', () => {
    logger.info(`Baileys Engine listening on port ${PORT}`);
    logger.info(`API Key: ${API_KEY.substring(0, 12)}...`);
    logger.info(`HTTP Proxy: ${PROXY_URL}`);
});
