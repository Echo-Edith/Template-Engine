# ... existing code ...
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author == self.bot.user:
            return

        # Check for premium design log channel (1563544108696477802)
        if str(message.channel.id) == PREMIUM_SCAN_CHANNEL_ID:
            # Check if message has attached blueprint.json file
            json_attachment = None
            for attachment in message.attachments:
                if attachment.filename.endswith(".json"):
                    json_attachment = attachment
                    break

            if json_attachment:
                try:
                    # Download and parse blueprint file
                    content_bytes = await json_attachment.read()
                    payload = json.loads(content_bytes.decode('utf-8'))

                    target_guild_id = payload.get("target_server_id")
                    user_id = payload.get("submitting_user_id")
                    layout = payload.get("layout", {})

                    if not target_guild_id or target_guild_id == "N/A":
                        return

                    guild = self.bot.get_guild(int(target_guild_id))

                    # IF BOT IS NOT IN THE TARGET GUILD
                    if not guild:
                        print(f"[Auto-Build Warning] Bot is not in target guild: {target_guild_id}")
                        
                        # DM the user with Add Bot link & Retry button
                        if user_id:
                            try:
                                user_obj = await self.bot.fetch_user(int(user_id))
                                if user_obj:
                                    invite_url = f"https://discord.com/api/oauth2/authorize?client_id={self.bot.user.id}&permissions=8&scope=bot%20applications.commands&guild_id={target_guild_id}"
                                    
                                    dm_embed = discord.Embed(
                                        title="Bot Required to Auto-Build Server",
                                        description=f"The Template Engine bot is not currently in server `{target_guild_id}`.\n\nPlease add the bot to your server using the button below and click **Retry Auto-Deploy**.",
                                        color=discord.Color.gold()
                                    )
                                    
                                    view = discord.ui.View()
                                    view.add_item(discord.ui.Button(label="Add Bot to Server", url=invite_url, style=discord.ButtonStyle.link))
                                    await user_obj.send(embed=dm_embed, view=view)
                            except Exception as dm_err:
                                print(f"[DM Error] Could not DM user: {dm_err}")
                        return

                    # BOT IS IN GUILD -> EXECUTE AUTOMATIC BUILDING
                    print(f"[Auto-Build] Starting layout deployment for guild {guild.name} ({guild.id})")
                    
                    # 1. Create Roles
                    role_map = {}
                    for r_data in layout.get("roles", []):
                        r_name = r_data.get("name")
                        r_color = r_data.get("color", "#8b5cf6")
                        try:
                            hex_val = int(r_color.lstrip('#'), 16)
                            color_obj = discord.Color(hex_val)
                        except Exception:
                            color_obj = discord.Color.purple()
                        
                        existing_role = discord.utils.get(guild.roles, name=r_name)
                        if not existing_role:
                            new_role = await guild.create_role(name=r_name, color=color_obj)
                            role_map[r_name] = new_role
                        else:
                            role_map[r_name] = existing_role

                    # 2. Create Categories & Channels
                    for cat_data in layout.get("categories", []):
                        cat_name = cat_data.get("name", "CATEGORY")
                        category = await guild.create_category(name=cat_name)

                        for chan_data in cat_data.get("channels", []):
                            ch_name = chan_data.get("name", "channel")
                            ch_type = chan_data.get("type", "text")
                            ch_topic = chan_data.get("topic", "")

                            if ch_type == "voice":
                                await guild.create_voice_channel(name=ch_name, category=category)
                            else:
                                await guild.create_text_channel(name=ch_name, category=category, topic=ch_topic)

                    print(f"[Auto-Build Complete] Successfully built layout for {guild.name}")

                except Exception as build_err:
                    print(f"[Auto-Build Failure] Error building layout: {build_err}")
# ... existing code ...
