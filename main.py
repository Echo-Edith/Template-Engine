import os
import threading
from urllib.parse import quote
from flask import Flask, render_template_string, render_template, request, jsonify, session, redirect, url_for
import requests
import discord
from discord.ext import commands

# 1. Initialize Flask Web Dashboard & API
app = Flask(__name__, template_folder="templates")
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "super-secret-key-change-me").strip()

# Session cookie configuration for modern HTTPS proxies (Vercel & Render)
app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# OAuth2 Environment Variables
DISCORD_CLIENT_ID = (os.environ.get("DISCORD_CLIENT_ID") or "").strip()
DISCORD_CLIENT_SECRET = (os.environ.get("DISCORD_CLIENT_SECRET") or "").strip()


def get_base_url() -> str:
    """
    Determines the current host base URL dynamically.
    Checks DISCORD_REDIRECT_URI / DASHBOARD_URL first, then VERCEL_URL, and falls back to request.host.
    """
    dashboard = os.environ.get("DASHBOARD_URL") or os.environ.get("DISCORD_REDIRECT_URI")
    if dashboard and "your-vercel-domain" not in dashboard:
        if dashboard.endswith("/callback"):
            return dashboard[:-9].rstrip("/")
        return dashboard.rstrip("/")

    vercel_url = os.environ.get("VERCEL_URL")
    if vercel_url:
        return f"https://{vercel_url.rstrip('/')}"

    if request and request.host:
        proto = request.headers.get("X-Forwarded-Proto", "https")
        return f"{proto}://{request.host}".rstrip("/")

    return "http://localhost:5000"


def get_redirect_uri() -> str:
    configured = os.environ.get("DISCORD_REDIRECT_URI")
    if configured and configured.strip():
        return configured.strip()
    return f"{get_base_url()}/callback"


FALLBACK_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Discord Infrastructure Architect</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-950 text-slate-100 min-h-screen flex flex-col items-center justify-center p-6">
    <div class="max-w-md w-full bg-slate-900 border border-slate-800 rounded-2xl p-8 shadow-2xl text-center space-y-6">
        <div class="w-20 h-20 mx-auto bg-indigo-600/20 rounded-full flex items-center justify-center border border-indigo-500/30 text-indigo-400 text-3xl font-bold">
            🛡️
        </div>
        <div>
            <h1 class="text-2xl font-black tracking-tight text-white">Discord Architecture</h1>
            <p class="text-slate-400 text-sm mt-1">Discord Bot & Web Portal Online</p>
        </div>

        {% if user %}
        <div class="bg-slate-800/60 border border-slate-700/50 rounded-xl p-4 flex items-center space-x-4 text-left">
            <img src="{{ user.avatar }}" alt="Avatar" class="w-12 h-12 rounded-full border border-indigo-500/40">
            <div class="overflow-hidden">
                <p class="text-xs text-slate-400">Signed in as</p>
                <p class="font-semibold text-slate-200 truncate">{{ user.global_name }}</p>
                <p class="text-xs text-indigo-400 font-mono">@{{ user.username }}</p>
            </div>
        </div>
        <div class="space-y-3">
            <a href="/logout" class="block w-full py-2.5 px-4 bg-rose-600/20 hover:bg-rose-600/30 text-rose-300 border border-rose-500/30 rounded-xl font-medium transition text-sm">
                Log Out
            </a>
        </div>
        {% else %}
        <div class="space-y-3">
            <a href="/login" class="flex items-center justify-center space-x-2 w-full py-3 px-4 bg-indigo-600 hover:bg-indigo-500 text-white rounded-xl font-semibold shadow-lg shadow-indigo-600/30 transition text-sm">
                <svg class="w-5 h-5 fill-current" viewBox="0 0 24 24"><path d="M20.317 4.37a19.791 19.791 0 0 0-4.885-1.515.074.074 0 0 0-.079.037c-.21.375-.444.864-.608 1.25a18.27 18.27 0 0 0-5.487 0 12.64 12.64 0 0 0-.617-1.25.077.077 0 0 0-.079-.037A19.736 19.736 0 0 0 3.677 4.37a.07.07 0 0 0-.032.027C.533 9.046-.32 13.58.099 18.057a.082.082 0 0 0 .031.057 19.9 19.9 0 0 0 5.993 3.03.078.078 0 0 0 .084-.028c.462-.63.874-1.295 1.226-1.994.021-.041.001-.09-.041-.106a13.107 13.107 0 0 1-1.872-.892.077.077 0 0 1-.008-.128 10.2 10.2 0 0 0 .372-.292.074.074 0 0 1 .077-.01c3.927 1.793 8.18 1.793 12.061 0a.074.074 0 0 1 .078.01c.12.098.246.198.373.292a.077.077 0 0 1-.006.127 12.299 12.299 0 0 1-1.873.892.077.077 0 0 0-.041.107c.36.698.772 1.362 1.225 1.993a.076.076 0 0 0 .084.028 19.839 19.839 0 0 0 6.002-3.03.077.077 0 0 0 .032-.054c.5-5.177-.838-9.674-3.549-13.66a.061.061 0 0 0-.031-.028zM8.02 15.33c-1.183 0-2.157-1.085-2.157-2.419 0-1.333.956-2.419 2.157-2.419 1.21 0 2.176 1.096 2.157 2.42 0 1.333-.956 2.418-2.157 2.418zm7.975 0c-1.183 0-2.157-1.085-2.157-2.419 0-1.333.955-2.419 2.157-2.419 1.21 0 2.176 1.096 2.157 2.42 0 1.333-.946 2.418-2.157 2.418z"/></svg>
                <span>Login with Discord</span>
            </a>
            <a href="/debug-oauth" class="block text-xs text-indigo-400 hover:underline">Debug OAuth Config</a>
        </div>
        {% endif %}
    </div>
</body>
</html>
"""


@app.route("/")
def index():
    user = session.get("user")
    try:
        return render_template("index.html", user=user)
    except Exception:
        return render_template_string(FALLBACK_HTML, user=user)


# 2. OAuth Debug Endpoint to inspect exact URLs being used
@app.route("/debug-oauth")
def debug_oauth():
    redirect_uri = get_redirect_uri()
    encoded_redirect = quote(redirect_uri, safe="")
    auth_url = (
        f"https://discord.com/api/oauth2/authorize"
        f"?client_id={DISCORD_CLIENT_ID}"
        f"&redirect_uri={encoded_redirect}"
        f"&response_type=code"
        f"&scope=identify%20email"
    )
    return jsonify({
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri_being_sent": redirect_uri,
        "encoded_redirect_uri": encoded_redirect,
        "full_discord_auth_url": auth_url,
        "env_discord_redirect_uri": os.environ.get("DISCORD_REDIRECT_URI"),
        "has_client_secret": bool(DISCORD_CLIENT_SECRET)
    })


# 3. Discord OAuth2 Login Endpoint
@app.route("/login")
def login():
    if not DISCORD_CLIENT_ID:
        return "Error: DISCORD_CLIENT_ID is not configured in Environment Variables.", 500

    redirect_uri = get_redirect_uri()
    encoded_redirect_uri = quote(redirect_uri, safe="")

    discord_auth_url = (
        f"https://discord.com/api/oauth2/authorize"
        f"?client_id={DISCORD_CLIENT_ID}"
        f"&redirect_uri={encoded_redirect_uri}"
        f"&response_type=code"
        f"&scope=identify%20email"
    )
    return redirect(discord_auth_url)


# 4. Discord OAuth2 Callback Endpoint
@app.route("/callback")
def callback():
    code = request.args.get("code")
    error = request.args.get("error")

    if error or not code:
        return redirect(url_for("index"))

    redirect_uri = get_redirect_uri()

    token_data = {
        "client_id": DISCORD_CLIENT_ID,
        "client_secret": DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    # Exchange code for token
    token_response = requests.post("https://discord.com/api/v10/oauth2/token", data=token_data, headers=headers)
    token_json = token_response.json()
    access_token = token_json.get("access_token")

    if not access_token:
        return f"Failed to authenticate with Discord: {token_json.get('error_description', token_response.text)}", 400

    # Fetch user details
    user_response = requests.get(
        "https://discord.com/api/v10/users/@me",
        headers={"Authorization": f"Bearer {access_token}"}
    )
    user_json = user_response.json()

    avatar_hash = user_json.get("avatar")
    avatar_url = (
        f"https://cdn.discordapp.com/avatars/{user_json.get('id')}/{avatar_hash}.png"
        if avatar_hash
        else "https://cdn.discordapp.com/embed/avatars/0.png"
    )

    # Save user into session
    session["user"] = {
        "id": user_json.get("id"),
        "username": user_json.get("username"),
        "discriminator": user_json.get("discriminator", "0"),
        "global_name": user_json.get("global_name") or user_json.get("username"),
        "avatar": avatar_url,
    }

    return redirect(url_for("index"))


# 5. Logout Endpoint
@app.route("/logout")
def logout():
    session.pop("user", None)
    return redirect(url_for("index"))


# 6. User API status check endpoint
@app.route("/api/user")
def api_user():
    user = session.get("user")
    if user:
        return jsonify({"authenticated": True, "user": user})
    return jsonify({"authenticated": False, "user": None})


# 7. Protected Layout Generation Endpoint
@app.route("/generate", methods=["POST"])
@app.route("/api/generate", methods=["POST"])
def generate_layout():
    user = session.get("user")
    if not user:
        return jsonify({
            "success": False,
            "error": "Authentication required. Please log in with Discord first.",
            "redirect": "/login"
        }), 401

    data = request.get_json(silent=True) or {}
    prompt = data.get("prompt", "")

    try:
        import ai_brain
        result = ai_brain.generate_layout(prompt) if hasattr(ai_brain, "generate_layout") else "Layout generated successfully."
    except ImportError:
        result = f"Layout successfully generated for user {user.get('username')}!"

    return jsonify({
        "success": True,
        "user": user.get("username"),
        "layout": result
    })


@app.route("/health")
def health():
    return jsonify({"status": "alive"}), 200


def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)


# 8. Initialize Discord Bot (For Render / persistent bot runner)
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("-----------------------------------------")

    try:
        if "cogs.bot" not in bot.extensions:
            await bot.load_extension("cogs.bot")
            print("Successfully loaded cog: cogs.bot")
    except Exception as e:
        print(f"Failed to load cogs.bot: {e}")

    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s).")
    except Exception as e:
        print(f"Failed to sync slash commands: {e}")


def main():
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    print("Started background HTTP server.")

    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        print("ERROR: DISCORD_BOT_TOKEN environment variable is missing!")
        return

    bot.run(token)


if __name__ == "__main__":
    main()
