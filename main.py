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
ROBLOX_COOKIE = (os.environ.get("ROBLOX_COOKIE") or os.environ.get("ROBLOSECURITY") or "").strip()
VERCEL_TE_SITE_URL = (os.environ.get("VERCEL_TE_SITE_URL") or "https://your-vercel-app.vercel.app").strip()
SYSTEM_LOG_WEBHOOK_URL = (os.environ.get("SYSTEM_LOG_WEBHOOK_URL") or "").strip()

# --- MONGO DATABASE CONNECTIONS ---
db_premium = None
db_audit = None
db_layout_ticket_config = None
db_purchase_receipts = None
db_pending_blueprints = None

if MONGO_URI:
    try:
        mongo_client = MongoClient(MONGO_URI)
        try:
            db = mongo_client.get_default_database()
        except Exception:
            db = mongo_client["static_studio_db"]
        db_premium = db["premium_users"]
        db_audit = db["audit_logs"]
        db_layout_ticket_config = db["layout_ticket_config"]
        db_purchase_receipts = db["purchase_receipts"]
        db_pending_blueprints = db["pending_blueprints"]
    except Exception as e:
        print(f"[Mongo Warning] Could not connect to MongoDB in main.py: {e}")

LOCAL_PREMIUM_USERS = [
    {"user_id": "1219266886143967245", "username": "EchoOwner", "expires_at": "2099-12-31T23:59:59Z", "plan": "Lifetime VIP"}
]
LOCAL_AUDIT_LOGS = []
LOCAL_TICKET_CONFIG = {
    "title": "Layout Premium Deployment Ticket",
    "dev_product_id": "3722941069",
    "instructions": "Follow the step-by-step images below to purchase the developer product, then click 'Verify & Auto-Build'.",
    "step_images": []
}

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
                    "title": f"Owner Panel Action: {action}",
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

def is_authenticated():
    return session.get("authenticated", False)

# --- OWNER CONTROL PANEL TEMPLATE ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Template Engine - Owner Control Panel</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body { background-color: #0b0f17; color: #f3f4f6; font-family: 'Inter', system-ui, sans-serif; }
        .glass-card { background: rgba(17, 24, 39, 0.7); backdrop-filter: blur(16px); border: 1px solid rgba(255, 255, 255, 0.08); }
        .gold-gradient { background: linear-gradient(135deg, #f59e0b 0%, #d97706 100%); }
        .sidebar-transition { transition: transform 0.3s ease-in-out; }
    </style>
</head>
<body class="min-h-screen flex flex-col justify-between">

    <!-- LOGIN OVERLAY -->
    <div id="login-modal" class="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-md">
        <div class="glass-card p-8 rounded-2xl max-w-md w-full mx-4 shadow-2xl border border-amber-500/20">
            <div class="text-center mb-6">
                <div class="w-16 h-16 gold-gradient rounded-full flex items-center justify-center mx-auto mb-3 shadow-lg shadow-amber-500/20 font-black text-xl text-slate-950">
                    TE
                </div>
                <h1 class="text-2xl font-bold text-white tracking-wide">Owner Control Panel</h1>
                <p class="text-xs text-slate-400 mt-1">Master Management Node</p>
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
    <div id="dashboard" class="hidden min-h-screen flex flex-col md:flex-row">

        <!-- MOBILE SIDEBAR OVERLAY -->
        <div id="sidebar-overlay" onclick="toggleSidebar()" class="fixed inset-0 bg-black/60 z-30 hidden md:hidden"></div>

        <!-- HAMBURGER SIDEBAR -->
        <aside id="sidebar" class="fixed md:static inset-y-0 left-0 z-40 w-64 glass-card border-r border-slate-800 flex flex-col transform -translate-x-full md:translate-x-0 sidebar-transition">
            <div class="p-4 border-b border-slate-800 flex items-center justify-between">
                <div class="flex items-center gap-3">
                    <div class="w-9 h-9 gold-gradient rounded-xl flex items-center justify-center text-slate-950 font-black">TE</div>
                    <div>
                        <h2 class="font-bold text-white text-sm">Template Engine</h2>
                        <p class="text-[10px] text-amber-400 font-medium">Owner Panel</p>
                    </div>
                </div>
                <button onclick="toggleSidebar()" class="md:hidden text-slate-400 hover:text-white p-1 text-sm font-bold">[X]</button>
            </div>

            <nav class="p-3 space-y-1 flex-grow">
                <button onclick="switchTab('analytics')" id="nav-analytics" class="w-full flex items-center gap-3 px-3 py-2.5 rounded-xl text-xs font-semibold text-amber-400 bg-amber-500/10 border border-amber-500/30">
                    Revenue Analytics
                </button>
                <button onclick="switchTab('premium')" id="nav-premium" class="w-full flex items-center gap-3 px-3 py-2.5 rounded-xl text-xs font-semibold text-slate-400 hover:text-white hover:bg-slate-800/50">
                    Subscriber Roster
                </button>
                <button onclick="switchTab('ticket')" id="nav-ticket" class="w-full flex items-center gap-3 px-3 py-2.5 rounded-xl text-xs font-semibold text-slate-400 hover:text-white hover:bg-slate-800/50">
                    Ticket & Panel Config
                </button>
                <button onclick="switchTab('deploy')" id="nav-deploy" class="w-full flex items-center gap-3 px-3 py-2.5 rounded-xl text-xs font-semibold text-slate-400 hover:text-white hover:bg-slate-800/50">
                    Open Cloud Deploy
                </button>
                <button onclick="switchTab('logs')" id="nav-logs" class="w-full flex items-center gap-3 px-3 py-2.5 rounded-xl text-xs font-semibold text-slate-400 hover:text-white hover:bg-slate-800/50">
                    System Audit Logs
                </button>
            </nav>

            <div class="p-4 border-t border-slate-800 space-y-2">
                <a href="{{ vercel_url }}" target="_blank" class="w-full px-3 py-2 bg-slate-800 border border-slate-700 text-slate-300 rounded-xl text-xs font-semibold hover:text-white transition flex items-center justify-center gap-2">
                    Open Vercel TE
                </a>
                <button onclick="logout()" class="w-full px-3 py-2 bg-rose-500/10 border border-rose-500/20 text-rose-400 rounded-xl text-xs font-semibold hover:bg-rose-500 hover:text-white transition">
                    Logout
                </button>
            </div>
        </aside>

        <!-- CONTENT AREA -->
        <div class="flex-grow flex flex-col min-w-0">
            <!-- TOP BAR -->
            <header class="glass-card border-b border-slate-800 px-4 py-3 flex justify-between items-center sticky top-0 z-20">
                <div class="flex items-center gap-3">
                    <button onclick="toggleSidebar()" class="md:hidden p-2 text-slate-300 hover:text-white text-xs font-bold">[Menu]</button>
                    <h2 id="tab-header-title" class="font-bold text-white text-base">Revenue Analytics</h2>
                </div>
                <div class="flex items-center gap-2">
                    <button onclick="refreshActiveTab()" class="p-2 text-slate-400 hover:text-white transition text-xs font-bold">[Refresh]</button>
                </div>
            </header>

            <main class="p-4 md:p-6 space-y-6 flex-grow overflow-y-auto">
                
                <!-- TAB 1: REVENUE ANALYTICS -->
                <div id="tab-content-analytics" class="tab-page space-y-6">
                    <div class="glass-card rounded-2xl p-6">
                        <div class="flex flex-col sm:flex-row justify-between sm:items-center gap-4 mb-6">
                            <div>
                                <h3 class="text-lg font-bold text-white">Roblox Group & Experience Revenue</h3>
                                <p class="text-xs text-slate-400">Powered by Roblox Economy APIs</p>
                            </div>
                            <div class="flex items-center gap-1.5 bg-slate-900/80 p-1 rounded-xl border border-slate-800">
                                <button onclick="setRevenueTimeframe('1d')" id="tf-1d" class="px-3 py-1.5 rounded-lg text-xs font-semibold text-amber-400 bg-amber-500/10 border border-amber-500/30">1D</button>
                                <button onclick="setRevenueTimeframe('7d')" id="tf-7d" class="px-3 py-1.5 rounded-lg text-xs font-semibold text-slate-400 hover:text-white">7D</button>
                                <button onclick="setRevenueTimeframe('30d')" id="tf-30d" class="px-3 py-1.5 rounded-lg text-xs font-semibold text-slate-400 hover:text-white">30D</button>
                                <button onclick="setRevenueTimeframe('1y')" id="tf-1y" class="px-3 py-1.5 rounded-lg text-xs font-semibold text-slate-400 hover:text-white">1Y</button>
                            </div>
                        </div>

                        <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
                            <div class="bg-slate-900/60 border border-slate-800 p-4 rounded-xl">
                                <span class="text-xs font-medium text-slate-400">Total Revenue</span>
                                <div class="text-2xl font-black text-amber-400 mt-1">R$ <span id="stat-total-rev">0</span></div>
                            </div>
                            <div class="bg-slate-900/60 border border-slate-800 p-4 rounded-xl">
                                <span class="text-xs font-medium text-slate-400">Sale of Goods</span>
                                <div class="text-2xl font-black text-emerald-400 mt-1">R$ <span id="stat-sale-goods">0</span></div>
                            </div>
                            <div class="bg-slate-900/60 border border-slate-800 p-4 rounded-xl">
                                <span class="text-xs font-medium text-slate-400">Pending Robux</span>
                                <div class="text-2xl font-black text-indigo-400 mt-1">R$ <span id="stat-pending">0</span></div>
                            </div>
                            <div class="bg-slate-900/60 border border-slate-800 p-4 rounded-xl">
                                <span class="text-xs font-medium text-slate-400">Commissions</span>
                                <div class="text-2xl font-black text-slate-300 mt-1">R$ <span id="stat-commissions">0</span></div>
                            </div>
                        </div>
                    </div>
                </div>

                <!-- TAB 2: PREMIUM SUBSCRIBERS -->
                <div id="tab-content-premium" class="tab-page hidden space-y-6">
                    <div class="glass-card rounded-2xl p-6">
                        <h3 class="text-base font-bold text-white mb-4">Active Subscriber Roster</h3>
                        
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
                                        <th class="p-3">User Profile</th>
                                        <th class="p-3">Discord Mention</th>
                                        <th class="p-3">Expires At</th>
                                        <th class="p-3 text-right">Action</th>
                                    </tr>
                                </thead>
                                <tbody id="premium-table-body" class="divide-y divide-slate-800/50"></tbody>
                            </table>
                        </div>
                    </div>
                </div>

                <!-- TAB 3: LAYOUT TICKET & DISCORD PANELS CONFIG -->
                <div id="tab-content-ticket" class="tab-page hidden space-y-6">
                    <div class="glass-card rounded-2xl p-6 space-y-6">
                        <div>
                            <h3 class="text-base font-bold text-white">Layout Ticket & Panel Controls</h3>
                            <p class="text-xs text-slate-400">Post account verification, unsync, or layout ticket panels directly to Discord channels.</p>
                        </div>

                        <!-- PANEL DISPATCH ACTIONS -->
                        <div class="grid grid-cols-1 sm:grid-cols-3 gap-3 p-4 bg-slate-900/60 border border-slate-800 rounded-xl">
                            <div>
                                <h4 class="text-xs font-bold text-white mb-1">Post Layout Ticket Panel</h4>
                                <p class="text-[10px] text-slate-400 mb-2">Sends purchase ticket to channel.</p>
                                <div class="flex gap-2">
                                    <input type="text" id="ticket-channel-id" placeholder="Channel ID" class="w-full bg-slate-900 border border-slate-700 rounded-lg px-2.5 py-1.5 text-xs text-white">
                                    <button type="button" onclick="triggerTicketPanel()" class="px-3 py-1.5 bg-amber-600 hover:bg-amber-500 text-white rounded-lg text-xs font-bold">Post</button>
                                </div>
                            </div>
                            <div>
                                <h4 class="text-xs font-bold text-white mb-1">Post Verification Panel</h4>
                                <p class="text-[10px] text-slate-400 mb-2">Sends /verification-set panel.</p>
                                <div class="flex gap-2">
                                    <input type="text" id="verify-channel-id" placeholder="Channel ID" class="w-full bg-slate-900 border border-slate-700 rounded-lg px-2.5 py-1.5 text-xs text-white">
                                    <button type="button" onclick="triggerVerificationPanel()" class="px-3 py-1.5 bg-indigo-600 hover:bg-indigo-500 text-white rounded-lg text-xs font-bold">Post</button>
                                </div>
                            </div>
                            <div>
                                <h4 class="text-xs font-bold text-white mb-1">Post Unsync Panel</h4>
                                <p class="text-[10px] text-slate-400 mb-2">Sends /un-sync-set panel.</p>
                                <div class="flex gap-2">
                                    <input type="text" id="unsync-channel-id" placeholder="Channel ID" class="w-full bg-slate-900 border border-slate-700 rounded-lg px-2.5 py-1.5 text-xs text-white">
                                    <button type="button" onclick="triggerUnsyncPanel()" class="px-3 py-1.5 bg-rose-600 hover:bg-rose-500 text-white rounded-lg text-xs font-bold">Post</button>
                                </div>
                            </div>
                        </div>

                        <form id="ticket-config-form" class="space-y-4 border-t border-slate-800 pt-4">
                            <div>
                                <label class="block text-xs font-semibold uppercase text-slate-400 mb-1">Ticket Title</label>
                                <input type="text" id="ticket-title" class="w-full bg-slate-900 border border-slate-800 rounded-xl px-3 py-2 text-xs text-white focus:border-amber-500">
                            </div>
                            <div>
                                <label class="block text-xs font-semibold uppercase text-slate-400 mb-1">Developer Product ID (Roblox)</label>
                                <input type="text" id="ticket-devid" class="w-full bg-slate-900 border border-slate-800 rounded-xl px-3 py-2 text-xs text-white focus:border-amber-500">
                            </div>
                            <div>
                                <label class="block text-xs font-semibold uppercase text-slate-400 mb-1">Instructions Text</label>
                                <textarea id="ticket-instructions" rows="3" class="w-full bg-slate-900 border border-slate-800 rounded-xl px-3 py-2 text-xs text-white focus:border-amber-500"></textarea>
                            </div>

                            <div class="border-t border-slate-800 pt-4 space-y-3">
                                <label class="block text-xs font-semibold uppercase text-slate-400">Step-by-Step Instruction Images</label>
                                
                                <div class="flex items-center gap-3">
                                    <input type="file" id="image-upload-input" accept="image/*" class="hidden" onchange="handleImageSelected(event)">
                                    <button type="button" onclick="document.getElementById('image-upload-input').click()" class="px-4 py-2 bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 rounded-xl text-xs font-semibold">
                                        + Add Image (Photo Library / Choose File)
                                    </button>
                                </div>

                                <div id="step-images-list" class="space-y-2"></div>
                            </div>

                            <button type="submit" class="gold-gradient text-slate-950 font-bold px-6 py-2.5 rounded-xl text-xs hover:opacity-90">
                                Save Ticket Configuration
                            </button>
                        </form>
                    </div>
                </div>

                <!-- TAB 4: ROBLOX OPEN CLOUD DEPLOYMENT -->
                <div id="tab-content-deploy" class="tab-page hidden space-y-6">
                    <div class="glass-card rounded-2xl p-6 space-y-4">
                        <h3 class="text-base font-bold text-white">Deploy Script via Open Cloud</h3>
                        <p class="text-xs text-slate-400">Paste Luau script to publish directly to your Roblox Universe place.</p>
                        
                        <div>
                            <label class="block text-xs font-semibold uppercase text-slate-400 mb-1">Luau Script</label>
                            <textarea id="deploy-lua-code" rows="8" placeholder="Paste script here..." class="w-full bg-slate-900 border border-slate-800 rounded-xl p-3 text-xs text-emerald-400 font-mono focus:outline-none focus:border-amber-500"></textarea>
                        </div>
                        <button onclick="deployScriptToRoblox()" class="gold-gradient text-slate-950 font-bold px-6 py-2.5 rounded-xl text-xs hover:opacity-90">
                            Publish Place Payload
                        </button>
                        <div id="deploy-status" class="text-xs font-semibold"></div>
                    </div>
                </div>

                <!-- TAB 5: AUDIT LOGS -->
                <div id="tab-content-logs" class="tab-page hidden space-y-6">
                    <div class="glass-card rounded-2xl p-6">
                        <h3 class="text-base font-bold text-white mb-4">Audit Feed</h3>
                        <div id="audit-feed" class="space-y-3 max-h-96 overflow-y-auto pr-1 text-xs"></div>
                    </div>
                </div>

            </main>

            <footer class="glass-card border-t border-slate-800 p-4 text-center text-xs text-slate-500">
                Template Engine Owner Master Panel - Unified Flask Architecture
            </footer>
        </div>
    </div>

    <script>
        let currentTF = '1d';
        let currentTab = 'analytics';
        let stepImages = [];

        function toggleSidebar() {
            const sidebar = document.getElementById('sidebar');
            const overlay = document.getElementById('sidebar-overlay');
            sidebar.classList.toggle('-translate-x-full');
            overlay.classList.toggle('hidden');
        }

        function switchTab(tab) {
            currentTab = tab;
            document.querySelectorAll('.tab-page').forEach(el => el.classList.add('hidden'));
            document.getElementById(`tab-content-${tab}`).classList.remove('hidden');

            const titleMap = {
                'analytics': 'Revenue Analytics',
                'premium': 'Subscriber Roster',
                'ticket': 'Ticket & Panel Config',
                'deploy': 'Open Cloud Deploy',
                'logs': 'System Audit Logs'
            };
            document.getElementById('tab-header-title').innerText = titleMap[tab] || 'Dashboard';

            ['analytics', 'premium', 'ticket', 'deploy', 'logs'].forEach(t => {
                const navBtn = document.getElementById(`nav-${t}`);
                if (t === tab) {
                    navBtn.className = "w-full flex items-center gap-3 px-3 py-2.5 rounded-xl text-xs font-semibold text-amber-400 bg-amber-500/10 border border-amber-500/30";
                } else {
                    navBtn.className = "w-full flex items-center gap-3 px-3 py-2.5 rounded-xl text-xs font-semibold text-slate-400 hover:text-white hover:bg-slate-800/50";
                }
            });

            if (window.innerWidth < 768) toggleSidebar();
            refreshActiveTab();
        }

        function refreshActiveTab() {
            if (currentTab === 'analytics') fetchRevenue();
            else if (currentTab === 'premium') fetchPremiumUsers();
            else if (currentTab === 'ticket') loadTicketConfig();
            else if (currentTab === 'logs') fetchAuditFeed();
        }

        async function checkAuth() {
            const res = await fetch('/api/auth-status');
            const data = await res.json();
            if (data.authenticated) {
                document.getElementById('login-modal').classList.add('hidden');
                document.getElementById('dashboard').classList.remove('hidden');
                refreshActiveTab();
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
                btn.className = (t === tf) ? "px-3 py-1.5 rounded-lg text-xs font-semibold text-amber-400 bg-amber-500/10 border border-amber-500/30" : "px-3 py-1.5 rounded-lg text-xs font-semibold text-slate-400 hover:text-white";
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
                document.getElementById('stat-total-rev').innerText = (data.total || data.totalRevenue || 0).toLocaleString();
                document.getElementById('stat-sale-goods').innerText = (data.sale_of_goods || data.saleOfGoods || 0).toLocaleString();
                document.getElementById('stat-pending').innerText = (data.pending_robux || data.pendingRobux || 0).toLocaleString();
                document.getElementById('stat-commissions').innerText = (data.commissions || 0).toLocaleString();
            } catch(e) { console.error(e); }
        }

        async function fetchPremiumUsers() {
            const res = await fetch('/api/premium-users');
            const list = await res.json();
            const tbody = document.getElementById('premium-table-body');
            tbody.innerHTML = '';
            list.forEach(user => {
                const tr = document.createElement('tr');
                tr.className = "hover:bg-slate-900/40 transition";
                const pfpUrl = `https://cdn.discordapp.com/embed/avatars/${parseInt(user.user_id) % 5}.png`;
                tr.innerHTML = `
                    <td class="p-3 font-semibold text-white flex items-center gap-2.5">
                        <img src="${pfpUrl}" class="w-6 h-6 rounded-full border border-slate-700">
                        <span>${user.username || 'User'}</span>
                    </td>
                    <td class="p-3 font-mono text-amber-400">&lt;@${user.user_id}&gt;</td>
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
        });

        async function revokePremium(user_id) {
            await fetch('/api/premium-users/remove', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ user_id })
            });
            fetchPremiumUsers();
        }

        async function triggerTicketPanel() {
            const channelId = document.getElementById('ticket-channel-id').value;
            if (!channelId) return alert("Please enter a Discord Channel ID.");
            const res = await fetch('/api/dispatch-panel', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ type: 'ticket', channel_id: channelId })
            });
            const data = await res.json();
            alert(data.message || (data.success ? "Ticket Panel posted." : "Failed to post panel."));
        }

        async function triggerVerificationPanel() {
            const channelId = document.getElementById('verify-channel-id').value;
            if (!channelId) return alert("Please enter a Discord Channel ID.");
            const res = await fetch('/api/dispatch-panel', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ type: 'verification', channel_id: channelId })
            });
            const data = await res.json();
            alert(data.message || (data.success ? "Verification Panel posted." : "Failed to post panel."));
        }

        async function triggerUnsyncPanel() {
            const channelId = document.getElementById('unsync-channel-id').value;
            if (!channelId) return alert("Please enter a Discord Channel ID.");
            const res = await fetch('/api/dispatch-panel', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ type: 'unsync', channel_id: channelId })
            });
            const data = await res.json();
            alert(data.message || (data.success ? "Unsync Panel posted." : "Failed to post panel."));
        }

        async function loadTicketConfig() {
            const res = await fetch('/api/ticket-config');
            const data = await res.json();
            document.getElementById('ticket-title').value = data.title || '';
            document.getElementById('ticket-devid').value = data.dev_product_id || '';
            document.getElementById('ticket-instructions').value = data.instructions || '';
            stepImages = data.step_images || [];
            renderStepImages();
        }

        function handleImageSelected(event) {
            const file = event.target.files[0];
            if (!file) return;
            const reader = new FileReader();
            reader.onload = function(e) {
                stepImages.push(e.target.result);
                renderStepImages();
            };
            reader.readAsDataURL(file);
        }

        function moveImage(index, dir) {
            const newIndex = index + dir;
            if (newIndex < 0 || newIndex >= stepImages.length) return;
            const temp = stepImages[index];
            stepImages[index] = stepImages[newIndex];
            stepImages[newIndex] = temp;
            renderStepImages();
        }

        function removeImage(index) {
            stepImages.splice(index, 1);
            renderStepImages();
        }

        function renderStepImages() {
            const container = document.getElementById('step-images-list');
            container.innerHTML = '';
            stepImages.forEach((imgUrl, i) => {
                const div = document.createElement('div');
                div.className = "flex items-center justify-between p-2.5 bg-slate-900 border border-slate-800 rounded-xl gap-3";
                div.innerHTML = `
                    <div class="flex items-center gap-3 min-w-0">
                        <span class="text-xs font-bold text-amber-400">Step ${i+1}</span>
                        <img src="${imgUrl}" class="w-12 h-12 object-cover rounded-lg border border-slate-700">
                    </div>
                    <div class="flex items-center gap-1.5">
                        <button type="button" onclick="moveImage(${i}, -1)" ${i===0?'disabled':''} class="px-2 py-1 text-xs bg-slate-800 rounded text-slate-300 disabled:opacity-30">Up</button>
                        <button type="button" onclick="moveImage(${i}, 1)" ${i===stepImages.length-1?'disabled':''} class="px-2 py-1 text-xs bg-slate-800 rounded text-slate-300 disabled:opacity-30">Down</button>
                        <button type="button" onclick="removeImage(${i})" class="px-2 py-1 text-xs bg-rose-500/20 text-rose-400 rounded">Delete</button>
                    </div>
                `;
                container.appendChild(div);
            });
        }

        document.getElementById('ticket-config-form').addEventListener('submit', async (e) => {
            e.preventDefault();
            const title = document.getElementById('ticket-title').value;
            const dev_product_id = document.getElementById('ticket-devid').value;
            const instructions = document.getElementById('ticket-instructions').value;

            await fetch('/api/ticket-config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ title, dev_product_id, instructions, step_images: stepImages })
            });

            alert("Ticket Configuration Saved.");
        });

        async function deployScriptToRoblox() {
            const code = document.getElementById('deploy-lua-code').value;
            const statusEl = document.getElementById('deploy-status');
            statusEl.innerText = "Publishing payload...";
            statusEl.className = "text-xs font-semibold text-amber-400";

            const res = await fetch('/api/deploy-script', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ script: code })
            });
            const data = await res.json();
            if (data.success) {
                statusEl.innerText = "Successfully published place script to Roblox.";
                statusEl.className = "text-xs font-semibold text-emerald-400";
            } else {
                statusEl.innerText = "Error: " + data.error;
                statusEl.className = "text-xs font-semibold text-rose-400";
            }
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

# Dispatch Panel Route (/verification-set, /un-sync-set, ticket panel)
@web_app.route("/api/dispatch-panel", methods=["POST"])
def dispatch_panel_route():
    if not is_authenticated(): return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json() or {}
    panel_type = data.get("type")
    channel_id = data.get("channel_id")

    if not channel_id or not panel_type:
        return jsonify({"success": False, "error": "Missing panel type or channel_id"}), 400

    if bot_cog_ref and bot.loop:
        asyncio.run_coroutine_threadsafe(
            bot_cog_ref.dispatch_panel_to_channel(panel_type, channel_id),
            bot.loop
        )
        log_system_event("Dispatched Panel", f"Owner dispatched {panel_type} panel to channel {channel_id}.")
        return jsonify({"success": True, "message": f"Dispatched {panel_type} panel to channel {channel_id}."})
    return jsonify({"success": False, "error": "Bot loop not active"}), 503

# Revenue Analytics Endpoint (Roblox API)
@web_app.route("/api/revenue", methods=["GET"])
def api_revenue():
    if not is_authenticated():
        return jsonify({"error": "Unauthorized"}), 401

    timeframe = request.args.get("timeframe", "1d").lower()
    timeframe_map = {"1d": "Day", "7d": "Week", "30d": "Month", "1y": "Year", "365d": "Year"}
    tf_param = timeframe_map.get(timeframe, "Day")

    headers = {}
    if ROBLOX_COOKIE:
        clean_cookie = ROBLOX_COOKIE if ROBLOX_COOKIE.startswith(".ROBLOSECURITY=") else f".ROBLOSECURITY={ROBLOX_COOKIE}"
        headers["Cookie"] = clean_cookie
    if ROBLOX_OPEN_CLOUD_KEY:
        headers["x-api-key"] = ROBLOX_OPEN_CLOUD_KEY

    urls = []
    if ROBLOX_GROUP_ID:
        urls.append(f"https://economy.roblox.com/v1/groups/{ROBLOX_GROUP_ID}/revenue/summary/{tf_param}")
    if ROBLOX_UNIVERSE_ID:
        urls.append(f"https://apis.roblox.com/universes/v1/{ROBLOX_UNIVERSE_ID}/economy/revenue/summary/{tf_param}")

    for url in urls:
        try:
            resp = requests.get(url, headers=headers, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                return jsonify({
                    "timeframe": timeframe,
                    "sale_of_goods": data.get("itemSaleRevenue", data.get("itemSale", 0)),
                    "advance_rebates": data.get("publishingAdvanceRebates", 0),
                    "commissions": data.get("commissionRevenue", data.get("comission", 0)),
                    "total": data.get("totalRevenue", data.get("total", 0)),
                    "pending_robux": data.get("pendingRobux", 0)
                })
        except Exception as e:
            print(f"[Revenue API Fetch Warning] {e}")

    return jsonify({
        "timeframe": timeframe,
        "sale_of_goods": 0,
        "advance_rebates": 0,
        "commissions": 0,
        "total": 0,
        "pending_robux": 0
    })

# Premium Users Management Endpoint
@web_app.route("/api/premium-users", methods=["GET"])
def get_premium_users():
    if not is_authenticated(): return jsonify({"error": "Unauthorized"}), 401
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
    if not is_authenticated(): return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json() or {}
    user_id, username = data.get("user_id"), data.get("username", "User")
    days = int(data.get("days") or 30)

    if not user_id: return jsonify({"error": "Missing user_id"}), 400

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
    if not is_authenticated(): return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json() or {}
    user_id = str(data.get("user_id"))

    if db_premium is not None:
        db_premium.delete_one({"user_id": user_id})
    else:
        global LOCAL_PREMIUM_USERS
        LOCAL_PREMIUM_USERS = [u for u in LOCAL_PREMIUM_USERS if u.get("user_id") != user_id]

    log_system_event("Revoked Premium", f"Revoked Premium from Discord User ID {user_id}.")
    return jsonify({"success": True})

# Ticket Config Endpoint
@web_app.route("/api/ticket-config", methods=["GET", "POST"])
def manage_ticket_config():
    if not is_authenticated(): return jsonify({"error": "Unauthorized"}), 401
    global LOCAL_TICKET_CONFIG

    if request.method == "POST":
        data = request.get_json() or {}
        if db_layout_ticket_config is not None:
            db_layout_ticket_config.update_one({"config_id": "global"}, {"$set": data}, upsert=True)
        else:
            LOCAL_TICKET_CONFIG = data
        log_system_event("Updated Ticket Config", "Owner updated layout ticket and step-by-step images.")
        return jsonify({"success": True})

    if db_layout_ticket_config is not None:
        cfg = db_layout_ticket_config.find_one({"config_id": "global"}) or LOCAL_TICKET_CONFIG
        cfg.pop("_id", None)
        return jsonify(cfg)
    return jsonify(LOCAL_TICKET_CONFIG)

@web_app.route("/api/audit-logs", methods=["GET"])
def get_audit_logs():
    if not is_authenticated(): return jsonify({"error": "Unauthorized"}), 401
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
    user_id = str(data.get("user_id"))
    target_server_id = data.get("target_server_id")
    blueprint_url = data.get("blueprint_url")
    blueprint_data = data.get("blueprint_data")
    log_channel_id = data.get("log_channel_id", "1563544108696477802")

    if not target_server_id or not blueprint_data:
        return jsonify({"success": False, "error": "Missing target_server_id or blueprint_data"}), 400

    # CHECK PREMIUM AUTO-BUILD STATUS
    if is_user_premium(user_id):
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
            log_system_event("Premium Auto-Build Queued", f"User {user_id} triggered premium auto-build for server {target_server_id}.")
            return jsonify({"success": True, "message": "Premium user detected. Direct handshake build queued immediately."}), 200

    # NON-PREMIUM USER: SAVE TO PENDING BLUEPRINTS QUEUE
    pending_entry = {
        "user_id": user_id,
        "target_server_id": target_server_id,
        "blueprint_url": blueprint_url,
        "blueprint_data": blueprint_data,
        "log_channel_id": log_channel_id,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    if db_pending_blueprints is not None:
        db_pending_blueprints.update_one({"user_id": user_id}, {"$set": pending_entry}, upsert=True)
    log_system_event("Pending Design Saved", f"Non-premium user {user_id} submitted design. Queued for dev product purchase verification.")

    return jsonify({
        "success": True,
        "status": "pending_payment",
        "message": "Design saved to database queue. Purchase developer product via ticket channel to complete deployment."
    }), 200

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
        print(f"Successfully registered and synced {len(synced)} global slash command(s).")
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
