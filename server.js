/**
 * Render Owner Control Panel for Template Engine
 * Protected by password authentication ("minion")
 * Integrates Roblox Open Cloud APIs, Premium User Management, and Analytics
 */

const express = require('express');
const session = require('express-session');
const axios = require('axios');
const path = require('path');

const app = express();
const PORT = process.env.PORT || 3000;

// Environment Variables
const PANEL_PASSWORD = process.env.PANEL_PASSWORD || "minion";
const ROBLOX_OPEN_CLOUD_KEY = (process.env.ROBLOX_OPEN_CLOUD_KEY || "").trim();
const ROBLOX_UNIVERSE_ID = (process.env.ROBLOX_UNIVERSE_ID || "").trim();
const ROBLOX_GROUP_ID = (process.env.ROBLOX_GROUP_ID || "").trim();
const VERCEL_TE_SITE_URL = (process.env.VERCEL_TE_SITE_URL || "https://your-vercel-app.vercel.app").trim();
const SYSTEM_LOG_WEBHOOK_URL = (process.env.SYSTEM_LOG_WEBHOOK_URL || "").trim();

// In-Memory Storage (Fallback if no DB attached)
const premiumUsersList = [
    { user_id: "1219266886143967245", username: "EchoOwner", expires_at: "2099-12-31T23:59:59Z", plan: "Lifetime VIP" }
];

const auditLogs = [];

// Middleware Setup
app.use(express.json());
app.use(express.urlencoded({ extended: true }));

app.use(session({
    secret: process.env.SESSION_SECRET || 'template_engine_secret_key_1337',
    resave: false,
    saveUninitialized: false,
    cookie: { secure: false, maxAge: 24 * 60 * 60 * 1000 } // 24 Hours
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
    auditLogs.unshift({ action, details, timestamp });
    if (auditLogs.length > 50) auditLogs.pop();

    if (!SYSTEM_LOG_WEBHOOK_URL) return;

    try {
        await axios.post(SYSTEM_LOG_WEBHOOK_URL, {
            embeds: [{
                title: `🛡️ Owner Panel Action: ${action}`,
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

// Roblox Group Revenue & Economy Metrics
app.get('/api/revenue', requireAuth, async (req, res) => {
    const timeframe = req.query.timeframe || '1d';
    
    if (!ROBLOX_OPEN_CLOUD_KEY || !ROBLOX_UNIVERSE_ID) {
        return res.json({
            error: "Missing credentials",
            details: "Please set ROBLOX_OPEN_CLOUD_KEY and ROBLOX_UNIVERSE_ID in your Render Environment Variables."
        });
    }

    try {
        let tfDays = 1;
        if (timeframe === '7d') tfDays = 7;
        else if (timeframe === '30d') tfDays = 30;
        else if (timeframe === '1y') tfDays = 365;

        const now = new Date();
        const startTime = new Date(now.getTime() - (tfDays * 24 * 60 * 60 * 1000)).toISOString();
        const endTime = now.toISOString();

        const response = await axios.post(
            `https://apis.roblox.com/analytics-query-api/v1/universes/${ROBLOX_UNIVERSE_ID}/metrics`,
            {
                metric: "DailyRevenue",
                granularity: "OneDay",
                startTime: startTime,
                endTime: endTime
            },
            {
                headers: {
                    "x-api-key": ROBLOX_OPEN_CLOUD_KEY,
                    "Content-Type": "application/json"
                }
            }
        );

        let totalRevenue = 0;
        if (response.data && response.data.queryResults) {
            response.data.queryResults.forEach(row => {
                totalRevenue += (row.value || 0);
            });
        }

        return res.json({
            timeframe: timeframe,
            saleOfGoods: totalRevenue,
            advanceRebates: 0,
            commissions: 0,
            communityPayouts: 0,
            creatorRewards: 0,
            totalRevenue: totalRevenue,
            pendingRobux: Math.floor(totalRevenue * 0.3), // Estimated pending
            universeId: ROBLOX_UNIVERSE_ID,
            groupId: ROBLOX_GROUP_ID || "N/A"
        });

    } catch (error) {
        console.error("[Revenue Error]", error.response ? error.response.data : error.message);
        return res.status(500).json({
            error: "Failed to fetch revenue from Roblox Open Cloud API.",
            details: error.response ? JSON.stringify(error.response.data) : error.message
        });
    }
});

// Manage Premium Users
app.get('/api/premium-users', requireAuth, (req, res) => {
    return res.json(premiumUsersList);
});

app.post('/api/premium-users/add', requireAuth, (req, res) => {
    const { user_id, username, days } = req.body;
    if (!user_id || !username) {
        return res.status(400).json({ error: "Missing user_id or username" });
    }

    const durationDays = parseInt(days) || 30;
    const expiresAt = new Date(Date.now() + durationDays * 24 * 60 * 60 * 1000).toISOString();

    const existingIndex = premiumUsersList.findIndex(u => u.user_id === user_id);
    if (existingIndex >= 0) {
        premiumUsersList[existingIndex].expires_at = expiresAt;
        premiumUsersList[existingIndex].plan = `${durationDays} Days Extended`;
    } else {
        premiumUsersList.push({
            user_id: user_id,
            username: username,
            expires_at: expiresAt,
            plan: `${durationDays} Days Tier`
        });
    }

    logWebhookEvent("Granted Premium", `Granted ${durationDays} days of Premium to Discord User ${username} (${user_id}).`);
    return res.json({ success: true, message: `Granted premium to ${username}` });
});

app.post('/api/premium-users/remove', requireAuth, (req, res) => {
    const { user_id } = req.body;
    const index = premiumUsersList.findIndex(u => u.user_id === user_id);
    if (index >= 0) {
        const removed = premiumUsersList.splice(index, 1)[0];
        logWebhookEvent("Revoked Premium", `Revoked Premium from ${removed.username} (${user_id}).`);
        return res.json({ success: true, message: "User removed from premium list." });
    }
    return res.status(404).json({ error: "User not found." });
});

// System Audit Logs
app.get('/api/audit-logs', requireAuth, (req, res) => {
    return res.json(auditLogs);
});

// Single Page Frontend HTML
app.get('*', (req, res) => {
    res.send(`
<!DOCTYPE html>
<html lang="en" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Template Engine - Owner Panel</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        body { background-color: #0b0f17; color: #f3f4f6; font-family: 'Inter', system-ui, sans-serif; }
        .glass-card { background: rgba(17, 24, 39, 0.7); backdrop-filter: blur(16px); border: 1px solid rgba(255, 255, 255, 0.08); }
        .gold-gradient { background: linear-gradient(135deg, #f59e0b 0%, #d97706 100%); }
    </style>
</head>
<body class="min-h-screen flex flex-col justify-between">

    <!-- LOGIN OVERLAY -->
    <div id="login-modal" class="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-md">
        <div class="glass-card p-8 rounded-2xl max-w-md w-full mx-4 shadow-2xl border border-amber-500/20">
            <div class="text-center mb-6">
                <div class="w-16 h-16 gold-gradient rounded-full flex items-center justify-center mx-auto mb-3 shadow-lg shadow-amber-500/20">
                    <i class="fa-solid fa-crown text-2xl text-slate-950"></i>
                </div>
                <h1 class="text-2xl font-bold text-white tracking-wide">Owner Control Panel</h1>
                <p class="text-xs text-slate-400 mt-1">Render Cloud Owner Node</p>
            </div>
            
            <form id="login-form" class="space-y-4">
                <div>
                    <label class="block text-xs font-semibold uppercase text-slate-400 mb-1">Passcode</label>
                    <input type="password" id="passcode-input" placeholder="Enter owner passcode" class="w-full bg-slate-900 border border-slate-700 rounded-xl px-4 py-3 text-white focus:outline-none focus:border-amber-500 transition">
                </div>
                <div id="login-error" class="hidden text-rose-500 text-xs font-semibold text-center"></div>
                <button type="submit" class="w-full gold-gradient text-slate-950 font-bold py-3 rounded-xl hover:opacity-90 transition shadow-lg shadow-amber-500/10">
                    Authenticate
                </button>
            </form>
        </div>
    </div>

    <!-- MAIN DASHBOARD -->
    <div id="dashboard" class="hidden min-h-screen flex flex-col">
        <!-- Top Nav -->
        <header class="glass-card border-b border-slate-800 px-6 py-4 flex justify-between items-center sticky top-0 z-40">
            <div class="flex items-center gap-3">
                <div class="w-10 h-10 gold-gradient rounded-xl flex items-center justify-center text-slate-950 font-black">TE</div>
                <div>
                    <h2 class="font-bold text-white text-base leading-tight">Template Engine</h2>
                    <p class="text-xs text-amber-400 font-medium">Render Owner Control Center</p>
                </div>
            </div>
            <div class="flex items-center gap-3">
                <a href="${VERCEL_TE_SITE_URL}" target="_blank" class="px-3 py-2 bg-slate-800 border border-slate-700 text-slate-300 rounded-xl text-xs font-semibold hover:text-white transition flex items-center gap-1.5">
                    <i class="fa-solid fa-arrow-up-right-from-square text-[10px]"></i> Open Vercel TE
                </a>
                <button onclick="fetchRevenue()" class="p-2 text-slate-400 hover:text-white transition"><i class="fa-solid fa-rotate"></i></button>
                <button onclick="logout()" class="px-4 py-2 bg-rose-500/10 border border-rose-500/20 text-rose-400 rounded-xl text-xs font-semibold hover:bg-rose-500 hover:text-white transition">Logout</button>
            </div>
        </header>

        <main class="max-w-7xl w-full mx-auto p-4 md:p-6 space-y-6 flex-grow">
            <!-- REVENUE OVERVIEW CARD -->
            <div class="glass-card rounded-2xl p-6 relative overflow-hidden">
                <div class="flex flex-col md:flex-row justify-between md:items-center gap-4 mb-6">
                    <div>
                        <h3 class="text-lg font-bold text-white flex items-center gap-2"><i class="fa-solid fa-chart-line text-amber-400"></i> Roblox Group Revenue Summary</h3>
                        <p class="text-xs text-slate-400">Real-time group earnings powered by Roblox Open Cloud API</p>
                    </div>
                    <div class="flex items-center gap-2 bg-slate-900/80 p-1 rounded-xl border border-slate-800">
                        <button onclick="setRevenueTimeframe('1d')" id="tf-1d" class="px-3 py-1.5 rounded-lg text-xs font-semibold text-amber-400 bg-amber-500/10 border border-amber-500/30">1D</button>
                        <button onclick="setRevenueTimeframe('7d')" id="tf-7d" class="px-3 py-1.5 rounded-lg text-xs font-semibold text-slate-400 hover:text-white">7D</button>
                        <button onclick="setRevenueTimeframe('30d')" id="tf-30d" class="px-3 py-1.5 rounded-lg text-xs font-semibold text-slate-400 hover:text-white">30D</button>
                        <button onclick="setRevenueTimeframe('1y')" id="tf-1y" class="px-3 py-1.5 rounded-lg text-xs font-semibold text-slate-400 hover:text-white">1Y</button>
                    </div>
                </div>

                <div class="grid grid-cols-1 md:grid-cols-4 gap-4">
                    <div class="bg-slate-900/60 border border-slate-800 p-4 rounded-xl">
                        <span class="text-xs font-medium text-slate-400">Total Revenue</span>
                        <div class="text-2xl font-black text-amber-400 mt-1 flex items-center gap-1">
                            <span>R$</span> <span id="stat-total-rev">0</span>
                        </div>
                    </div>
                    <div class="bg-slate-900/60 border border-slate-800 p-4 rounded-xl">
                        <span class="text-xs font-medium text-slate-400">Sale of Goods</span>
                        <div class="text-2xl font-black text-emerald-400 mt-1 flex items-center gap-1">
                            <span>R$</span> <span id="stat-sale-goods">0</span>
                        </div>
                    </div>
                    <div class="bg-slate-900/60 border border-slate-800 p-4 rounded-xl">
                        <span class="text-xs font-medium text-slate-400">Pending Robux (Est. 30%)</span>
                        <div class="text-2xl font-black text-indigo-400 mt-1 flex items-center gap-1">
                            <span>R$</span> <span id="stat-pending">0</span>
                        </div>
                    </div>
                    <div class="bg-slate-900/60 border border-slate-800 p-4 rounded-xl">
                        <span class="text-xs font-medium text-slate-400">Commissions & Rebates</span>
                        <div class="text-2xl font-black text-slate-300 mt-1 flex items-center gap-1">
                            <span>R$</span> <span id="stat-commissions">0</span>
                        </div>
                    </div>
                </div>
            </div>

            <!-- MANAGEMENT TABS -->
            <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
                <!-- Premium Subscribers Control -->
                <div class="lg:col-span-2 glass-card rounded-2xl p-6">
                    <h3 class="text-base font-bold text-white mb-4 flex items-center gap-2"><i class="fa-solid fa-users text-amber-400"></i> Premium Subscriber Roster</h3>
                    
                    <form id="add-premium-form" class="grid grid-cols-1 sm:grid-cols-3 gap-2 mb-4">
                        <input type="text" id="add-uid" placeholder="Discord User ID" class="bg-slate-900 border border-slate-800 rounded-xl px-3 py-2 text-xs text-white focus:outline-none focus:border-amber-500" required>
                        <input type="text" id="add-uname" placeholder="Username" class="bg-slate-900 border border-slate-800 rounded-xl px-3 py-2 text-xs text-white focus:outline-none focus:border-amber-500" required>
                        <div class="flex gap-2">
                            <input type="number" id="add-days" placeholder="Days (30)" class="w-full bg-slate-900 border border-slate-800 rounded-xl px-3 py-2 text-xs text-white focus:outline-none focus:border-amber-500">
                            <button type="submit" class="gold-gradient text-slate-950 font-bold px-4 rounded-xl text-xs hover:opacity-90">Grant</button>
                        </div>
                    </form>

                    <div class="overflow-x-auto">
                        <table class="w-full text-left text-xs">
                            <thead class="bg-slate-900/80 text-slate-400 border-b border-slate-800">
                                <tr>
                                    <th class="p-3">User</th>
                                    <th class="p-3">Discord ID</th>
                                    <th class="p-3">Expires At</th>
                                    <th class="p-3 text-right">Action</th>
                                </tr>
                            </thead>
                            <tbody id="premium-table-body" class="divide-y divide-slate-800/50">
                                <!-- Dynamic User Rows -->
                            </tbody>
                        </table>
                    </div>
                </div>

                <!-- Live Audit Feed -->
                <div class="glass-card rounded-2xl p-6">
                    <h3 class="text-base font-bold text-white mb-4 flex items-center gap-2"><i class="fa-solid fa-clock-rotate-left text-amber-400"></i> Audit Feed</h3>
                    <div id="audit-feed" class="space-y-3 max-h-80 overflow-y-auto pr-1 text-xs">
                        <div class="text-slate-500 italic">Loading audit entries...</div>
                    </div>
                </div>
            </div>
        </main>

        <footer class="glass-card border-t border-slate-800 p-4 text-center text-xs text-slate-500">
            Template Engine Owner Node &bull; Render Deployment
        </footer>
    </div>

    <script>
        let currentTF = '1d';

        async function checkAuth() {
            const res = await fetch('/api/auth-status');
            const data = await res.json();
            if (data.authenticated) {
                document.getElementById('login-modal').classList.add('hidden');
                document.getElementById('dashboard').classList.remove('hidden');
                loadDashboardData();
            } else {
                document.getElementById('login-modal').classList.remove('hidden');
            }
        }

        document.getElementById('login-form').addEventListener('submit', async (e) => {
            e.preventDefault();
            const password = document.getElementById('passcode-input').value;
            const res = await fetch('/api/login', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ password })
            });
            const data = await res.json();
            if (data.success) {
                checkAuth();
            } else {
                const errEl = document.getElementById('login-error');
                errEl.innerText = data.error;
                errEl.classList.remove('hidden');
            }
        });

        async function logout() {
            await fetch('/api/logout', { method: 'POST' });
            window.location.reload();
        }

        function setRevenueTimeframe(tf) {
            currentTF = tf;
            ['1d', '7d', '30d', '1y'].forEach(t => {
                const btn = document.getElementById(\`tf-\${t}\`);
                if (t === tf) {
                    btn.className = "px-3 py-1.5 rounded-lg text-xs font-semibold text-amber-400 bg-amber-500/10 border border-amber-500/30";
                } else {
                    btn.className = "px-3 py-1.5 rounded-lg text-xs font-semibold text-slate-400 hover:text-white";
                }
            });
            fetchRevenue();
        }

        async function fetchRevenue() {
            try {
                const res = await fetch(\`/api/revenue?timeframe=\${currentTF}\`);
                const data = await res.json();
                if (data.error) {
                    document.getElementById('stat-total-rev').innerText = "0";
                    return;
                }
                document.getElementById('stat-total-rev').innerText = (data.totalRevenue || 0).toLocaleString();
                document.getElementById('stat-sale-goods').innerText = (data.saleOfGoods || 0).toLocaleString();
                document.getElementById('stat-pending').innerText = (data.pendingRobux || 0).toLocaleString();
                document.getElementById('stat-commissions').innerText = (data.commissions || 0).toLocaleString();
            } catch(e) {
                console.error(e);
            }
        }

        async function fetchPremiumUsers() {
            const res = await fetch('/api/premium-users');
            const list = await res.json();
            const tbody = document.getElementById('premium-table-body');
            tbody.innerHTML = '';
            list.forEach(user => {
                const tr = document.createElement('tr');
                tr.className = "hover:bg-slate-900/40 transition";
                tr.innerHTML = \`
                    <td class="p-3 font-semibold text-white">\${user.username}</td>
                    <td class="p-3 font-mono text-slate-400">\${user.user_id}</td>
                    <td class="p-3 text-slate-400">\${new Date(user.expires_at).toLocaleDateString()}</td>
                    <td class="p-3 text-right">
                        <button onclick="revokePremium('\${user.user_id}')" class="text-rose-400 hover:text-rose-300 font-semibold text-xs">Revoke</button>
                    </td>
                \`;
                tbody.appendChild(tr);
            });
        }

        document.getElementById('add-premium-form').addEventListener('submit', async (e) => {
            e.preventDefault();
            const user_id = document.getElementById('add-uid').value;
            const username = document.getElementById('add-uname').value;
            const days = document.getElementById('add-days').value || 30;

            await fetch('/api/premium-users/add', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ user_id, username, days })
            });

            document.getElementById('add-uid').value = '';
            document.getElementById('add-uname').value = '';
            document.getElementById('add-days').value = '';
            fetchPremiumUsers();
            fetchAuditFeed();
        });

        async function revokePremium(user_id) {
            await fetch('/api/premium-users/remove', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ user_id })
            });
            fetchPremiumUsers();
            fetchAuditFeed();
        }

        async function fetchAuditFeed() {
            const res = await fetch('/api/audit-logs');
            const logs = await res.json();
            const feed = document.getElementById('audit-feed');
            feed.innerHTML = '';
            logs.forEach(log => {
                const div = document.createElement('div');
                div.className = "p-2.5 rounded-xl bg-slate-900/50 border border-slate-800";
                div.innerHTML = \`
                    <div class="font-semibold text-amber-400">\${log.action}</div>
                    <div class="text-slate-300 mt-0.5">\${log.details}</div>
                    <div class="text-[10px] text-slate-500 mt-1">\${new Date(log.timestamp).toLocaleTimeString()}</div>
                \`;
                feed.appendChild(div);
            });
        }

        function loadDashboardData() {
            fetchRevenue();
            fetchPremiumUsers();
            fetchAuditFeed();
        }

        checkAuth();
    </script>
</body>
</html>
    `);
});

app.listen(PORT, () => {
    console.log(`[Render Owner Panel] Running on port ${PORT}`);
});
