import os
def set_user_premium(user_id: str, duration_td: timedelta):
    user_id_str = str(user_id)
    now = datetime.now(timezone.utc)
    expires_at = now + duration_td

    if db_premium is not None:
        db_premium.update_one(
            {"user_id": user_id_str},
            {"$set": {"user_id": user_id_str, "expires_at": expires_at.isoformat(), "updated_at": now.isoformat()}},
            upsert=True
        )

def remove_user_premium(user_id: str):
    user_id_str = str(user_id)
    if db_premium is not None:
        db_premium.delete_one({"user_id": user_id_str})

def set_premium_embed_config(title: str = None, description: str = None, perks: list = None):
    update_data = {}
    if title: update_data["title"] = title
    if description: update_data["description"] = description
    if perks is not None: update_data["perks"] = perks

    if db_premium_config is not None:
        db_premium_config.update_one({"config_key": "global"}, {"$set": update_data}, upsert=True)

def get_premium_embed_config() -> dict:
    if db_premium_config is not None:
        return db_premium_config.find_one({"config_key": "global"}) or {}
    return {}

def get_ticket_config():
    if db_layout_ticket_config is not None:
        cfg = db_layout_ticket_config.find_one({"config_id": "global"})
        if cfg:
            return cfg
    return {
        "title": "Layout Premium Deployment Ticket",
        "instructions": "Follow the instructions below to complete your layout request.",
        "step_images": []
    }

import json
class PremiumConfigModal(discord.ui.Modal, title="Configure /premium Embed"):
    embed_title = discord.ui.TextInput(
        label="Embed Title",
        placeholder="Template Engine Premium",
        required=False,
        max_length=256
    )
    embed_description = discord.ui.TextInput(
        label="Embed Description",
        style=discord.TextStyle.paragraph,
        placeholder="Enter custom description body text...",
        required=False,
        max_length=2000
    )
    embed_perks = discord.ui.TextInput(
        label="Perks List (One perk per line)",
        style=discord.TextStyle.paragraph,
        placeholder="Instant server layout auto-deployments\nGold Glassmorphism web dashboard access",
        required=False,
        max_length=2000
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        title_val = self.embed_title.value.strip() or None
        desc_val = self.embed_description.value.strip() or None
        perks_val = [line.strip() for line in self.embed_perks.value.split("\n") if line.strip()] if self.embed_perks.value.strip() else None

        set_premium_embed_config(title=title_val, description=desc_val, perks=perks_val)

        embed = discord.Embed(
            title="[PREMIUM CONFIG UPDATED]",
            description="Successfully updated the `/premium` embed configuration.",
            color=0x2ecc71
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


class LayoutTicketView(discord.ui.View):
    def __init__(self, step_index: int = 0):
        super().__init__(timeout=None)
        self.step_index = step_index

    @discord.ui.button(label="Next Step Image", style=discord.ButtonStyle.secondary, custom_id="btn_ticket_next")
    async def next_step(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = get_ticket_config()
        images = cfg.get("step_images", [])
        if not images:
            await interaction.response.send_message("No tutorial images uploaded yet.", ephemeral=True)
            return

        next_idx = (self.step_index + 1) % len(images)
        embed = discord.Embed(title=f"Step {next_idx + 1} of {len(images)}", description=cfg.get("instructions"), color=0xF1C40F)
        embed.set_image(url=images[next_idx])
        await interaction.response.edit_message(embed=embed, view=LayoutTicketView(step_index=next_idx))

    @discord.ui.button(label="Buy Design with Points (100 Points)", style=discord.ButtonStyle.primary, custom_id="btn_ticket_buy_points")
    async def buy_points(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        user_id_str = str(interaction.user.id)
        current_pts = get_user_points(user_id_str)

        if current_pts < 100:
            err = discord.Embed(
                title="[INSUFFICIENT POINTS]",
                description=f"You have **{current_pts} Points**. Server layout designs cost **100 Points**.\nEarn points by inviting members, entering giveaways, or buying designs!",
                color=0xf43f5e
            )
            await interaction.followup.send(embed=err, ephemeral=True)
            return

        add_user_points(user_id_str, -100)
        bonus_pts = add_user_points(user_id_str, 30)

        embed = discord.Embed(
            title="[DESIGN PURCHASED WITH POINTS]",
            description=(
                f"Successfully redeemed **100 Points** for your server layout design!\n\n"
                f"• **Bonus Points Granted:** +30 Points\n"
                f"• **New Balance:** **{bonus_pts} Points**\n\n"
                f"Your design deployment has been approved."
            ),
            color=0x2ecc71
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


class GiveawayPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Enter", style=discord.ButtonStyle.success, custom_id="btn_gw_join")
    async def join_leave_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        msg_id = str(interaction.message.id)
        data = get_giveaway_data(msg_id)
        if not data or data.get("ended"):
            err_embed = discord.Embed(title="[GIVEAWAY ENDED]", description="This giveaway has ended.", color=0xf43f5e)
            await interaction.response.send_message(embed=err_embed, ephemeral=True)
            return

        user_id = str(interaction.user.id)
        entries_dict = data.get("entries", {})

        # Check unallowed roles filter
        unallowed_role_ids = data.get("unallowed_role_ids", [])
        if hasattr(interaction.user, "roles") and any(role.id in unallowed_role_ids for role in interaction.user.roles):
            err_embed = discord.Embed(title="[ENTRY RESTRICTED]", description="You have a role that is excluded from entering this giveaway.", color=0xf43f5e)
            await interaction.response.send_message(embed=err_embed, ephemeral=True)
            return

        # Check allowed roles requirement filter
        allowed_role_ids = data.get("allowed_role_ids", [])
        if allowed_role_ids and hasattr(interaction.user, "roles"):
            if not any(role.id in allowed_role_ids for role in interaction.user.roles):
                err_embed = discord.Embed(title="[ROLE REQUIRED]", description="You do not have the required role to enter this giveaway.", color=0xf43f5e)
                await interaction.response.send_message(embed=err_embed, ephemeral=True)
                return

        if user_id in entries_dict:
            entries_dict.pop(user_id)
            data["entries"] = entries_dict
            save_giveaway_data(msg_id, data)
            await interaction.response.send_message("You have left the giveaway.", ephemeral=True)
            return

        # Calculate multi-entry weight
        multi_entry = data.get("multi_entry", 1)
        multi_entry_role_ids = data.get("multi_entry_role_ids", [])
        has_multi = any(role.id in multi_entry_role_ids for role in interaction.user.roles) if hasattr(interaction.user, "roles") and multi_entry_role_ids else False

        user_weight = multi_entry if has_multi else 1
        entries_dict[user_id] = user_weight
        data["entries"] = entries_dict
        save_giveaway_data(msg_id, data)

        join_embed = discord.Embed(
            title="[GIVEAWAY JOINED]",
            description=f"Successfully entered giveaway for **{data.get('points_prize', 0)} Points**! (Entries Granted: **{user_weight}**)",
            color=0x2ecc71
        )
        await interaction.response.send_message(embed=join_embed, ephemeral=True)


class VerificationPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Generate Code", style=discord.ButtonStyle.primary, custom_id="btn_gen_code")
    async def generate_code_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id_str = str(interaction.user.id)
        code = f"TE-{random.randint(100000, 999999)}"
        PENDING_VERIFICATION_CODES[user_id_str] = {"code": code, "created_at": datetime.now(timezone.utc)}

        embed = discord.Embed(
            title="[YOUR VERIFICATION CODE]",
            description=f"Copy and paste this code into your Roblox bio:\n\n`{code}`\n\nExpires in 10 minutes.",
            color=0xF1C40F
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class UnsyncPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Unsync Account", style=discord.ButtonStyle.danger, custom_id="btn_unsync_acc")
    async def unsync_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id_str = str(interaction.user.id)
        remove_user_roblox_link(user_id_str)
        try: await interaction.user.edit(nick=None)
        except Exception: pass

        embed = discord.Embed(
            title="[ACCOUNT UNSYNCED]",
            description="Your Roblox account link has been removed.",
            color=0x2ecc71
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class BotCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.check_active_giveaways.start()
        self.check_pending_invites_stay.start()

    def cog_unload(self):
        self.check_active_giveaways.cancel()
        self.check_pending_invites_stay.cancel()

    def check_owner(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == BOT_OWNER_ID

    async def update_bot_role_color(self, guild: discord.Guild):
        try:
            bot_member = guild.me or await guild.fetch_member(self.bot.user.id)
            if not bot_member: return
            
            manageable_roles = [r for r in bot_member.roles if not r.is_default() and r.is_bot_managed()]
            if manageable_roles:
                target_role = manageable_roles[-1]
                dark_gray = discord.Color(0x34495e)
                if target_role.color != dark_gray:
                    await target_role.edit(color=dark_gray, reason="Auto-configured bot role color to gray")
        except Exception as e:
            print(f"[Role Color Update Exception in {guild.name}] {e}")

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            await self.update_bot_role_color(guild)
            try:
                invs = await guild.invites()
                SERVER_INVITE_CACHE[guild.id] = {inv.code: inv.uses for inv in invs}
            except Exception: pass
        
        # Force global sync when bot is ready
        try:
            synced = await self.bot.tree.sync()
            print(f"[BotCog OnReady] Synchronized {len(synced)} global slash command(s).")
        except Exception as e:
            print(f"[BotCog Sync Error] {e}")

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        await self.update_bot_role_color(guild)
        try:
            invs = await guild.invites()
            SERVER_INVITE_CACHE[guild.id] = {inv.code: inv.uses for inv in invs}
        except Exception: pass
        
        # Sync tree on joining new server
        try:
            await self.bot.tree.sync(guild=guild)
        except Exception: pass

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild = member.guild
        now = datetime.now(timezone.utc)

        # Welcome Message Handling
        settings = get_guild_settings(str(guild.id))
        target_ch_id = settings.get("welcome_channel_id")
        welcome_tpl = settings.get("welcome_message") or "Welcome, {user}.\nPlease read channel."

        channel = None
        if target_ch_id and str(target_ch_id).isdigit():
            channel = guild.get_channel(int(target_ch_id))
        if not channel:
            channel = guild.system_channel

        if channel:
            try:
                formatted_msg = welcome_tpl.replace("{user}", member.mention).replace("{server}", guild.name)
                embed = discord.Embed(title="Welcome!", description=formatted_msg, color=0x3b82f6)
                await channel.send(content=member.mention, embed=embed)
            except Exception: pass

        # Invite Tracking
        account_age_days = (now - member.created_at).days
        if account_age_days < 30:
            return

        old_invites = SERVER_INVITE_CACHE.get(guild.id, {})
        new_inviter_id = None

        try:
            current_invites = await guild.invites()
            for inv in current_invites:
                old_uses = old_invites.get(inv.code, 0)
                if inv.uses > old_uses and inv.inviter:
                    new_inviter_id = str(inv.inviter.id)
                    break
            SERVER_INVITE_CACHE[guild.id] = {inv.code: inv.uses for inv in current_invites}
        except Exception: pass

        if new_inviter_id and new_inviter_id != str(member.id):
            pending_entry = {
                "inviter_id": new_inviter_id,
                "joined_user_id": str(member.id),
                "guild_id": str(guild.id),
                "joined_at": now.isoformat(),
                "unlock_at": (now + timedelta(days=7)).isoformat()
            }
            if db_pending_invites is not None:
                db_pending_invites.insert_one(pending_entry)
            else:
                LOCAL_PENDING_INVITES[str(member.id)] = pending_entry

    @tasks.loop(minutes=30.0)
    async def check_pending_invites_stay(self):
        now = datetime.now(timezone.utc)
        pending_list = list(db_pending_invites.find()) if db_pending_invites is not None else list(LOCAL_PENDING_INVITES.values())

        for entry in pending_list:
            unlock_raw = entry.get("unlock_at")
            if not unlock_raw: continue
            unlock_at = datetime.fromisoformat(unlock_raw)
            if unlock_at.tzinfo is None: unlock_at = unlock_at.replace(tzinfo=timezone.utc)

            if now >= unlock_at:
                guild = self.bot.get_guild(int(entry["guild_id"]))
                inviter_id = entry["inviter_id"]
                joined_user_id = entry["joined_user_id"]

                still_in_server = guild and guild.get_member(int(joined_user_id)) is not None
                if still_in_server:
                    add_user_points(inviter_id, 5)

                if db_pending_invites is not None:
                    db_pending_invites.delete_one({"joined_user_id": joined_user_id})
                else:
                    LOCAL_PENDING_INVITES.pop(joined_user_id, None)

    @check_pending_invites_stay.before_loop
    async def before_check_pending_invites(self):
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=10.0)
    async def check_active_giveaways(self):
        now = datetime.now(timezone.utc)
        active_gw = list(db_giveaways.find({"ended": False})) if db_giveaways is not None else [v for v in LOCAL_GIVEAWAYS.values() if not v.get("ended")]

        for gw in active_gw:
            end_time_raw = gw.get("end_time")
            if not end_time_raw: continue
            end_time = datetime.fromisoformat(end_time_raw) if isinstance(end_time_raw, str) else end_time_raw
            if end_time.tzinfo is None: end_time = end_time.replace(tzinfo=timezone.utc)

            if now >= end_time:
                await self.finalize_giveaway(gw)

    @check_active_giveaways.before_loop
    async def before_check_giveaways(self):
        await self.bot.wait_until_ready()

    async def finalize_giveaway(self, gw_data: dict):
        msg_id = gw_data.get("message_id")
        channel_id = gw_data.get("channel_id")
        gw_data["ended"] = True
        save_giveaway_data(msg_id, gw_data)

        channel = self.bot.get_channel(int(channel_id))
        if not channel: return

        try: msg = await channel.fetch_message(int(msg_id))
        except Exception: msg = None

        entries_dict = gw_data.get("entries", {})
        winner_count = gw_data.get("winners", 1)
        points_prize = gw_data.get("points_prize", 50)

        # Build weighted entries pool
        weighted_pool = []
        for uid, weight in entries_dict.items():
            weighted_pool.extend([uid] * weight)

        winners = []
        if weighted_pool:
            unique_entrants = list(set(weighted_pool))
            actual_count = min(winner_count, len(unique_entrants))
            winners = random.sample(unique_entrants, actual_count)

        for winner_id in winners:
            add_user_points(winner_id, points_prize)

        winner_pings = " ".join([f"<@{w}>" for w in winners]) if winners else "None"

        if msg:
            end_embed = discord.Embed(
                title=f"[GIVEAWAY ENDED] {gw_data.get('title')}",
                description=f"**Prize:** **{points_prize} Points**\n\n**Winners:** {winner_pings}\n**Hosted By:** <@{gw_data.get('host_id', BOT_OWNER_ID)}>",
                color=0xF1C40F
            )
            end_embed.set_image(url=GIVEAWAY_BANNER_URL)
            end_embed.set_footer(text=f"{winner_count} winners | Ended")
            await msg.edit(embed=end_embed, view=None)

        announcement_embed = discord.Embed(
            title="Giveaway Ended!",
            description=f"Congratulations {winner_pings}! You won **{points_prize} Points**!",
            color=0xF1C40F
        )
        await channel.send(content=f"{winner_pings}" if winners else None, embed=announcement_embed)

    # --- RESTORED SLASH COMMANDS (OWNER RESTRICTED) ---

    @app_commands.command(name="premium-set", description="Add or remove TE Premium status for a user (Owner restricted).")
    @app_commands.describe(
        user="Target Discord User",
        action="Select add or remove action",
        duration="Duration if adding (e.g., 10m, 1d, 30d, inf)"
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Add Premium", value="add"),
        app_commands.Choice(name="Remove Premium", value="remove")
    ])
    async def premium_set_cmd(self, interaction: discord.Interaction, user: discord.User, action: app_commands.Choice[str], duration: str = "30d"):
        if not self.check_owner(interaction):
            await interaction.response.send_message("Access denied.", ephemeral=True)
            return

        act = action.value
        if act == "add":
            td = parse_duration_to_timedelta(duration)
            set_user_premium(str(user.id), td)
            exp_time = datetime.now(timezone.utc) + td
            embed = discord.Embed(
                title="[PREMIUM GRANTED]",
                description=f"Granted TE Premium to {user.mention} for **{duration}**.\nExpires: <t:{int(exp_time.timestamp())}:F>",
                color=0x2ecc71
            )
        else:
            remove_user_premium(str(user.id))
            embed = discord.Embed(
                title="[PREMIUM REMOVED]",
                description=f"Removed TE Premium status from {user.mention}.",
                color=0xf43f5e
            )

    @app_commands.command(name="giveaway-start", description="Start a points giveaway (Owner restricted).")
    @app_commands.describe(
        channel="Target channel",
        winners="Winner count",
        duration="Duration (10m, 1h, 1d)",
        title="Giveaway title",
        points_prize="Custom point prize amount",
        allowed_roles="Required roles (Comma-separated Role IDs)",
        unallowed_roles="Excluded roles (Comma-separated Role IDs)",
        multi_entry="Number of entries for bonus role members",
        multi_entry_roles="Bonus roles for extra entries (Comma-separated Role IDs)"
    )
    async def giveaway_start(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        winners: int,
        duration: str,
        title: str,
        points_prize: int = 50,
        allowed_roles: str = None,
        unallowed_roles: str = None,
        multi_entry: int = 1,
        multi_entry_roles: str = None
    ):
        if not self.check_owner(interaction):
            await interaction.response.send_message("Access denied.", ephemeral=True)
            return

        td = parse_duration_to_timedelta(duration)
        end_time = datetime.now(timezone.utc) + td

        allowed_ids = [int(x.strip()) for x in allowed_roles.split(",") if x.strip().isdigit()] if allowed_roles else []
        unallowed_ids = [int(x.strip()) for x in unallowed_roles.split(",") if x.strip().isdigit()] if unallowed_roles else []
        multi_entry_ids = [int(x.strip()) for x in multi_entry_roles.split(",") if x.strip().isdigit()] if multi_entry_roles else []

        embed = discord.Embed(
            title=title,
            description=(
                f"**Prize:** **{points_prize} Points**\n\n"
                f"**Time Remaining**\n"
                f"<t:{int(end_time.timestamp())}:R> (<t:{int(end_time.timestamp())}:F>)\n\n"
                f"**Hosted By**\n"
                f"{interaction.user.mention}"
            ),
            color=0xF1C40F
        )
        embed.set_image(url=GIVEAWAY_BANNER_URL)
        embed.set_footer(text=f"{winners} winners | Ends At | {end_time.strftime('%Y/%m/%d %I:%M %p')}")

        msg = await channel.send(embed=embed, view=GiveawayPanelView())
        gw_data = {
            "message_id": str(msg.id),
            "channel_id": str(channel.id),
            "host_id": str(interaction.user.id),
            "title": title,
            "winners": winners,
            "points_prize": points_prize,
            "end_time": end_time.isoformat(),
            "allowed_role_ids": allowed_ids,
            "unallowed_role_ids": unallowed_ids,
            "multi_entry": multi_entry,
            "multi_entry_role_ids": multi_entry_ids,
            "entries": {},
            "ended": False
        }
        save_giveaway_data(str(msg.id), gw_data)
        await interaction.response.send_message(f"Giveaway deployed to {channel.mention}.", ephemeral=True)

    @app_commands.command(name="premium", description="View Premium info (Owner restricted).")
    async def premium_public(self, interaction: discord.Interaction):
        if not self.check_owner(interaction):
            await interaction.response.send_message("Access denied.", ephemeral=True)
            return
        await interaction.response.send_message("TE Premium details.", ephemeral=True)

    @app_commands.command(name="ping", description="Check latency (Owner restricted).")
    async def ping(self, interaction: discord.Interaction):
        if not self.check_owner(interaction):
            await interaction.response.send_message("Access denied.", ephemeral=True)
            return
        await interaction.response.send_message(f"Pong! Latency: `{round(self.bot.latency * 1000)}ms`", ephemeral=True)

    @app_commands.command(name="website", description="Get website link.")
    async def website_link(self, interaction: discord.Interaction):
        await interaction.response.send_message("Website link: https://templateengine-xi.vercel.app", ephemeral=True)


async def setup(bot):
    bot.add_view(VerificationPanelView())
    bot.add_view(UnsyncPanelView())
    bot.add_view(GiveawayPanelView())
    bot.add_view(LayoutTicketView())
    await bot.add_cog(BotCog(bot))
