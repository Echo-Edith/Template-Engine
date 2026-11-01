import os
import json
import asyncio
import requests
from flask import Flask, request, jsonify, render_template, session, redirect, url_for
from pymongo import MongoClient

# Attempt importing ai_brain if present in directory
try:
    import ai_brain
    AI_BRAIN_AVAILABLE = True
except ImportError:
    AI_BRAIN_AVAILABLE = False

app = Flask(__name__, template_folder="templates")
app.secret_key = os.environ.get("SESSION_SECRET", "template_engine_secret_key_1337").strip()

# Database & Environment Setup
MONGO_URI = os.environ.get("MONGO_URI")
DISCORD_CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "").strip()
DISCORD_CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET", "").strip()
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
PREMIUM_DESIGN_CHANNEL_ID = "1563544108696477802"

db_users = None
db_premium = None

if MONGO_URI:
    try:
        mongo_client = MongoClient(MONGO_URI)
        try:
            db = mongo_client.get_default_database()
        except Exception:
            db = mongo_client.get_database("static_studio_db")
        db_users = db["persistent_users"]
        db_premium = db["premium_users"]
        print("[MongoDB Keep-Alive] Connected successfully.")
    except Exception as e:
        print(f"[MongoDB Warning] {e}")


def strip_emojis(text: str) -> str:
    import re
    if not text:
        return ""
    clean = re.sub(r'[\U00010000-\U0010ffff\u2600-\u27ff\u2300-\u23ff]', '', text)
    return clean.strip()


@app.route('/')
def index():
    user = session.get('user')
    roblox_user = None
    is_premium = False

    if user and db_users:
        doc = db_users.find_one({"discord_id": str(user.get("id"))})
        if doc:
            roblox_user = doc.get("roblox_data")

    if user and db_premium:
        p_doc = db_premium.find_one({"user_id": str(user.get("id"))})
        if p_doc:
            is_premium = True

    return render_template(
        'index.html',
        user=user,
        roblox_user=roblox_user,
        is_premium=is_premium,
        discord_client_id=DISCORD_CLIENT_ID,
        roblox_store_url="https://www.roblox.com"
    )


@app.route('/generate-blueprint', methods=['POST'])
def generate_blueprint():
    data = request.get_json() or {}
    prompt = data.get('prompt', '').strip()
    server_id = data.get('server_id', '').strip()

    if not prompt:
        return jsonify({"success": False, "error": "Prompt description is required."}), 400

    # Run AI generation via ai_brain if available
    if AI_BRAIN_AVAILABLE:
        try:
            print(f"Triggering ai_brain generation for prompt: {prompt}")
            ai_result = asyncio.run(ai_brain.generate_server_layout(
                prompt=prompt,
                server_id=server_id,
                user_info={"id": "web_user", "username": "Web Client"}
            ))

            if isinstance(ai_result, dict) and ai_result.get("success"):
                layout_data = ai_result.get("data", {})

                # Process categories and extract clean channel names directly without default replacements
                for category in layout_data.get("categories", []):
                    category["name"] = strip_emojis(category.get("name", ""))

                    for channel in category.get("channels", []):
                        # Extract exact channel name from AI response without forced fallbacks
                        raw_name = (
                            channel.get("name") or 
                            channel.get("clean_name") or 
                            channel.get("title") or 
                            channel.get("formatted_name") or 
                            ""
                        )
                        channel["name"] = strip_emojis(str(raw_name)).strip()

                return jsonify({
                    "success": True,
                    "blueprint": layout_data
                }), 200
            else:
                err_msg = ai_result.get("error") if isinstance(ai_result, dict) else "AI generation failed."
                return jsonify({
                    "success": False,
                    "error": f"AI layout generation failed: {err_msg}"
                }), 400

        except Exception as e:
            print(f"[AI Generation Exception] {e}")
            return jsonify({
                "success": False,
                "error": f"AI layout generation failed: {str(e)}"
            }), 400

    # Stop and return error if GEMINI API / AI module is unavailable; no premade layouts used
    return jsonify({
        "success": False,
        "error": "AI generation service is currently unavailable. GEMINI_API_KEY environment variable is not configured."
    }), 500


@app.route('/api/submit-design', methods=['POST'])
def submit_design():
    data = request.get_json() or {}
    user = session.get('user', {})
    user_id = str(user.get('id', '1219266886143967245'))
    username = str(user.get('username', 'Web User'))

    is_premium = False
    if db_premium and user_id:
        p_doc = db_premium.find_one({"user_id": user_id})
        if p_doc:
            is_premium = True

    target_server_id = data.get('target_server_id', '')
    server_name = data.get('server_name', 'Custom Server')
    invite_link = data.get('invite_link', '')
    prompt = data.get('prompt', '')
    layout = data.get('layout', {})

    categories_count = data.get('categories_count', 0)
    channels_count = data.get('channels_count', 0)
    roles_count = data.get('roles_count', 0)

    payload = {
        "submitting_user_id": user_id,
        "submitting_username": username,
        "is_premium": is_premium,
        "target_server_id": target_server_id,
        "server_name": server_name,
        "invite_link": invite_link,
        "prompt": prompt,
        "layout": layout
    }

    # Dispatch to Discord Channel Log via Bot HTTP API
    if DISCORD_BOT_TOKEN:
        headers = {
            "Authorization": f"Bot {DISCORD_BOT_TOKEN}"
        }

        embed = {
            "title": "👑 TE Premium Instant Auto-Build Dispatch" if is_premium else "📋 Standard Design Submitted",
            "color": 0xF1C40F if is_premium else 0x8B5CF6,
            "fields": [
                {"name": "👤 User", "value": f"<@{user_id}> (`{user_id}`)", "inline": True},
                {"name": "🎯 Target Guild ID", "value": f"`{target_server_id}`", "inline": True},
                {"name": "🔗 Server Invite", "value": invite_link or "`N/A`", "inline": False},
                {"name": "📊 Layout Overview", "value": f"**Categories:** {categories_count} | **Channels:** {channels_count} | **Roles:** {roles_count}", "inline": False},
                {"name": "📝 Prompt Description", "value": prompt or "*No prompt specified*", "inline": False}
            ],
            "footer": {"text": "Template Engine Infrastructure Log"}
        }

        files = {
            "payload_json": ("payload.json", json.dumps(embed), "application/json"),
            "file": ("blueprint.json", json.dumps(payload, indent=2), "application/json")
        }

        try:
            url = f"https://discord.com/api/v10/channels/{PREMIUM_DESIGN_CHANNEL_ID}/messages"
            resp = requests.post(url, headers=headers, files=files)
            print(f"[Discord Log Post] Status Code: {resp.status_code}")
        except Exception as e:
            print(f"[Discord Log Post Error] {e}")

    return jsonify({
        "success": True,
        "is_premium": is_premium,
        "message": "Design submitted successfully!"
    }), 200


def run():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)


if __name__ == '__main__':
    run()
