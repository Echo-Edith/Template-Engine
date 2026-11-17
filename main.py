import os
import asyncio
import threading
from flask import Flask, jsonify
import discord
from discord.ext import commands
import brand

SECRET_KEY = os.environ.get("SESSION_SECRET", "template_engine_secret_key_1337").strip()

web_app = Flask(__name__)
web_app.secret_key = SECRET_KEY

@web_app.route("/")
@web_app.route("/health")
def health_check():
    return jsonify({
        "status": "online",
        "bot": str(bot.user) if bot.user else "initializing"
    }), 200

def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    import logging
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)
    web_app.run(host="0.0.0.0", port=port)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    help_command=None,
    max_messages=20000  # how many recent messages the bot remembers (the message log needs this to show old text)
)

@bot.event
async def setup_hook():
    try:
        # features must load first: commands.py imports helpers from it
        for ext in ("cogs.features", "cogs.commands"):
            if ext not in bot.extensions:
                await bot.load_extension(ext)
                print(f"[Setup Hook] Loaded extension: {ext}")
            else:
                await bot.reload_extension(ext)
                print(f"[Setup Hook] Reloaded extension: {ext}")

        brand.rebrand_tree(bot.tree)
        synced = await bot.tree.sync()
        print(f"[Setup Hook] Successfully registered {len(synced)} slash commands globally!")
    except Exception as e:
        print(f"[Setup Hook Error] Failed syncing slash commands: {e}")

@bot.event
async def on_ready():
    print("-----------------------------------------")
    print(f"Logged in as: {bot.user} (ID: {bot.user.id})")
    print("-----------------------------------------")
    try:
        brand.rebrand_tree(bot.tree)
        synced = await bot.tree.sync()
        print(f"[on_ready] Verified global slash commands ({len(synced)} active).")
    except Exception as e:
        print(f"[on_ready Error] {e}")

@bot.command(name="sync")
async def sync_cmd(ctx: commands.Context):
    if ctx.author.id == 1219266886143967245:  # BOT_OWNER_ID
        msg = await ctx.send("Syncing commands globally with Discord...")
        try:
            brand.rebrand_tree(bot.tree)
            synced = await bot.tree.sync()
            await msg.edit(content=f"Successfully synced **{len(synced)}** slash commands globally across Discord!")
        except Exception as e:
            await msg.edit(content=f"Sync error: `{e}`")

async def runner():
    token = os.environ.get("DISCORD_BOT_TOKEN") or os.environ.get("BOT_TOKEN")
    if not token or not token.strip():
        print("CRITICAL ERROR: DISCORD_BOT_TOKEN missing!")
        return

    async with bot:
        await bot.start(token.strip())

def keep_awake():
    """Render free web services sleep after about 15 minutes without a visit. This visits our own public address."""
    import time
    import urllib.request
    url = os.environ.get("RENDER_EXTERNAL_URL", "").strip()
    if not url:
        return
    time.sleep(60)
    while True:
        try:
            urllib.request.urlopen(url.rstrip("/") + "/health", timeout=15).read()
        except Exception as e:
            print(f"[Keep Awake] {e!r}")
        time.sleep(600)

def main():
    health_thread = threading.Thread(target=run_health_server, daemon=True)
    health_thread.start()
    threading.Thread(target=keep_awake, daemon=True).start()
    print("Background web server started on port 10000.")

    try:
        asyncio.run(runner())
        print("[Main] The bot stopped without an error.")
    except KeyboardInterrupt:
        print("Bot runner shutdown gracefully.")
    except BaseException as e:
        # Print the reason in the logs, wait a little (so Discord does not block fast restarts), then exit so the host restarts us.
        import traceback, time
        print(f"[Main] The bot crashed: {e!r}")
        traceback.print_exc()
        time.sleep(15)
        raise

if __name__ == "__main__":
    main()
