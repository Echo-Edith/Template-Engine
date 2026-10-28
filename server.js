const express = require('express');
const session = require('express-session');
const axios = require('axios');
const path = require('path');
const { MongoClient } = require('mongodb');

const app = express();
const PORT = process.env.PORT || 3000;

const PANEL_PASSWORD = process.env.PANEL_PASSWORD || "minion";
const ROBLOX_COOKIE_RAW = (process.env.ROBLOX_COOKIE || process.env.ROBLOSECURITY || "").trim();
const ROBLOX_OPEN_CLOUD_KEY = (process.env.ROBLOX_OPEN_CLOUD_KEY || "").trim();
const ROBLOX_UNIVERSE_ID = (process.env.ROBLOX_UNIVERSE_ID || "").trim();
const ROBLOX_GROUP_ID = (process.env.ROBLOX_GROUP_ID || "").trim();
const VERCEL_TE_SITE_URL = (process.env.VERCEL_TE_SITE_URL || "https://your-vercel-app.vercel.app").trim();
const SYSTEM_LOG_WEBHOOK_URL = (process.env.SYSTEM_LOG_WEBHOOK_URL || "").trim();
const MONGO_URI = process.env.MONGO_URI;

function getFormattedCookie() {
    if (!ROBLOX_COOKIE_RAW) return "";
    const clean = ROBLOX_COOKIE_RAW.replace(/^["']|["']$/g, '');
    if (clean.startsWith(".ROBLOSECURITY=")) return clean;
    return `.ROBLOSECURITY=${clean}`;
}

let db_premium = null;
let db_audit = null;

if (MONGO_URI) {
    MongoClient.connect(MONGO_URI)
        .then(client => {
            const db = client.db();
            db_premium = db.collection("premium_users");
            db_audit = db.collection("audit_logs");
            console.log("[Mongo] Connected successfully.");
        })
        .catch(err => console.error("[Mongo Error]", err.message));
}

const localPremiumUsers = [
    { user_id: "1219266886143967245", username: "EchoOwner", expires_at: "2099-12-31T23:59:59Z", plan: "Lifetime VIP" }
];
const localAuditLogs = [];

app.use(express.json());
app.use(express.urlencoded({ extended: true }));
app.use(session({
    secret: process.env.SESSION_SECRET || 'template_engine_secret_key_1337',
    resave: false,
    saveUninitialized: false,
    cookie: { secure: false, maxAge: 24 * 60 * 60 * 1000 }
}));

function requireAuth(req, res, next) {
    if (req.session && req.session.authenticated) return next();
    return res.status(401).json({ error: "Unauthorized access." });
}

app.post('/api/login', (req, res) => {
    const { password } = req.body;
    if (password === PANEL_PASSWORD) {
        req.session.authenticated = true;
        return res.json({ success: true });
    }
    return res.status(401).json({ success: false, error: "Invalid password." });
});

app.post('/api/logout', (req, res) => {
    req.session.destroy();
    return res.json({ success: true });
});

app.get('/api/auth-status', (req, res) => {
    return res.json({ authenticated: !!(req.session && req.session.authenticated) });
});

app.get('/api/revenue', requireAuth, async (req, res) => {
    const timeframe = req.query.timeframe || '1d';
    const tfMap = { '1d': 'Day', '7d': 'Week', '30d': 'Month', '1y': 'Year', '365d': 'Year' };
    const tfParam = tfMap[timeframe] || 'Day';

    const urls = [];
    if (ROBLOX_GROUP_ID) urls.push(`https://economy.roblox.com/v1/groups/${ROBLOX_GROUP_ID}/revenue/summary/${tfParam}`);
    if (ROBLOX_UNIVERSE_ID) urls.push(`https://apis.roblox.com/universes/v1/${ROBLOX_UNIVERSE_ID}/economy/revenue/summary/${tfParam}`);

    const cleanCookie = getFormattedCookie();
    if (cleanCookie) {
        const primaryHeaders = { "Cookie": cleanCookie, "User-Agent": "Mozilla/5.0" };
        for (const url of urls) {
            try {
                const response = await axios.get(url, { headers: primaryHeaders, timeout: 5000 });
                if (response.status === 200) {
                    const data = response.data;
                    return res.json({
                        sale_of_goods: data.itemSaleRevenue || data.itemSale || 0,
                        commissions: data.commissionRevenue || 0,
                        total: data.totalRevenue || data.total || 0,
                        pending_robux: data.pendingRobux || 0
                    });
                }
            } catch (e) {}
        }
    }

    if (ROBLOX_OPEN_CLOUD_KEY) {
        const fallbackHeaders = { "x-api-key": ROBLOX_OPEN_CLOUD_KEY };
        for (const url of urls) {
            try {
                const response = await axios.get(url, { headers: fallbackHeaders, timeout: 5000 });
                if (response.status === 200) {
                    const data = response.data;
                    return res.json({
                        sale_of_goods: data.itemSaleRevenue || data.itemSale || 0,
                        commissions: data.commissionRevenue || 0,
                        total: data.totalRevenue || data.total || 0,
                        pending_robux: data.pendingRobux || 0
                    });
                }
            } catch (e) {}
        }
    }

    return res.json({ sale_of_goods: 0, commissions: 0, total: 0, pending_robux: 0 });
});

app.get('/api/premium-users', requireAuth, async (req, res) => {
    if (db_premium) {
        const users = await db_premium.find({}).toArray();
        return res.json(users);
    }
    return res.json(localPremiumUsers);
});

app.post('/api/premium-users/add', requireAuth, async (req, res) => {
    const { user_id, username, days } = req.body;
    const durationDays = parseInt(days) || 30;
    const expiresAt = new Date(Date.now() + durationDays * 24 * 60 * 60 * 1000).toISOString();
    if (db_premium) {
        await db_premium.updateOne({ user_id: String(user_id) }, { $set: { user_id: String(user_id), username, expires_at: expiresAt } }, { upsert: true });
    } else {
        localPremiumUsers.push({ user_id, username, expires_at: expiresAt });
    }
    return res.json({ success: true });
});

app.post('/api/premium-users/remove', requireAuth, async (req, res) => {
    const { user_id } = req.body;
    if (db_premium) await db_premium.deleteOne({ user_id: String(user_id) });
    return res.json({ success: true });
});

app.listen(PORT, () => {
    console.log(`[Render Owner Panel] Running on port ${PORT}`);
});
