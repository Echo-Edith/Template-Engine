import os
import re
import json
import random
import requests
from datetime import datetime, timezone, timedelta
from flask import Flask, redirect, request, session, render_template, jsonify
from pymongo import MongoClient

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")

app = Flask(__name__, template_folder=TEMPLATE_DIR)
app.secret_key = (os.getenv("FLASK_SECRET_KEY") or "super-secret-key-fallback-12345").strip()

# Enforce long-term persistent browser sessions (365 Days)
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=365)
app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

application = app
handler = app

BLUEPRINTS_DIR = "/tmp/blueprints" if os.getenv("VERCEL") else os.path.join(os.getcwd(), "blueprints")
os.makedirs(BLUEPRINTS_DIR, exist_ok=True)

# Discord Configuration
CLIENT_ID = (os.getenv("DISCORD_CLIENT_ID") or "").strip()
CLIENT_SECRET = (os.getenv("DISCORD_CLIENT_SECRET") or "").strip()
BOT_TOKEN = (os.getenv("DISCORD_BOT_TOKEN") or os.getenv("BOT_TOKEN") or "").strip()

# Roblox Integration Configuration
ROBLOX_OPEN_CLOUD_KEY = (os.getenv("ROBLOX_OPEN_CLOUD_KEY") or "").strip()
ROBLOX_SUBSCRIPTION_ID = (os.getenv("SUBSCRIPTION_ID") or "EXP-4378714879071224384").strip()
ROBLOX_PLACE_ID = (os.getenv("ROBLOX_PLACE_ID") or "6094950399").strip()
ROBLOX_STORE_URL = (os.getenv("ROBLOX_STORE_URL") or f"https://www.roblox.com/games/{ROBLOX_PLACE_ID}/store").strip()

# Target Channels
PREMIUM_DESIGN_CHANNEL_ID = "1563544108696477802"

MONGO_URI = os.getenv("MONGO_URI")
DISCORD_API_BASE_URL = "https://discord.com/api/v10"

db_bans = None
db_premium = None
db_users = None
db_stats = None

if MONGO_URI:
    try:
        mongo_client = MongoClient(MONGO_URI)
        try:
            db = mongo_client.get_default_database()
        except Exception:
            db = mongo_client.get_database("static_studio_db")
        db_bans = db["bans"]
        db_premium = db["premium_users"]
        db_users = db["persistent_users"]
        db_stats = db["stats"]
    except Exception as e:
        print(f"[MongoDB Warning] Could not connect to Mongo: {e}")

WEBSITE_BANS = {}
LOCAL_PREMIUM_USERS = {}
LOCAL_PERSISTENT_USERS = {}

@app.before_request
def make_session_permanent():
    session.permanent = True

def save_user_session_db(discord_id: str, discord_data: dict, roblox_data: dict = None):
    if not discord_id:
        return
    discord_id_str = str(discord_id)
    now = datetime.now(timezone.utc).isoformat()

    existing = load_user_session_db(discord_id_str)
    
    update_payload = {
        "discord_id": discord_id_str,
        "discord_data": discord_data or existing.get("discord_data", {}),
        "updated_at": now
    }
    
    if roblox_data is not None:
        update_payload["roblox_data"] = roblox_data
    elif "roblox_data" in existing:
        update_payload["roblox_data"] = existing["roblox_data"]

    if db_users is not None:
        try:
            db_users.update_one(
                {"discord_id": discord_id_str},
                {"$set": update_payload},
                upsert=True
            )
        except Exception as e:
            print(f"[User Persistence Error] {e}")
    else:
        LOCAL_PERSISTENT_USERS[discord_id_str] = update_payload

def load_user_session_db(discord_id: str) -> dict:
    if not discord_id:
        return {}
    discord_id_str = str(discord_id)

    if db_users is not None:
        try:
            return db_users.find_one({"discord_id": discord_id_str}) or {}
        except Exception as e:
            print(f"[Load User Error] {e}")
            return {}
    return LOCAL_PERSISTENT_USERS.get(discord_id_str, {})

def is_user_premium(user_id: str) -> bool:
    if not user_id:
        return False
    user_id_str = str(user_id)
    now = datetime.now(timezone.utc)

    if db_premium is not None:
        try:
            doc = db_premium.find_one({"user_id": user_id_str})
            if doc and "expires_at" in doc:
                expires_at = doc["expires_at"]
                if isinstance(expires_at, str):
                    expires_at = datetime.fromisoformat(expires_at)
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                return expires_at > now
        except Exception as e:
            print(f"[Premium Check Error] {e}")
            return False
        return False
    else:
        exp = LOCAL_PREMIUM_USERS.get(user_id_str)
        return exp > now if exp else False

def calculate_account_age_days(discord_id: str) -> str:
    try:
        snowflake = int(discord_id)
        timestamp_ms = (snowflake >> 22) + 1420070400000
        created_at = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc)
        now = datetime.now(timezone.utc)
        days_old = (now - created_at).days
        return f"{days_old} days"
    except Exception:
        return "Unknown"

def is_user_banned(user_id):
    if not user_id:
        return None
    user_id_str = str(user_id)
    if db_bans is not None:
        try:
            return db_bans.find_one({"user_id": user_id_str, "location": "website"})
        except Exception:
            return None
    return WEBSITE_BANS.get(user_id_str)

def send_bot_embed_file_log(channel_id: str, embed_payload: dict, json_payload: dict) -> bool:
    if not BOT_TOKEN:
        print("[Bot Log Error] BOT_TOKEN missing")
        return False

    url = f"{DISCORD_API_BASE_URL}/channels/{channel_id}/messages"
    headers = {"Authorization": f"Bot {BOT_TOKEN}"}

    file_bytes = json.dumps(json_payload, indent=2).encode("utf-8")
    payload_json_str = json.dumps({"embeds": [embed_payload]})

    files = {
        "payload_json": (None, payload_json_str, "application/json"),
        "file": ("blueprint.json", file_bytes, "application/json")
    }

    try:
        res = requests.post(url, headers=headers, files=files, timeout=10)
        return res.status_code in (200, 201)
    except Exception as e:
        print(f"[Discord API Log Exception] {e}")

    return False

@app.route("/")
def home():
    try:
        user = session.get("user")
        roblox_user = session.get("roblox_user")

        if session.get("user_id"):
            db_record = load_user_session_db(session["user_id"])
            if db_record:
                user = user or db_record.get("discord_data")
                roblox_user = roblox_user or db_record.get("roblox_data")
                if user:
                    session["user"] = user
                if roblox_user and roblox_user.get("username"):
                    session["roblox_user"] = roblox_user

        user_is_premium = False
        if user:
            user_is_premium = is_user_premium(user.get("id"))
            ban_info = is_user_banned(user.get("id"))
            if ban_info:
                return render_template(
                    "index.html",
                    is_banned=True,
                    user=user,
                    roblox_user=roblox_user,
                    is_premium=user_is_premium,
                    dev_message=ban_info.get("message_from_dev", "Suspended by developer."),
                    discord_client_id=CLIENT_ID,
                    roblox_store_url=ROBLOX_STORE_URL
                )

        return render_template(
            "index.html",
            is_banned=False,
            user=user,
            roblox_user=roblox_user,
            is_premium=user_is_premium,
            discord_client_id=CLIENT_ID,
            roblox_store_url=ROBLOX_STORE_URL
        )
    except Exception as e:
        return f"Template Engine Error: {str(e)}", 500

@app.route("/generate-blueprint", methods=["POST"])
def generate_blueprint_endpoint():
    try:
        payload = request.get_json(silent=True, force=True) or {}
        prompt = (payload.get("prompt") or "").strip()
        server_id = (payload.get("server_id") or "").strip()

        if not prompt or not server_id:
            return jsonify({"success": False, "error": "Prompt and Server ID are required."}), 400

        prompt_lower = prompt.lower()
        
        # Build base dynamic categories based on prompt context
        categories = [
            {
                "name": "INFORMATION",
                "emoji": "📌",
                "permissions": {},
                "channels": [
                    {"name": "welcome", "emoji": "👋", "type": "text", "topic": "Welcome to the server!", "read_only": False, "permissions": {}},
                    {"name": "rules", "emoji": "📜", "type": "text", "topic": "Server rules and guidelines", "read_only": True, "permissions": {}},
                    {"name": "announcements", "emoji": "📢", "type": "text", "topic": "Official announcements", "read_only": True, "permissions": {}}
                ]
            },
            {
                "name": "COMMUNITY HUB",
                "emoji": "💬",
                "permissions": {},
                "channels": [
                    {"name": "general", "emoji": "💬", "type": "text", "topic": "General discussion", "read_only": False, "permissions": {}},
                    {"name": "media", "emoji": "📷", "type": "text", "topic": "Share photos, videos, and art", "read_only": False, "permissions": {}},
                    {"name": "bot-commands", "emoji": "🤖", "type": "text", "topic": "Bot interactions", "read_only": False, "permissions": {}}
                ]
            }
        ]

        # Add specialized categories according to prompt keywords
        if any(w in prompt_lower for w in ["gaming", "game", "roblox", "esports", "play"]):
            categories.append({
                "name": "GAMING ZONE",
                "emoji": "🎮",
                "permissions": {},
                "channels": [
                    {"name": "game-chat", "emoji": "🎮", "type": "text", "topic": "Gaming discussions", "read_only": False, "permissions": {}},
                    {"name": "clips-and-highlights", "emoji": "🎬", "type": "text", "topic": "Share your best gameplay moments", "read_only": False, "permissions": {}},
                    {"name": "Squad 1", "emoji": "🔊", "type": "voice", "topic": "", "read_only": False, "permissions": {}},
                    {"name": "Squad 2", "emoji": "🔊", "type": "voice", "topic": "", "read_only": False, "permissions": {}}
                ]
            })

        if any(w in prompt_lower for w in ["trading", "shop", "market", "store", "crypto", "business"]):
            categories.append({
                "name": "MARKETPLACE",
                "emoji": "🛒",
                "permissions": {},
                "channels": [
                    {"name": "deals-and-offers", "emoji": "🏷️", "type": "text", "topic": "Latest store deals", "read_only": False, "permissions": {}},
                    {"name": "vouch-reviews", "emoji": "⭐", "type": "text", "topic": "Customer vouches and reviews", "read_only": False, "permissions": {}}
                ]
            })

        # Voice lounge
        categories.append({
            "name": "VOICE CHANNELS",
            "emoji": "🔊",
            "permissions": {},
            "channels": [
                {"name": "General Lounge", "emoji": "🔊", "type": "voice", "topic": "", "read_only": False, "permissions": {}},
                {"name": "Chilling Room", "emoji": "☕", "type": "voice", "topic": "", "read_only": False, "permissions": {}}
            ]
        })

        # Staff administration category
        categories.append({
            "name": "STAFF ONLY",
            "emoji": "🛡️",
            "permissions": {},
            "channels": [
                {"name": "staff-chat", "emoji": "🔒", "type": "text", "topic": "Private staff discussion", "read_only": False, "permissions": {}},
                {"name": "audit-logs", "emoji": "📝", "type": "text", "topic": "Moderation and audit logs", "read_only": True, "permissions": {}}
            ]
        })

        # Generate custom roles matching prompt scope
        roles = [
            {"name": "Owner", "color": "#f1c40f"},
            {"name": "Admin", "color": "#e74c3c"},
            {"name": "Moderator", "color": "#3498db"},
            {"name": "VIP", "color": "#9b59b6"},
            {"name": "Member", "color": "#2ecc71"}
        ]

        # Extract title from prompt or fallback
        words = [w.capitalize() for w in prompt.split()[:3]]
        server_name = " ".join(words) + " Community" if words else "Custom Generated Server"

        blueprint = {
            "server_name": server_name,
            "roles": roles,
            "categories": categories
        }

        return jsonify({"success": True, "blueprint": blueprint}), 200

    except Exception as e:
        print(f"[AI Generation Exception] {e}")
        return jsonify({"success": False, "error": f"Generation Failed: {str(e)}"}), 500

        embed = {
            "title": "TE Premium Design Dispatched",
            "color": 0xF1C40F if has_premium else 0x3b82f6,
            "fields": [
                {"name": "User Mention", "value": f"<@{user_id}>", "inline": True},
                {"name": "User ID", "value": f"`{user_id}`", "inline": True},
                {"name": "Account Age", "value": f"`{account_age}`", "inline": True},
                {"name": "Target Guild ID", "value": f"`{target_server_id}`", "inline": True},
                {"name": "Invite Link", "value": f"`{invite_link}`", "inline": True},
                {"name": "Server Overview", "value": overview_str, "inline": False},
                {"name": "Prompt Description", "value": f"```\n{prompt_desc}\n```", "inline": False}
            ],
            "footer": {"text": "Template Engine Auto-Build Infrastructure"},
            "timestamp": datetime.now(timezone.utc).isoformat()
        }

        full_payload = {
            "submitting_user_id": user_id,
            "target_server_id": target_server_id,
            "server_name": server_name,
            "invite_link": invite_link,
            "prompt": prompt_desc,
            "layout": payload.get("layout", {})
        }

        send_bot_embed_file_log(PREMIUM_DESIGN_CHANNEL_ID, embed, full_payload)

        return jsonify({
            "success": True,
            "is_premium": has_premium,
            "message": "Design blueprint successfully dispatched."
        }), 200

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/login")
def login():
    if not CLIENT_ID:
        return "Error: DISCORD_CLIENT_ID is not configured.", 500

    configured = os.getenv("DISCORD_REDIRECT_URI")
    redirect_uri = configured.strip() if configured and configured.strip() else f"{request.host_url.rstrip('/')}/callback"

    discord_auth_url = (
        f"{DISCORD_API_BASE_URL}/oauth2/authorize"
        f"?client_id={CLIENT_ID}"
        f"&redirect_uri={requests.utils.quote(redirect_uri, safe='')}"
        f"&response_type=code"
        f"&scope=identify%20email"
    )
    return redirect(discord_auth_url)

@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code:
        return redirect("/")

    configured = os.getenv("DISCORD_REDIRECT_URI")
    redirect_uri = configured.strip() if configured and configured.strip() else f"{request.host_url.rstrip('/')}/callback"

    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    try:
        token_response = requests.post(f"{DISCORD_API_BASE_URL}/oauth2/token", data=data, headers=headers)
        if token_response.status_code != 200:
            return f"OAuth Error ({token_response.status_code}): {token_response.text}", 400

        tokens = token_response.json()
        access_token = tokens.get("access_token")

        user_headers = {"Authorization": f"Bearer {access_token}"}
        user_response = requests.get(f"{DISCORD_API_BASE_URL}/users/@me", headers=user_headers)

        if user_response.status_code == 200:
            user_data = user_response.json()
            avatar_hash = user_data.get("avatar")
            avatar_url = f"https://cdn.discordapp.com/avatars/{user_data['id']}/{avatar_hash}.png" if avatar_hash else "https://cdn.discordapp.com/embed/avatars/0.png"

            user_obj = {
                "id": user_data["id"],
                "username": user_data["username"],
                "discriminator": user_data.get("discriminator", "0"),
                "avatar": avatar_url
            }

            session["user"] = user_obj
            session["user_id"] = user_data["id"]

            db_record = load_user_session_db(user_data["id"])
            if db_record.get("roblox_data"):
                session["roblox_user"] = db_record["roblox_data"]

            save_user_session_db(user_data["id"], user_obj, session.get("roblox_user"))

    except Exception as e:
        print(f"[OAuth Callback Error] {e}")

    return redirect("/")

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

@app.route("/health")
def health():
    return jsonify({"status": "alive"}), 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
