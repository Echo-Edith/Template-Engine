import os
import asyncio
import threading
from flask import Flask, jsonify, request
import discord
from discord.ext import commands

# Render Web Services require an open HTTP port.
web_app = Flask(__name__)

# Shared global event loop for thread-safe bot execution
bot_loop = None

@web_app.route("/")
@web_app.route("/health")
def health_check():
    return jsonify({
        "status": "online",
        "bot": str(bot.user) if bot.user else "initializing"
    }), 200


@web_app.route("/api/bot-autobuild", methods=["POST"])
def trigger_autobuild():
    """Endpoint called by keep_alive.py (Vercel) to trigger instant server builds for TE Premium users."""
    data = request.get_json() or {}
    secret = data.get("secret", "")
    expected_secret = os.getenv("FLASK_SECRET_KEY", "super-secret-key-fallback").strip()

    if secret != expected_secret:
        return jsonify({"success": False, "error": "Unauthorized internal request."}), 401

    target_server_id = data.get("target_server_id")
    layout = data.get("layout")
    user_id = data.get("user_id")

    if not target_server_id or not layout:
        return jsonify({"success": False, "error": "Missing target_server_id or layout."}), 400

    try:
        guild_id = int(target_server_id)
        guild = bot.get_guild(guild_id)
    except Exception:
        guild = None

    if not guild:
        return jsonify({
            "success": False,
            "error": f"Bot is not in the target server (ID: {target_server_id}). Please invite the bot first!"
        }), 404

    # Fetch BotCog instance and execute build thread-safely
    bot_cog = bot.get_cog("BotCog")
    if not bot_cog:
        return jsonify({"success": False, "error": "BotCog is not loaded on bot runner."}), 500

    if bot_loop and bot_loop.is_running():
        future = asyncio.run_coroutine_threadsafe(
            bot_cog.execute_blueprint_build(guild, layout),
            bot_loop
        )
        try:
            future.result(timeout=60) # Wait up to 60s for build completion
            return jsonify({
                "success": True,
                "message": f"Successfully auto-built server '{guild.name}' ({guild.id})!"
            }), 200
        except Exception as e:
            return jsonify({"success": False, "error": f"Build execution failed: {str(e)}"}), 500
    else:
        return jsonify({"success": False, "error": "Bot loop is not running."}), 500


def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    import logging
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)
    web_app.run(host="0.0.0.0", port=port)


intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    help_command=None
)


@bot.event
async def on_ready():
    global bot_loop
    bot_loop = asyncio.get_running_loop()

    print("-----------------------------------------")
    print(f"Logged in as: {bot.user} (ID: {bot.user.id})")
    print("-----------------------------------------")

    try:
        if "cogs.bot" not in bot.extensions:
            await bot.load_extension("cogs.bot")
            print("Successfully loaded extension cog: cogs.bot")
    except Exception as e:
        print(f"CRITICAL ERROR loading cog cogs.bot: {e}")

    try:
        synced = await bot.tree.sync()
        print(f"Successfully registered & synced {len(synced)} global slash command(s).")
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
    print("Background health server started on port 10000.")

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        print("Bot runner shutdown gracefully.")


if __name__ == "__main__":
    main()
