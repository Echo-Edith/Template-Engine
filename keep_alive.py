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

RECAPTCHA_SECRET_KEY = (os.getenv("RECAPTCHA_SECRET_KEY") or "").strip()
RECAPTCHA_SITE_KEY = (os.getenv("RECAPTCHA_SITE_KEY") or "").strip()

# Roblox Integration Configuration
ROBLOX_OPEN_CLOUD_KEY = (os.getenv("ROBLOX_OPEN_CLOUD_KEY") or "").strip()
ROBLOX_SUBSCRIPTION_ID = (os.getenv("SUBSCRIPTION_ID") or "EXP-4378714879071224384").strip()
ROBLOX_PLACE_ID = (os.getenv("ROBLOX_PLACE_ID") or "6094950399").strip()
ROBLOX_STORE_URL = (os.getenv("ROBLOX_STORE_URL") or f"https://www.roblox.com/games/{ROBLOX_PLACE_ID}/store").strip()

# Target Channels & Webhooks
SUBSCRIPTION_LOG_CHANNEL_ID = (os.getenv("SUBSCRIPTION_LOG_CHANNEL_ID") or "1564262413514182739").strip()
DESIGN_CHANNEL_ID = (os.getenv("DESIGN_CHANNEL_ID") or "1563544108696477802").strip()
PREMIUM_DESIGN_CHANNEL_ID = (os.getenv("PREMIUM_DESIGN_CHANNEL_ID") or "1563544108696477802").strip()

SYSTEM_LOG_WEBHOOK_URL = (os.getenv("SYSTEM_LOG_WEBHOOK_URL") or "").strip()
WEBSITE_WEBHOOK_URL = (os.getenv("WEBSITE_WEBHOOK_URL") or SYSTEM_LOG_WEBHOOK_URL).strip()
DESIGN_WEBHOOK_URL = (os.getenv("DESIGN_WEBHOOK_URL") or SYSTEM_LOG_WEBHOOK_URL).strip()

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


# --- WEBHOOK & BOT DISPATCH LOGGING HELPERS ---
def send_webhook_log(webhook_url: str, embed_payload: dict) -> bool:
    if not webhook_url:
        return False
    try:
        res = requests.post(webhook_url, json={"embeds": [embed_payload]}, timeout=6)
        return res.status_code in (200, 204)
    except Exception as e:
        print(f"[Webhook Log Exception] {e}")
    return False


def send_bot_native_log(channel_id: str, embed_payload: dict) -> bool:
    if not BOT_TOKEN:
        return False

    target_channel = str(channel_id).strip()
    url = f"{DISCORD_API_BASE_URL}/channels/{target_channel}/messages"
    headers = {
        "Authorization": f"Bot {BOT_TOKEN}",
        "Content-Type": "application/json"
    }

    body = {"embeds": [embed_payload]}

    try:
        res = requests.post(url, headers=headers, json=body, timeout=8)
        return res.status_code in (200, 201)
    except Exception as e:
        print(f"[Discord API Log Exception] {e}")

    return False


def dispatch_event_log(event_type: str, title: str, user_data: dict, action_desc: str, color: int = 0x3b82f6, extra_fields: dict = None):
    """
    Non-Premium Users -> Dispatched via Webhooks (WEBSITE_WEBHOOK_URL / DESIGN_WEBHOOK_URL).
    TE Premium Users  -> Dispatched via Native Bot API for direct automated building.
    """
    user_data = user_data or {}
    user_id = str(user_data.get("id", "0"))
    username = user_data.get("username", "Anonymous")
    account_age = calculate_account_age(user_id) if user_id != "0" else "Unknown"

    has_premium = is_user_premium(user_id)
    premium_tag = "👑 TE PREMIUM MEMBER" if has_premium else "Standard Member"

    user_info_block = (
        f"User: <@{user_id}>\n"
        f"Membership: `{premium_tag}`\n"
        f"Username: `{username}`\n"
        f"User ID: `{user_id}`\n"
        f"Account Age: `{account_age}`"
    )

    fields = [
        {"name": "User Information", "value": user_info_block, "inline": False},
        {"name": "Activity Details", "value": action_desc, "inline": False}
    ]

    if extra_fields:
        for k, v in extra_fields.items():
            fields.append({"name": k, "value": f"`{v}`", "inline": False})

    embed = {
        "title": title,
        "color": color,
        "fields": fields,
        "footer": {"text": "Template Engine Infrastructure"},
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

    if has_premium:
        target_channel = PREMIUM_DESIGN_CHANNEL_ID if event_type == "design" else SUBSCRIPTION_LOG_CHANNEL_ID
        send_bot_native_log(target_channel, embed)
    else:
        target_webhook = DESIGN_WEBHOOK_URL if event_type == "design" else WEBSITE_WEBHOOK_URL
        send_webhook_log(target_webhook, embed)


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
                    recaptcha_site_key=RECAPTCHA_SITE_KEY,
                    discord_client_id=CLIENT_ID,
                    roblox_store_url=ROBLOX_STORE_URL
                )

        return render_template(
            "index.html",
            is_banned=False,
            user=user,
            roblox_user=roblox_user,
            is_premium=user_is_premium,
            recaptcha_site_key=RECAPTCHA_SITE_KEY,
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

        target_server_id = payload.get("target_server_id", "Unknown")
        server_name = payload.get("server_name", "Custom Server")
        categories_count = payload.get("categories_count", 0)
        channels_count = payload.get("channels_count", 0)
        roles_count = payload.get("roles_count", 0)

        action_text = (
            f"Submitted layout blueprint for server **{server_name}** (`{target_server_id}`).\n"
            f"• Categories: `{categories_count}` | Channels: `{channels_count}` | Roles: `{roles_count}`\n"
            f"• Status: {'🚀 Instant Auto-Deploy (Premium)' if has_premium else '📝 Pending Manual Review (Standard)'}"
        )

        dispatch_event_log(
            event_type="design",
            title="📐 [Design Blueprint Submitted]",
            user_data=user,
            action_desc=action_text,
            color=0xF1C40F if has_premium else 0x3b82f6,
            extra_fields={
                "Target Server ID": target_server_id,
                "Invite Link": payload.get("invite_link", "N/A")
            }
        )

        return jsonify({
            "success": True,
            "is_premium": has_premium,
            "message": "Design blueprint successfully dispatched."
        }), 200

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


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

            # Log login ONLY for authenticated users
            dispatch_event_log(
                event_type="login",
                title="🔐 [Website OAuth Login]",
                user_data=user_obj,
                action_desc="Authenticated via Discord OAuth2.",
                color=0x3b82f6
            )
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
