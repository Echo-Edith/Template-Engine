import os
import secrets
from datetime import datetime, timezone, timedelta
import json
import asyncio
import requests
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from pymongo import MongoClient

# Import ai_brain module
import ai_brain
import uuid
from typing import Optional

app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET", "").strip()
if not app.secret_key:
    # No hard-coded secret (anyone could forge logins with it). Set SESSION_SECRET in production.
    # Without it, use a key made from the Discord client secret, so logins survive restarts and serverless cold starts.
    _base = os.environ.get("DISCORD_CLIENT_SECRET", "").strip()
    if _base:
        import hashlib
        app.secret_key = hashlib.sha256(("te-session|" + _base).encode()).hexdigest()
        print("[Warning] SESSION_SECRET is not set. Using a key made from DISCORD_CLIENT_SECRET.")
    else:
        print("[Warning] SESSION_SECRET is not set. Using a random key: logins reset on every restart.")
        app.secret_key = secrets.token_hex(32)

# Stay logged in: the login cookie lasts 30 days (it is refreshed every visit)
from datetime import timedelta as _td
app.permanent_session_lifetime = _td(days=30)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("DISCORD_REDIRECT_URI", "").strip().startswith("https://"),
)

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
db_designs = None
db_shop = None
db_web_users = None
db_inbox = None
LOCAL_WEB_USERS = {}
LOCAL_INBOX = []
LOCAL_DESIGNS = []  # used only when there is no database

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
        db_designs = db["web_designs"]
        db_shop = db["shop_items"]
        db_web_users = db["web_users"]
        db_inbox = db["web_inbox"]
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


def get_free_mode() -> bool:
    """True while the owner has turned on /free in the bot (same database)."""
    if db_settings is not None:
        try:
            return bool((db_settings.find_one({"_id": "free_mode"}) or {}).get("on", False))
        except Exception as e:
            print(f"[Free Mode Read Error] {e}")
    return False


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


JST = timezone(timedelta(hours=9))
DEFAULT_AI_PER_DAY = 3
LOCAL_AI_USAGE = {}


def get_ai_per_day() -> int:
    """Daily AI limit for standard members, set with /change limits in the bot. 0 = no limit."""
    if db_settings is not None:
        try:
            value = (db_settings.find_one({"_id": "limits"}) or {}).get("ai_per_day")
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
        except Exception as e:
            print(f"[Limits Read Error] {e}")
    return DEFAULT_AI_PER_DAY


def ai_used_today(user_id: str) -> int:
    today = datetime.now(JST).strftime("%Y-%m-%d")
    doc = LOCAL_AI_USAGE.get(str(user_id), {})
    if db_settings is not None:
        try:
            doc = db_settings.find_one({"_id": f"usage:{user_id}"}) or {}
        except Exception as e:
            print(f"[Usage Read Error] {e}")
    return int(doc.get("ai_count", 0)) if doc.get("ai_day") == today else 0


def record_ai_use(user_id: str):
    today = datetime.now(JST).strftime("%Y-%m-%d")
    used = ai_used_today(user_id) + 1
    LOCAL_AI_USAGE[str(user_id)] = {"ai_day": today, "ai_count": used}
    if db_settings is not None:
        try:
            db_settings.update_one({"_id": f"usage:{user_id}"}, {"$set": {"ai_day": today, "ai_count": used}}, upsert=True)
        except Exception as e:
            print(f"[Usage Save Error] {e}")



# -------------------------------------------------------------
# TEMPLATE GALLERY: ready layouts anyone can open and edit
# -------------------------------------------------------------
def _channel(name, kind="text", emoji="\U0001F4AC", topic="", read_only=False):
    perms = {"view": True, "send": not read_only, "embed": True, "attach": True}
    return {"name": name, "type": kind, "topic": topic, "emoji": emoji, "read_only": read_only,
            "permissions": perms, "formatted_name": f"{emoji} │ {name}"}


def _category(name, emoji, channels):
    return {"name": name, "emoji": emoji, "channels": [_channel(*c) for c in channels]}


def _staff_roles(*extra):
    base = [("Owner", "#f1c40f"), ("Admin", "#e74c3c"), ("Moderator", "#3498db")]
    return [{"name": n, "color": c} for n, c in list(base) + list(extra) + [("Member", "#95a5a6")]]


INFO_CAT = ("INFORMATION", "\U0001F4E2", [
    ("welcome", "text", "\U0001F44B", "Welcome to the server", True),
    ("rules", "text", "\U0001F4DC", "Read the rules first", True),
    ("announcements", "announcement", "\U0001F4E3", "News and updates", True),
])
STAFF_CAT = ("STAFF", "\U0001F6E1", [
    ("staff-chat", "text", "\U0001F6E1", "Private staff talk"),
    ("mod-log", "text", "\U0001F4CB", "Moderation records", True),
])

TEMPLATE_LIBRARY = [
    {"id": "gaming", "name": "Gaming Community", "emoji": "\U0001F3AE",
     "description": "Game chat, LFG, clips and voice rooms for a gaming group.",
     "layout": {"server_name": "Gaming Community", "description": "A friendly place to play together.",
        "roles": _staff_roles(("VIP", "#9b59b6"), ("Gamer", "#2ecc71")),
        "categories": [
            _category(*INFO_CAT),
            _category("COMMUNITY", "\U0001F4AC", [("general", "text", "\U0001F4AC", "Talk about anything"), ("memes", "text", "\U0001F602", "Funny pictures"), ("bot-commands", "text", "\U0001F916", "Use bots here")]),
            _category("GAMING", "\U0001F3AE", [("looking-for-group", "text", "\U0001F91D", "Find teammates"), ("clips-and-highlights", "text", "\U0001F3AC", "Share your best moments"), ("game-news", "text", "\U0001F4F0", "Updates and patch notes")]),
            _category("VOICE", "\U0001F50A", [("Lounge", "voice", "\U0001F6CB", ""), ("Squad 1", "voice", "\U0001F3A7", ""), ("Squad 2", "voice", "\U0001F3A7", ""), ("AFK", "voice", "\U0001F4A4", "")]),
            _category(*STAFF_CAT)]}},
    {"id": "anime", "name": "Anime Fan Club", "emoji": "\U0001F338",
     "description": "Anime talk, manga, fan art and watch parties.",
     "layout": {"server_name": "Anime Fan Club", "description": "For anime and manga fans.",
        "roles": _staff_roles(("Otaku", "#ff69b4"), ("Artist", "#e67e22")),
        "categories": [
            _category(*INFO_CAT),
            _category("CHAT", "\U0001F4AC", [("general", "text", "\U0001F4AC", ""), ("introductions", "text", "\U0001F44B", "Say hello"), ("memes", "text", "\U0001F602", "")]),
            _category("ANIME", "\U0001F338", [("seasonal-anime", "text", "\U0001F4FA", "This season's shows"), ("manga-chat", "text", "\U0001F4D6", ""), ("recommendations", "text", "⭐", "Share what to watch"), ("spoilers", "text", "\U0001F6AB", "Spoilers only")]),
            _category("CREATIVE", "\U0001F3A8", [("fan-art", "text", "\U0001F3A8", "Show your art"), ("cosplay", "text", "\U0001F457", "")]),
            _category("VOICE", "\U0001F50A", [("Watch Party", "voice", "\U0001F4FA", ""), ("Chill Room", "voice", "\U0001F3B5", "")]),
            _category(*STAFF_CAT)]}},
    {"id": "study", "name": "Study Group", "emoji": "\U0001F4DA",
     "description": "Homework help, focus rooms and subject channels.",
     "layout": {"server_name": "Study Group", "description": "Study together and stay focused.",
        "roles": _staff_roles(("Tutor", "#1abc9c"), ("Student", "#3498db")),
        "categories": [
            _category(*INFO_CAT),
            _category("GENERAL", "\U0001F4AC", [("general", "text", "\U0001F4AC", ""), ("goals-and-progress", "text", "\U0001F3AF", "Share your goals"), ("resources", "text", "\U0001F4DA", "Useful links and notes")]),
            _category("SUBJECTS", "\U0001F4D6", [("math", "text", "➕", ""), ("science", "text", "\U0001F52C", ""), ("languages", "text", "\U0001F5E3", ""), ("coding", "text", "\U0001F4BB", ""), ("homework-help", "text", "\U0001F64B", "Ask your questions")]),
            _category("FOCUS ROOMS", "\U0001F3A7", [("Silent Study", "voice", "\U0001F92B", ""), ("Study Together", "voice", "\U0001F4DA", ""), ("Break Room", "voice", "☕", "")]),
            _category(*STAFF_CAT)]}},
    {"id": "business", "name": "Team Workspace", "emoji": "\U0001F4BC",
     "description": "A clean setup for a team, company or project group.",
     "layout": {"server_name": "Team Workspace", "description": "Work together in one place.",
        "roles": [{"name": n, "color": c} for n, c in [("Founder", "#f1c40f"), ("Manager", "#e74c3c"), ("Team Lead", "#3498db"), ("Developer", "#2ecc71"), ("Designer", "#e67e22"), ("Guest", "#95a5a6")]],
        "categories": [
            _category("COMPANY", "\U0001F3E2", [("announcements", "announcement", "\U0001F4E3", "Company news", True), ("handbook", "text", "\U0001F4D8", "Rules and guides", True), ("general", "text", "\U0001F4AC", "")]),
            _category("PROJECTS", "\U0001F4C1", [("project-updates", "text", "\U0001F4CA", ""), ("tasks", "text", "✅", ""), ("ideas", "text", "\U0001F4A1", ""), ("feedback", "text", "\U0001F4DD", "")]),
            _category("TEAMS", "\U0001F465", [("development", "text", "\U0001F4BB", ""), ("design", "text", "\U0001F3A8", ""), ("marketing", "text", "\U0001F4E3", "")]),
            _category("MEETINGS", "\U0001F3A4", [("Daily Standup", "voice", "\U0001F5E3", ""), ("Meeting Room", "voice", "\U0001F4BC", ""), ("Break Room", "voice", "☕", "")])]}},
    {"id": "creator", "name": "Creator & Streamer", "emoji": "\U0001F3A5",
     "description": "For streamers and video creators with a fan community.",
     "layout": {"server_name": "Creator Community", "description": "Where fans and the creator meet.",
        "roles": _staff_roles(("Creator", "#9146ff"), ("Subscriber", "#f1c40f"), ("Fan", "#2ecc71")),
        "categories": [
            _category(*INFO_CAT),
            _category("STREAM", "\U0001F3A5", [("live-now", "announcement", "\U0001F534", "Stream alerts", True), ("video-uploads", "text", "\U0001F4F9", "New videos", True), ("schedule", "text", "\U0001F4C5", "Stream times", True)]),
            _category("COMMUNITY", "\U0001F4AC", [("general", "text", "\U0001F4AC", ""), ("fan-art", "text", "\U0001F3A8", ""), ("clips", "text", "\U0001F3AC", ""), ("suggestions", "text", "\U0001F4A1", "")]),
            _category("VOICE", "\U0001F50A", [("Stream Hangout", "voice", "\U0001F3A4", ""), ("Fan Lounge", "voice", "\U0001F3A7", "")]),
            _category(*STAFF_CAT)]}},
    {"id": "roleplay", "name": "Roleplay World", "emoji": "\U0001F3F0",
     "description": "Story channels, character sheets and in-character chat.",
     "layout": {"server_name": "Roleplay World", "description": "Tell stories together.",
        "roles": _staff_roles(("Game Master", "#8e44ad"), ("Player", "#27ae60")),
        "categories": [
            _category(*INFO_CAT),
            _category("OUT OF CHARACTER", "\U0001F4AC", [("ooc-chat", "text", "\U0001F4AC", "Talk outside the story"), ("character-sheets", "text", "\U0001F4C4", ""), ("lore", "text", "\U0001F4DC", "World history", True)]),
            _category("IN CHARACTER", "\U0001F3AD", [("tavern", "text", "\U0001F37A", ""), ("town-square", "text", "\U0001F3D8", ""), ("adventure", "text", "⚔", ""), ("dm-scenes", "text", "\U0001F4D6", "")]),
            _category("VOICE", "\U0001F50A", [("Campfire", "voice", "\U0001F525", ""), ("Game Night", "voice", "\U0001F3B2", "")]),
            _category(*STAFF_CAT)]}},
    {"id": "minecraft", "name": "Minecraft Server", "emoji": "⛏",
     "description": "Survival server chat, builds, trading and support.",
     "layout": {"server_name": "Minecraft Community", "description": "Survive and build together.",
        "roles": _staff_roles(("Builder", "#e67e22"), ("Player", "#2ecc71")),
        "categories": [
            _category(*INFO_CAT),
            _category("SERVER", "\U0001F5A5", [("server-status", "text", "\U0001F7E2", "Online or not", True), ("ip-and-rules", "text", "\U0001F4CB", "How to join", True), ("support", "text", "\U0001F4E9", "Get help")]),
            _category("COMMUNITY", "\U0001F4AC", [("general", "text", "\U0001F4AC", ""), ("build-showcase", "text", "\U0001F3D7", "Show your builds"), ("trading", "text", "\U0001F4B1", ""), ("screenshots", "text", "\U0001F4F8", "")]),
            _category("VOICE", "\U0001F50A", [("Survival Chat", "voice", "\U0001F3A7", ""), ("Build Together", "voice", "\U0001F3D7", "")]),
            _category(*STAFF_CAT)]}},
    {"id": "music", "name": "Music & Art", "emoji": "\U0001F3B5",
     "description": "Share songs, art and creative work with others.",
     "layout": {"server_name": "Music & Art Hub", "description": "Create and share.",
        "roles": _staff_roles(("Musician", "#e91e63"), ("Artist", "#ff9800")),
        "categories": [
            _category(*INFO_CAT),
            _category("COMMUNITY", "\U0001F4AC", [("general", "text", "\U0001F4AC", ""), ("introductions", "text", "\U0001F44B", ""), ("collabs", "text", "\U0001F91D", "Find people to create with")]),
            _category("MUSIC", "\U0001F3B5", [("share-your-music", "text", "\U0001F3A7", ""), ("music-recommendations", "text", "⭐", ""), ("feedback", "text", "\U0001F4DD", "")]),
            _category("ART", "\U0001F3A8", [("share-your-art", "text", "\U0001F5BC", ""), ("work-in-progress", "text", "✏", ""), ("art-feedback", "text", "\U0001F4DD", "")]),
            _category("VOICE", "\U0001F50A", [("Listening Party", "voice", "\U0001F3B6", ""), ("Jam Room", "voice", "\U0001F3B8", ""), ("Art Stream", "voice", "\U0001F3A8", "")]),
            _category(*STAFF_CAT)]}},
]


def _template_by_id(template_id: str):
    return next((t for t in TEMPLATE_LIBRARY if t["id"] == template_id), None)


def _count_layout(layout: dict) -> tuple:
    cats = layout.get("categories", []) or []
    return len(cats), sum(len(c.get("channels", []) or []) for c in cats), len(layout.get("roles", []) or [])


@app.route("/api/templates")
def api_templates():
    items = []
    for t in TEMPLATE_LIBRARY:
        cats, chans, roles = _count_layout(t["layout"])
        items.append({"id": t["id"], "name": t["name"], "emoji": t["emoji"], "description": t["description"],
                      "categories": cats, "channels": chans, "roles": roles})
    return jsonify({"success": True, "templates": items})


@app.route("/api/templates/<template_id>")
def api_template_use(template_id):
    user = session.get("user")
    if not user:
        return jsonify({"success": False, "error": "Please log in with Discord first."}), 401
    template = _template_by_id(template_id)
    if not template:
        return jsonify({"success": False, "error": "Template not found."}), 404
    layout = json.loads(json.dumps(template["layout"]))  # fresh copy
    save_design(str(user.get("id", "0")), "template", template["name"], layout)
    return jsonify({"success": True, "blueprint": layout})


# -------------------------------------------------------------
# MY DESIGNS: everything a member did (no limit)
# -------------------------------------------------------------
DESIGN_KINDS = {"generated", "ai_edit", "template", "submitted", "shared"}


def save_design(user_id: str, kind: str, title: str, layout: dict, extra: Optional[dict] = None):
    """Saves one history entry. Never raises: history must not break the site."""
    try:
        if kind not in DESIGN_KINDS or not isinstance(layout, dict) or len(json.dumps(layout)) > 400000:
            return
        doc = {
            "_id": uuid.uuid4().hex,
            "user_id": str(user_id),
            "kind": kind,
            "title": str(title or "Untitled")[:120],
            "layout": layout,
            "created_at": datetime.now(timezone.utc).timestamp(),
        }
        doc.update(extra or {})
        if db_designs is not None:
            db_designs.insert_one(doc)
        else:
            LOCAL_DESIGNS.append(doc)
    except Exception as e:
        print(f"[Design Save Error] {e}")


def _own_designs(user_id: str) -> list:
    if db_designs is not None:
        return list(db_designs.find({"user_id": str(user_id)}).sort("created_at", -1))
    return sorted([d for d in LOCAL_DESIGNS if d["user_id"] == str(user_id)], key=lambda d: d["created_at"], reverse=True)


@app.route("/api/designs")
def api_designs():
    user = session.get("user")
    if not user:
        return jsonify({"success": False, "error": "Please log in with Discord first."}), 401
    try:
        items = []
        for d in _own_designs(user["id"]):
            cats, chans, roles = _count_layout(d.get("layout", {}))
            items.append({"id": d["_id"], "kind": d["kind"], "title": d.get("title", ""), "created_at": d["created_at"],
                          "categories": cats, "channels": chans, "roles": roles, "build_mode": d.get("build_mode", "")})
        return jsonify({"success": True, "designs": items})
    except Exception as e:
        print(f"[Designs List Error] {e}")
        return jsonify({"success": False, "error": "Could not load your designs."}), 500


@app.route("/api/designs/<design_id>", methods=["GET", "DELETE"])
def api_design_one(design_id):
    user = session.get("user")
    if not user:
        return jsonify({"success": False, "error": "Please log in with Discord first."}), 401
    uid = str(user["id"])
    try:
        if db_designs is not None:
            doc = db_designs.find_one({"_id": design_id, "user_id": uid})
        else:
            doc = next((d for d in LOCAL_DESIGNS if d["_id"] == design_id and d["user_id"] == uid), None)
        if not doc:
            return jsonify({"success": False, "error": "Design not found."}), 404
        if request.method == "DELETE":
            if db_designs is not None:
                db_designs.delete_one({"_id": design_id, "user_id": uid})
            else:
                LOCAL_DESIGNS.remove(doc)
            return jsonify({"success": True})
        return jsonify({"success": True, "blueprint": doc["layout"], "title": doc.get("title", "")})
    except Exception as e:
        print(f"[Design Error] {e}")
        return jsonify({"success": False, "error": "Something went wrong."}), 500



# -------------------------------------------------------------
# SHARE A DESIGN: a link anyone can open (after logging in) to copy the layout
# -------------------------------------------------------------
@app.route("/api/designs/<design_id>/share", methods=["POST"])
def api_design_share(design_id):
    user = session.get("user")
    if not user:
        return jsonify({"success": False, "error": "Please log in with Discord first."}), 401
    uid = str(user["id"])
    try:
        if db_designs is not None:
            doc = db_designs.find_one({"_id": design_id, "user_id": uid})
        else:
            doc = next((d for d in LOCAL_DESIGNS if d["_id"] == design_id and d["user_id"] == uid), None)
        if not doc:
            return jsonify({"success": False, "error": "Design not found."}), 404
        share_id = doc.get("share_id") or uuid.uuid4().hex[:10]
        if db_designs is not None:
            db_designs.update_one({"_id": design_id}, {"$set": {"share_id": share_id}})
        else:
            doc["share_id"] = share_id
        return jsonify({"success": True, "url": request.host_url.rstrip("/") + "/s/" + share_id})
    except Exception as e:
        print(f"[Share Error] {e}")
        return jsonify({"success": False, "error": "Could not create the link."}), 500


def _find_shared(share_id: str):
    if db_designs is not None:
        return db_designs.find_one({"share_id": share_id})
    return next((d for d in LOCAL_DESIGNS if d.get("share_id") == share_id), None)


@app.route("/s/<share_id>")
def shared_link(share_id):
    session["pending_share"] = share_id[:20]
    return redirect(url_for("index"))


@app.route("/api/shared/<share_id>")
def api_shared(share_id):
    user = session.get("user")
    if not user:
        return jsonify({"success": False, "error": "Please log in with Discord first."}), 401
    try:
        doc = _find_shared(share_id)
        if not doc:
            return jsonify({"success": False, "error": "This link is not valid anymore."}), 404
        layout = json.loads(json.dumps(doc["layout"]))
        layout.pop("build_meta", None)
        save_design(str(user["id"]), "shared", doc.get("title", "Shared design"), layout)
        return jsonify({"success": True, "blueprint": layout})
    except Exception as e:
        print(f"[Shared Open Error] {e}")
        return jsonify({"success": False, "error": "Something went wrong."}), 500


# -------------------------------------------------------------
# USAGE METER, POINTS PAGE, BUILD PROGRESS
# -------------------------------------------------------------
def _jst_week_key_and_reset():
    now = datetime.now(JST)
    iso = now.isocalendar()
    monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return f"{iso[0]}-W{iso[1]:02d}", int((monday + timedelta(days=7)).timestamp())


def _limit_settings() -> dict:
    limits = {"ai_per_day": DEFAULT_AI_PER_DAY, "builds_per_week": 1}
    if db_settings is not None:
        try:
            doc = db_settings.find_one({"_id": "limits"}) or {}
            for key in limits:
                value = doc.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    limits[key] = value
        except Exception as e:
            print(f"[Limits Read Error] {e}")
    return limits


def _builds_used(user_id: str) -> int:
    week, _ = _jst_week_key_and_reset()
    if db_settings is not None:
        try:
            doc = db_settings.find_one({"_id": f"usage:{user_id}"}) or {}
            return int(doc.get("build_count", 0)) if doc.get("build_week") == week else 0
        except Exception as e:
            print(f"[Usage Read Error] {e}")
    return 0


@app.route("/api/usage")
def api_usage():
    user = session.get("user")
    if not user:
        return jsonify({"success": False, "error": "Please log in with Discord first."}), 401
    uid = str(user["id"])
    premium = is_user_premium(uid)
    limits = _limit_settings()
    now = datetime.now(JST)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return jsonify({
        "success": True,
        "premium": premium,
        "free_mode": get_free_mode(),
        "ai": {"used": ai_used_today(uid), "limit": limits["ai_per_day"], "resets": int(tomorrow.timestamp())},
        "builds": {"used": _builds_used(uid), "limit": limits["builds_per_week"], "resets": _jst_week_key_and_reset()[1]},
    })


_NAME_CACHE = {}


def _discord_name(user_id: str) -> str:
    cached = _NAME_CACHE.get(user_id)
    if cached and cached[1] > datetime.now().timestamp():
        return cached[0]
    name = f"User {user_id[-4:]}"
    if DISCORD_BOT_TOKEN:
        try:
            r = requests.get(f"https://discord.com/api/v10/users/{user_id}", headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}, timeout=4)
            if r.status_code == 200:
                j = r.json()
                name = j.get("global_name") or j.get("username") or name
        except Exception as e:
            print(f"[Name Lookup Error] {e}")
    _NAME_CACHE[user_id] = (name, datetime.now().timestamp() + 3600)
    return name


@app.route("/api/points")
def api_points():
    user = session.get("user")
    if not user:
        return jsonify({"success": False, "error": "Please log in with Discord first."}), 401
    uid = str(user["id"])
    premium = is_user_premium(uid)
    points, board, shop = 0, [], []
    try:
        if db_users is not None:
            doc = db_users.find_one({"discord_id": uid}) or {}
            points = int(doc.get("points", 0))
            for row in db_users.find({"points": {"$gt": 0}}).sort("points", -1).limit(10):
                rid = str(row.get("discord_id"))
                board.append({"name": _discord_name(rid), "points": int(row.get("points", 0)), "you": rid == uid})
        if db_shop is not None:
            for item in db_shop.find({}, {"_id": 0}):
                price = int(item.get("price", 0))
                discount = int(item.get("premium_discount") or 0)
                your_price = max(1, round(price * (100 - discount) / 100)) if (premium and discount > 0) else price
                stock = item.get("stock")
                shop.append({
                    "name": item.get("name", "Item"), "price": price, "your_price": your_price, "discount": discount,
                    "stock": "Unlimited" if stock is None else ("Sold out" if stock <= 0 else f"{stock} left"),
                    "reward": ("Lifetime TE Premium" if item.get("permanent") else f"{int(item.get('premium_days', 0))} days TE Premium") if item.get("reward") == "premium"
                              else ("Discord role" if item.get("reward") == "role" else "Delivered by staff"),
                })
            shop.sort(key=lambda i: (i["price"], i["name"].lower()))
        return jsonify({"success": True, "points": points, "premium": premium, "leaderboard": board, "shop": shop})
    except Exception as e:
        print(f"[Points Page Error] {e}")
        return jsonify({"success": False, "error": "Could not load your points."}), 500


@app.route("/api/build-status/<build_id>")
def api_build_status(build_id):
    user = session.get("user")
    if not user:
        return jsonify({"success": False, "error": "Please log in with Discord first."}), 401
    stage = {"stage": "sent", "detail": "Waiting for the bot to pick it up...", "done": 0, "total": 0}
    try:
        if db_settings is not None:
            doc = db_settings.find_one({"_id": f"build:{build_id}"})
            if doc:
                if str(doc.get("user_id")) != str(user["id"]):
                    return jsonify({"success": False, "error": "Not your build."}), 403
                stage = {k: doc.get(k) for k in ("stage", "detail", "done", "total")}
        return jsonify({"success": True, **stage})
    except Exception as e:
        print(f"[Build Status Error] {e}")
        return jsonify({"success": False, "error": "Could not read the status."}), 500



# -------------------------------------------------------------
# SEND A DESIGN TO A PERSON (designs only, no text, so nobody can be harassed)
# Safety: people must opt in to being found, can block senders, sends are rate limited,
# and the sender's own text is never shown (no title, no message).
# -------------------------------------------------------------
import re as _re
SEND_LIMIT_PER_HOUR = 10
INBOX_MAX = 50
_SEND_LOG = {}
_SEARCH_LOG = {}


def _rate_ok(log: dict, key: str, limit: int, window: int) -> bool:
    now = datetime.now().timestamp()
    hits = [t for t in log.get(key, []) if now - t < window]
    if len(hits) >= limit:
        log[key] = hits
        return False
    hits.append(now)
    log[key] = hits
    return True


def remember_web_user(user_id, display_name, username, avatar):
    """Saves who logged in, so other people can search for them (if they allow it)."""
    try:
        uid = str(user_id)
        fields = {"name": str(display_name or username or "User")[:40], "username": str(username or "")[:40],
                  "username_lower": str(username or "").lower()[:40], "avatar": avatar}
        if db_web_users is not None:
            db_web_users.update_one({"_id": uid}, {"$set": fields, "$setOnInsert": {"accepts": True, "blocked": []}}, upsert=True)
        else:
            doc = LOCAL_WEB_USERS.setdefault(uid, {"_id": uid, "accepts": True, "blocked": []})
            doc.update(fields)
    except Exception as e:
        print(f"[Web User Save Error] {e}")


def _web_user(user_id: str):
    if db_web_users is not None:
        return db_web_users.find_one({"_id": str(user_id)})
    return LOCAL_WEB_USERS.get(str(user_id))


def _web_user_update(user_id: str, changes: dict):
    if db_web_users is not None:
        db_web_users.update_one({"_id": str(user_id)}, {"$set": changes}, upsert=True)
    else:
        LOCAL_WEB_USERS.setdefault(str(user_id), {"_id": str(user_id), "accepts": True, "blocked": []}).update(changes)


@app.route("/api/people/search")
def api_people_search():
    user = session.get("user")
    if not user:
        return jsonify({"success": False, "error": "Please log in with Discord first."}), 401
    me = str(user["id"])
    q = str(request.args.get("q", "")).strip().lower().lstrip("@")
    if len(q) < 3:
        return jsonify({"success": True, "people": []})
    if not _rate_ok(_SEARCH_LOG, me, 30, 60):
        return jsonify({"success": False, "error": "Too many searches. Wait a minute."}), 429
    try:
        if db_web_users is not None:
            if q.isdigit():
                rows = list(db_web_users.find({"_id": q}).limit(1))
            else:
                rows = list(db_web_users.find({"username_lower": {"$regex": "^" + _re.escape(q)}}).limit(15))
        else:
            rows = [u for u in LOCAL_WEB_USERS.values() if u["_id"] == q or u.get("username_lower", "").startswith(q)]
        people = []
        for u in rows:
            if u["_id"] == me or u.get("accepts") is False or me in (u.get("blocked") or []):
                continue
            people.append({"id": u["_id"], "name": u.get("name", "User"), "username": u.get("username", ""), "avatar": u.get("avatar")})
            if len(people) >= 5:
                break
        return jsonify({"success": True, "people": people})
    except Exception as e:
        print(f"[People Search Error] {e}")
        return jsonify({"success": False, "error": "Search failed."}), 500


@app.route("/api/designs/<design_id>/send", methods=["POST"])
def api_design_send(design_id):
    user = session.get("user")
    if not user:
        return jsonify({"success": False, "error": "Please log in with Discord first."}), 401
    me = str(user["id"])
    to = str((request.get_json() or {}).get("to", "")).strip()
    if not to.isdigit() or to == me:
        return jsonify({"success": False, "error": "Pick another person."}), 400
    if not _rate_ok(_SEND_LOG, me, SEND_LIMIT_PER_HOUR, 3600):
        return jsonify({"success": False, "error": f"You can send {SEND_LIMIT_PER_HOUR} designs per hour. Try again later."}), 429
    try:
        if db_designs is not None:
            doc = db_designs.find_one({"_id": design_id, "user_id": me})
        else:
            doc = next((d for d in LOCAL_DESIGNS if d["_id"] == design_id and d["user_id"] == me), None)
        if not doc:
            return jsonify({"success": False, "error": "Design not found."}), 404
        target = _web_user(to)
        if not target or target.get("accepts") is False:
            return jsonify({"success": False, "error": "This person can not receive designs."}), 404
        if me in (target.get("blocked") or []):
            return jsonify({"success": True})  # looks the same, so a blocked person is not told
        pending = (db_inbox.count_documents({"to": to}) if db_inbox is not None else len([i for i in LOCAL_INBOX if i["to"] == to]))
        if pending >= INBOX_MAX:
            return jsonify({"success": False, "error": "This person's inbox is full."}), 409
        layout = json.loads(json.dumps(doc["layout"]))
        layout.pop("build_meta", None)
        item = {"_id": uuid.uuid4().hex, "to": to, "from": me, "from_name": str(user.get("username", "User"))[:40],
                "read": False, "layout": layout, "created_at": datetime.now(timezone.utc).timestamp()}
        if db_inbox is not None:
            db_inbox.insert_one(item)
        else:
            LOCAL_INBOX.append(item)
        return jsonify({"success": True})
    except Exception as e:
        print(f"[Send Design Error] {e}")
        return jsonify({"success": False, "error": "Could not send the design."}), 500


def _inbox_items(me: str) -> list:
    if db_inbox is not None:
        return list(db_inbox.find({"to": me}).sort("created_at", -1))
    return sorted([i for i in LOCAL_INBOX if i["to"] == me], key=lambda i: i["created_at"], reverse=True)


def _inbox_get(me: str, item_id: str):
    if db_inbox is not None:
        return db_inbox.find_one({"_id": item_id, "to": me})
    return next((i for i in LOCAL_INBOX if i["_id"] == item_id and i["to"] == me), None)


def _inbox_delete(item: dict):
    if db_inbox is not None:
        db_inbox.delete_one({"_id": item["_id"]})
    elif item in LOCAL_INBOX:
        LOCAL_INBOX.remove(item)


@app.route("/api/inbox")
def api_inbox():
    user = session.get("user")
    if not user:
        return jsonify({"success": False, "error": "Please log in with Discord first."}), 401
    me = str(user["id"])
    try:
        items = []
        for i in _inbox_items(me):
            cats, chans, roles = _count_layout(i.get("layout", {}))
            items.append({"id": i["_id"], "from_name": i.get("from_name", "User"), "created_at": i["created_at"],
                          "categories": cats, "channels": chans, "roles": roles, "unread": not i.get("read", False)})
        me_doc = _web_user(me) or {}
        return jsonify({"success": True, "items": items, "unread": sum(1 for i in items if i["unread"]),
                        "accepts": me_doc.get("accepts") is not False,
                        "blocked_count": len(me_doc.get("blocked") or [])})
    except Exception as e:
        print(f"[Inbox Error] {e}")
        return jsonify({"success": False, "error": "Could not load your inbox."}), 500


@app.route("/api/inbox/mark-read", methods=["POST"])
def api_inbox_mark_read():
    user = session.get("user")
    if not user:
        return jsonify({"success": False, "error": "Please log in with Discord first."}), 401
    me = str(user["id"])
    try:
        if db_inbox is not None:
            db_inbox.update_many({"to": me, "read": {"$ne": True}}, {"$set": {"read": True}})
        else:
            for i in LOCAL_INBOX:
                if i["to"] == me:
                    i["read"] = True
        return jsonify({"success": True})
    except Exception as e:
        print(f"[Inbox Read Error] {e}")
        return jsonify({"success": False, "error": "Something went wrong."}), 500


@app.route("/api/inbox/<item_id>/<action>", methods=["POST"])
def api_inbox_action(item_id, action):
    user = session.get("user")
    if not user:
        return jsonify({"success": False, "error": "Please log in with Discord first."}), 401
    me = str(user["id"])
    if action not in ("accept", "decline", "block"):
        return jsonify({"success": False, "error": "Unknown action."}), 400
    try:
        item = _inbox_get(me, item_id)
        if not item:
            return jsonify({"success": False, "error": "This design is not in your inbox anymore."}), 404
        if action == "accept":
            layout = item["layout"]
            save_design(me, "shared", f"From {item.get('from_name', 'a friend')}", layout)
            _inbox_delete(item)
            return jsonify({"success": True, "blueprint": layout})
        if action == "block":
            sender = item["from"]
            blocked = list((_web_user(me) or {}).get("blocked") or [])
            if sender not in blocked:
                blocked.append(sender)
            _web_user_update(me, {"blocked": blocked})
            for other in [i for i in _inbox_items(me) if i["from"] == sender]:
                _inbox_delete(other)
            return jsonify({"success": True})
        _inbox_delete(item)
        return jsonify({"success": True})
    except Exception as e:
        print(f"[Inbox Action Error] {e}")
        return jsonify({"success": False, "error": "Something went wrong."}), 500


@app.route("/api/share-settings", methods=["POST"])
def api_share_settings():
    user = session.get("user")
    if not user:
        return jsonify({"success": False, "error": "Please log in with Discord first."}), 401
    me = str(user["id"])
    data = request.get_json() or {}
    try:
        if "accepts" in data:
            _web_user_update(me, {"accepts": bool(data["accepts"])})
        if data.get("clear_blocked"):
            _web_user_update(me, {"blocked": []})
        return jsonify({"success": True})
    except Exception as e:
        print(f"[Share Settings Error] {e}")
        return jsonify({"success": False, "error": "Could not save."}), 500


@app.route("/")
def index():
    user = session.get("user")
    roblox_user = session.get("roblox_user")
    is_premium = False
    if user and user.get("id") and not _web_user(user["id"]):
        remember_web_user(user["id"], user.get("username"), user.get("username"), user.get("avatar"))  # for people who logged in before
    pending_share = session.get("pending_share", "") if user else ""
    if pending_share:
        session.pop("pending_share", None)

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
        free_mode=get_free_mode(),
        pending_share=pending_share,
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
    # prompt=none: if this Discord account already allowed the site, Discord signs in at once with no screen.
    # First time (or after the person removed access), Discord sends them back with an error and we ask normally.
    if not request.args.get("force"):
        discord_auth_url += "&prompt=none"
    return redirect(discord_auth_url)


@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code:
        # prompt=none cannot sign in silently (never allowed before, or Discord needs a login): show the normal screen
        if request.args.get("error") in ("interaction_required", "login_required", "consent_required"):
            return redirect(url_for("login", force=1))
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

        session.permanent = True
        remember_web_user(user_id, user_data.get("global_name") or user_data.get("username"), user_data.get("username"), avatar_url)
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

        # Free limit: standard members get a few AI generations per day (TE Premium has no limit)
        user_id = str(user.get("id", "0"))
        limit = 0 if is_user_premium(user_id) else get_ai_per_day()
        if limit > 0 and ai_used_today(user_id) >= limit:
            return jsonify({
                "success": False,
                "error": f"You used all {limit} free AI generations for today. They come back at 00:00 Tokyo time. TE Premium members have no limit."
            }), 429

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

        if limit > 0:
            record_ai_use(user_id)  # only successful generations count
        blueprint = ai_result.get("data", {})
        save_design(user_id, "generated", prompt, blueprint)
        return jsonify({"success": True, "blueprint": blueprint}), 200

    except Exception as e:
        print(f"[Generate Blueprint Error] {e}")
        return jsonify({"success": False, "error": f"Generation failed: {str(e)}"}), 500


@app.route("/edit-blueprint", methods=["POST"])
def edit_blueprint_endpoint():
    """TE Premium only: change an existing layout with a short instruction."""
    try:
        user = session.get("user")
        if not user:
            return jsonify({"success": False, "error": "Unauthorized. Please log in with Discord."}), 401
        if not is_user_premium(str(user.get("id", "0"))):
            return jsonify({"success": False, "error": "Edit with AI is a TE Premium feature."}), 403

        data = request.get_json() or {}
        instruction = str(data.get("instruction", "")).strip()
        layout = data.get("layout")
        if not instruction or len(instruction) > 500:
            return jsonify({"success": False, "error": "Write what you want to change (up to 500 characters)."}), 400
        if not isinstance(layout, dict) or len(json.dumps(layout)) > 150000:
            return jsonify({"success": False, "error": "The layout is missing or too big."}), 400

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(ai_brain.edit_server_layout(layout, instruction))
        finally:
            loop.close()

        if not result.get("success"):
            return jsonify({"success": False, "error": result.get("error", "Edit failed.")}), 500
        save_design(str(user.get("id", "0")), "ai_edit", instruction, result["data"])
        return jsonify({"success": True, "blueprint": result["data"]}), 200
    except Exception as e:
        print(f"[Edit Blueprint Error] {e}")
        return jsonify({"success": False, "error": f"Edit failed: {str(e)}"}), 500


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
    if not _re.fullmatch(r"\d{17,20}", str(target_server_id).strip()):
        return jsonify({"success": False, "error": "Enter your Server ID (a number with 17 to 20 digits) before submitting."}), 400
    target_server_id = str(target_server_id).strip()
    prompt_text = payload.get("prompt", "No prompt description provided.")
    server_name = payload.get("server_name", "Custom Server")

    categories_count = payload.get("categories_count", 0)
    channels_count = payload.get("channels_count", 0)
    roles_count = payload.get("roles_count", 0)
    layout = payload.get("layout", {})
    # Only TE Premium can pick "add to my server". Everyone else always rebuilds.
    build_mode = "add" if (is_premium and payload.get("build_mode") == "add") else "rebuild"

    account_age = ai_brain.calculate_account_age(user_id)

    embed_title = "Premium Auto-Build Submitted" if is_premium else "Standard Design Submitted"
    embed_color = 0xC9962B if is_premium else 0xE0661A

    # Normal embed on purpose: the bot reads this message to start the build and send the DM.
    # The separator is a solid line (strikethrough over blank characters), so it has no gaps.
    embed_payload = {
        "title": embed_title,
        "description": ai_brain.log_description([
            ("User", f"<@{user_id}>"),
            ("User ID", user_id),
            ("Account Age", account_age),
            ("Target Server ID", target_server_id),
            ("Categories", categories_count),
            ("Channels", channels_count),
            ("Roles", roles_count),
            ("Build Mode", "Add to existing server" if build_mode == "add" else "Rebuild from scratch"),
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
        "prompt": prompt_text,
        "is_premium": is_premium,
        "build_mode": build_mode,
        "layout": layout
    }

    json_bytes = json.dumps(attachment_data, indent=2).encode("utf-8")

    delivered = False
    build_id = ""
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
                try:
                    build_id = str(res.json().get("id", ""))
                except Exception:
                    build_id = ""
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

    if isinstance(layout, dict):
        save_design(user_id, "submitted", server_name, layout, {"build_mode": build_mode, "target_server_id": str(target_server_id)})
    return jsonify({
        "success": True,
        "is_premium": is_premium,
        "build_id": build_id,
        "message": "Design submitted successfully!"
    }), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=os.environ.get("FLASK_DEBUG") == "1")
