import os
import re
import json
import asyncio
import threading
import requests
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify, render_template_string, session, redirect, url_for
import discord
from discord.ext import commands
from pymongo import MongoClient

# --- CONFIGURATION & ENV VARS ---
PANEL_PASSWORD = os.environ.get("PANEL_PASSWORD", "minion").strip()
SECRET_KEY = os.environ.get("SESSION_SECRET", "template_engine_secret_key_1337").strip()

MONGO_URI = os.environ.get("MONGO_URI")
ROBLOX_OPEN_CLOUD_KEY = (os.environ.get("ROBLOX_OPEN_CLOUD_KEY") or "").strip()
ROBLOX_UNIVERSE_ID = (os.environ.get("ROBLOX_UNIVERSE_ID") or "").strip()
ROBLOX_GROUP_ID = (os.environ.get("ROBLOX_GROUP_ID") or "").strip()
VERCEL_TE_SITE_URL = (os.environ.get("VERCEL_TE_SITE_URL") or "https://your-vercel-app.vercel.app").strip()
SYSTEM_LOG_WEBHOOK_URL = (os.environ.get("SYSTEM_LOG_WEBHOOK_URL") or "").strip()

# --- MONGO DATABASE CONNECTIONS ---
db_premium = None
db_audit = None

if MONGO_URI:
    try:
        mongo_client = MongoClient(MONGO_URI)
        try:
            db = mongo_client.get_default_database()
        except Exception:
            db = mongo_client["static_studio_db"]
        db_premium = db["premium_users"]
        db_audit = db["audit_logs"]
    except Exception as e:
        print(f"[Mongo Warning] Could not connect to MongoDB in main.py: {e}")

# Fallback in-memory structures if MongoDB is unattached
LOCAL_PREMIUM_USERS = [
    {"user_id": "1219266886143967245", "username": "EchoOwner", "expires_at": "2099-12-31T23:59:59Z", "plan": "Lifetime VIP"}
]
LOCAL_AUDIT_LOGS = []

def log_system_event(action: str, details: str):
    timestamp = datetime.now(timezone.utc).isoformat()
    log_entry = {"action": action, "details": details, "timestamp": timestamp}
    
    if db_audit is not None:
        try:
            db_audit.insert_one(log_entry)
        except Exception as e:
            print(f"[Audit Log DB Error] {e}")
    else:
        LOCAL_AUDIT_LOGS.insert(0, log_entry)
        if len(LOCAL_AUDIT_LOGS) > 50:
            LOCAL_AUDIT_LOGS.pop()

    if SYSTEM_LOG_WEBHOOK_URL:
        try:
            requests.post(SYSTEM_LOG_WEBHOOK_URL, json={
                "embeds": [{
                    "title": f"🛡️ Owner Panel Action: {action}",
                    "description": details,
                    "color": 0xf1c40f,
                    "timestamp": timestamp
                }]
            }, timeout=3)
        except Exception as err:
            print(f"[Webhook Log Warning] {err}")

# --- FLASK APPLICATION SETUP ---
web_app = Flask(__name__)
web_app.secret_key = SECRET_KEY

bot_cog_ref = None

# --- AUTH HELPER ---
def is_authenticated():
    return session.get("authenticated", False)

# --- WEB ROUTES & OWNER CONTROL PANEL ---

HTML_TEMPLATE = """
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
                <p class="text-xs text-slate-400 mt-1">Template Engine Master Node</p>
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
                    <p class="text-xs text-amber-400 font-medium">Render Master Control Center</p>
                </div>
            </div>
            <div class="flex items-center gap-3">
                <a href="{{ vercel_url }}" target="_blank" class="px-3 py-2 bg-slate-800 border border-slate-700 text-slate-300 rounded-xl text-xs font-semibold hover:text-white transition flex items-center gap-1.5">
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
            Template Engine Owner Node &bull; Render Host
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
                const btn = document.getElementById(`tf-${t}`);
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
                const res = await fetch(`/api/revenue?timeframe=${currentTF}`);
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
                tr.innerHTML = `
                    <td class="p-3 font-semibold text-white">${user.username || 'User'}</td>
                    <td class="p-3 font-mono text-slate-400">${user.user_id}</td>
                    <td class="p-3 text-slate-400">${new Date(user.expires_at).toLocaleDateString()}</td>
                    <td class="p-3 text-right">
                        <button onclick="revokePremium('${user.user_id}')" class="text-rose-400 hover:text-rose-300 font-semibold text-xs">Revoke</button>
                    </td>
                `;
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
                div.innerHTML = `
                    <div class="font-semibold text-amber-400">${log.action}</div>
                    <div class="text-slate-300 mt-0.5">${log.details}</div>
                    <div class="text-[10px] text-slate-500 mt-1">${new Date(log.timestamp).toLocaleTimeString()}</div>
                `;
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
"""

@web_app.route("/")
@web_app.route("/dashboard")
def dashboard_view():
    return render_template_string(HTML_TEMPLATE, vercel_url=VERCEL_TE_SITE_URL)

@web_app.route("/health")
def health_check():
    return jsonify({
        "status": "online",
        "bot": str(bot.user) if bot.user else "initializing"
    }), 200

# API Auth Routes
@web_app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json() or {}
    password = data.get("password", "")
    if password == PANEL_PASSWORD:
        session["authenticated"] = True
        log_system_event("Owner Login", "Owner logged into Render Control Panel.")
        return jsonify({"success": True, "message": "Authenticated."})
    log_system_event("Failed Login", "An invalid password was entered on Owner Panel.")
    return jsonify({"success": False, "error": "Invalid passcode."}), 401

@web_app.route("/api/logout", methods=["POST"])
def api_logout():
    session.pop("authenticated", None)
    return jsonify({"success": True, "message": "Logged out."})

@web_app.route("/api/auth-status", methods=["GET"])
def api_auth_status():
    return jsonify({"authenticated": is_authenticated()})

# Revenue Analytics Endpoint (Open Cloud)
@web_app.route("/api/revenue", methods=["GET"])
def api_revenue():
    if not is_authenticated():
        return jsonify({"error": "Unauthorized"}), 401

    if not ROBLOX_OPEN_CLOUD_KEY or not ROBLOX_UNIVERSE_ID:
        return jsonify({"error": "Missing Open Cloud credentials"}), 400

    timeframe = request.args.get("timeframe", "1d").lower()
    tf_days = 1
    if timeframe == "7d": tf_days = 7
    elif timeframe == "30d": tf_days = 30
    elif timeframe in ["1y", "365d"]: tf_days = 365

    now = datetime.now(timezone.utc)
    start_time = (now - timedelta(days=tf_days)).strftime("%Y-%m-%dT00:00:00Z")
    end_time = now.strftime("%Y-%m-%dT00:00:00Z")

    url = f"https://apis.roblox.com/analytics-query-api/v1/universes/{ROBLOX_UNIVERSE_ID}/metrics"
    headers = {
        "x-api-key": ROBLOX_OPEN_CLOUD_KEY,
        "Content-Type": "application/json"
    }
    payload = {
        "metric": "DailyRevenue",
        "granularity": "OneDay",
        "startTime": start_time,
        "endTime": end_time
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            total_rev = 0
            if "queryResults" in data:
                for row in data["queryResults"]:
                    total_rev += row.get("value", 0)
            return jsonify({
                "timeframe": timeframe,
                "saleOfGoods": total_rev,
                "commissions": 0,
                "totalRevenue": total_rev,
                "pendingRobux": int(total_rev * 0.3)
            })
        return jsonify({"error": f"Open Cloud error: {resp.status_code}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Premium Users Management Endpoint
@web_app.route("/api/premium-users", methods=["GET"])
def get_premium_users():
    if not is_authenticated():
        return jsonify({"error": "Unauthorized"}), 401

    users = []
    if db_premium is not None:
        for doc in db_premium.find():
            doc["_id"] = str(doc.get("_id"))
            users.append(doc)
    else:
        users = LOCAL_PREMIUM_USERS
    return jsonify(users)

@web_app.route("/api/premium-users/add", methods=["POST"])
def add_premium_user():
    if not is_authenticated():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json() or {}
    user_id = data.get("user_id")
    username = data.get("username", "User")
    days = int(data.get("days") or 30)

    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400

    expires_at = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()

    if db_premium is not None:
        db_premium.update_one(
            {"user_id": str(user_id)},
            {"$set": {"user_id": str(user_id), "username": username, "expires_at": expires_at, "plan": f"{days} Days Tier"}},
            upsert=True
        )
    else:
        LOCAL_PREMIUM_USERS.append({"user_id": str(user_id), "username": username, "expires_at": expires_at, "plan": f"{days} Days Tier"})

    log_system_event("Granted Premium", f"Granted {days} days of Premium to {username} ({user_id}).")
    return jsonify({"success": True})

@web_app.route("/api/premium-users/remove", methods=["POST"])
def remove_premium_user():
    if not is_authenticated():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json() or {}
    user_id = str(data.get("user_id"))

    if db_premium is not None:
        db_premium.delete_one({"user_id": user_id})
    else:
        global LOCAL_PREMIUM_USERS
        LOCAL_PREMIUM_USERS = [u for u in LOCAL_PREMIUM_USERS if u.get("user_id") != user_id]

    log_system_event("Revoked Premium", f"Revoked Premium from Discord User ID {user_id}.")
    return jsonify({"success": True})

@web_app.route("/api/audit-logs", methods=["GET"])
def get_audit_logs():
    if not is_authenticated():
        return jsonify({"error": "Unauthorized"}), 401

    logs = []
    if db_audit is not None:
        for doc in db_audit.find().sort("_id", -1).limit(50):
            doc["_id"] = str(doc.get("_id"))
            logs.append(doc)
    else:
        logs = LOCAL_AUDIT_LOGS
    return jsonify(logs)

@web_app.route("/api/auto-build", methods=["POST"])
def auto_build_endpoint():
    data = request.get_json() or {}
    user_id = data.get("user_id")
    target_server_id = data.get("target_server_id")
    blueprint_url = data.get("blueprint_url")
    blueprint_data = data.get("blueprint_data")
    log_channel_id = data.get("log_channel_id", "1563544108696477802")

    if not target_server_id or not blueprint_data:
        return jsonify({"success": False, "error": "Missing target_server_id or blueprint_data"}), 400

    if bot_cog_ref and bot.loop:
        asyncio.run_coroutine_threadsafe(
            bot_cog_ref.handle_direct_handshake_build(
                user_id=user_id,
                target_server_id=target_server_id,
                blueprint_url=blueprint_url,
                blueprint_data=blueprint_data,
                log_channel_id=log_channel_id
            ),
            bot.loop
        )
        return jsonify({"success": True, "message": "Direct handshake build queued in background"}), 200

    return jsonify({"success": False, "error": "Bot instance or event loop not ready"}), 503

def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    import logging
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)
    web_app.run(host="0.0.0.0", port=port)

# --- DISCORD BOT SETUP ---
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    help_command=None
)

@bot.event
async def on_ready():
    global bot_cog_ref
    print("-----------------------------------------")
    print(f"Logged in as: {bot.user} (ID: {bot.user.id})")
    print("-----------------------------------------")

    try:
        if "cogs.bot" not in bot.extensions:
            await bot.load_extension("cogs.bot")
            print("Successfully loaded extension cog: cogs.bot")
        else:
            await bot.reload_extension("cogs.bot")
            print("Successfully reloaded extension cog: cogs.bot")

        bot_cog_ref = bot.get_cog("BotCog")
    except Exception as e:
        print(f"CRITICAL ERROR loading cog cogs.bot: {e}")

    try:
        synced = await bot.tree.sync()
        print(f"Successfully registered & synced {len(synced)} global slash command(s).")
    except Exception as e:
        print(f"Failed to sync global slash commands: {e}")

async def runner():
    token = os.environ.get("DISCORD_BOT_TOKEN") or os.environ.get("BOT_TOKEN")
    if not token or not token.strip():
        print("CRITICAL ERROR: DISCORD_BOT_TOKEN environment variable is missing!")
        return

    async with bot:
        await bot.start(token.strip())

def main():
    health_thread = threading.Thread(target=run_health_server, daemon=True)
    health_thread.start()
    print("Background Owner Control Panel and API server started on port 10000.")

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        print("Bot runner shutdown gracefully.")

if __name__ == "__main__":
    main()
