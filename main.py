import os
import asyncio
import threading
from flask import Flask, jsonify
import discord
from discord.ext import commands

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
    help_command=None
)

@bot.event
async def setup_hook():
    try:
        if "cogs.bot" not in bot.extensions:
            await bot.load_extension("cogs.bot")
            print("[Setup Hook] Loaded extension: cogs.bot")
        else:
            await bot.reload_extension("cogs.bot")
            print("[Setup Hook] Reloaded extension: cogs.bot")
            
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
        synced = await bot.tree.sync()
        print(f"[on_ready] Verified global slash commands ({len(synced)} active).")
    except Exception as e:
        print(f"[on_ready Error] {e}")

@bot.command(name="sync")
async def sync_cmd(ctx: commands.Context):
    if ctx.author.id == 1219266886143967245:  # BOT_OWNER_ID
        msg = await ctx.send("Syncing commands globally with Discord...")
        try:
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

def main():
    health_thread = threading.Thread(target=run_health_server, daemon=True)
    health_thread.start()
    print("Background web server started on port 10000.")

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        print("Bot runner shutdown gracefully.")

if __name__ == "__main__":
    main()
