"""Permission checker: pure logic (no discord import needed).

scan(guild) -> list of findings. Each finding is a dict:
    id, severity ("high"/"medium"/"low"), title, detail,
    fix: None or {"type": "role_perm"|"chan_perm"|"verification", ...}

The scan only reads the guild object (duck typed), so it can be tested
with simple stub objects.
"""

SEV_COST = {"high": 20, "medium": 8, "low": 3}
SEV_ICON = {"high": "\U0001F534", "medium": "\U0001F7E0", "low": "\U0001F7E1"}

# Permissions that are dangerous when a normal role has them
DANGEROUS = [
    ("administrator", "Administrator"),
    ("manage_guild", "Manage Server"),
    ("manage_roles", "Manage Roles"),
    ("manage_channels", "Manage Channels"),
    ("manage_webhooks", "Manage Webhooks"),
    ("ban_members", "Ban Members"),
    ("kick_members", "Kick Members"),
    ("mention_everyone", "Mention Everyone"),
    ("manage_messages", "Manage Messages"),
]
# Permissions @everyone should never have
EVERYONE_BAD = ["administrator", "manage_guild", "manage_roles", "manage_channels",
                "manage_webhooks", "ban_members", "kick_members", "mention_everyone",
                "manage_messages", "manage_nicknames", "moderate_members"]
LABELS = dict(DANGEROUS)
LABELS.update({"manage_nicknames": "Manage Nicknames", "moderate_members": "Timeout Members"})

STAFF_WORDS = ("staff", "mod", "admin", "log", "private", "team", "internal", "audit", "ticket-log")
READONLY_WORDS = ("rules", "announce", "welcome", "news", "info", "faq", "updates")


def _perm(perms, name):
    return bool(getattr(perms, name, False))


def _everyone_overwrite(channel, everyone):
    """(view, send) overwrite values for @everyone: True/False/None."""
    try:
        ow = channel.overwrites_for(everyone)
        return getattr(ow, "view_channel", None), getattr(ow, "send_messages", None)
    except Exception:
        return None, None


def _can_view(channel, everyone):
    """Can @everyone see this channel? Uses the effective permission when possible."""
    try:
        return bool(channel.permissions_for(everyone).view_channel)
    except Exception:
        view, _ = _everyone_overwrite(channel, everyone)
        return view is not False


def _can_send(channel, everyone):
    try:
        p = channel.permissions_for(everyone)
        return bool(p.view_channel and p.send_messages)
    except Exception:
        return False


def _finding(fid, sev, title, detail, fix=None):
    return {"id": fid, "severity": sev, "title": title, "detail": detail, "fix": fix}


def scan(guild):
    findings = []
    everyone = guild.default_role
    ep = everyone.permissions

    # 1. @everyone dangerous permissions
    bad = [n for n in EVERYONE_BAD if _perm(ep, n)]
    if bad:
        sev = "high"
        findings.append(_finding(
            "everyone_perms", sev, "@everyone has dangerous permissions",
            "Everyone in the server can use: " + ", ".join(LABELS[n] for n in bad) + ".",
            {"type": "role_perm", "role_id": everyone.id, "role_name": "@everyone",
             "remove": bad}))

    # 2. Normal roles with dangerous permissions
    me = getattr(guild, "me", None)
    bot_top = getattr(getattr(me, "top_role", None), "position", None)
    for role in guild.roles:
        if role.is_default() or getattr(role, "managed", False):
            continue
        rp = role.permissions
        if _perm(rp, "administrator"):
            continue  # handled below as an admin count
        members = len(getattr(role, "members", []) or [])
        risky = [n for n in ("mention_everyone", "manage_guild", "manage_roles", "manage_webhooks", "ban_members")
                 if _perm(rp, n)]
        if risky and members >= 15:
            findings.append(_finding(
                f"role_many_{role.id}", "medium", f"Big role has strong permissions: {role.name}",
                f"`{members}` members can use: " + ", ".join(LABELS[n] for n in risky) + ".",
                {"type": "role_perm", "role_id": role.id, "role_name": role.name, "remove": risky}))
        elif "mention_everyone" in risky and members >= 1 and not any(w in role.name.lower() for w in ("admin", "mod", "owner", "staff")):
            findings.append(_finding(
                f"role_ping_{role.id}", "medium", f"Role can ping @everyone: {role.name}",
                "This role does not look like staff but can mention everyone.",
                {"type": "role_perm", "role_id": role.id, "role_name": role.name, "remove": ["mention_everyone"]}))

    # 3. Too many administrators
    admin_roles = [r for r in guild.roles if not r.is_default() and not getattr(r, "managed", False)
                   and _perm(r.permissions, "administrator")]
    admin_humans = set()
    for r in admin_roles:
        for m in getattr(r, "members", []) or []:
            if not getattr(m, "bot", False):
                admin_humans.add(m.id)
    if len(admin_roles) > 2:
        findings.append(_finding(
            "many_admin_roles", "medium", "Many roles have Administrator",
            f"`{len(admin_roles)}` roles have full power: " + ", ".join(r.name for r in admin_roles[:6]) + ". Keep this small.", None))
    if len(admin_humans) > 5:
        findings.append(_finding(
            "many_admins", "low", "Many people are administrators",
            f"`{len(admin_humans)}` people have Administrator. Fewer admins means fewer accidents.", None))

    # 4. Bot role position
    if bot_top is not None and guild.roles:
        highest = max(r.position for r in guild.roles)
        if bot_top < highest - 3 and len(guild.roles) > 6:
            findings.append(_finding(
                "bot_low", "low", "The bot role is low in the role list",
                "Roles above the bot cannot be managed by it. Drag the bot role higher in Server Settings > Roles.", None))

    # 5. Channels
    flagged_cats = set()
    for cat in guild.categories:
        cat_visible = _can_view(cat, everyone)
        cat_name = cat.name.lower()
        staffish_cat = any(w in cat_name for w in STAFF_WORDS)
        # staff category visible to everyone
        if staffish_cat and cat_visible:
            findings.append(_finding(
                f"cat_open_{cat.id}", "high", f"Staff category is public: {cat.name}",
                "Anyone can see this category. Staff areas should be private.",
                {"type": "chan_perm", "channel_id": cat.id, "channel_name": cat.name,
                 "set": {"view_channel": False}}))
            flagged_cats.add(cat.id)
            continue
        # private category but a child channel is opened to everyone
        if not cat_visible:
            for ch in cat.channels:
                view, _ = _everyone_overwrite(ch, everyone)
                if view is True:
                    findings.append(_finding(
                        f"leak_{ch.id}", "high", f"Private category has a public channel: #{ch.name}",
                        f"The category `{cat.name}` is private, but this channel lets @everyone in.",
                        {"type": "chan_perm", "channel_id": ch.id, "channel_name": ch.name,
                         "set": {"view_channel": None}}))

    for ch in guild.channels:
        if ch in guild.categories:
            continue
        name = ch.name.lower()
        is_text = not hasattr(ch, "bitrate")  # voice and stage channels have a bitrate
        if not _can_view(ch, everyone):
            continue
        parent_private = ch.category is not None and not _can_view(ch.category, everyone)
        if parent_private or (ch.category is not None and ch.category.id in flagged_cats):
            continue  # already reported with its category
        if any(w in name for w in STAFF_WORDS) and not any(w in name for w in READONLY_WORDS):
            if any(w in name for w in ("staff", "mod-", "admin", "audit", "-log", "private", "team", "internal")):
                findings.append(_finding(
                    f"staff_{ch.id}", "high", f"Staff-looking channel is public: #{ch.name}",
                    "Everyone can read this channel. If it is for staff, make it private.",
                    {"type": "chan_perm", "channel_id": ch.id, "channel_name": ch.name,
                     "set": {"view_channel": False}}))
                continue
        if is_text and any(w in name for w in ("rules", "announce", "welcome", "news")) and _can_send(ch, everyone):
            findings.append(_finding(
                f"ro_{ch.id}", "medium", f"Anyone can write in #{ch.name}",
                "This looks like a read-only channel, but @everyone can send messages.",
                {"type": "chan_perm", "channel_id": ch.id, "channel_name": ch.name,
                 "set": {"send_messages": False}}))

    # 6. Verification level and content filter
    vl = getattr(getattr(guild, "verification_level", None), "value", None)
    if vl is None:
        vl = getattr(guild, "verification_level", 0)
    try:
        vl = int(vl)
    except Exception:
        vl = 0
    if vl < 1:
        findings.append(_finding(
            "verify", "medium", "Verification level is None",
            "New accounts can join and talk with no check. Low level needs a verified email.",
            {"type": "verification", "to": 1, "from": vl}))
    cf = getattr(getattr(guild, "explicit_content_filter", None), "value", None)
    if cf is not None and int(cf) == 0:
        findings.append(_finding(
            "filter", "low", "Explicit content filter is off",
            "Turn it on in Server Settings > Safety Setup to scan images from members.", None))

    return findings


def score(findings):
    total = 100
    for f in findings:
        total -= SEV_COST.get(f["severity"], 3)
    return max(0, min(100, total))


def order(findings):
    rank = {"high": 0, "medium": 1, "low": 2}
    return sorted(findings, key=lambda f: rank.get(f["severity"], 3))


def fixable(findings):
    return [f for f in findings if f.get("fix")]


def describe_fix(fix):
    """One short line (N3 English) saying what the fix will do."""
    t = fix["type"]
    if t == "role_perm":
        names = ", ".join(LABELS.get(n, n) for n in fix["remove"])
        return f"Remove {names} from {fix['role_name']}"
    if t == "chan_perm":
        parts = []
        for k, v in fix["set"].items():
            label = {"view_channel": "view", "send_messages": "send messages"}.get(k, k)
            parts.append(f"{'reset' if v is None else ('allow' if v else 'block')} {label}")
        return f"{fix['channel_name']}: " + ", ".join(parts) + " for @everyone"
    if t == "verification":
        return "Set verification level to Low"
    return "Fix"
