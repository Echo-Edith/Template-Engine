import os
import secrets
from datetime import datetime, timezone
import json
import asyncio
import requests
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from pymongo import MongoClient

# Import ai_brain module
import ai_brain

app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET", "").strip()
if not app.secret_key:
    # No hard-coded secret (anyone could forge logins with it). Set SESSION_SECRET in production.
    print("[Warning] SESSION_SECRET is not set. Using a random key: logins reset on every restart.")
    app.secret_key = secrets.token_hex(32)

DISCORD_CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "").strip()
DISCORD_CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET", "").strip()
DISCORD_REDIRECT_URI = os.environ.get("DISCORD_REDIRECT_URI", "").strip()

PREMIUM_DESIGN_CHANNEL_ID = "1563544108696477802"
NON_PREMIUM_DESIGN_CHANNEL_ID = "1563524101522464900"
DISCORD_BOT_TOKEN = (os.environ.get("DISCORD_BOT_TOKEN") or os.environ.get("BOT_TOKEN") or "").strip()

MONGO_URI = os.getenv("MONGO_URI")
db_premium = None
db_users = None
db_settings = None

if MONGO_URI:
    try:
        mongo_client = MongoClient(MONGO_URI)
        try:
            db = mongo_client.get_default_database()
        except Exception:
            db = mongo_client.get_database("static_studio_db")
        db_premium = db["premium_users"]
        db_users = db["persistent_users"]
        db_settings = db["settings"]
    except Exception as e:
        print(f"[MongoDB Warning in Flask] {e}")


def get_subscription_price() -> int:
    """Price shown on the site. Set by the bot's /change price command (same database)."""
    price = int(os.environ.get("DEFAULT_SUBSCRIPTION_PRICE", "299") or 299)
    if db_settings is not None:
        try:
            doc = db_settings.find_one({"_id": "prices"}) or {}
            value = doc.get("subscription_robux")
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                price = value
        except Exception as e:
            print(f"[Price Read Error] {e}")
    return price


SITE_THEMES = {"blueprint", "winter", "halloween", "christmas", "spring", "summer", "stpatrick"}


def get_site_theme() -> str:
    """Theme chosen with the bot's /theme command (same database). Falls back to the default."""
    if db_settings is not None:
        try:
            doc = db_settings.find_one({"_id": "website_theme"}) or {}
            if doc.get("theme") in SITE_THEMES:
                return doc["theme"]
        except Exception as e:
            print(f"[Theme Read Error] {e}")
    return "blueprint"


# Permanent staff always have premium (same list as the bot's HARDCODED_STAFF)
HARDCODED_STAFF = {"1219266886143967245"}


def is_user_premium(discord_id: str) -> bool:
    if not discord_id or discord_id == "0":
        return False
    if str(discord_id) in HARDCODED_STAFF:
        return True
    if db_premium is not None:
        try:
            doc = db_premium.find_one({"discord_id": str(discord_id)})
            if doc:
                raw = doc.get("expires_at")
                if raw:
                    try:
                        exp = datetime.fromisoformat(str(raw))
                        if exp.tzinfo is None:
                            exp = exp.replace(tzinfo=timezone.utc)
                        if exp <= datetime.now(timezone.utc):
                            return False  # premium ran out
                    except ValueError:
                        pass
                return True
        except Exception as e:
            print(f"[Mongo Premium Check Error] {e}")
    return False


@app.route("/")
def index():
    user = session.get("user")
    roblox_user = session.get("roblox_user")
    is_premium = False

    if user and user.get("id"):
        is_premium = is_user_premium(user["id"])
        if db_users is not None and not roblox_user:
            try:
                p_doc = db_users.find_one({"discord_id": str(user["id"])})
                if p_doc and p_doc.get("roblox_data"):
                    roblox_user = p_doc["roblox_data"]
                    session["roblox_user"] = roblox_user
            except Exception as e:
                print(f"[Mongo Fetch User Error] {e}")

    return render_template(
        "index.html",
        user=user,
        roblox_user=roblox_user,
        is_premium=is_premium,
        discord_client_id=DISCORD_CLIENT_ID,
        subscription_price=get_subscription_price(),
        site_theme=get_site_theme(),
        roblox_store_url=os.environ.get("ROBLOX_STORE_URL", "https://www.roblox.com/games/6094950399/Template-Engine#!/store").strip()
    )


@app.route("/login")
def login():
    if not DISCORD_CLIENT_ID or not DISCORD_REDIRECT_URI:
        return "Discord Auth configuration missing.", 500
    scope = "identify guilds"
    discord_auth_url = (
        f"https://discord.com/api/oauth2/authorize?client_id={DISCORD_CLIENT_ID}"
        f"&redirect_uri={requests.utils.quote(DISCORD_REDIRECT_URI)}&response_type=code&scope={scope}"
    )
    return redirect(discord_auth_url)


@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code:
        return redirect(url_for("index"))

    data = {
        "client_id": DISCORD_CLIENT_ID,
        "client_secret": DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": DISCORD_REDIRECT_URI
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    try:
        token_res = requests.post("https://discord.com/api/oauth2/token", data=data, headers=headers)
        token_json = token_res.json()
        access_token = token_json.get("access_token")

        if not access_token:
            return redirect(url_for("index"))

        user_res = requests.get(
            "https://discord.com/api/users/@me",
            headers={"Authorization": f"Bearer {access_token}"}
        )
        user_data = user_res.json()

        avatar_id = user_data.get("avatar")
        user_id = user_data.get("id")
        avatar_url = f"https://cdn.discordapp.com/avatars/{user_id}/{avatar_id}.png" if avatar_id else "https://cdn.discordapp.com/embed/avatars/0.png"

        session["user"] = {
            "id": user_id,
            "username": user_data.get("username"),
            "discriminator": user_data.get("discriminator"),
            "avatar": avatar_url
        }
    except Exception as e:
        print(f"[OAuth Callback Error] {e}")

    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/generate-blueprint", methods=["POST"])
def generate_blueprint_endpoint():
    try:
        data = request.get_json() or {}
        prompt = data.get("prompt", "").strip()
        server_id = data.get("server_id", "").strip()

        if not prompt:
            return jsonify({"success": False, "error": "Prompt is required."}), 400

        user = session.get("user")
        if not user:
            return jsonify({"success": False, "error": "Unauthorized. Please log in with Discord."}), 401

        # Call ai_brain.generate_server_layout directly via asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            ai_result = loop.run_until_complete(
                ai_brain.generate_server_layout(
                    prompt=prompt,
                    server_id=server_id,
                    user_info=user
                )
            )
        finally:
            loop.close()

        if not ai_result or not ai_result.get("success"):
            error_msg = ai_result.get("error") if ai_result else "AI layout generation failed."
            return jsonify({"success": False, "error": error_msg}), 500

        blueprint = ai_result.get("data", {})
        return jsonify({"success": True, "blueprint": blueprint}), 200

    except Exception as e:
        print(f"[Generate Blueprint Error] {e}")
        return jsonify({"success": False, "error": f"Generation failed: {str(e)}"}), 500


@app.route("/api/submit-design", methods=["POST"])
def submit_design_endpoint():
    user = session.get("user")
    if not user:
        return jsonify({"success": False, "error": "Unauthorized. Please log in with Discord."}), 401

    payload = request.get_json() or {}
    user_id = user.get("id", "0")
    is_premium = is_user_premium(user_id)

    target_channel_id = PREMIUM_DESIGN_CHANNEL_ID if is_premium else NON_PREMIUM_DESIGN_CHANNEL_ID

    target_server_id = payload.get("target_server_id", "Not Provided")
    invite_link = payload.get("invite_link", "Not Provided")
    prompt_text = payload.get("prompt", "No prompt description provided.")
    server_name = payload.get("server_name", "Custom Server")

    categories_count = payload.get("categories_count", 0)
    channels_count = payload.get("channels_count", 0)
    roles_count = payload.get("roles_count", 0)
    layout = payload.get("layout", {})

    account_age = ai_brain.calculate_account_age(user_id)

    embed_title = "Premium Auto-Build Submitted" if is_premium else "Standard Design Submitted"
    embed_color = 0xC9962B if is_premium else 0xE0661A

    # Classic embed on purpose: the bot reads this message (and its .json file) to start the build and send the DM,
    # so it must work on every discord.py version.
    embed_payload = {
        "title": embed_title,
        "description": ai_brain.log_description([
            ("User", f"<@{user_id}>"),
            ("User ID", user_id),
            ("Account Age", account_age),
            ("Target Server ID", target_server_id),
            ("Invite Link", invite_link),
            ("Categories", categories_count),
            ("Channels", channels_count),
            ("Roles", roles_count),
            ("Prompt", prompt_text[:900] if len(str(prompt_text)) > 80 else prompt_text),
        ]),
        "color": embed_color,
        "footer": {"text": "Template Engine Infrastructure Log"},
        "timestamp": ai_brain.datetime.now(ai_brain.timezone.utc).isoformat()
    }

    attachment_data = {
        "submitting_user_id": user_id,
        "target_server_id": target_server_id,
        "server_name": server_name,
        "invite_link": invite_link,
        "prompt": prompt_text,
        "is_premium": is_premium,
        "layout": layout
    }

    json_bytes = json.dumps(attachment_data, indent=2).encode("utf-8")

    delivered = False
    delivery_error = "The bot token is not configured on the server."

    if DISCORD_BOT_TOKEN:
        try:
            url = f"https://discord.com/api/v10/channels/{target_channel_id}/messages"
            headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}

            payload_json = {
                "embeds": [embed_payload]
            }

            files = {
                "payload_json": (None, json.dumps(payload_json), "application/json"),
                "files[0]": ("blueprint.json", json_bytes, "application/json")
            }

            res = requests.post(url, headers=headers, files=files, timeout=20)
            if res.status_code in (200, 201):
                delivered = True
            else:
                delivery_error = f"Discord rejected the submission (HTTP {res.status_code})."
                print(f"[Discord API Log Drop] Status {res.status_code}: {res.text}")
        except Exception as e:
            delivery_error = "Could not reach Discord."
            print(f"[Discord Log Exception] {e}")

    if not delivered:
        return jsonify({
            "success": False,
            "error": f"Your design was not sent. {delivery_error} Please try again later."
        }), 502

    return jsonify({
        "success": True,
        "is_premium": is_premium,
        "message": "Design submitted successfully!"
    }), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=os.environ.get("FLASK_DEBUG") == "1")
