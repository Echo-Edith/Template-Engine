import os
import asyncio
import threading
from flask import Flask, request, jsonify
import discord
from discord.ext import commands

web_app = Flask(__name__)

bot_cog_ref = None


@web_app.route("/")
@web_app.route("/health")
def health_check():
    return jsonify({
        "status": "online",
        "bot": str(bot.user) if bot.user else "initializing"
    }), 200


@web_app.route("/api/auto-build", methods=["POST"])
def auto_build_endpoint():
    data = request.get_json() or {}
    user_id = data.get("user_id")
    target_server_id = data.get("target_server_id")
    blueprint_url = data.get("blueprint_url")
    blueprint_data = data.get("blueprint_data")
    log_channel_id = data.get("log_channel_id", "1563544108696477802")

    if not target_server_id or not blueprint_data:
        return jsonify({"success": False, "error": "Missing target_server_id or blueprint_data"}), 400

    if bot_cog_ref and bot.loop:
        asyncio.run_coroutine_threadsafe(
            bot_cog_ref.handle_direct_handshake_build(
                user_id=user_id,
                target_server_id=target_server_id,
                blueprint_url=blueprint_url,
                blueprint_data=blueprint_data,
                log_channel_id=log_channel_id
            ),
            bot.loop
        )
        return jsonify({"success": True, "message": "Direct handshake build queued in background"}), 200

    return jsonify({"success": False, "error": "Bot instance or event loop not ready"}), 503


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
    global bot_cog_ref
    print("-----------------------------------------")
    print(f"Logged in as: {bot.user} (ID: {bot.user.id})")
    print("-----------------------------------------")

    try:
        if "cogs.bot" not in bot.extensions:
            await bot.load_extension("cogs.bot")
            print("Successfully loaded extension cog: cogs.bot")
        else:
            await bot.reload_extension("cogs.bot")
            print("Successfully reloaded extension cog: cogs.bot")

        bot_cog_ref = bot.get_cog("BotCog")
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
    print("Background health server and handshake API started on port 10000.")

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        print("Bot runner shutdown gracefully.")


if __name__ == "__main__":
    main()
