import os
import json
import requests
from datetime import datetime, timezone
from flask import Flask, redirect, request, session, render_template, jsonify, send_from_directory
from pymongo import MongoClient
from google import genai
from google.genai import types

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")

app = Flask(__name__, template_folder=TEMPLATE_DIR)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "super-secret-key-fallback").strip()

application = app
handler = app

app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

BLUEPRINTS_DIR = "/tmp/blueprints" if os.getenv("VERCEL") else os.path.join(os.getcwd(), "blueprints")
os.makedirs(BLUEPRINTS_DIR, exist_ok=True)

CLIENT_ID = (os.getenv("DISCORD_CLIENT_ID") or "").strip()
CLIENT_SECRET = (os.getenv("DISCORD_CLIENT_SECRET") or "").strip()
RECAPTCHA_SECRET_KEY = (os.getenv("RECAPTCHA_SECRET_KEY") or "").strip()
RECAPTCHA_SITE_KEY = (os.getenv("RECAPTCHA_SITE_KEY") or "").strip()

SYSTEM_LOG_WEBHOOK_URL = (os.getenv("SYSTEM_LOG_WEBHOOK_URL") or "").strip()
WEBSITE_WEBHOOK_URL = (os.getenv("WEBSITE_WEBHOOK_URL") or SYSTEM_LOG_WEBHOOK_URL).strip()
DESIGN_WEBHOOK_URL = (os.getenv("DESIGN_WEBHOOK_URL") or SYSTEM_LOG_WEBHOOK_URL).strip()
PREMIUM_DESIGN_WEBHOOK_URL = (os.getenv("PREMIUM_DESIGN_WEBHOOK_URL") or DESIGN_WEBHOOK_URL).strip()

MONGO_URI = os.getenv("MONGO_URI")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

DISCORD_API_BASE_URL = "https://discord.com/api/v10"

GEMINI_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-pro"
]

db_bans = None
db_premium = None

if MONGO_URI:
    try:
        mongo_client = MongoClient(MONGO_URI)
        try:
            db = mongo_client.get_default_database()
        except Exception:
            db = mongo_client.get_database("static_studio_db")
        db_bans = db["bans"]
        db_premium = db["premium_users"]
    except Exception as e:
        print(f"[MongoDB Warning] Could not connect to Mongo: {e}")

WEBSITE_BANS = {}
LOCAL_PREMIUM_USERS = {}


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


def log_webhook_event(webhook_url: str, title: str, user_data: dict, action_desc: str, color: int = 0x8b5cf6, extra_fields: dict = None, target_server_id: str = None, blueprint_url: str = None, attach_button: bool = False):
    if not webhook_url:
        print("[Log Warning] Webhook URL is empty, skipping dispatch.")
        return

    user_data = user_data or {}
    user_id = str(user_data.get("id", "0"))
    username = user_data.get("username", "Anonymous / Unauthenticated")
    account_age = calculate_account_age(user_id) if user_id != "0" else "Unknown"

    has_premium = is_user_premium(user_id)
    premium_tag = "👑 **[TE PREMIUM MEMBER]**" if has_premium else "Standard User"

    user_info_block = (
        f"**User:** <@{user_id}>\n"
        f"**Membership:** {premium_tag}\n"
        f"**Username:** `{username}`\n"
        f"**User ID:** `{user_id}`\n"
        f"**Account Age:** `{account_age}`"
    )

    fields = [
        {"name": "User Information", "value": user_info_block, "inline": False},
        {"name": "Activity", "value": action_desc, "inline": False}
    ]

    if extra_fields:
        for k, v in extra_fields.items():
            val_str = str(v)
            if k == "Invite Link":
                field_val = val_str
            elif k == "Build Command":
                field_val = f"`{val_str}`"
            elif len(val_str) > 800:
                field_val = f"```json\n{val_str[:750]}\n...```"
            else:
                field_val = f"`{val_str}`"

            fields.append({
                "name": k,
                "value": field_val,
                "inline": False
            })

    payload = {
        "username": "Studio Logger",
        "avatar_url": "https://cdn.discordapp.com/embed/avatars/0.png",
        "embeds": [{
            "title": title,
            "color": color,
            "fields": fields,
            "footer": {"text": "Studio Logging System"},
            "timestamp": datetime.now(timezone.utc).isoformat()
        }]
    }

    if attach_button and target_server_id and blueprint_url:
        payload["components"] = [
            {
                "type": 1,
                "components": [
                    {
                        "type": 2,
                        "style": 3,
                        "label": "⚡ One-Click Build Server",
                        "custom_id": f"buildbtn_{target_server_id}_{blueprint_url}"
                    }
                ]
            }
        ]

    # Critical Fix: Ensure ?with_components=true parameter is present for interactive components
    target_url = webhook_url
    if attach_button and "with_components" not in target_url:
        sep = "&" if "?" in target_url else "?"
        target_url = f"{target_url}{sep}with_components=true"

    try:
        res = requests.post(target_url, json=payload, timeout=8)
        if res.status_code not in (200, 204):
            print(f"[Log Warning] Webhook primary dispatch returned status {res.status_code}: {res.text}")
            # Fallback retry without buttons if Discord rejected custom components
            if attach_button:
                payload.pop("components", None)
                fallback_url = webhook_url
                requests.post(fallback_url, json=payload, timeout=5)
    except Exception as e:
        print(f"[Log Error] Webhook post failure: {e}")


@app.route("/")
def home():
    user = session.get("user")
    user_is_premium = False
    if user:
        user_is_premium = is_user_premium(user.get("id"))
        ban_info = is_user_banned(user.get("id"))
        if ban_info:
            return render_template(
                "index.html",
                is_banned=True,
                user=user,
                is_premium=user_is_premium,
                dev_message=ban_info.get("message_from_dev", "Suspended by developer."),
                recaptcha_site_key=RECAPTCHA_SITE_KEY,
                discord_client_id=CLIENT_ID
            )

    return render_template(
        "index.html",
        is_banned=False,
        user=user,
        is_premium=user_is_premium,
        recaptcha_site_key=RECAPTCHA_SITE_KEY,
        discord_client_id=CLIENT_ID
    )


@app.route("/login")
def login():
    if not CLIENT_ID:
        return "Error: DISCORD_CLIENT_ID is not configured in Environment Variables.", 500

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

            session["user"] = {
                "id": user_data["id"],
                "username": user_data["username"],
                "discriminator": user_data.get("discriminator", "0"),
                "avatar": avatar_url
            }

            try:
                log_webhook_event(
                    webhook_url=WEBSITE_WEBHOOK_URL,
                    title="[Website Login Logged]",
                    user_data=user_data,
                    action_desc="Authenticated via Discord OAuth2 and accessed dashboard.",
                    color=0x3b82f6
                )
            except Exception as le:
                print(f"[Login Webhook Error] {le}")
    except Exception as e:
        print(f"[OAuth Callback Error] {e}")

    return redirect("/")


@app.route("/api/verify-captcha", methods=["POST"])
def verify_captcha():
    data = request.get_json() or {}
    token = data.get("token")
    if not token or not RECAPTCHA_SECRET_KEY:
        return jsonify({"success": True}), 200

    try:
        verify_res = requests.post(
            "https://www.google.com/recaptcha/api/siteverify",
            data={"secret": RECAPTCHA_SECRET_KEY, "response": token},
            timeout=5
        ).json()
        return jsonify({"success": verify_res.get("success", False)})
    except Exception:
        return jsonify({"success": True}), 200


@app.route("/api/generate", methods=["POST"])
def api_generate():
    user = session.get("user")
    if not user:
        return jsonify({
            "success": False,
            "error": "Authentication required. Please log in with Discord first.",
            "redirect": "/login"
        }), 401

    data = request.get_json() or {}
    prompt = data.get("prompt", "")
    server_id = data.get("server_id", "")

    if not prompt:
        return jsonify({"success": False, "error": "Prompt description is required."}), 400

    if not GEMINI_API_KEY:
        return jsonify({"success": False, "error": "GEMINI_API_KEY missing on server."}), 500

    client = genai.Client(api_key=GEMINI_API_KEY)

    system_instruction = (
        "You are an expert Discord Infrastructure Architect. Output ONLY valid JSON representing a Discord server layout."
        " Required structure:\n"
        "{\n"
        '  "server_name": "String",\n'
        '  "roles": [{"name": "Role Name", "color": "#HexColor"}],\n'
        '  "categories": [\n'
        "    {\n"
        '      "name": "CATEGORY NAME",\n'
        '      "channels": [\n'
        "        {\n"
        '          "name": "channel-name",\n'
        '          "type": "text|voice|announcement",\n'
        '          "topic": "Description",\n'
        '          "read_only": false,\n'
        '          "permissions": {"view": true, "send": true, "embed": true, "attach": true}\n'
        "        }\n"
        "      ]\n"
        "    }\n"
        "  ]\n"
        "}"
    )

    last_error = None
    layout_data = None
    used_model = None

    for model_name in GEMINI_MODELS:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=f"Create layout for: {prompt}",
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    response_mime_type="application/json",
                    temperature=0.7
                )
            )
            layout_data = json.loads(response.text)
            used_model = model_name
            break
        except Exception as e:
            last_error = e
            continue

    if not layout_data:
        return jsonify({
            "success": False,
            "error": f"All Gemini models experienced error. Last Error: {str(last_error)}"
        }), 503

    layout_data["build_meta"] = {
        "target_server_id": server_id,
        "prompt": prompt,
        "model_used": used_model
    }

    return jsonify({"success": True, "data": layout_data, "model_used": used_model})


@app.route("/api/submit-design", methods=["POST"])
def submit_design():
    user = session.get("user")
    if not user:
        return jsonify({
            "success": False,
            "error": "Authentication required. Please log in with Discord first.",
            "redirect": "/login"
        }), 401

    data = request.get_json() or {}
    target_server_id = str(data.get("target_server_id", "Not Provided")).strip()
    server_name = data.get("server_name", "Custom Server")
    invite_link = data.get("invite_link", "Not Provided")
    prompt = data.get("prompt", "Not Provided")
    categories_count = data.get("categories_count", 0)
    channels_count = data.get("channels_count", 0)
    roles_count = data.get("roles_count", 0)

    layout = data.get("layout") or data.get("build_file") or data.get("file")
    has_premium = is_user_premium(user.get("id"))

    file_id = f"blueprint_{int(datetime.now().timestamp() * 1000)}"
    filename = f"{file_id}.json"
    file_path = os.path.join(BLUEPRINTS_DIR, filename)

    blueprint_content = layout if layout else data
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(blueprint_content, f, indent=2)
    except Exception as e:
        print(f"[Blueprint File Save Error] {e}")

    blueprint_url = f"{get_base_url()}/blueprint/{filename}"
    build_command_text = f"/build url: {blueprint_url}"

    target_webhook = PREMIUM_DESIGN_WEBHOOK_URL if has_premium else DESIGN_WEBHOOK_URL
    log_title = "👑 [TE PREMIUM] Blueprint Submitted" if has_premium else "[New Blueprint Submitted]"

    extra_fields = {
        "Server Name": server_name,
        "Target Server ID": target_server_id,
        "Invite Link": invite_link,
        "Prompt Description": prompt[:500] if isinstance(prompt, str) else str(prompt),
        "Build Command": build_command_text,
        "Architecture Overview": f"Categories: `{categories_count}` | Channels: `{channels_count}` | Roles: `{roles_count}`"
    }

    try:
        log_webhook_event(
            webhook_url=target_webhook,
            title=log_title,
            user_data=user,
            action_desc="User submitted a server layout blueprint to staff.",
            color=0xf1c40f if has_premium else 0x2ecc71,
            extra_fields=extra_fields,
            target_server_id=target_server_id,
            blueprint_url=blueprint_url,
            attach_button=has_premium
        )
    except Exception as err:
        print(f"[Submit Webhook Error] {err}")

    return jsonify({
        "success": True,
        "message": "Blueprint successfully submitted to staff!",
        "url": blueprint_url,
        "is_premium": has_premium
    })


@app.route("/blueprint/<path:filename>")
def serve_blueprint(filename):
    return send_from_directory(BLUEPRINTS_DIR, filename, mimetype="application/json")


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
