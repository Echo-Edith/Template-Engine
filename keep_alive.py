import os
import re
import json
import random
import requests
from datetime import datetime, timezone, timedelta
from flask import Flask, redirect, request, session, render_template, jsonify, send_from_directory
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
ROBLOX_SUBSCRIPTION_ID = (os.getenv("SUBSCRIPTION_ID") or "EXP-4378714879071224384").strip()
ROBLOX_PLACE_ID = (os.getenv("ROBLOX_PLACE_ID") or "6094950399").strip()
ROBLOX_STORE_URL = (os.getenv("ROBLOX_STORE_URL") or f"https://www.roblox.com/games/{ROBLOX_PLACE_ID}/store").strip()

# Target Log Channels
DESIGN_CHANNEL_ID = (os.getenv("DESIGN_CHANNEL_ID") or "1563544108696477802").strip()
PREMIUM_DESIGN_CHANNEL_ID = (os.getenv("PREMIUM_DESIGN_CHANNEL_ID") or "1563544108696477802").strip()

SYSTEM_LOG_WEBHOOK_URL = (os.getenv("SYSTEM_LOG_WEBHOOK_URL") or "").strip()
WEBSITE_WEBHOOK_URL = (os.getenv("WEBSITE_WEBHOOK_URL") or SYSTEM_LOG_WEBHOOK_URL).strip()

BOT_API_PORT = int(os.getenv("BOT_PORT", "10000"))
BOT_SERVER_URL = (os.getenv("BOT_SERVER_URL") or f"http://127.0.0.1:{BOT_API_PORT}").rstrip("/")

MONGO_URI = os.getenv("MONGO_URI")

DISCORD_API_BASE_URL = "https://discord.com/api/v10"

db_bans = None
db_premium = None
db_users = None

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

    update_payload = {
        "discord_id": discord_id_str,
        "discord_data": discord_data,
        "updated_at": now
    }
    if roblox_data is not None:
        update_payload["roblox_data"] = roblox_data

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
        existing = LOCAL_PERSISTENT_USERS.get(discord_id_str, {})
        existing.update(update_payload)
        LOCAL_PERSISTENT_USERS[discord_id_str] = existing


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


def set_user_premium(user_id: str, duration_td: timedelta = timedelta(days=30)):
    user_id_str = str(user_id)
    now = datetime.now(timezone.utc)
    expires_at = now + duration_td

    if db_premium is not None:
        try:
            db_premium.update_one(
                {"user_id": user_id_str},
                {"$set": {"user_id": user_id_str, "expires_at": expires_at.isoformat(), "updated_at": now.isoformat()}},
                upsert=True
            )
        except Exception as e:
            print(f"[Set Premium Error] {e}")
    else:
        LOCAL_PREMIUM_USERS[user_id_str] = expires_at


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


def get_base_url() -> str:
    dashboard = os.getenv("DASHBOARD_URL") or os.getenv("DISCORD_REDIRECT_URI")
    if dashboard and "your-vercel-domain" not in dashboard:
        if dashboard.endswith("/callback"):
            return dashboard[:-9].rstrip("/")
        return dashboard.rstrip("/")

    vercel_url = os.getenv("VERCEL_URL")
    if vercel_url:
        return f"https://{vercel_url.rstrip('/')}"

    if request and request.host_url:
        return request.host_url.rstrip("/")

    return "http://localhost:5000"


def get_redirect_uri() -> str:
    configured = os.getenv("DISCORD_REDIRECT_URI")
    if configured and configured.strip():
        return configured.strip()
    return f"{get_base_url()}/callback"


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


def log_event_via_bot(channel_source: str, title: str, user_data: dict, action_desc: str, color: int = 0x8b5cf6, extra_fields: dict = None):
    user_data = user_data or {}
    user_id = str(user_data.get("id", "0"))
    username = user_data.get("username", "Anonymous")
    account_age = calculate_account_age(user_id) if user_id != "0" else "Unknown"

    has_premium = is_user_premium(user_id)
    premium_tag = "[TE PREMIUM MEMBER]" if has_premium else "Standard User"

    user_info_block = (
        f"User: <@{user_id}>\n"
        f"Membership: {premium_tag}\n"
        f"Username: `{username}`\n"
        f"User ID: `{user_id}`\n"
        f"Account Age: `{account_age}`"
    )

    fields = [
        {"name": "User Information", "value": user_info_block, "inline": False},
        {"name": "Activity", "value": action_desc, "inline": False}
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

    send_bot_native_log(channel_source, embed)


@app.route("/")
def home():
    try:
        user = session.get("user")
        roblox_user = session.get("roblox_user")

        if not user and session.get("user_id"):
            db_record = load_user_session_db(session["user_id"])
            if db_record:
                user = db_record.get("discord_data")
                roblox_user = roblox_user or db_record.get("roblox_data")
                session["user"] = user
                if roblox_user:
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
        return f"Template Engine Engine Error: {str(e)}", 500


# --- ROBLOX BIO VERIFICATION SYSTEM ---
@app.route("/api/generate-bio-code", methods=["POST"])
def generate_bio_code():
    data = request.get_json(silent=True) or {}
    username = data.get("username", "").strip()

    if not username:
        return jsonify({"success": False, "error": "Username required."}), 400

    code = f"TE-{random.randint(1000, 9999)}"
    session["pending_bio_code"] = code
    session["pending_roblox_username"] = username

    return jsonify({"success": True, "code": code})


@app.route("/api/verify-bio", methods=["POST"])
def verify_bio():
    data = request.get_json(silent=True) or {}
    username = data.get("username", "").strip()
    provided_code = data.get("code", "").strip()
    user = session.get("user")

    if not user:
        return jsonify({"success": False, "error": "You must be logged in with Discord first."}), 401

    if not username or not provided_code:
        return jsonify({"success": False, "error": "Missing username or verification code."}), 400

    try:
        # Step 1: Resolve Roblox Username to User ID
        res = requests.post("https://users.roblox.com/v1/usernames/users", json={"usernames": [username], "excludeBannedUsers": True}, timeout=5)
        res_data = res.json()
        
        if not res_data.get("data") or len(res_data["data"]) == 0:
            return jsonify({"success": False, "error": f"Roblox user '{username}' not found."}), 404

        roblox_id = res_data["data"][0]["id"]
        exact_username = res_data["data"][0]["name"]

        # Step 2: Fetch User Profile Information (Bio/About)
        profile_res = requests.get(f"https://users.roblox.com/v1/users/{roblox_id}", timeout=5)
        profile_data = profile_res.json()
        bio_text = profile_data.get("description", "")

        # Step 3: Check if the exact code exists in their bio
        if provided_code not in bio_text:
            return jsonify({
                "success": False, 
                "error": f"Code '{provided_code}' was not found in profile bio for @{exact_username}. Please make sure you saved changes."
            }), 400

        # Verification successful: Save Roblox session & grant TE Premium
        roblox_obj = {
            "id": str(roblox_id),
            "username": exact_username
        }
        session["roblox_user"] = roblox_obj

        save_user_session_db(user["id"], user, roblox_obj)
        set_user_premium(user["id"], timedelta(days=30))

        log_event_via_bot(
            channel_source=PREMIUM_DESIGN_CHANNEL_ID,
            title="[Roblox Bio Verified & TE Premium Granted]",
            user_data=user,
            action_desc=f"Successfully verified Roblox profile bio code for `@{exact_username}` (`{roblox_id}`). TE Premium activated.",
            color=0xf1c40f,
            extra_fields={
                "Roblox Username": exact_username,
                "Roblox User ID": str(roblox_id),
                "Verification Code": provided_code
            }
        )

        return jsonify({"success": True, "message": "Account linked successfully!"})

    except Exception as e:
        return jsonify({"success": False, "error": f"Roblox API verification failed: {str(e)}"}), 500


@app.route("/logout-roblox")
def logout_roblox():
    session.pop("roblox_user", None)
    user = session.get("user")
    if user:
        save_user_session_db(user["id"], user, roblox_data={})
    return redirect("/")


# --- AI DESIGN & BLUEPRINT GENERATION ---
@app.route("/generate-blueprint", methods=["POST"])
def generate_blueprint_endpoint():
    try:
        import google.generativeai as genai
        
        data = request.get_json(silent=True) or {}
        prompt = data.get("prompt", "").strip()

        if not prompt:
            return jsonify({"success": False, "error": "Prompt cannot be empty."}), 400

        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            return jsonify({"success": False, "error": "GEMINI_API_KEY environment variable is missing on Vercel."}), 500

        genai.configure(api_key=api_key)

        system_instruction = (
            "You are an expert Discord Server Architect. Your job is to output a valid JSON object representing a full Discord server blueprint based on the user request. "
            "Strictly return ONLY raw JSON without markdown formatting, code blocks, or markdown backticks. "
            "JSON structure MUST follow:\n"
            "{\n"
            '  "server_name": "String",\n'
            '  "roles": [{"name": "Role Name", "color": "#HEX"}],\n'
            '  "categories": [\n'
            '    {\n'
            '      "name": "CATEGORY NAME",\n'
            '      "channels": [\n'
            '        {"name": "channel-name", "type": "text|voice|announcement", "emoji": "", "topic": "Description"}\n'
            '      ]\n'
            '    }\n'
            '  ]\n'
            "}"
        )

        model = genai.GenerativeModel("gemini-1.5-flash", system_instruction=system_instruction)
        response = model.generate_content(prompt)
        raw_text = response.text.strip() if response and hasattr(response, "text") else ""

        json_match = re.search(r"\{.*\}", raw_text, re.DOTALL)
        if json_match:
            clean_json_str = json_match.group(0)
        else:
            clean_json_str = re.sub(r"^```(json)?|```$", "", raw_text, flags=re.MULTILINE).strip()

        blueprint_json = json.loads(clean_json_str)

        return jsonify({"success": True, "blueprint": blueprint_json}), 200

    except json.JSONDecodeError:
        return jsonify({"success": False, "error": "AI generated an invalid blueprint format. Please try again."}), 500
    except Exception as e:
        return jsonify({"success": False, "error": f"AI Engine Exception: {str(e)}"}), 500


# --- DISCORD OAUTH2 LOGIN & CALLBACK ---
@app.route("/login")
def login():
    if not CLIENT_ID:
        return "Error: DISCORD_CLIENT_ID is not configured.", 500

    redirect_uri = get_redirect_uri()
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

    redirect_uri = get_redirect_uri()

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

            save_user_session_db(user_data["id"], user_obj, session.get("roblox_user"))

            log_event_via_bot(
                channel_source=PREMIUM_DESIGN_CHANNEL_ID,
                title="[Website Login Logged]",
                user_data=user_data,
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
