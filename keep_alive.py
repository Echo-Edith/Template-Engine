import os
import re
import json
import random
import requests
import asyncio
from datetime import datetime, timezone, timedelta
from flask import Flask, redirect, request, session, render_template, jsonify
from pymongo import MongoClient

# Attempt to import ai_brain for Gemini AI generation
try:
    import ai_brain
except ImportError:
    ai_brain = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")

app = Flask(__name__, template_folder=TEMPLATE_DIR)
app.secret_key = (os.getenv("FLASK_SECRET_KEY") or "super-secret-key-fallback-12345").strip()

# Enforce persistent browser sessions (365 Days)
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

RECAPTCHA_SECRET_KEY = (os.getenv("RECAPTCHA_SECRET_KEY") or "").strip()
RECAPTCHA_SITE_KEY = (os.getenv("RECAPTCHA_SITE_KEY") or "").strip()

# Roblox Integration Configuration
ROBLOX_OPEN_CLOUD_KEY = (os.getenv("ROBLOX_OPEN_CLOUD_KEY") or "").strip()
ROBLOX_SUBSCRIPTION_ID = (os.getenv("SUBSCRIPTION_ID") or "EXP-4378714879071224384").strip()
ROBLOX_PLACE_ID = (os.getenv("ROBLOX_PLACE_ID") or "6094950399").strip()
ROBLOX_STORE_URL = (os.getenv("ROBLOX_STORE_URL") or f"https://www.roblox.com/games/{ROBLOX_PLACE_ID}/store").strip()

# Target Channels
SUBSCRIPTION_LOG_CHANNEL_ID = (os.getenv("SUBSCRIPTION_LOG_CHANNEL_ID") or "1564262413514182739").strip()
DESIGN_CHANNEL_ID = (os.getenv("DESIGN_CHANNEL_ID") or "1563524101522464900").strip()
PREMIUM_DESIGN_CHANNEL_ID = (os.getenv("PREMIUM_DESIGN_CHANNEL_ID") or "1563544108696477802").strip()

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


def calculate_account_age(discord_id: str) -> str:
    try:
        snowflake = int(discord_id)
        timestamp_ms = (snowflake >> 22) + 1420070400000
        created_at = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc)
        now = datetime.now(timezone.utc)

        days_old = (now - created_at).days
        if days_old >= 365:
            years = days_old // 365
            rem_days = days_old % 365
            return f"{years} yr{'' if years == 1 else 's'}, {rem_days} day{'' if rem_days == 1 else 's'} ({days_old} days total)"
        return f"{days_old} day{'' if days_old == 1 else 's'}"
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
                    recaptcha_site_key="",
                    discord_client_id=CLIENT_ID,
                    roblox_store_url=ROBLOX_STORE_URL
                )

        return render_template(
            "index.html",
            is_banned=False,
            user=user,
            roblox_user=roblox_user,
            is_premium=user_is_premium,
            recaptcha_site_key="",
            discord_client_id=CLIENT_ID,
            roblox_store_url=ROBLOX_STORE_URL
        )
    except Exception as e:
        return f"Template Engine Error: {str(e)}", 500


# --- SUBMIT DESIGN DISPATCH ---
@app.route("/api/submit-design", methods=["POST"])
def submit_design_endpoint():
    try:
        user = session.get("user")
        if not user:
            return jsonify({"success": False, "error": "Authentication required to submit design."}), 401

        payload = request.get_json(silent=True, force=True) or {}
        user_id = str(user.get("id"))
        has_premium = is_user_premium(user_id)

        target_server_id = str(payload.get("target_server_id", "0")).strip()
        server_name = payload.get("server_name", "Custom Server")
        categories_count = payload.get("categories_count", 0)
        channels_count = payload.get("channels_count", 0)
        roles_count = payload.get("roles_count", 0)
        invite_link = payload.get("invite_link", "N/A")
        prompt_val = payload.get("prompt", "No prompt provided.")
        layout_data = payload.get("layout", {})

        account_age = calculate_account_age(user_id)

        # Formatted Embed UI (No plain text content)
        embed = {
            "title": "TE Premium Server Layout Submission" if has_premium else "Standard Server Layout Submission",
            "color": 0xF1C40F if has_premium else 0x3B82F6,
            "fields": [
                {"name": "User Mention", "value": f"<@{user_id}>", "inline": True},
                {"name": "User ID", "value": f"`{user_id}`", "inline": True},
                {"name": "Account Age", "value": f"`{account_age}`", "inline": True},
                {"name": "Target Guild ID", "value": f"`{target_server_id}`", "inline": True},
                {"name": "Server Invite Link", "value": invite_link, "inline": True},
                {"name": "Server Overview", "value": f"Categories: `{categories_count}` | Channels: `{channels_count}` | Roles: `{roles_count}`", "inline": False},
                {"name": "Prompt Description", "value": prompt_val[:1024], "inline": False}
            ],
            "footer": {"text": "Template Engine Auto-Build Infrastructure"},
            "timestamp": datetime.now(timezone.utc).isoformat()
        }

        target_channel = PREMIUM_DESIGN_CHANNEL_ID if has_premium else DESIGN_CHANNEL_ID

        # Prepare JSON File payload for the bot listener
        file_payload = {
            "submitting_user_id": user_id,
            "target_server_id": target_server_id,
            "server_name": server_name,
            "prompt": prompt_val,
            "layout": layout_data
        }
        json_file_bytes = json.dumps(file_payload, indent=2).encode('utf-8')

        if BOT_TOKEN:
            url = f"{DISCORD_API_BASE_URL}/channels/{target_channel}/messages"
            headers = {"Authorization": f"Bot {BOT_TOKEN}"}
            
            # Send multipart form data with JSON Embed payload + JSON File attachment
            files = {
                'payload_json': (None, json.dumps({'embeds': [embed]}), 'application/json'),
                'files[0]': ('blueprint.json', json_file_bytes, 'application/json')
            }
            try:
                res = requests.post(url, headers=headers, files=files, timeout=10)
                if res.status_code not in (200, 201):
                    print(f"[Log Dispatch Fail] Status Code: {res.status_code}, Response: {res.text}")
            except Exception as req_err:
                print(f"[Log Dispatch Error] {req_err}")

        return jsonify({
            "success": True,
            "is_premium": has_premium,
            "message": "Design blueprint successfully dispatched."
        }), 200

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/generate-blueprint", methods=["POST"])
def generate_blueprint_endpoint():
    try:
        data = request.get_json(silent=True, force=True) or {}
        prompt = (data.get("prompt") or "").strip()
        server_id = str(data.get("server_id") or "").strip()
        server_link = str(data.get("server_link") or "").strip()

        if not prompt:
            return jsonify({"success": False, "error": "Prompt cannot be empty."}), 400

        user = session.get("user") or {}

        # 1. Use ai_brain.py if available and GEMINI_API_KEY is configured
        if ai_brain and os.getenv("GEMINI_API_KEY"):
            try:
                # Execute async generate_server_layout function from ai_brain.py
                ai_result = asyncio.run(
                    ai_brain.generate_server_layout(
                        prompt=prompt,
                        channel_separator="│",
                        server_id=server_id,
                        server_link=server_link,
                        user_info=user if user.get("id") else None,
                        webhook_url=None
                    )
                )

                if ai_result.get("success") and ai_result.get("data"):
                    blueprint_data = ai_result["data"]
                    return jsonify({"success": True, "blueprint": blueprint_data}), 200
                elif ai_result.get("error"):
                    print(f"[ai_brain Warning] {ai_result['error']}")
            except Exception as ai_err:
                print(f"[ai_brain Exception] {ai_err}")

        # 2. Dynamic Keyword Fallback (Runs if ai_brain or GEMINI_API_KEY is missing/fails)
        prompt_lower = prompt.lower()
        roles = [
            {"name": "Owner", "color": "#f1c40f"},
            {"name": "Admin", "color": "#e74c3c"},
            {"name": "Moderator", "color": "#e67e22"},
            {"name": "VIP", "color": "#9b59b6"},
            {"name": "Member", "color": "#2ecc71"}
        ]

        categories = [
            {
                "name": "INFORMATION",
                "emoji": "📌",
                "permissions": {},
                "channels": [
                    {"name": "welcome", "emoji": "👋", "type": "text", "topic": "Welcome to the server!", "read_only": True, "permissions": {"view": True, "send": False, "embed": True, "attach": True}},
                    {"name": "rules", "emoji": "📜", "type": "text", "topic": "Server rules and guidelines.", "read_only": True, "permissions": {"view": True, "send": False, "embed": True, "attach": True}},
                    {"name": "announcements", "emoji": "📢", "type": "text", "topic": "Official announcements.", "read_only": True, "permissions": {"view": True, "send": False, "embed": True, "attach": True}}
                ]
            },
            {
                "name": "COMMUNITY HUB",
                "emoji": "💬",
                "permissions": {},
                "channels": [
                    {"name": "general-chat", "emoji": "💬", "type": "text", "topic": "Main general discussion.", "read_only": False, "permissions": {"view": True, "send": True, "embed": True, "attach": True}},
                    {"name": "bot-commands", "emoji": "🤖", "type": "text", "topic": "Bot commands channel.", "read_only": False, "permissions": {"view": True, "send": True, "embed": True, "attach": True}},
                    {"name": "media-gallery", "emoji": "📸", "type": "text", "topic": "Share photos and media.", "read_only": False, "permissions": {"view": True, "send": True, "embed": True, "attach": True}}
                ]
            }
        ]

        if any(w in prompt_lower for w in ["game", "gaming", "esports", "roblox", "clan"]):
            categories.append({
                "name": "GAMING ZONE",
                "emoji": "🎮",
                "permissions": {},
                "channels": [
                    {"name": "game-chat", "emoji": "🎮", "type": "text", "topic": "Gaming discussions & clips.", "read_only": False, "permissions": {"view": True, "send": True, "embed": True, "attach": True}},
                    {"name": "lfg-squads", "emoji": "👥", "type": "text", "topic": "Find teammates here.", "read_only": False, "permissions": {"view": True, "send": True, "embed": True, "attach": True}},
                    {"name": "Squad Voice 1", "emoji": "🔊", "type": "voice", "topic": "", "read_only": False, "permissions": {"view": True, "send": True, "embed": True, "attach": True}},
                    {"name": "Squad Voice 2", "emoji": "🔊", "type": "voice", "topic": "", "read_only": False, "permissions": {"view": True, "send": True, "embed": True, "attach": True}}
                ]
            })

        if any(w in prompt_lower for w in ["staff", "admin", "mod", "vip", "support", "ticket"]):
            categories.append({
                "name": "STAFF HEADQUARTERS",
                "emoji": "🛡️",
                "permissions": {},
                "channels": [
                    {"name": "staff-chat", "emoji": "💬", "type": "text", "topic": "Staff internal channel.", "read_only": False, "permissions": {"view": True, "send": True, "embed": True, "attach": True}},
                    {"name": "mod-logs", "emoji": "📋", "type": "text", "topic": "Action logs.", "read_only": True, "permissions": {"view": True, "send": False, "embed": True, "attach": True}},
                    {"name": "Staff Meeting Voice", "emoji": "🔊", "type": "voice", "topic": "", "read_only": False, "permissions": {"view": True, "send": True, "embed": True, "attach": True}}
                ]
            })

        categories.append({
            "name": "VOICE CHANNELS",
            "emoji": "🔊",
            "permissions": {},
            "channels": [
                {"name": "General Lounge", "emoji": "🔊", "type": "voice", "topic": "", "read_only": False, "permissions": {"view": True, "send": True, "embed": True, "attach": True}},
                {"name": "AFK Quiet Room", "emoji": "💤", "type": "voice", "topic": "", "read_only": False, "permissions": {"view": True, "send": True, "embed": True, "attach": True}}
            ]
        })

        blueprint = {
            "server_name": prompt.title()[:30] + " Community",
            "roles": roles,
            "categories": categories
        }

        return jsonify({"success": True, "blueprint": blueprint}), 200

    except Exception as e:
        return jsonify({"success": False, "error": f"Failed generating blueprint: {str(e)}"}), 500


# --- DISCORD OAUTH2 LOGIN & CALLBACK ---
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
