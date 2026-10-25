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

    target_url = webhook_url
    if attach_button and "with_components" not in target_url:
        sep = "&" if "?" in target_url else "?"
        target_url = f"{target_url}{sep}with_components=true"

    try:
        res = requests.post(target_url, json=payload, timeout=8)
        if res.status_code not in (200, 204):
            print(f"[Log Warning] Webhook primary dispatch returned status {res.status_code}: {res.text}")
            if attach_button:
                payload.pop("components", None)
                requests.post(webhook_url, json=payload, timeout=5)
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

    # Explicit webhook selection for Premium vs Standard
    target_webhook = PREMIUM_DESIGN_WEBHOOK_URL if has_premium and PREMIUM_DESIGN_WEBHOOK_URL else DESIGN_WEBHOOK_URL
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
```

---

### File 2: `templates/index.html`

```html:Index Template:templates/index.html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Discord Infrastructure Architect</title>

    <!-- FontAwesome Icons -->
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    
    <!-- SortableJS for Drag and Drop -->
    <script src="https://cdnjs.cloudflare.com/ajax/libs/Sortable/1.15.0/Sortable.min.js"></script>

    <!-- Global State & Callbacks defined BEFORE Google reCAPTCHA script loads -->
    <script>
        const RECAPTCHA_SITE_KEY = "{{ recaptcha_site_key or '' }}".trim();
        const DISCORD_CLIENT_ID = "{{ discord_client_id or '' }}".trim();
        const IS_USER_LOGGED_IN = {{ 'true' if user else 'false' }};
        const IS_TE_PREMIUM = {{ 'true' if is_premium else 'false' }};
        let isCaptchaVerified = false;

        function checkAuthAndCaptchaStatus() {
            const btn = document.getElementById('verify-continue-btn');
            const label = document.getElementById('continue-btn-label');
            
            if (!IS_USER_LOGGED_IN) {
                if (btn) btn.disabled = true;
                if (label) label.innerText = "Log In with Discord First";
                return;
            }

            if (!isCaptchaVerified && RECAPTCHA_SITE_KEY && RECAPTCHA_SITE_KEY.length > 5) {
                if (btn) btn.disabled = true;
                if (label) label.innerText = "Complete Verification Below";
                return;
            }

            if (btn) {
                btn.disabled = false;
                btn.removeAttribute('disabled');
            }
            if (label) label.innerText = "Verify & Continue";
        }

        // Global Callback Handler for Google reCAPTCHA
        window.onCaptchaVerified = function(token) {
            isCaptchaVerified = true;
            checkAuthAndCaptchaStatus();
            if (token && RECAPTCHA_SITE_KEY) {
                fetch('/api/verify-captcha', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ token })
                }).catch(e => console.log('Captcha verification log:', e));
            }
        };

        window.onCaptchaExpired = function() {
            isCaptchaVerified = false;
            checkAuthAndCaptchaStatus();
        };

        // Fallback polling loop to detect reCAPTCHA state in real-time
        setInterval(function() {
            if (typeof grecaptcha !== 'undefined' && !isCaptchaVerified) {
                try {
                    const response = grecaptcha.getResponse();
                    if (response && response.length > 0) {
                        isCaptchaVerified = true;
                        checkAuthAndCaptchaStatus();
                    }
                } catch(e) {}
            }
        }, 500);
    </script>

    <!-- Google reCAPTCHA v2 API Script -->
    <script src="https://www.google.com/recaptcha/api.js" async defer></script>

    <style>
        :root {
            --bg-base: #090a10;
            --panel-bg: rgba(18, 16, 32, 0.55);
            --panel-border: rgba(255, 255, 255, 0.12);
            --glass-inner-bg: rgba(255, 255, 255, 0.03);
            --glass-inner-border: rgba(255, 255, 255, 0.07);
            
            --input-bg: rgba(13, 12, 22, 0.65);
            --input-border: rgba(139, 92, 246, 0.25);
            
            --discord-dark: #2b2d31;
            --discord-sidebar: rgba(15, 14, 25, 0.6);
            --discord-channel: #949ba4;
            --discord-hover: rgba(139, 92, 246, 0.2);
            
            --text-title: #ffffff;
            --text-sub: #b5bac1;
            --text-muted: #707580;
            
            --accent-purple: #8b5cf6;
            --accent-purple-glow: rgba(139, 92, 246, 0.4);
            --accent-gold: #f1c40f;
            --accent-gold-glow: rgba(241, 196, 15, 0.4);
            --accent-violet: #6366f1;
            --accent-green: #2ecc71;
            --accent-red: #f43f5e;
            
            --card-bg: rgba(20, 18, 33, 0.45);
            --card-border: rgba(255, 255, 255, 0.08);
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
            font-family: 'gg sans', 'Noto Sans', 'Helvetica Neue', Helvetica, Arial, sans-serif;
            -webkit-tap-highlight-color: transparent;
        }

        body {
            min-height: 100vh;
            min-height: 100dvh;
            background: var(--bg-base);
            background-image: 
                radial-gradient(circle at 15% 15%, rgba(139, 92, 246, 0.28) 0%, transparent 45%),
                radial-gradient(circle at 85% 85%, rgba(99, 102, 241, 0.22) 0%, transparent 50%),
                radial-gradient(circle at 50% 50%, rgba(168, 85, 247, 0.12) 0%, transparent 65%);
            background-attachment: fixed;
            color: #ffffff;
            display: flex;
            justify-content: center;
            align-items: center;
            padding: 16px;
        }

        .glass-panel {
            background: var(--panel-bg);
            backdrop-filter: blur(28px) saturate(180%);
            -webkit-backdrop-filter: blur(28px) saturate(180%);
            border: 1px solid var(--panel-border);
            border-top: 1px solid rgba(255, 255, 255, 0.22);
            border-left: 1px solid rgba(255, 255, 255, 0.18);
            border-radius: 24px;
            box-shadow: 
                0 30px 60px rgba(0, 0, 0, 0.7),
                0 0 40px rgba(139, 92, 246, 0.15),
                inset 0 1px 0 rgba(255, 255, 255, 0.2);
            width: 100%;
            max-width: 480px;
            padding: 32px 24px;
            position: relative;
            margin: auto;
            transition: max-width 0.3s cubic-bezier(0.4, 0, 0.2, 1);
        }

        .glass-panel.layout-mode {
            max-width: 680px;
        }

        .page {
            display: none;
            opacity: 0;
            transform: translateY(8px);
            transition: opacity 0.3s ease, transform 0.3s ease;
        }

        .page.active {
            display: block;
            opacity: 1;
            transform: translateY(0);
        }

        .brand-header {
            text-align: center;
            margin-bottom: 24px;
        }

        .brand-header h1 {
            font-size: 1.6rem;
            font-weight: 800;
            background: linear-gradient(135deg, #ffffff 30%, #c084fc 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin-bottom: 6px;
            letter-spacing: -0.3px;
        }

        .brand-header p {
            color: var(--text-sub);
            font-size: 0.88rem;
        }

        .user-auth-card {
            background: var(--glass-inner-bg);
            border: 1px solid var(--glass-inner-border);
            backdrop-filter: blur(12px);
            -webkit-backdrop-filter: blur(12px);
            border-radius: 14px;
            padding: 12px 16px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 20px;
            box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.05);
        }

        .user-info {
            display: flex;
            align-items: center;
            gap: 12px;
        }

        .user-avatar {
            width: 38px;
            height: 38px;
            border-radius: 50%;
            border: 1px solid var(--accent-purple-glow);
        }

        .premium-badge {
            background: linear-gradient(135deg, #f1c40f 0%, #f39c12 100%);
            color: #000;
            font-weight: 800;
            font-size: 0.7rem;
            padding: 3px 8px;
            border-radius: 12px;
            letter-spacing: 0.5px;
            box-shadow: 0 0 10px rgba(241, 196, 15, 0.5);
            display: inline-flex;
            align-items: center;
            gap: 4px;
        }

        .logout-link {
            color: var(--accent-red);
            font-size: 0.82rem;
            text-decoration: none;
            font-weight: 600;
            transition: opacity 0.2s;
        }

        .logout-link:hover {
            opacity: 0.8;
        }

        .recaptcha-glass-cover {
            background: rgba(12, 10, 20, 0.5);
            border: 1px solid rgba(139, 92, 246, 0.25);
            backdrop-filter: blur(16px);
            -webkit-backdrop-filter: blur(16px);
            border-radius: 16px;
            padding: 12px;
            display: flex;
            justify-content: center;
            align-items: center;
            margin: 0 auto 20px auto;
            width: 100%;
            max-width: 330px;
            min-height: 85px;
            box-shadow: inset 0 0 15px rgba(139, 92, 246, 0.08);
        }

        .label-heading {
            display: block;
            font-size: 0.72rem;
            font-weight: 700;
            letter-spacing: 0.8px;
            text-transform: uppercase;
            color: var(--text-sub);
            margin-bottom: 8px;
        }

        .label-heading span.required {
            color: var(--accent-red);
        }

        .form-group {
            margin-bottom: 16px;
        }

        .form-control {
            width: 100%;
            padding: 12px 14px;
            background: var(--input-bg);
            border: 1px solid var(--input-border);
            border-radius: 10px;
            color: #ffffff;
            font-size: 0.9rem;
            outline: none;
            backdrop-filter: blur(10px);
            -webkit-backdrop-filter: blur(10px);
            transition: all 0.2s ease;
        }

        .form-control option {
            background: #121020;
            color: #ffffff;
        }

        .form-control:focus {
            border-color: var(--accent-purple);
            box-shadow: 0 0 12px var(--accent-purple-glow);
        }

        .btn-glass {
            width: 100%;
            padding: 14px;
            background: linear-gradient(135deg, rgba(139, 92, 246, 0.85) 0%, rgba(99, 102, 241, 0.85) 100%);
            border: 1px solid rgba(255, 255, 255, 0.2);
            border-radius: 12px;
            color: #ffffff;
            font-weight: 600;
            font-size: 0.92rem;
            cursor: pointer;
            backdrop-filter: blur(10px);
            -webkit-backdrop-filter: blur(10px);
            box-shadow: 0 8px 20px rgba(139, 92, 246, 0.35);
            transition: all 0.25s ease;
            display: flex;
            justify-content: center;
            align-items: center;
            gap: 10px;
            min-height: 48px;
            text-decoration: none;
        }

        .btn-glass.btn-premium {
            background: linear-gradient(135deg, rgba(241, 196, 15, 0.9) 0%, rgba(243, 156, 18, 0.9) 100%);
            color: #000000;
            font-weight: 800;
            box-shadow: 0 8px 20px rgba(241, 196, 15, 0.4);
            border: 1px solid rgba(255, 255, 255, 0.4);
        }

        .btn-glass:hover:not(:disabled) {
            transform: translateY(-1px);
            box-shadow: 0 12px 25px rgba(139, 92, 246, 0.5);
            filter: brightness(1.1);
        }

        .btn-glass.btn-premium:hover:not(:disabled) {
            box-shadow: 0 12px 25px rgba(241, 196, 15, 0.6);
        }

        .btn-glass:disabled {
            opacity: 0.45;
            cursor: not-allowed;
            transform: none;
            box-shadow: none;
        }

        .discord-sidebar-wrapper {
            background: var(--discord-sidebar);
            backdrop-filter: blur(16px);
            -webkit-backdrop-filter: blur(16px);
            border-radius: 14px;
            padding: 14px;
            border: 1px solid var(--card-border);
            margin-bottom: 16px;
        }

        .discord-server-header {
            font-weight: 700;
            font-size: 1rem;
            padding-bottom: 10px;
            border-bottom: 1px solid var(--glass-inner-border);
            margin-bottom: 12px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .discord-category {
            margin-bottom: 14px;
        }

        .discord-category-title {
            font-size: 0.72rem;
            font-weight: 700;
            color: var(--text-sub);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 4px 6px;
            cursor: pointer;
        }

        .discord-channel-item {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 8px 10px;
            border-radius: 8px;
            color: var(--discord-channel);
            font-size: 0.88rem;
            cursor: pointer;
            margin-top: 3px;
            touch-action: pan-y;
            transition: all 0.2s ease;
        }

        .discord-channel-item:hover, .discord-channel-item.active {
            background: var(--discord-hover);
            color: #ffffff;
        }

        .discord-channel-item.active {
            background: rgba(139, 92, 246, 0.3);
            border: 1px solid rgba(139, 92, 246, 0.4);
            color: #ffffff;
            font-weight: 600;
        }

        .add-btn-inline {
            background: rgba(255, 255, 255, 0.02);
            border: 1px dashed var(--input-border);
            border-radius: 8px;
            color: var(--text-sub);
            font-size: 0.8rem;
            padding: 8px;
            cursor: pointer;
            width: 100%;
            margin-top: 8px;
            transition: all 0.2s ease;
        }

        .add-btn-inline:hover {
            border-color: var(--accent-purple);
            color: #ffffff;
            background: rgba(139, 92, 246, 0.1);
        }

        .editor-card {
            background: var(--card-bg);
            backdrop-filter: blur(16px);
            -webkit-backdrop-filter: blur(16px);
            border: 1px solid var(--card-border);
            border-radius: 14px;
            padding: 16px;
            margin-bottom: 16px;
        }

        .editor-card h3 {
            font-size: 1rem;
            font-weight: 700;
            margin-bottom: 12px;
        }

        .permissions-toggle-box {
            display: flex;
            align-items: center;
            justify-content: space-between;
            background: var(--glass-inner-bg);
            border: 1px solid var(--glass-inner-border);
            border-radius: 10px;
            padding: 12px;
            margin-top: 12px;
        }

        .roles-flex {
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            margin-bottom: 10px;
        }

        .role-pill {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 6px 12px;
            border-radius: 16px;
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid var(--glass-inner-border);
            font-size: 0.82rem;
            touch-action: pan-y;
        }

        .role-dot {
            width: 10px;
            height: 10px;
            border-radius: 50%;
        }

        .modal-backdrop {
            position: fixed;
            top: 0;
            left: 0;
            width: 100vw;
            height: 100vh;
            background: rgba(0, 0, 0, 0.75);
            backdrop-filter: blur(12px);
            -webkit-backdrop-filter: blur(12px);
            z-index: 999;
            display: none;
            justify-content: center;
            align-items: center;
            padding: 16px;
        }

        .modal-card {
            background: #121020;
            border: 1px solid var(--panel-border);
            border-radius: 20px;
            width: 100%;
            max-width: 520px;
            max-height: 85vh;
            overflow-y: auto;
            padding: 24px;
            box-shadow: 0 20px 50px rgba(0,0,0,0.8);
        }

        .perm-grid-inline {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
            gap: 10px;
            margin-top: 12px;
        }

        .perm-item-inline {
            display: flex;
            align-items: center;
            justify-content: space-between;
            background: rgba(255, 255, 255, 0.03);
            border: 1px solid var(--glass-inner-border);
            border-radius: 10px;
            padding: 10px 12px;
        }

        .sync-channel-checkboxes {
            max-height: 220px;
            overflow-y: auto;
            background: var(--input-bg);
            border: 1px solid var(--input-border);
            border-radius: 10px;
            padding: 10px;
        }

        .sync-item {
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 8px 6px;
            font-size: 0.88rem;
            color: var(--text-sub);
            border-bottom: 1px solid rgba(255,255,255,0.03);
            cursor: pointer;
        }

        .sync-item:last-child {
            border-bottom: none;
        }

        .sync-item input[type="checkbox"] {
            width: 16px;
            height: 16px;
            accent-color: var(--accent-purple);
        }

        .robux-badge {
            background: rgba(241, 196, 15, 0.15);
            border: 1px solid rgba(241, 196, 15, 0.3);
            color: #f1c40f;
            border-radius: 12px;
            padding: 12px 16px;
            margin: 16px 0;
            font-size: 0.88rem;
            line-height: 1.5;
            text-align: left;
        }

        .spinner {
            border: 2px solid rgba(255,255,255,0.2);
            width: 16px;
            height: 16px;
            border-radius: 50%;
            border-left-color: #ffffff;
            animation: spin 0.8s linear infinite;
            display: inline-block;
        }

        @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
    </style>
</head>
<body>

    <div id="panel-wrapper" class="glass-panel">

        <!-- PAGE 1: LANDING & CAPTCHA/AUTH -->
        <div id="page-landing" class="page active">
            <div class="brand-header">
                <h1>Discord Architect</h1>
                <p>Login with Discord and complete verification to begin.</p>
            </div>

            {% if user %}
                <div class="user-auth-card">
                    <div class="user-info">
                        <img src="{{ user.avatar }}" class="user-avatar" alt="Avatar">
                        <div>
                            <span style="font-weight: 600; font-size: 0.95rem; display:block;">{{ user.username }}</span>
                            {% if is_premium %}
                                <span class="premium-badge"><i class="fa-solid fa-crown"></i> TE PREMIUM</span>
                            {% endif %}
                        </div>
                    </div>
                    <a href="/logout" class="logout-link">Logout</a>
                </div>
            {% else %}
                <div style="margin-bottom: 20px;">
                    <a href="/login" class="btn-glass" style="background: linear-gradient(135deg, rgba(88, 101, 242, 0.9) 0%, rgba(124, 58, 237, 0.9) 100%);">
                        <i class="fa-brands fa-discord fa-lg"></i> Log In with Discord
                    </a>
                </div>
            {% endif %}

            <!-- Direct Google Auto-Render reCAPTCHA Box -->
            <div class="recaptcha-glass-cover">
                {% if recaptcha_site_key and recaptcha_site_key|length > 5 %}
                    <div class="g-recaptcha" 
                         data-sitekey="{{ recaptcha_site_key }}" 
                         data-theme="dark" 
                         data-callback="onCaptchaVerified" 
                         data-expired-callback="onCaptchaExpired"></div>
                {% else %}
                    <span style="font-size: 0.78rem; color: #a1a1aa; text-align:center;">[Verification Ready - Key Auto Bypass]</span>
                {% endif %}
            </div>

            <button id="verify-continue-btn" class="btn-glass" disabled onclick="proceedToGenerator()">
                <span id="continue-btn-label">Log In & Verify to Continue</span> <i class="fa-solid fa-arrow-right"></i>
            </button>
        </div>

        <!-- PAGE 2: GENERATOR PROMPT -->
        <div id="page-generator" class="page">
            <div class="brand-header">
                <h1>Layout Generator</h1>
                <p>AI will build your Discord channel & role architecture.</p>
            </div>

            <div class="form-group">
                <label class="label-heading">PROMPT DESCRIPTION <span class="required">*</span></label>
                <textarea id="gen-prompt" class="form-control" rows="3" placeholder="e.g. Create a professional gaming server layout with VIP tiers, media channels, and staff areas..."></textarea>
            </div>

            <div class="form-group">
                <label class="label-heading">TARGET DISCORD SERVER ID <span class="required">*</span></label>
                <input type="text" id="gen-server-id" class="form-control" placeholder="e.g. 123456789012345678" oninput="updateBotInviteLink()">
            </div>

            <div class="form-group">
                <label class="label-heading">SERVER INVITE LINK <span class="required">*</span></label>
                <input type="text" id="gen-server-link" class="form-control" placeholder="https://discord.gg/yourcode">
            </div>

            <button id="generate-btn" class="btn-glass" style="margin-top: 24px;" onclick="handleAIGeneration()">
                <i class="fa-solid fa-wand-magic-sparkles"></i> <span>Generate Server Layout</span>
            </button>
        </div>

        <!-- PAGE 3: LAYOUT DASHBOARD -->
        <div id="page-layout" class="page">
            <div class="discord-sidebar-wrapper">
                <div class="discord-server-header">
                    <span id="server-title-heading">Server Structure</span>
                    <i class="fa-solid fa-shield-halved" style="color: var(--accent-purple); font-size: 0.9rem;"></i>
                </div>

                <div id="sidebar-category-container"></div>

                <button class="add-btn-inline" onclick="addCategory()">
                    <i class="fa-solid fa-plus"></i> Add Category
                </button>
            </div>

            <div class="editor-card" id="channel-editor-card">
                <h3 id="channel-settings-title">Channel Settings</h3>

                <div class="form-group">
                    <label class="label-heading">EMOJI</label>
                    <input type="text" id="edit-chan-emoji" class="form-control" placeholder="💬" oninput="updateActiveChannel()">
                </div>

                <div class="form-group">
                    <label class="label-heading">CLEAN NAME (WITHOUT EMOJI)</label>
                    <input type="text" id="edit-chan-name" class="form-control" oninput="updateActiveChannel()">
                </div>

                <div class="form-group">
                    <label class="label-heading">TYPE</label>
                    <select id="edit-chan-type" class="form-control" onchange="updateActiveChannel()">
                        <option value="text">Text Channel</option>
                        <option value="voice">Voice Channel</option>
                        <option value="announcement">Announcement</option>
                    </select>
                </div>

                <div class="form-group">
                    <label class="label-heading">TOPIC</label>
                    <input type="text" id="edit-chan-topic" class="form-control" placeholder="Channel description..." oninput="updateActiveChannel()">
                </div>

                <!-- PERMISSIONS TOGGLE -->
                <div class="permissions-toggle-box">
                    <div>
                        <span style="font-weight:600; font-size:0.88rem; display:block;">Read-Only / Announcement</span>
                        <span style="font-size:0.75rem; color:var(--text-sub);">Only admins can send messages</span>
                    </div>
                    <input type="checkbox" id="edit-chan-readonly" style="width:18px; height:18px; accent-color: var(--accent-purple);" onchange="updateActiveChannel()">
                </div>
            </div>

            <div class="editor-card">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
                    <h3 style="margin-bottom:0;">Server Roles</h3>
                </div>
                <div id="roles-container" class="roles-flex"></div>
                <button class="add-btn-inline" onclick="addNewRole()">+ New Role</button>
            </div>

            <button id="next-page-btn" class="btn-glass" onclick="goToPermissionsPage()">
                <span>Next Page: Permissions</span> <i class="fa-solid fa-arrow-right"></i>
            </button>
        </div>

        <!-- PAGE 4: PERMISSIONS MANAGEMENT -->
        <div id="page-permissions" class="page">
            <div class="brand-header">
                <h1>Channel & Category Permissions</h1>
                <p>Configure permissions and apply them across multiple categories or channels simultaneously.</p>
            </div>

            <div class="editor-card">
                <div class="form-group">
                    <label class="label-heading">1. TARGET ROLE <span class="required">*</span></label>
                    <select id="perm-role-selector" class="form-control">
                        <option value="@everyone">@everyone</option>
                    </select>
                </div>

                <div class="form-group">
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom: 6px;">
                        <label class="label-heading" style="margin-bottom:0;">2. SELECT CHANNELS & CATEGORIES TO APPLY TO <span class="required">*</span></label>
                        <div style="font-size:0.75rem; color: var(--accent-purple); cursor:pointer;" onclick="toggleSelectAllChannels(true)">Select All</div>
                    </div>
                    <div id="sync-channel-list" class="sync-channel-checkboxes"></div>
                </div>

                <div class="form-group" style="margin-top: 18px;">
                    <label class="label-heading">3. CONFIGURE PERMISSIONS TO APPLY</label>
                    <div class="perm-grid-inline" id="inline-perm-grid">
                        <!-- Checkboxes populated via JS -->
                    </div>
                </div>

                <button class="btn-glass" style="margin-top: 18px; background: rgba(139, 92, 246, 0.25); border: 1px solid var(--accent-purple);" onclick="saveAndApplyPermissions()">
                    <i class="fa-solid fa-check-double"></i> Save & Apply Permissions
                </button>
            </div>

            <button id="submit-design-btn" class="btn-glass" onclick="handleInitialSubmitClick()">
                <i class="fa-solid fa-paper-plane"></i> <span id="submit-btn-label">Submit design to staff</span>
            </button>
        </div>

        <!-- PAGE 5: COMPLETION -->
        <div id="page-final" class="page" style="text-align: center;">
            <i class="fa-solid fa-circle-check fa-3x" style="color: var(--accent-green); margin-bottom: 18px; filter: drop-shadow(0 0 10px rgba(46, 204, 113, 0.4));"></i>
            <div class="brand-header">
                <h1 id="final-title-heading">Blueprint Submitted!</h1>
                <p id="final-desc-text">Your blueprint has been sent to staff for deployment review.</p>
            </div>

            <a id="bot-invite-btn" href="#" target="_blank" class="btn-glass" style="margin-bottom: 12px; background: rgba(255, 255, 255, 0.05); border: 1px solid var(--accent-purple); text-decoration: none;">
                <i class="fa-brands fa-discord"></i> Add Bot to Server
            </a>

            <button class="btn-glass" onclick="resetBuilder()" style="margin-top: 10px;">
                Start New Layout
            </button>
        </div>

    </div>

    <!-- NON-PREMIUM ROBUX NOTICE MODAL -->
    <div id="robux-notice-modal" class="modal-backdrop">
        <div class="modal-card" style="text-align: center;">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
                <h3 style="font-size:1.1rem; font-weight:700; color: #f1c40f;">
                    <i class="fa-solid fa-coins"></i> Standard Build Notice
                </h3>
                <i class="fa-solid fa-xmark" onclick="closeRobuxNoticeModal()" style="cursor:pointer; font-size:1.2rem;"></i>
            </div>

            <div class="robux-badge">
                <strong><i class="fa-solid fa-circle-info"></i> Notice:</strong> Building a custom server layout requires Robux unless you have an active <strong>TE Premium</strong> subscription (299 Robux/mo).
            </div>

            <p style="font-size:0.88rem; color: var(--text-sub); margin-bottom: 20px; line-height: 1.5;">
                Submitting this design will dispatch your blueprint to staff for manual deployment review.
            </p>

            <div style="display:flex; gap:10px;">
                <button class="btn-glass" style="background: rgba(255, 255, 255, 0.08); border: 1px solid var(--glass-inner-border);" onclick="closeRobuxNoticeModal()">
                    Cancel
                </button>
                <button class="btn-glass" onclick="confirmNonPremiumSubmission()">
                    Confirm & Submit
                </button>
            </div>
        </div>
    </div>

    <script>
        document.addEventListener("DOMContentLoaded", () => {
            if (!RECAPTCHA_SITE_KEY || RECAPTCHA_SITE_KEY.length < 5) {
                isCaptchaVerified = true;
            }
            checkAuthAndCaptchaStatus();
            applyPremiumTheme();
        });

        function applyPremiumTheme() {
            const btn = document.getElementById('submit-design-btn');
            const label = document.getElementById('submit-btn-label');

            if (label) {
                label.innerText = "Submit design to staff";
            }

            if (IS_TE_PREMIUM && btn) {
                btn.classList.add('btn-premium');
            } else if (btn) {
                btn.classList.remove('btn-premium');
            }
        }

        let currentLayout = { 
            server_name: "New Server", 
            roles: [
                { name: "Owner", color: "#f1c40f" },
                { name: "Admin", color: "#e74c3c" },
                { name: "Member", color: "#2ecc71" }
            ], 
            categories: [
                {
                    name: "INFORMATION",
                    emoji: "📌",
                    permissions: {},
                    channels: [
                        { name: "welcome", emoji: "👋", type: "text", topic: "Welcome channel", read_only: false, permissions: {} },
                        { name: "announcements", emoji: "📢", type: "text", topic: "Server updates", read_only: true, permissions: {} }
                    ]
                }
            ] 
        };
        let activeCatIdx = 0;
        let activeChanIdx = 0;

        const DISCORD_PERMISSIONS_LIST = [
            { id: "view", label: "View Channel" },
            { id: "send", label: "Send Messages" },
            { id: "embed", label: "Embed Links" },
            { id: "attach", label: "Attach Files" },
            { id: "history", label: "Read Message History" },
            { id: "connect", label: "Connect (Voice)" },
            { id: "speak", label: "Speak (Voice)" },
            { id: "manage", label: "Manage Channel" }
        ];

        function proceedToGenerator() {
            if (!IS_USER_LOGGED_IN) {
                window.location.href = "/login";
                return;
            }
            navigateTo('page-generator');
        }

        function navigateTo(pageId) {
            document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
            const targetPage = document.getElementById(pageId);
            if (targetPage) {
                targetPage.classList.add('active');
            }
            
            const panelWrapper = document.getElementById('panel-wrapper');
            if (pageId === 'page-layout' || pageId === 'page-permissions') {
                panelWrapper.classList.add('layout-mode');
            } else {
                panelWrapper.classList.remove('layout-mode');
            }

            if (pageId === 'page-generator' || pageId === 'page-final') {
                updateBotInviteLink();
            }

            if (pageId === 'page-permissions') {
                applyPremiumTheme();
                initPermissionsPage();
            }

            window.scrollTo({ top: 0, behavior: 'smooth' });
        }

        function stripEmoji(str) {
            return (str || '').replace(/[\u{1F600}-\u{1F64F}\u{1F300}-\u{1F5FF}\u{1F680}-\u{1F6FF}\u{2600}-\u{26FF}\u{2700}-\u{27BF}]/gu, '').trim();
        }

        function formatDiscordChannelName(name) {
            return stripEmoji(name)
                .toLowerCase()
                .replace(/[^a-z0-9\s-_]/g, '')
                .replace(/\s+/g, '-');
        }

        async function handleAIGeneration() {
            const serverId = document.getElementById('gen-server-id').value.trim();
            const serverLink = document.getElementById('gen-server-link').value.trim();
            const promptVal = document.getElementById('gen-prompt').value.trim();

            if (!serverId || !serverLink || !promptVal) {
                alert('Please fill in all required fields (Prompt, Server ID, and Invite Link).');
                return;
            }

            const btn = document.getElementById('generate-btn');
            btn.disabled = true;
            btn.innerHTML = `<span class="spinner"></span> Generating Layout with AI...`;

            try {
                const response = await fetch('/api/generate', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ 
                        prompt: promptVal,
                        server_id: serverId
                    })
                });

                const result = await response.json();
                if (result.success && result.data) {
                    currentLayout = result.data;
                } else if (result.error) {
                    alert('Generation error: ' + result.error);
                }
            } catch (err) {
                console.error(err);
                alert('An error occurred during AI generation. Please try again.');
            } finally {
                btn.disabled = false;
                btn.innerHTML = `<i class="fa-solid fa-wand-magic-sparkles"></i> <span>Generate Server Layout</span>`;
                activeCatIdx = 0;
                activeChanIdx = 0;
                renderDashboard();
                navigateTo('page-layout');
            }
        }

        function renderDashboard() {
            renderSidebar();
            renderActiveChannelDetails();
            renderRoles();
        }

        function renderSidebar() {
            const container = document.getElementById('sidebar-category-container');
            container.innerHTML = '';
            document.getElementById('server-title-heading').innerText = currentLayout.server_name || "Server Structure";

            (currentLayout.categories || []).forEach((cat, cIdx) => {
                const catDiv = document.createElement('div');
                catDiv.className = 'discord-category';

                const cleanCatName = stripEmoji(cat.name || 'CATEGORY');
                const displayName = cat.emoji ? `${cat.emoji} ${cleanCatName}` : cleanCatName;

                const headerDiv = document.createElement('div');
                headerDiv.className = 'discord-category-title';
                headerDiv.innerHTML = `
                    <span>▾ ${displayName}</span>
                    <i class="fa-solid fa-xmark" onclick="deleteCategory(${cIdx})" style="cursor:pointer;"></i>
                `;
                catDiv.appendChild(headerDiv);

                const channelList = document.createElement('div');
                (cat.channels || []).forEach((chan, chIdx) => {
                    const item = document.createElement('div');
                    const isActive = (cIdx === activeCatIdx && chIdx === activeChanIdx);
                    item.className = `discord-channel-item ${isActive ? 'active' : ''}`;
                    item.onclick = () => selectChannel(cIdx, chIdx);

                    const icon = chan.type === 'voice' ? '🔊' : '#';
                    const cleanName = formatDiscordChannelName(chan.name);

                    item.innerHTML = `
                        <div style="display:flex; align-items:center; gap:8px;">
                            <span>${icon}</span>
                            <span>${chan.emoji ? chan.emoji + ' ' : ''}${cleanName || 'channel'}</span>
                        </div>
                        <i class="fa-solid fa-xmark" onclick="event.stopPropagation(); deleteChannel(${cIdx}, ${chIdx})"></i>
                    `;
                    channelList.appendChild(item);
                });

                const addChanBtn = document.createElement('button');
                addChanBtn.className = 'add-btn-inline';
                addChanBtn.innerHTML = `+ add channel`;
                addChanBtn.onclick = () => addChannel(cIdx);

                catDiv.appendChild(channelList);
                catDiv.appendChild(addChanBtn);
                container.appendChild(catDiv);
            });
        }

        function selectChannel(cIdx, chIdx) {
            activeCatIdx = cIdx;
            activeChanIdx = chIdx;
            renderDashboard();
        }

        function renderActiveChannelDetails() {
            const editorCard = document.getElementById('channel-editor-card');
            const chan = currentLayout.categories[activeCatIdx]?.channels[activeChanIdx];
            
            if (!chan) {
                editorCard.style.display = 'none';
                return;
            }

            editorCard.style.display = 'block';
            const cleanName = formatDiscordChannelName(chan.name);
            document.getElementById('channel-settings-title').innerText = `Channel Settings — #${cleanName || 'channel'}`;
            document.getElementById('edit-chan-emoji').value = chan.emoji || '';
            document.getElementById('edit-chan-name').value = cleanName || '';
            document.getElementById('edit-chan-type').value = chan.type || 'text';
            document.getElementById('edit-chan-topic').value = chan.topic || '';
            document.getElementById('edit-chan-readonly').checked = !!chan.read_only;
        }

        function updateActiveChannel() {
            const chan = currentLayout.categories[activeCatIdx]?.channels[activeChanIdx];
            if (!chan) return;

            chan.emoji = document.getElementById('edit-chan-emoji').value.trim();
            chan.name = formatDiscordChannelName(document.getElementById('edit-chan-name').value);
            chan.type = document.getElementById('edit-chan-type').value;
            chan.topic = document.getElementById('edit-chan-topic').value;
            chan.read_only = document.getElementById('edit-chan-readonly').checked;

            renderSidebar();
        }

        function renderRoles() {
            const container = document.getElementById('roles-container');
            container.innerHTML = '';

            (currentLayout.roles || []).forEach((r, idx) => {
                const pill = document.createElement('div');
                pill.className = 'role-pill';
                pill.innerHTML = `
                    <span class="role-dot" style="background:${r.color || '#8b5cf6'}"></span>
                    <span>${r.name}</span>
                    <i class="fa-solid fa-xmark" onclick="deleteRole(${idx})" style="cursor:pointer; margin-left:4px;"></i>
                `;
                container.appendChild(pill);
            });
        }

        function addCategory() {
            if (!currentLayout.categories) currentLayout.categories = [];
            currentLayout.categories.push({ name: "NEW CATEGORY", emoji: "📁", permissions: {}, channels: [] });
            activeCatIdx = currentLayout.categories.length - 1;
            activeChanIdx = 0;
            renderDashboard();
        }

        function addChannel(cIdx) {
            if(!currentLayout.categories[cIdx].channels) currentLayout.categories[cIdx].channels = [];
            currentLayout.categories[cIdx].channels.push({ name: "new-channel", type: "text", read_only: false, permissions: {} });
            selectChannel(cIdx, currentLayout.categories[cIdx].channels.length - 1);
        }

        function deleteCategory(cIdx) {
            currentLayout.categories.splice(cIdx, 1);
            if (activeCatIdx >= currentLayout.categories.length) {
                activeCatIdx = Math.max(0, currentLayout.categories.length - 1);
            }
            activeChanIdx = 0;
            renderDashboard();
        }

        function deleteChannel(cIdx, chIdx) {
            currentLayout.categories[cIdx].channels.splice(chIdx, 1);
            if (activeChanIdx >= currentLayout.categories.length) {
                activeChanIdx = Math.max(0, currentLayout.categories.length - 1);
            }
            renderDashboard();
        }

        function addNewRole() {
            const name = prompt("Role name:");
            if (name) {
                if(!currentLayout.roles) currentLayout.roles = [];
                currentLayout.roles.push({ name, color: "#8b5cf6" });
                renderRoles();
            }
        }

        function deleteRole(idx) {
            currentLayout.roles.splice(idx, 1);
            renderRoles();
        }

        function goToPermissionsPage() {
            navigateTo('page-permissions');
        }

        function initPermissionsPage() {
            // Populate Target Role Dropdown
            const roleSelector = document.getElementById('perm-role-selector');
            roleSelector.innerHTML = `<option value="@everyone">@everyone</option>`;
            (currentLayout.roles || []).forEach(r => {
                roleSelector.innerHTML += `<option value="${r.name}">${r.name}</option>`;
            });

            // Populate all Channels & Categories list for multi-select
            populateMultiSelectChannelList();

            // Render all permissions checkboxes inline
            renderInlinePermissionsGrid();
        }

        function populateMultiSelectChannelList() {
            const syncContainer = document.getElementById('sync-channel-list');
            syncContainer.innerHTML = '';

            (currentLayout.categories || []).forEach((cat, cIdx) => {
                const catKey = `cat-${cIdx}`;
                const catName = stripEmoji(cat.name || `Category ${cIdx + 1}`);
                syncContainer.innerHTML += `
                    <label class="sync-item">
                        <input type="checkbox" value="${catKey}" class="target-perm-cb"> 
                        <span style="font-weight:700; color:#fff;">📁 CATEGORY: ${catName}</span>
                    </label>
                `;

                (cat.channels || []).forEach((chan, chIdx) => {
                    const chanKey = `chan-${cIdx}-${chIdx}`;
                    const cName = formatDiscordChannelName(chan.name);
                    syncContainer.innerHTML += `
                        <label class="sync-item" style="padding-left: 24px;">
                            <input type="checkbox" value="${chanKey}" class="target-perm-cb"> 
                            <span># ${cName}</span>
                        </label>
                    `;
                });
            });
        }

        function toggleSelectAllChannels(select) {
            document.querySelectorAll('.target-perm-cb').forEach(cb => {
                cb.checked = select;
            });
        }

        function renderInlinePermissionsGrid() {
            const grid = document.getElementById('inline-perm-grid');
            grid.innerHTML = '';

            DISCORD_PERMISSIONS_LIST.forEach(p => {
                grid.innerHTML += `
                    <div class="perm-item-inline">
                        <span style="font-size:0.85rem; font-weight:600;">${p.label}</span>
                        <input type="checkbox" id="inline-perm-cb-${p.id}" checked style="width:18px; height:18px; accent-color: var(--accent-purple);">
                    </div>
                `;
            });
        }

        function saveAndApplyPermissions() {
            const selectedTargets = document.querySelectorAll('.target-perm-cb:checked');
            if (selectedTargets.length === 0) {
                alert('Please check at least one Category or Channel from the list.');
                return;
            }

            const activeRole = document.getElementById('perm-role-selector').value;

            // Construct permissions buffer from inline grid
            const permBuffer = {};
            DISCORD_PERMISSIONS_LIST.forEach(p => {
                const cb = document.getElementById(`inline-perm-cb-${p.id}`);
                permBuffer[p.id] = cb ? cb.checked : true;
            });

            // Apply to all selected target objects
            selectedTargets.forEach(cb => {
                const key = cb.value;
                let targetObj = null;

                if (key.startsWith('cat-')) {
                    const cIdx = parseInt(key.split('-')[1]);
                    targetObj = currentLayout.categories[cIdx];
                } else if (key.startsWith('chan-')) {
                    const parts = key.split('-');
                    targetObj = currentLayout.categories[parseInt(parts[1])]?.channels[parseInt(parts[2])];
                }

                if (targetObj) {
                    if (!targetObj.permissions) targetObj.permissions = {};
                    targetObj.permissions[activeRole] = { ...permBuffer };
                }
            });

            // Reset selection and reset permission toggles to default
            document.querySelectorAll('.target-perm-cb').forEach(cb => cb.checked = false);
            DISCORD_PERMISSIONS_LIST.forEach(p => {
                const cb = document.getElementById(`inline-perm-cb-${p.id}`);
                if (cb) cb.checked = true;
            });

            alert(`Successfully applied permissions for [${activeRole}] to ${selectedTargets.length} selected target(s)! Form reset for next config.`);
        }

        function updateBotInviteLink() {
            const rawServerId = document.getElementById('gen-server-id')?.value.trim() || "";
            const sanitizedServerId = rawServerId.replace(/[^0-9]/g, '');
            
            if (!DISCORD_CLIENT_ID) {
                console.warn("DISCORD_CLIENT_ID missing; invite link cannot be generated.");
                return;
            }

            let inviteUrl = `https://discord.com/api/oauth2/authorize?client_id=${encodeURIComponent(DISCORD_CLIENT_ID)}&permissions=8&scope=bot%20applications.commands`;
            if (sanitizedServerId) {
                inviteUrl += `&guild_id=${sanitizedServerId}`;
            }

            const btn = document.getElementById('bot-invite-btn');
            if (btn) btn.href = inviteUrl;
        }

        function handleInitialSubmitClick() {
            const serverId = document.getElementById('gen-server-id').value.trim();
            const serverLink = document.getElementById('gen-server-link').value.trim();

            if (!serverId || !serverLink) {
                alert('Server ID and Server Invite Link are required to submit.');
                return;
            }

            if (!IS_TE_PREMIUM) {
                document.getElementById('robux-notice-modal').style.display = 'flex';
            } else {
                handleSubmitDesign();
            }
        }

        function closeRobuxNoticeModal() {
            document.getElementById('robux-notice-modal').style.display = 'none';
        }

        function confirmNonPremiumSubmission() {
            closeRobuxNoticeModal();
            handleSubmitDesign();
        }

        async function handleSubmitDesign() {
            const serverId = document.getElementById('gen-server-id').value.trim();
            const serverLink = document.getElementById('gen-server-link').value.trim();
            const promptVal = document.getElementById('gen-prompt').value.trim();

            const btn = document.getElementById('submit-design-btn');
            btn.disabled = true;
            btn.innerHTML = `<span class="spinner"></span> Dispatching Design...`;

            updateBotInviteLink();

            let channelCount = 0;
            (currentLayout.categories || []).forEach(c => {
                channelCount += (c.channels || []).length;
            });

            const payload = {
                target_server_id: serverId,
                server_name: currentLayout.server_name || "Custom Server",
                invite_link: serverLink,
                prompt: promptVal,
                categories_count: (currentLayout.categories || []).length,
                channels_count: channelCount,
                roles_count: (currentLayout.roles || []).length,
                layout: currentLayout
            };

            try {
                const response = await fetch('/api/submit-design', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                const resData = await response.json();
                
                const heading = document.getElementById('final-title-heading');
                const desc = document.getElementById('final-desc-text');

                if (resData.is_premium) {
                    if (heading) heading.innerText = "👑 TE Premium Design Dispatched!";
                    if (desc) desc.innerText = "Your blueprint has been sent to the staff channel for deployment!";
                } else {
                    if (heading) heading.innerText = "Blueprint Submitted to Staff!";
                    if (desc) desc.innerText = "Your blueprint has been sent to staff for deployment review.";
                }
            } catch (err) {
                console.error('Submission error:', err);
            } finally {
                btn.disabled = false;
                applyPremiumTheme();
                navigateTo('page-final');
            }
        }

        function resetBuilder() {
            document.getElementById('gen-prompt').value = '';
            document.getElementById('gen-server-id').value = '';
            document.getElementById('gen-server-link').value = '';
            currentLayout = {
                server_name: "New Server",
                roles: [],
                categories: []
            };
            activeCatIdx = 0;
            activeChanIdx = 0;
            navigateTo('page-generator');
        }
    </script>
</body>
</html>
