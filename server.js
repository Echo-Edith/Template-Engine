/**
 * Render Owner Control Panel for Template Engine
 * Protected by password authentication ("minion")
 * Integrates Roblox Open Cloud APIs, Premium User Management, and Analytics
 */

const express = require('express');
const session = require('express-session');
const axios = require('axios');
const path = require('path');
const { MongoClient } = require('mongodb');

const app = express();
const PORT = process.env.PORT || 3000;

// Environment Variables
const PANEL_PASSWORD = process.env.PANEL_PASSWORD || "minion";
const ROBLOX_OPEN_CLOUD_KEY = (process.env.ROBLOX_OPEN_CLOUD_KEY || "").trim();
const ROBLOX_UNIVERSE_ID = (process.env.ROBLOX_UNIVERSE_ID || "").trim();
const ROBLOX_GROUP_ID = (process.env.ROBLOX_GROUP_ID || "").trim();
const ROBLOX_COOKIE = (process.env.ROBLOX_COOKIE || process.env.ROBLOSECURITY || "").trim();
const VERCEL_TE_SITE_URL = (process.env.VERCEL_TE_SITE_URL || "https://your-vercel-app.vercel.app").trim();
const SYSTEM_LOG_WEBHOOK_URL = (process.env.SYSTEM_LOG_WEBHOOK_URL || "").trim();
const MONGO_URI = process.env.MONGO_URI;

// MongoDB Connection Setup
let db_premium = null;
let db_audit = null;

if (MONGO_URI) {
    MongoClient.connect(MONGO_URI)
        .then(client => {
            const db = client.db();
            db_premium = db.collection("premium_users");
            db_audit = db.collection("audit_logs");
            console.log("[Mongo] Connected successfully to shared MongoDB.");
        })
        .catch(err => console.error("[Mongo Error]", err.message));
}

// In-Memory Storage Fallback
const localPremiumUsers = [
    { user_id: "1219266886143967245", username: "EchoOwner", expires_at: "2099-12-31T23:59:59Z", plan: "Lifetime VIP" }
];
const localAuditLogs = [];

// Middleware Setup
app.use(express.json());
app.use(express.urlencoded({ extended: true }));

app.use(session({
    secret: process.env.SESSION_SECRET || 'template_engine_secret_key_1337',
    resave: false,
    saveUninitialized: false,
    cookie: { secure: false, maxAge: 24 * 60 * 60 * 1000 }
}));

// Auth Guard Middleware
function requireAuth(req, res, next) {
    if (req.session && req.session.authenticated) {
        return next();
    }
    return res.status(401).json({ error: "Unauthorized access. Please login first." });
}

// Log Webhook Utility
async function logWebhookEvent(action, details) {
    const timestamp = new Date().toISOString();
    const logEntry = { action, details, timestamp };

    if (db_audit) {
        try { await db_audit.insertOne(logEntry); } catch (e) { console.error(e); }
    } else {
        localAuditLogs.unshift(logEntry);
        if (localAuditLogs.length > 50) localAuditLogs.pop();
    }

    if (!SYSTEM_LOG_WEBHOOK_URL) return;

    try {
        await axios.post(SYSTEM_LOG_WEBHOOK_URL, {
            embeds: [{
                title: `Owner Panel Action: ${action}`,
                description: details,
                color: 0xf1c40f,
                timestamp: timestamp
            }]
        });
    } catch (err) {
        console.error("[Webhook Error]", err.message);
    }
}

// --- API ENDPOINTS ---

// Login Endpoint
app.post('/api/login', (req, res) => {
    const { password } = req.body;
    if (password === PANEL_PASSWORD) {
        req.session.authenticated = true;
        logWebhookEvent("Owner Login Successful", "Owner logged into Render Control Panel.");
        return res.json({ success: true, message: "Authenticated successfully." });
    }
    logWebhookEvent("Failed Login Attempt", "An invalid password was entered.");
    return res.status(401).json({ success: false, error: "Invalid password." });
});

// Logout Endpoint
app.post('/api/logout', (req, res) => {
    req.session.destroy();
    return res.json({ success: true, message: "Logged out." });
});

// Auth Status Check
app.get('/api/auth-status', (req, res) => {
    return res.json({ authenticated: !!(req.session && req.session.authenticated) });
});

// Roblox Revenue Endpoint
app.get('/api/revenue', requireAuth, async (req, res) => {
    const timeframe = req.query.timeframe || '1d';
    const tfMap = { '1d': 'Day', '7d': 'Week', '30d': 'Month', '1y': 'Year', '365d': 'Year' };
    const tfParam = tfMap[timeframe] || 'Day';

    const headers = {};
    if (ROBLOX_COOKIE) {
        headers["Cookie"] = ROBLOX_COOKIE.startsWith(".ROBLOSECURITY=") ? ROBLOX_COOKIE : `.ROBLOSECURITY=${ROBLOX_COOKIE}`;
    }
    if (ROBLOX_OPEN_CLOUD_KEY) {
        headers["x-api-key"] = ROBLOX_OPEN_CLOUD_KEY;
    }

    const urls = [];
    if (ROBLOX_GROUP_ID) urls.push(`https://economy.roblox.com/v1/groups/${ROBLOX_GROUP_ID}/revenue/summary/${tfParam}`);
    if (ROBLOX_UNIVERSE_ID) urls.push(`https://apis.roblox.com/universes/v1/${ROBLOX_UNIVERSE_ID}/economy/revenue/summary/${tfParam}`);

    for (const url of urls) {
        try {
            const response = await axios.get(url, { headers, timeout: 5000 });
            if (response.status === 200) {
                const data = response.data;
                return res.json({
                    timeframe: timeframe,
                    sale_of_goods: data.itemSaleRevenue || data.itemSale || 0,
                    advance_rebates: data.publishingAdvanceRebates || 0,
                    commissions: data.commissionRevenue || data.comission || 0,
                    total: data.totalRevenue || data.total || 0,
                    pending_robux: data.pendingRobux || 0
                });
            }
        } catch (e) {}
    }

    return res.json({
        timeframe: timeframe,
        sale_of_goods: 0,
        advance_rebates: 0,
        commissions: 0,
        total: 0,
        pending_robux: 0
    });
});

// Manage Premium Users
app.get('/api/premium-users', requireAuth, async (req, res) => {
    if (db_premium) {
        const users = await db_premium.find({}).toArray();
        return res.json(users);
    }
    return res.json(localPremiumUsers);
});

app.post('/api/premium-users/add', requireAuth, async (req, res) => {
    const { user_id, username, days } = req.body;
    if (!user_id || !username) {
        return res.status(400).json({ error: "Missing user_id or username" });
    }

    const durationDays = parseInt(days) || 30;
    const expiresAt = new Date(Date.now() + durationDays * 24 * 60 * 60 * 1000).toISOString();

    if (db_premium) {
        await db_premium.updateOne(
            { user_id: String(user_id) },
            { $set: { user_id: String(user_id), username: username, expires_at: expiresAt, plan: `${durationDays} Days Tier` } },
            { upsert: true }
        );
    } else {
        const existingIndex = localPremiumUsers.findIndex(u => u.user_id === user_id);
        if (existingIndex >= 0) {
            localPremiumUsers[existingIndex].expires_at = expiresAt;
        } else {
            localPremiumUsers.push({ user_id, username, expires_at: expiresAt, plan: `${durationDays} Days Tier` });
        }
    }

    logWebhookEvent("Granted Premium", `Granted ${durationDays} days of Premium to Discord User ${username} (${user_id}).`);
    return res.json({ success: true, message: `Granted premium to ${username}` });
});

app.post('/api/premium-users/remove', requireAuth, async (req, res) => {
    const { user_id } = req.body;
    if (db_premium) {
        await db_premium.deleteOne({ user_id: String(user_id) });
    } else {
        const index = localPremiumUsers.findIndex(u => u.user_id === user_id);
        if (index >= 0) localPremiumUsers.splice(index, 1);
    }
    logWebhookEvent("Revoked Premium", `Revoked Premium from Discord User ID ${user_id}.`);
    return res.json({ success: true, message: "User removed from premium list." });
});

// System Audit Logs
app.get('/api/audit-logs', requireAuth, async (req, res) => {
    if (db_audit) {
        const logs = await db_audit.find({}).sort({ _id: -1 }).limit(50).toArray();
        return res.json(logs);
    }
    return res.json(localAuditLogs);
});

// HTML Interface
app.get('*', (req, res) => {
    res.send(`
<!DOCTYPE html>
<html lang="en" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Template Engine - Owner Panel</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body { background-color: #0b0f17; color: #f3f4f6; font-family: 'Inter', system-ui, sans-serif; }
        .glass-card { background: rgba(17, 24, 39, 0.7); backdrop-filter: blur(16px); border: 1px solid rgba(255, 255, 255, 0.08); }
        .gold-gradient { background: linear-gradient(135deg, #f59e0b 0%, #d97706 100%); }
    </style>
</head>
<body class="min-h-screen flex flex-col justify-between">
    <div id="login-modal" class="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-md">
        <div class="glass-card p-8 rounded-2xl max-w-md w-full mx-4 shadow-2xl border border-amber-500/20">
            <div class="text-center mb-6">
                <div class="w-16 h-16 gold-gradient rounded-full flex items-center justify-center mx-auto mb-3 text-xl font-black text-slate-950">TE</div>
                <h1 class="text-2xl font-bold text-white tracking-wide">Owner Control Panel</h1>
            </div>
            <form id="login-form" class="space-y-4">
                <input type="password" id="passcode-input" placeholder="Enter owner passcode" class="w-full bg-slate-900 border border-slate-700 rounded-xl px-4 py-3 text-white">
                <button type="submit" class="w-full gold-gradient text-slate-950 font-bold py-3 rounded-xl">Authenticate</button>
            </form>
        </div>
    </div>
</body>
</html>
    `);
});

app.listen(PORT, () => {
    console.log(`[Render Owner Panel] Running on port ${PORT}`);
});
