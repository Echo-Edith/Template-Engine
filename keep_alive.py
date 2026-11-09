import os
import secrets
from datetime import datetime, timezone, timedelta
import json
import asyncio
import requests
from flask import Flask, render_template, request, jsonify, session, redirect, url_for, abort, render_template_string, g
from pymongo import MongoClient

# Import ai_brain module
import ai_brain
import shop_items
import brand
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
db_promos = None
db_import_requests = None
db_shop_requests = None
LOCAL_IMPORTS = {}
db_sec_counters = None
db_sec_log = None
db_sec_blocks = None
db_sec_alerts = None
db_owner_jobs = None
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
        brand.configure(db_settings, auto=True)      # the website reads the brand from the database (cached for 5 seconds)
        db_designs = db["web_designs"]
        db_shop = db["shop_items"]
        db_web_users = db["web_users"]
        db_inbox = db["web_inbox"]
        db_promos = db["promo_codes"]
        db_import_requests = db["import_requests"]
        db_shop_requests = db["shop_requests"]
        db_sec_counters = db["security_counters"]
        db_sec_log = db["security_log"]
        db_sec_blocks = db["security_blocks"]
        db_sec_alerts = db["security_alerts"]
        try:
            db_sec_counters.create_index("exp", expireAfterSeconds=0)
            db_sec_log.create_index([("at", -1)])
        except Exception as _e:
            print(f"[Security Index Error] {_e}")
        db_owner_jobs = db["owner_jobs"]
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
                return _sec_ai_cap(value)
        except Exception as e:
            print(f"[Limits Read Error] {e}")
    return _sec_ai_cap(DEFAULT_AI_PER_DAY)


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
DESIGN_KINDS = {"generated", "ai_edit", "template", "submitted", "shared", "imported"}


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
                          "categories": cats, "channels": chans, "roles": roles, "build_mode": d.get("build_mode", ""), "on_profile": bool(d.get("on_profile"))})
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


def _limit_settings(raw: bool = False) -> dict:
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
    if not raw:
        limits["ai_per_day"] = _sec_ai_cap(limits["ai_per_day"])
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


# =====================================================================
# COSMETICS SHOP: website themes and name colors (bought with TE points)
# Buying goes through the bot (it owns the points). Equipping only changes a setting, so the website does it.
# =====================================================================
_SHOP_LOG = {}
_PRICES = {"at": 0.0}


def load_cosmetic_prices(force: bool = False):
    """Loads the prices the owner changed (saved in the database, checked every 10 seconds)."""
    now = datetime.now().timestamp()
    if not force and now - _PRICES["at"] < 10:
        return
    _PRICES["at"] = now
    if db_settings is not None:
        try:
            doc = db_settings.find_one({"_id": "shop_prices"}) or {}
            shop_items.set_overrides(doc.get("prices") or {})
        except Exception as e:
            print(f"[Shop Prices Error] {e}")


def _owned_items(uid: str) -> list:
    return list((_web_user(uid) or {}).get("owned") or [])


def _equipped(uid: str) -> dict:
    doc = _web_user(uid) or {}
    owned = set(doc.get("owned") or [])
    skin = doc.get("skin") if f"skin:{doc.get('skin')}" in owned else ""
    color = doc.get("name_color") if f"color:{doc.get('name_color')}" in owned else ""
    return {"skin": skin or "", "name_color": color or ""}


def _name_color_of(uid: str) -> str:
    try:
        return _equipped(str(uid))["name_color"]
    except Exception:
        return ""


@app.route("/api/shop/cosmetics")
def api_shop_cosmetics():
    user = session.get("user")
    if not user:
        return jsonify({"success": False, "error": "Please log in with Discord first."}), 401
    load_cosmetic_prices()
    uid = str(user["id"])
    points = 0
    if db_users is not None:
        try:
            points = int((db_users.find_one({"discord_id": uid}) or {}).get("points", 0))
        except Exception as e:
            print(f"[Shop Points Error] {e}")
    owned = set(_owned_items(uid))
    eq = _equipped(uid)
    items = []
    for it in shop_items.catalog():
        it = dict(it)
        it["owned"] = it["id"] in owned
        it["equipped"] = (it["kind"] == "skin" and eq["skin"] == it["key"]) or (it["kind"] == "color" and eq["name_color"] == it["key"])
        items.append(it)
    return jsonify({"success": True, "points": points, "items": items, "equipped": eq,
                    "name": (_web_user(uid) or {}).get("name") or user.get("username") or "You"})


@app.route("/api/shop/buy", methods=["POST"])
def api_shop_buy():
    user = session.get("user")
    if not user:
        return jsonify({"success": False, "error": "Please log in with Discord first."}), 401
    uid = str(user["id"])
    item = str((request.get_json(silent=True) or {}).get("id", ""))
    load_cosmetic_prices()
    if shop_items.price_of(item) is None:
        return jsonify({"success": False, "error": "This item does not exist."}), 400
    if item in _owned_items(uid):
        return jsonify({"success": False, "error": "You already own this item."}), 400
    if db_shop_requests is None:
        return jsonify({"success": False, "error": "The shop is not available right now."}), 503
    if not _rate_ok(_SHOP_LOG, uid, 12, 60):
        return jsonify({"success": False, "error": "Too many tries. Please wait a minute."}), 429
    rid = uuid.uuid4().hex[:12]
    db_shop_requests.insert_one({"rid": rid, "user_id": uid, "item": item, "status": "pending",
                                 "created_at": datetime.now(timezone.utc).timestamp()})
    return jsonify({"success": True, "id": rid})


@app.route("/api/shop/buy/<rid>")
def api_shop_buy_status(rid):
    user = session.get("user")
    if not user:
        return jsonify({"success": False, "error": "Please log in with Discord first."}), 401
    req = db_shop_requests.find_one({"rid": str(rid), "user_id": str(user["id"])}) if db_shop_requests is not None else None
    if not req:
        return jsonify({"success": False, "status": "missing"}), 404
    return jsonify({"success": True, "status": req["status"], "result": req.get("result") or {}})


@app.route("/api/shop/equip", methods=["POST"])
def api_shop_equip():
    user = session.get("user")
    if not user:
        return jsonify({"success": False, "error": "Please log in with Discord first."}), 401
    uid = str(user["id"])
    data = request.get_json(silent=True) or {}
    kind, key = str(data.get("kind", "")), str(data.get("key", ""))
    field = {"skin": "skin", "color": "name_color"}.get(kind)
    if field is None:
        return jsonify({"success": False, "error": "Bad request."}), 400
    if key and f"{kind}:{key}" not in _owned_items(uid):
        return jsonify({"success": False, "error": "You do not own this item."}), 403
    _web_user_update(uid, {field: key})
    return jsonify({"success": True})


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
                board.append({"name": _discord_name(rid), "points": int(row.get("points", 0)), "you": rid == uid, "color": _name_color_of(rid)})
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
_DM_LOG = {}


def notify_design_received(to_id: str, from_id: str, from_name: str, counts: tuple):
    """DMs the receiver that a design arrived and who sent it. No text from the sender is included.
    Only one DM per sender and receiver per hour, so nobody can be spammed. Never raises."""
    try:
        target = _web_user(to_id) or {}
        if target.get("bot_dms") is False or target.get("inbox_notify") is False:
            return  # the receiver turned these DMs off with /settings
        if not DISCORD_BOT_TOKEN or not _rate_ok(_DM_LOG, f"{from_id}>{to_id}", 1, 3600):
            return
        headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}", "Content-Type": "application/json"}
        site = (os.environ.get("WEBSITE_URL") or request.host_url).strip().rstrip("/")
        cats, chans, roles = counts
        embed = {
            "title": "New Design Received",
            "description": f"<@{from_id}> (`{from_name}`) sent you a server design.\nOpen the **Inbox** on the website to accept, decline or block.",
            "color": 0xC9962B,
            "fields": [
                {"name": "Categories", "value": f"`{cats}`", "inline": True},
                {"name": "Channels", "value": f"`{chans}`", "inline": True},
                {"name": "Roles", "value": f"`{roles}`", "inline": True},
            ],
            "footer": {"text": "Only designs can be sent, never text. You can block senders in the Inbox."},
        }
        dm = requests.post("https://discord.com/api/v10/users/@me/channels", headers=headers, json={"recipient_id": str(to_id)}, timeout=5)
        if dm.status_code not in (200, 201):
            return
        components = [{"type": 1, "components": [{"type": 2, "style": 5, "label": "Open Inbox", "url": site}]}]
        requests.post(f"https://discord.com/api/v10/channels/{dm.json()['id']}/messages", headers=headers,
                      json={"embeds": [embed], "components": components}, timeout=5)
    except Exception as e:
        print(f"[Design DM Error] {e}")


def _rate_ok(log: dict, key: str, limit: int, window: int) -> bool:
    if sec_level() >= 2 and not is_owner():
        limit = max(1, limit // 4)  # website lockdown: everything is 4 times stricter
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
            res = db_web_users.update_one({"_id": uid}, {"$set": fields, "$setOnInsert": {"accepts": True, "blocked": [], "created_at": datetime.now(timezone.utc).timestamp()}}, upsert=True)
            if getattr(res, "upserted_id", None) is not None:
                sec_new_account()
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
        rows = [u for u in rows if not is_blocked_user(u['_id'])]
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
        notify_design_received(to, me, item["from_name"], _count_layout(layout))
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
            sec_note_blocked_sender(sender)
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


# =====================================================================
# OWNER PANEL: only the IDs in OWNER_IDS can see or use this. Everyone else gets a plain 404.
# The website never changes data itself. It saves a "job" and the bot runs it with the same code as the slash commands.
# =====================================================================
OWNER_IDS = {"1219266886143967245"} | {x.strip() for x in os.environ.get("OWNER_IDS", "").split(",") if x.strip().isdigit()}
_OWNER_LOG = {}

# job type -> the only settings the website may send for it
OWNER_JOB_SPECS = {
    "panel": {"kind", "channel_id", "description"},
    "ticket_setup": {"category_id", "staff_role_id", "log_channel_id", "channel_id"},
    "greeting": {"guild_id", "kind", "channel_id", "message", "enabled"},
    "messagelog": {"guild_id", "channel_id", "enabled"},
    "promo_create": {"code", "points", "premium_days", "duration", "max_uses"},
    "promo_update": {"code", "points", "premium_days", "duration", "max_uses"},
    "promo_delete": {"code"},
    "points": {"user_id", "amount"},
    "premium": {"user_id", "action", "days", "permanent"},
    "staff": {"user_id", "action"},
    "free": {"on"},
    "limit": {"key", "value"},
    "theme": {"theme"},
    "trivia": {"enabled", "channel_id", "interval", "ask_now"},
    "invite": {"guild_id"},
    "maintenance": {"on", "message"},
    "blacklist_add": {"kind", "id", "reason"},
    "blacklist_remove": {"kind", "id"},
    "sync_roles": set(),
}


def is_owner() -> bool:
    user = session.get("user") or {}
    return str(user.get("id", "")) in OWNER_IDS


def owner_only(fn):
    from functools import wraps

    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not is_owner():
            sec_wrong_access()
            abort(404)
        return fn(*args, **kwargs)
    return wrapper


def _names_fast(ids) -> dict:
    """Looks up many Discord names at the same time (one by one would be slow)."""
    from concurrent.futures import ThreadPoolExecutor
    ids = list(dict.fromkeys(str(i) for i in ids))
    if not ids:
        return {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(zip(ids, pool.map(_discord_name, ids)))


def _owner_user_info(uid: str) -> dict:
    uid = str(uid)
    info = {"id": uid, "name": _discord_name(uid), "points": 0, "premium": "none", "staff": False}
    if db_users is not None:
        try:
            doc = db_users.find_one({"discord_id": uid}) or {}
            info["points"] = int(doc.get("points", 0))
            info["roblox"] = (doc.get("roblox_data") or {}).get("name", "") if isinstance(doc.get("roblox_data"), dict) else ""
        except Exception as e:
            print(f"[Owner User Error] {e}")
    if uid in HARDCODED_STAFF:
        info["premium"], info["staff"] = "permanent (staff)", True
    elif db_premium is not None:
        try:
            doc = db_premium.find_one({"discord_id": uid}) or {}
            info["staff"] = bool(doc.get("staff"))
            raw = doc.get("expires_at")
            if doc:
                info["premium"] = "active" if not raw else str(raw)[:10]
                if raw:
                    exp = datetime.fromisoformat(str(raw))
                    if exp.tzinfo is None:
                        exp = exp.replace(tzinfo=timezone.utc)
                    if exp <= datetime.now(timezone.utc):
                        info["premium"] = "expired " + str(raw)[:10]
                    elif exp.year >= 2100:
                        info["premium"] = "permanent"
        except Exception as e:
            print(f"[Owner Premium Error] {e}")
    return info


@app.route("/api/owner/overview")
@owner_only
def api_owner_overview():
    out = {"maintenance": get_maintenance(), "free_mode": get_free_mode(), "theme": get_site_theme(), "limits": _limit_settings(raw=True),
           "themes": sorted(SITE_THEMES), "db": db_owner_jobs is not None,
           "counts": {}, "guilds": [], "bot_online": False, "jobs": [], "trivia": {"enabled": True, "channel_id": "1569115319820230726", "interval": 30, "next_at": 0}}
    if db_settings is not None:
        try:
            t = db_settings.find_one({"_id": "trivia_event"}) or {}
            out["trivia"] = {"enabled": bool(t.get("enabled", True)), "channel_id": str(t.get("channel_id") or "1569115319820230726"),
                             "interval": int(t.get("interval") or 30), "next_at": float(t.get("next_at") or 0)}
            g = db_settings.find_one({"_id": "owner_guilds"}) or {}
            out["guilds"] = g.get("guilds", [])
            out["bot_online"] = (datetime.now(timezone.utc).timestamp() - float(g.get("updated_at", 0))) < 180
        except Exception as e:
            print(f"[Owner Overview Error] {e}")
    try:
        out["counts"] = {
            "premium": db_premium.count_documents({}) if db_premium is not None else 0,
            "staff": db_premium.count_documents({"staff": True}) if db_premium is not None else 0,
            "website_users": db_web_users.count_documents({}) if db_web_users is not None else 0,
            "designs": db_designs.count_documents({}) if db_designs is not None else 0,
            "codes": db_promos.count_documents({}) if db_promos is not None else 0,
        }
        if db_owner_jobs is not None:
            out["jobs"] = [{"id": j["jid"], "type": j["type"], "status": j["status"], "result": j.get("result", ""),
                            "at": j.get("created_at", 0)}
                           for j in db_owner_jobs.find({}).sort("created_at", -1).limit(15)]
    except Exception as e:
        print(f"[Owner Counts Error] {e}")
    return jsonify(out)


@app.route("/api/owner/job", methods=["POST"])
@owner_only
def api_owner_job():
    if not request.is_json:
        return jsonify({"success": False, "error": "Bad request."}), 400
    me = str(session["user"]["id"])
    if not _rate_ok(_OWNER_LOG, me, 40, 60):
        return jsonify({"success": False, "error": "Too many actions. Wait a moment."}), 429
    data = request.get_json(silent=True) or {}
    jtype = str(data.get("type", ""))
    spec = OWNER_JOB_SPECS.get(jtype)
    if spec is None:
        return jsonify({"success": False, "error": "Unknown action."}), 400
    if db_owner_jobs is None:
        return jsonify({"success": False, "error": "No database connection."}), 503
    params = {}
    for key, value in (data.get("params") or {}).items():
        if key in spec and isinstance(value, (str, int, bool)) and not (isinstance(value, str) and len(value) > 2000):
            params[key] = value.strip() if isinstance(value, str) else value
    jid = uuid.uuid4().hex[:12]
    db_owner_jobs.insert_one({"jid": jid, "type": jtype, "params": params, "by": me, "status": "pending",
                              "result": "", "created_at": datetime.now(timezone.utc).timestamp()})
    print(f"[Owner Panel] {me} queued {jtype} ({jid})")
    return jsonify({"success": True, "id": jid})


@app.route("/api/owner/job/<jid>")
@owner_only
def api_owner_job_status(jid):
    job = db_owner_jobs.find_one({"jid": str(jid)}) if db_owner_jobs is not None else None
    if not job:
        return jsonify({"status": "missing"}), 404
    return jsonify({"status": job["status"], "result": job.get("result", "")})


@app.route("/api/owner/promos")
@owner_only
def api_owner_promos():
    items = []
    if db_promos is not None:
        try:
            now = datetime.now(timezone.utc).timestamp()
            for d in db_promos.find({}, {"_id": 0}).sort("created_at", -1).limit(100):
                exp, mx = d.get("expires_at"), d.get("max_uses")
                active = not (exp is not None and exp <= now) and not (mx is not None and int(d.get("uses", 0)) >= int(mx))
                items.append({"code": d.get("code"), "points": d.get("points", 0), "premium_days": d.get("premium_days", 0),
                              "uses": d.get("uses", 0), "max_uses": mx, "expires_at": exp, "active": active})
        except Exception as e:
            print(f"[Owner Promos Error] {e}")
    return jsonify({"items": items})


@app.route("/api/owner/premium")
@owner_only
def api_owner_premium():
    items = []
    if db_premium is not None:
        try:
            for d in db_premium.find({}).sort("updated_at", -1).limit(60):
                uid = str(d.get("discord_id", ""))
                if uid:
                    items.append({"id": uid, "expires": str(d.get("expires_at") or "")[:10], "staff": bool(d.get("staff"))})
        except Exception as e:
            print(f"[Owner Premium List Error] {e}")
    names = _names_fast(it["id"] for it in items[:25])
    for it in items[:25]:
        it["name"] = names.get(it["id"], it["id"])
    return jsonify({"items": items})


@app.route("/api/owner/user/<uid>")
@owner_only
def api_owner_user(uid):
    if not (uid.isdigit() and 15 <= len(uid) <= 21):
        return jsonify({"error": "That is not a Discord ID."}), 400
    return jsonify(_owner_user_info(uid))


# =====================================================================
# BLACKLIST: blocked users cannot use the website. The owner changes it in the Owner Panel (the bot saves it).
# =====================================================================
_BL = {"at": 0.0, "data": {"users": {}, "guilds": {}}}


def get_blacklist() -> dict:
    now = datetime.now().timestamp()
    if now - _BL["at"] < 20:
        return _BL["data"]
    data = {"users": {}, "guilds": {}}
    if db_settings is not None:
        try:
            doc = db_settings.find_one({"_id": "blacklist"}) or {}
            data = {"users": dict(doc.get("users") or {}), "guilds": dict(doc.get("guilds") or {})}
        except Exception as e:
            print(f"[Blacklist Read Error] {e}")
    _BL["at"], _BL["data"] = now, data
    return data


def is_blocked_user(user_id) -> bool:
    uid = str(user_id or "")
    return bool(uid) and uid not in OWNER_IDS and uid not in HARDCODED_STAFF and uid in get_blacklist()["users"]


@app.before_request
def block_blacklisted_users():
    user = session.get("user")
    if user and request.path.startswith("/api/") and is_blocked_user(user.get("id")):
        return jsonify({"success": False, "error": "Your account is blocked from Template Engine."}), 403


@app.route("/api/owner/blacklist")
@owner_only
def api_owner_blacklist():
    data = get_blacklist()
    guild_names = {}
    try:
        for g in (db_settings.find_one({"_id": "owner_guilds"}) or {}).get("guilds", []) if db_settings is not None else []:
            guild_names[g["id"]] = g["name"]
    except Exception:
        pass
    names = _names_fast(list(data["users"])[:60])
    users = [{"id": uid, "name": names.get(uid, uid), "reason": v.get("reason", ""), "at": v.get("at", 0)} for uid, v in list(data["users"].items())[:60]]
    guilds = [{"id": gid, "name": guild_names.get(gid, "Server " + gid[-4:]), "reason": v.get("reason", ""), "at": v.get("at", 0)} for gid, v in list(data["guilds"].items())[:60]]
    return jsonify({"users": users, "guilds": guilds})


# =====================================================================
# PUBLIC PROFILE: /u/<username>. Off by default. Shows only designs the owner of the profile chose.
# No Discord ID, no messages, no search. Search engines are told not to list it.
# =====================================================================
PROFILE_PAGE = """<!doctype html>
<html lang="en" data-theme="{{ theme }}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow"><title>{{ name }} - Template Engine</title>
<style>
:root{--bg:#060f1c;--panel:#0a1a2e;--edge:#3a5f86;--text:#e6f0fa;--sub:#9db6cf;--acc:#e0691a;--gold:#c9962b;--card:#0d2239}
[data-theme="light"]{--bg:#e8eff6;--panel:#fffaf0;--edge:#b8862a;--text:#0d2238;--sub:#35546f;--acc:#d4580f;--gold:#8a6100;--card:#fff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:ui-monospace,Menlo,Consolas,monospace;padding:16px}
.wrap{max-width:720px;margin:0 auto;border:2px solid var(--edge);background:var(--panel);padding:20px;box-shadow:8px 8px 0 rgba(0,0,0,.35)}
.head{display:flex;gap:14px;align-items:center;flex-wrap:wrap}.ava{width:72px;height:72px;border:2px solid var(--edge);background:var(--acc);color:#fff;display:flex;align-items:center;justify-content:center;font-size:2rem;font-weight:700;text-transform:uppercase}
h1{margin:0;font-size:1.3rem;word-break:break-word}.badge{display:inline-block;margin-top:6px;padding:2px 8px;border:1px solid var(--gold);color:var(--gold);font-size:.72rem;text-transform:uppercase;letter-spacing:.06em}
.sub{color:var(--sub);font-size:.8rem;margin-top:4px}.grid{display:grid;gap:12px;margin-top:18px}
.card{border:1px solid var(--edge);background:var(--card);padding:12px}.card h2{margin:0 0 4px;font-size:1rem;word-break:break-word}
.meta{color:var(--sub);font-size:.76rem}.cats{margin:8px 0;font-size:.78rem;color:var(--sub);word-break:break-word}
a.btn{display:inline-block;margin-top:6px;padding:9px 14px;background:var(--acc);color:#fff;text-decoration:none;border:2px solid rgba(0,0,0,.35);font-weight:700;font-size:.78rem;text-transform:uppercase}
.foot{margin-top:18px;text-align:center}.foot a{color:var(--sub);font-size:.78rem}
{{ nc_css|safe }}
</style></head><body><div class="wrap">
<div class="head"><div class="ava">{{ name[:1] }}</div><div><h1 class="{{ 'nc-' + name_color if name_color else '' }}">{{ name }}</h1>
{% if premium %}<span class="badge">TE Premium</span>{% endif %}
<div class="sub">{{ designs|length }} public design{{ '' if designs|length == 1 else 's' }}</div></div></div>
<div class="grid">
{% for d in designs %}<div class="card"><h2>{{ d.title }}</h2>
<div class="meta">{{ d.categories }} categories, {{ d.channels }} channels, {{ d.roles }} roles</div>
{% if d.cat_names %}<div class="cats">{{ d.cat_names }}</div>{% endif %}
<a class="btn" href="/s/{{ d.share_id }}">Use this design</a></div>
{% else %}<div class="card"><div class="meta">No public designs yet.</div></div>{% endfor %}
</div>
<div class="foot"><a href="/">Open Template Engine</a></div></div></body></html>"""


@app.route("/api/profile/me")
def api_profile_me():
    user = session.get("user")
    if not user:
        return jsonify({"success": False, "error": "Please log in with Discord first."}), 401
    doc = _web_user(user["id"]) or {}
    username = doc.get("username") or user.get("username") or ""
    return jsonify({"success": True, "public": bool(doc.get("profile_public")), "username": username,
                    "url": request.host_url.rstrip("/") + "/u/" + username})


@app.route("/api/profile/settings", methods=["POST"])
def api_profile_settings():
    user = session.get("user")
    if not user:
        return jsonify({"success": False, "error": "Please log in with Discord first."}), 401
    data = request.get_json(silent=True) or {}
    _web_user_update(str(user["id"]), {"profile_public": bool(data.get("public"))})
    return jsonify({"success": True})


@app.route("/api/designs/<design_id>/profile", methods=["POST"])
def api_design_profile(design_id):
    user = session.get("user")
    if not user:
        return jsonify({"success": False, "error": "Please log in with Discord first."}), 401
    uid = str(user["id"])
    on = bool((request.get_json(silent=True) or {}).get("on"))
    try:
        if db_designs is not None:
            doc = db_designs.find_one({"_id": design_id, "user_id": uid})
        else:
            doc = next((d for d in LOCAL_DESIGNS if d["_id"] == design_id and d["user_id"] == uid), None)
        if not doc:
            return jsonify({"success": False, "error": "Design not found."}), 404
        changes = {"on_profile": on}
        if on and not doc.get("share_id"):
            changes["share_id"] = uuid.uuid4().hex[:10]
        if db_designs is not None:
            db_designs.update_one({"_id": design_id}, {"$set": changes})
        else:
            doc.update(changes)
        return jsonify({"success": True, "on": on})
    except Exception as e:
        print(f"[Profile Design Error] {e}")
        return jsonify({"success": False, "error": "Could not save."}), 500


@app.route("/u/<username>")
def public_profile(username):
    name_l = str(username).strip().lower()[:40]
    u = None
    if name_l:
        if db_web_users is not None:
            u = db_web_users.find_one({"username_lower": name_l, "profile_public": True})
        else:
            u = next((x for x in LOCAL_WEB_USERS.values() if x.get("username_lower") == name_l and x.get("profile_public")), None)
    if not u or is_blocked_user(u["_id"]):
        abort(404)  # same answer for "no such user" and "profile is private"
    uid = str(u["_id"])
    if db_designs is not None:
        docs = list(db_designs.find({"user_id": uid, "on_profile": True, "share_id": {"$exists": True}}).sort("created_at", -1).limit(30))
    else:
        docs = [d for d in _own_designs(uid) if d.get("on_profile") and d.get("share_id")][:30]
    designs = []
    for d in docs:
        cats, chans, roles = _count_layout(d.get("layout", {}))
        names = [str(c.get("name", ""))[:30] for c in (d.get("layout", {}).get("categories") or [])[:6]]
        designs.append({"title": d.get("title", "Untitled"), "categories": cats, "channels": chans, "roles": roles,
                        "cat_names": ", ".join(n for n in names if n), "share_id": d["share_id"]})
    viewer = session.get("user") or {}
    theme = ((_web_user(viewer["id"]) or {}).get("theme_mode") if viewer.get("id") else "") or ""
    return render_template_string(PROFILE_PAGE, name=u.get("name") or u.get("username") or "User",
                                  premium=is_user_premium(uid), designs=designs, theme="light" if theme == "light" else "dark",
                                  name_color=_name_color_of(uid), nc_css=shop_items.name_color_css())


# =====================================================================
# MAINTENANCE MODE: everyone except the owner sees "Back soon". The login pages stay open so the owner can sign in.
# =====================================================================
_MAINT = {"at": 0.0, "data": {"on": False, "message": ""}}
DEFAULT_MAINTENANCE_TEXT = "Template Engine is being updated. Please try again in a little while."


def get_maintenance() -> dict:
    if sec_level() >= 4:  # automatic full lockdown acts like maintenance mode
        return {"on": True, "message": "The website is locked for a short time because of suspicious activity. Please try again later."}
    now = datetime.now().timestamp()
    if now - _MAINT["at"] < 10:
        return _MAINT["data"]
    data = {"on": False, "message": ""}
    if db_settings is not None:
        try:
            doc = db_settings.find_one({"_id": "maintenance"}) or {}
            data = {"on": bool(doc.get("on")), "message": str(doc.get("message") or "")}
        except Exception as e:
            print(f"[Maintenance Read Error] {e}")
    _MAINT["at"], _MAINT["data"] = now, data
    return data


@app.before_request
def maintenance_gate():
    if request.path.startswith(("/static/", "/login", "/callback", "/logout", "/api/live")):
        return None
    m = get_maintenance()
    if not m["on"] or is_owner() or request.path.startswith("/api/owner/"):
        return None
    text = m["message"] or DEFAULT_MAINTENANCE_TEXT
    if request.path.startswith("/api/"):
        return jsonify({"success": False, "error": text, "maintenance": True}), 503, {"Retry-After": "60"}
    return render_template_string(MAINTENANCE_PAGE, text=text), 503, {"Retry-After": "60"}


MAINTENANCE_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60"><meta name="robots" content="noindex"><title>Back soon - Template Engine</title>
<style>:root{color-scheme:dark light}body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#060f1c;color:#e6f0fa;font-family:ui-monospace,Menlo,Consolas,monospace;padding:16px}
.box{max-width:480px;border:2px solid #c9962b;background:#0a1a2e;padding:24px;box-shadow:8px 8px 0 rgba(0,0,0,.4);text-align:center}
h1{margin:0 0 10px;color:#c9962b;font-size:1.3rem;text-transform:uppercase;letter-spacing:.08em}p{margin:0 0 8px;line-height:1.5;word-break:break-word}small{color:#9db6cf}
@media (prefers-color-scheme: light){body{background:#e8eff6;color:#0d2238}.box{background:#fffaf0;border-color:#b8862a}h1{color:#8a6100}small{color:#35546f}}</style></head>
<body><div class="box"><h1>Back soon</h1><p>{{ text }}</p><small>This page checks again every minute.</small></div></body></html>"""


# -------------------------------------------------------------
# IMPORT A REAL SERVER: the bot reads the server (only for people with Manage Server) and gives back a layout
# -------------------------------------------------------------
_IMPORT_LOG = {}


@app.route("/api/import/start", methods=["POST"])
def api_import_start():
    user = session.get("user")
    if not user:
        return jsonify({"success": False, "error": "Please log in with Discord first."}), 401
    if not request.is_json:
        return jsonify({"success": False, "error": "Bad request."}), 400
    uid = str(user["id"])
    gid = str((request.get_json(silent=True) or {}).get("server_id", "")).strip()
    if not _re.fullmatch(r"\d{17,20}", gid):
        return jsonify({"success": False, "error": "That is not a valid server ID."}), 400
    if gid in get_blacklist()["guilds"]:
        return jsonify({"success": False, "error": "This server is blocked from using Template Engine."}), 403
    if not _rate_ok(_IMPORT_LOG, uid, 4, 600):
        return jsonify({"success": False, "error": "Too many imports. Please wait a few minutes."}), 429
    if db_import_requests is None:
        return jsonify({"success": False, "error": "Import is not available right now."}), 503
    rid = uuid.uuid4().hex[:14]
    db_import_requests.insert_one({"rid": rid, "user_id": uid, "guild_id": gid, "status": "pending",
                                   "created_at": datetime.now(timezone.utc).timestamp()})
    return jsonify({"success": True, "id": rid})


@app.route("/api/import/<rid>")
def api_import_status(rid):
    user = session.get("user")
    if not user:
        return jsonify({"success": False, "error": "Please log in with Discord first."}), 401
    req = db_import_requests.find_one({"rid": str(rid), "user_id": str(user["id"])}) if db_import_requests is not None else None
    if not req:
        return jsonify({"success": False, "error": "Not found."}), 404
    if req["status"] == "error":
        return jsonify({"success": False, "status": "error", "error": req.get("error", "Import failed.")})
    if req["status"] != "done":
        return jsonify({"success": True, "status": req["status"]})
    layout = req.get("layout") or {}
    truncated = bool(layout.pop("truncated", False))
    if not req.get("saved"):
        save_design(str(user["id"]), "imported", layout.get("server_name") or "Imported server", layout)
        db_import_requests.update_one({"_id": req["_id"]}, {"$set": {"saved": True}})
    return jsonify({"success": True, "status": "done", "blueprint": layout, "truncated": truncated})


# =====================================================================
# SECURITY: automatic website lockdown (levels 0 to 4). Everything is saved in the database,
# so it works on Vercel, where every request can run on a different machine.
#   Level 1  watch: only written to the log
#   Level 2  slow down: stricter limits, AI limit 1 per day, new accounts cannot use AI
#   Level 3  AI generate, AI edit, send, import and share are off (except for premium and the owner)
#   Level 4  full lockdown: like maintenance mode (only the owner gets in)
# The bot sends the owner a DM with an Unlock button when the level goes up.
# =====================================================================
SEC_LEVEL_NAMES = {0: "Normal", 1: "Watch", 2: "Slow down", 3: "Risky features off", 4: "Full lockdown"}
SEC_TRIGGERS = {
    "traffic": {"label": "Traffic spike: requests per IP per minute", "threshold": 240, "level": 2, "block_min": 15},
    "ai_abuse": {"label": "AI abuse: AI calls per account per 10 minutes", "threshold": 10, "level": 2, "block_min": 60},
    "ai_global": {"label": "AI abuse: AI calls from everyone per 10 minutes", "threshold": 80, "level": 3, "block_min": 0},
    "new_accounts": {"label": "Mass new accounts: new logins per 10 minutes", "threshold": 40, "level": 2, "block_min": 0},
    "submit_spam": {"label": "Submit spam: submits per account per 10 minutes", "threshold": 6, "level": 2, "block_min": 60},
    "same_server": {"label": "Same server ID used by this many accounts per hour", "threshold": 4, "level": 2, "block_min": 60},
    "harassment": {"label": "Harassment: designs sent per account per hour", "threshold": 20, "level": 2, "block_min": 360},
    "blocked_sender": {"label": "Harassment: people who blocked the same sender per day", "threshold": 3, "level": 2, "block_min": 360},
    "failed_requests": {"label": "Failed requests per IP per 5 minutes", "threshold": 60, "level": 2, "block_min": 15},
    "wrong_access": {"label": "Owner area tried by a non-owner per 10 minutes", "threshold": 3, "level": 3, "block_min": 1440},
    "share_abuse": {"label": "Share link opened this many times per 10 minutes", "threshold": 300, "level": 2, "block_min": 0},
}
SEC_UNCOUNTED = ("/api/live", "/api/owner/", "/api/build-status/", "/api/import/", "/api/inbox", "/api/usage")
SEC_FEATURES = [(_re.compile(r"^/generate-blueprint$"), "ai"), (_re.compile(r"^/edit-blueprint$"), "ai"),
                (_re.compile(r"^/api/designs/[^/]+/send$"), "send"), (_re.compile(r"^/api/import/start$"), "import"),
                (_re.compile(r"^/api/designs/[^/]+/share$"), "share")]
SEC_FEATURE_TEXT = {"ai": "AI tools", "send": "Sending designs", "import": "Importing servers", "share": "Share links"}
SEC_CACHE = {"state": [0.0, {}], "cfg": [0.0, None], "blocks": [0.0, {}], "prem": {}}
LOCAL_SEC = {"state": {}, "cfg": {}, "counters": {}, "blocks": {}, "log": [], "alerts": []}


def sec_default_config() -> dict:
    return {"enabled": True, "dry_run": False, "auto_unlock_minutes": 30,
            "triggers": {k: {"on": True, "threshold": v["threshold"]} for k, v in SEC_TRIGGERS.items()}}


def sec_config() -> dict:
    now = datetime.now().timestamp()
    if SEC_CACHE["cfg"][1] is not None and now - SEC_CACHE["cfg"][0] < 10:
        return SEC_CACHE["cfg"][1]
    cfg = sec_default_config()
    saved = LOCAL_SEC["cfg"]
    if db_settings is not None:
        try:
            saved = db_settings.find_one({"_id": "security_config"}) or {}
        except Exception as e:
            print(f"[Security Config Error] {e}")
    for key in ("enabled", "dry_run"):
        if isinstance(saved.get(key), bool):
            cfg[key] = saved[key]
    if isinstance(saved.get("auto_unlock_minutes"), int) and 1 <= saved["auto_unlock_minutes"] <= 1440:
        cfg["auto_unlock_minutes"] = saved["auto_unlock_minutes"]
    for name, t in (saved.get("triggers") or {}).items():
        if name in cfg["triggers"] and isinstance(t, dict):
            if isinstance(t.get("on"), bool):
                cfg["triggers"][name]["on"] = t["on"]
            if isinstance(t.get("threshold"), int) and 1 <= t["threshold"] <= 1000000:
                cfg["triggers"][name]["threshold"] = t["threshold"]
    SEC_CACHE["cfg"] = [now, cfg]
    return cfg


def sec_save_config(cfg: dict):
    saved = {"enabled": cfg["enabled"], "dry_run": cfg["dry_run"], "auto_unlock_minutes": cfg["auto_unlock_minutes"], "triggers": cfg["triggers"]}
    LOCAL_SEC["cfg"] = saved
    SEC_CACHE["cfg"] = [0.0, None]
    if db_settings is not None:
        db_settings.update_one({"_id": "security_config"}, {"$set": saved}, upsert=True)


def sec_state() -> dict:
    now = datetime.now().timestamp()
    if now - SEC_CACHE["state"][0] < 5:
        return SEC_CACHE["state"][1]
    state = LOCAL_SEC["state"]
    if db_settings is not None:
        try:
            state = db_settings.find_one({"_id": "security_state"}) or {}
        except Exception as e:
            print(f"[Security State Error] {e}")
    state = dict(state)
    SEC_CACHE["state"] = [now, state]
    if int(state.get("level", 0)) > 0 and state.get("auto_unlock_at") and now > float(state["auto_unlock_at"]):
        sec_set_level(0, "auto_unlock", "The lockdown time ended.", by="auto")
        return SEC_CACHE["state"][1]
    return state


def sec_level() -> int:
    try:
        return int(sec_state().get("level", 0))
    except Exception:
        return 0


def _sec_ai_cap(value: int) -> int:
    if sec_level() >= 2:
        return 1 if value == 0 or value > 1 else value  # lockdown: free AI limit is 1 per day
    return value


def sec_ip() -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() or request.remote_addr or "?")[:64]


def sec_hash(value: str) -> str:
    import hashlib
    return hashlib.sha256((str(app.secret_key) + "|" + str(value)).encode()).hexdigest()[:12]


def sec_log(kind: str, level: int, detail: str, action: str = "", subject: str = ""):
    entry = {"at": datetime.now(timezone.utc).timestamp(), "kind": kind, "level": level, "detail": str(detail)[:300], "action": str(action)[:200], "subject": str(subject)[:60]}
    LOCAL_SEC["log"] = ([entry] + LOCAL_SEC["log"])[:100]
    if db_sec_log is not None:
        try:
            db_sec_log.insert_one(dict(entry))
            if secrets_chance():
                db_sec_log.delete_many({"at": {"$lt": entry["at"] - 14 * 86400}})
        except Exception as e:
            print(f"[Security Log Error] {e}")
    print(f"[Security] {kind} L{level} {detail} {action}")


def secrets_chance() -> bool:
    import random
    return random.random() < 0.02


def sec_count(key: str, window: int) -> int:
    """Counts one more hit in the current time window and returns the total."""
    now = datetime.now().timestamp()
    cid = f"{key}:{int(now // window)}"
    if db_sec_counters is not None:
        try:
            doc = db_sec_counters.find_one_and_update(
                {"_id": cid}, {"$inc": {"n": 1}, "$setOnInsert": {"exp": datetime.utcnow() + timedelta(seconds=window * 2)}},
                upsert=True, return_document=True)
            return int(doc.get("n", 1))
        except Exception as e:
            print(f"[Security Count Error] {e}")
    exp, n = LOCAL_SEC["counters"].get(cid, (now + window * 2, 0))
    LOCAL_SEC["counters"][cid] = (exp, n + 1)
    if len(LOCAL_SEC["counters"]) > 5000:
        LOCAL_SEC["counters"] = {k: v for k, v in LOCAL_SEC["counters"].items() if v[0] > now}
    return n + 1


def sec_count_unique(key: str, window: int, member: str) -> int:
    now = datetime.now().timestamp()
    cid = f"{key}:{int(now // window)}"
    if db_sec_counters is not None:
        try:
            doc = db_sec_counters.find_one_and_update(
                {"_id": cid}, {"$addToSet": {"m": member}, "$setOnInsert": {"exp": datetime.utcnow() + timedelta(seconds=window * 2)}},
                upsert=True, return_document=True)
            return len(doc.get("m", []))
        except Exception as e:
            print(f"[Security Count Error] {e}")
    exp, members = LOCAL_SEC["counters"].get(cid, (now + window * 2, set()))
    members = set(members) | {member}
    LOCAL_SEC["counters"][cid] = (exp, members)
    return len(members)


def sec_is_exempt(uid: str) -> bool:
    """Allow list: the owner, permanent staff and TE Premium members are never slowed down or blocked."""
    if not uid:
        return False
    if uid in OWNER_IDS or uid in HARDCODED_STAFF:
        return True
    cached = SEC_CACHE["prem"].get(uid)
    now = datetime.now().timestamp()
    if cached and now - cached[0] < 60:
        return cached[1]
    value = bool(is_user_premium(uid))
    SEC_CACHE["prem"][uid] = (now, value)
    if len(SEC_CACHE["prem"]) > 2000:
        SEC_CACHE["prem"].clear()
    return value


def sec_blocks() -> dict:
    now = datetime.now().timestamp()
    if now - SEC_CACHE["blocks"][0] < 8:
        return SEC_CACHE["blocks"][1]
    items = {}
    docs = list(LOCAL_SEC["blocks"].values())
    if db_sec_blocks is not None:
        try:
            docs = list(db_sec_blocks.find({"until": {"$gt": now}}).limit(500))
        except Exception as e:
            print(f"[Security Blocks Error] {e}")
    for d in docs:
        if float(d.get("until", 0)) > now:
            items[(d["kind"], d["value"])] = d
    SEC_CACHE["blocks"] = [now, items]
    return items


def sec_block(kind: str, value: str, minutes: int, reason: str, trigger: str, label: str = ""):
    now = datetime.now().timestamp()
    doc = {"_id": f"{kind}:{value}", "kind": kind, "value": value, "until": now + minutes * 60, "reason": reason[:200],
           "trigger": trigger, "at": now, "label": label[:60]}
    LOCAL_SEC["blocks"][doc["_id"]] = doc
    SEC_CACHE["blocks"][1][(kind, value)] = doc
    if db_sec_blocks is not None:
        try:
            db_sec_blocks.replace_one({"_id": doc["_id"]}, doc, upsert=True)
        except Exception as e:
            print(f"[Security Block Save Error] {e}")


def sec_unblock(kind: str, value: str):
    LOCAL_SEC["blocks"].pop(f"{kind}:{value}", None)
    SEC_CACHE["blocks"][1].pop((kind, value), None)
    if db_sec_blocks is not None:
        db_sec_blocks.delete_one({"_id": f"{kind}:{value}"})


def sec_set_level(level: int, trigger: str, reason: str, detail: str = "", by: str = "auto"):
    """Changes the lockdown level and tells the bot to DM the owner."""
    level = max(0, min(4, int(level)))
    now = datetime.now().timestamp()
    old = int(SEC_CACHE["state"][1].get("level", 0)) if SEC_CACHE["state"][1] else 0
    cfg = sec_config()
    recent = list((SEC_CACHE["state"][1] or {}).get("recent", []))[-19:]
    if level > 0:
        recent.append({"t": trigger, "at": now})
    state = {"level": level, "since": now if level != old else (SEC_CACHE["state"][1] or {}).get("since", now),
             "reason": reason[:300], "trigger": trigger, "by": by, "recent": recent,
             "auto_unlock_at": (now + cfg["auto_unlock_minutes"] * 60) if level > 0 else 0}
    LOCAL_SEC["state"] = state
    SEC_CACHE["state"] = [now, state]
    if db_settings is not None:
        try:
            db_settings.replace_one({"_id": "security_state"}, {"_id": "security_state", **state}, upsert=True)
        except Exception as e:
            print(f"[Security State Save Error] {e}")
    sec_log("level", level, f"{SEC_LEVEL_NAMES[old]} -> {SEC_LEVEL_NAMES[level]}: {reason}", by)
    if by == "auto" and level != old and (level > old or old > 0):
        alert = {"kind": "lockdown" if level > old else "unlocked", "level": level, "old_level": old, "trigger": trigger, "reason": reason[:300],
                 "detail": detail[:300], "by": by, "at": now, "auto_unlock_at": state["auto_unlock_at"], "pending": True}
        LOCAL_SEC["alerts"].append(alert)
        if db_sec_alerts is not None:
            try:
                db_sec_alerts.insert_one(dict(alert))
            except Exception as e:
                print(f"[Security Alert Error] {e}")


def sec_raise(level: int, trigger: str, reason: str, detail: str = ""):
    cfg = sec_config()
    if not cfg["enabled"] or not cfg["triggers"].get(trigger, {"on": True})["on"] and trigger in cfg["triggers"]:
        return
    current = sec_level()
    if cfg["dry_run"]:
        sec_log(trigger, level, f"DRY RUN: would raise the lockdown to level {level}. {reason}", "dry-run")
        return
    # three different warnings in 10 minutes means something bigger is going on
    now = datetime.now().timestamp()
    recent = [r for r in (SEC_CACHE["state"][1] or {}).get("recent", []) if now - r["at"] < 600] + [{"t": trigger, "at": now}]
    if len({r["t"] for r in recent}) >= 3 and level < 4:
        level, reason = 4, "3 different warnings in 10 minutes. " + reason
    if level > current:
        sec_set_level(level, trigger, reason, detail)
    else:  # same level again: keep the lockdown going a bit longer
        state = dict(SEC_CACHE["state"][1] or {})
        if state.get("level"):
            state["auto_unlock_at"] = now + cfg["auto_unlock_minutes"] * 60
            state["recent"] = recent[-20:]
            SEC_CACHE["state"] = [now, state]
            LOCAL_SEC["state"] = state
            if db_settings is not None:
                try:
                    db_settings.update_one({"_id": "security_state"}, {"$set": {"auto_unlock_at": state["auto_unlock_at"], "recent": state["recent"]}})
                except Exception as e:
                    print(f"[Security State Save Error] {e}")


def sec_trip(trigger: str, kind: str, value: str, detail: str, label: str = ""):
    """One source (an IP or an account) went over a limit: block it. If three sources do this, lock the whole site a bit."""
    cfg = sec_config()
    meta = SEC_TRIGGERS[trigger]
    if not cfg["enabled"] or not cfg["triggers"][trigger]["on"]:
        return
    if cfg["dry_run"]:
        if sec_count(f"dry:{trigger}:{value}", 600) == 1:  # write it once, not on every request
            sec_log(trigger, 1, f"DRY RUN: would block {kind} {label or value}. {detail}", "dry-run", value)
        return
    sec_block(kind, value, meta["block_min"], meta["label"], trigger, label)
    sec_log(trigger, 1, detail, f"blocked {kind} for {meta['block_min']} min", value)
    if sec_count(f"strike:{trigger}", 600) >= 3:
        sec_raise(meta["level"], trigger, f"3 different sources broke the rule: {meta['label']}.", detail)


def sec_global(trigger: str, detail: str):
    cfg = sec_config()
    if cfg["enabled"] and cfg["triggers"][trigger]["on"]:
        sec_raise(SEC_TRIGGERS[trigger]["level"], trigger, SEC_TRIGGERS[trigger]["label"] + ".", detail)


def sec_over(trigger: str, count: int) -> bool:
    cfg = sec_config()
    return cfg["enabled"] and cfg["triggers"][trigger]["on"] and count > cfg["triggers"][trigger]["threshold"]


def sec_new_account():
    try:
        if sec_over("new_accounts", sec_count("newacc", 600)):
            sec_global("new_accounts", "Many new accounts logged in at the same time.")
    except Exception as e:
        print(f"[Security New Account Error] {e}")


def sec_note_blocked_sender(sender: str):
    try:
        if sec_over("blocked_sender", sec_count_unique(f"blocked_sender:{sender}", 86400, str(session.get("user", {}).get("id", "?")))):
            if not sec_is_exempt(str(sender)):
                sec_trip("blocked_sender", "user", str(sender), "Many people blocked this sender.", (_web_user(sender) or {}).get("name", ""))
    except Exception as e:
        print(f"[Security Block Note Error] {e}")


def sec_wrong_access():
    try:
        cfg = sec_config()
        if not cfg["enabled"] or not cfg["triggers"]["wrong_access"]["on"]:
            return
        ip = sec_hash(sec_ip())
        uid = str((session.get("user") or {}).get("id", ""))
        n = sec_count(f"wrong:{ip}", 600)
        if n > cfg["triggers"]["wrong_access"]["threshold"] - 1:
            sec_trip("wrong_access", "ip", ip, f"Someone tried the owner area {n} times (account {uid or 'not logged in'}).", f"account {uid}" if uid else "")
            if uid and not cfg["dry_run"]:
                sec_block("user", uid, SEC_TRIGGERS["wrong_access"]["block_min"], "Tried the owner area", "wrong_access", (_web_user(uid) or {}).get("name", ""))
            if n >= 10:
                sec_raise(4, "wrong_access", "The owner area was tried 10 times in 10 minutes.", f"Account {uid or 'not logged in'}")
            else:
                sec_raise(SEC_TRIGGERS["wrong_access"]["level"], "wrong_access", SEC_TRIGGERS["wrong_access"]["label"] + ".", f"Account {uid or 'not logged in'}")
    except Exception as e:
        print(f"[Security Wrong Access Error] {e}")


@app.before_request
def security_gate():
    try:
        path = request.path
        if path.startswith("/static/") or path == "/api/live":
            return None
        cfg = sec_config()
        if not cfg["enabled"]:
            return None
        uid = str((session.get("user") or {}).get("id", ""))
        g.sec_exempt = sec_is_exempt(uid)
        if g.sec_exempt:
            return None
        ip = sec_hash(sec_ip())
        g.sec_ip = ip
        blocks = sec_blocks()
        hit = blocks.get(("ip", ip)) or (blocks.get(("user", uid)) if uid else None)
        if hit:
            g.sec_skip = True
            mins = max(1, int((float(hit["until"]) - datetime.now().timestamp()) // 60) + 1)
            text = f"You are blocked for about {mins} more minute(s) because of suspicious activity. If this is a mistake, wait and try again."
            if path.startswith("/api/") or request.method != "GET" or path in ("/generate-blueprint", "/edit-blueprint"):
                return jsonify({"success": False, "error": text}), 429, {"Retry-After": str(mins * 60)}
            return text, 429
        level = sec_level()
        # features that are switched off during a lockdown
        for pattern, feature in SEC_FEATURES:
            if request.method == "POST" and pattern.match(path):
                if level >= 3:
                    return jsonify({"success": False, "locked": True, "error": f"{SEC_FEATURE_TEXT[feature]} are switched off for a short time for safety. Please try again later."}), 503
                if level == 2 and feature == "ai":
                    created = (_web_user(uid) or {}).get("created_at", 0) if uid else 0
                    if not created or datetime.now(timezone.utc).timestamp() - float(created) < 86400:
                        return jsonify({"success": False, "locked": True, "error": "New accounts cannot use the AI tools right now. Please try again later."}), 503
                break
        # counting
        if request.method == "POST":
            if path in ("/generate-blueprint", "/edit-blueprint") and uid:
                if sec_over("ai_abuse", sec_count(f"ai:{uid}", 600)):
                    sec_trip("ai_abuse", "user", uid, "One account used the AI tools too often.", (_web_user(uid) or {}).get("name", ""))
                if sec_over("ai_global", sec_count("ai_all", 600)):
                    sec_global("ai_global", "The AI tools were used very often by many people.")
            elif path == "/api/submit-design" and uid:
                if sec_over("submit_spam", sec_count(f"submit:{uid}", 600)):
                    sec_trip("submit_spam", "user", uid, "One account submitted designs too often.", (_web_user(uid) or {}).get("name", ""))
                sid = str((request.get_json(silent=True) or {}).get("target_server_id", ""))[:20]
                if sid.isdigit() and sec_over("same_server", sec_count_unique(f"srv:{sid}", 3600, uid) - 1 + 1):
                    sec_trip("same_server", "user", uid, f"Many accounts used the same server ID ({sid}).", (_web_user(uid) or {}).get("name", ""))
            elif _re.match(r"^/api/designs/[^/]+/send$", path) and uid:
                if sec_over("harassment", sec_count(f"send:{uid}", 3600)):
                    sec_trip("harassment", "user", uid, "One account tried to send too many designs.", (_web_user(uid) or {}).get("name", ""))
        elif path.startswith("/s/") or path.startswith("/api/shared/"):
            share_id = path.rsplit("/", 1)[-1][:20]
            if sec_over("share_abuse", sec_count(f"share:{share_id}", 600)):
                sec_revoke_share(share_id)
        if path.startswith("/api/") and not path.startswith(SEC_UNCOUNTED):
            if sec_over("traffic", sec_count(f"ip:{ip}", 60)):
                sec_trip("traffic", "ip", ip, "One IP sent too many requests in one minute.")
            if sec_count("all", 60) > 3000:
                sec_global("traffic", "The website got more than 3000 requests in one minute.")
    except Exception as e:  # security must never break the website
        print(f"[Security Gate Error] {e}")
    return None


@app.after_request
def security_after(resp):
    try:
        if resp.status_code in (401, 403, 404, 429) and request.path.startswith("/api/") and not request.path.startswith(("/api/live", "/api/usage", "/api/inbox")) \
                and not getattr(g, "sec_skip", False) and getattr(g, "sec_ip", None) and not getattr(g, "sec_exempt", False):
            if sec_over("failed_requests", sec_count(f"fail:{g.sec_ip}", 300)):
                sec_trip("failed_requests", "ip", g.sec_ip, "One IP got many error answers in 5 minutes.")
    except Exception as e:
        print(f"[Security After Error] {e}")
    return resp


_BRAND_JSON_KEYS = ("error", "message", "note", "description", "text", "reward", "title")


@app.after_request
def brand_after(resp):
    """Shows the current brand everywhere: every page and every text the website sends goes through brand.apply()."""
    try:
        if resp.direct_passthrough or resp.status_code in (204, 304) or request.path.startswith("/api/owner/brand") or brand.get() == brand.DEFAULT:
            return resp
        if resp.mimetype == "text/html":
            resp.set_data(brand.apply(resp.get_data(as_text=True)))
        elif resp.mimetype == "application/json":
            data = resp.get_json(silent=True)       # only the message texts, never the saved designs
            if isinstance(data, dict):
                for key in _BRAND_JSON_KEYS:
                    if isinstance(data.get(key), str):
                        data[key] = brand.apply(data[key])
                resp.set_data(json.dumps(data))
    except Exception as e:
        print(f"[Brand After Error] {e}")
    return resp


# ----- Owner Panel: heist times (the bot reads them from the database) -----
HEIST_DEFAULTS = {"wait": 90, "run": 60, "cooldown": 1800}
HEIST_RANGE = {"wait": (15, 600), "run": (10, 600), "cooldown": (0, 86400)}


def heist_config() -> dict:
    saved = {}
    if db_settings is not None:
        try:
            saved = db_settings.find_one({"_id": "heist_config"}) or {}
        except Exception as e:
            print(f"[Heist Config Error] {e}")
    out = {}
    for key, default in HEIST_DEFAULTS.items():
        lo, hi = HEIST_RANGE[key]
        try:
            out[key] = min(hi, max(lo, int(saved.get(key, default))))
        except (TypeError, ValueError):
            out[key] = default
    return out


@app.route("/api/owner/heist")
@owner_only
def api_owner_heist():
    return jsonify({"heist": heist_config(), "default": HEIST_DEFAULTS, "range": HEIST_RANGE})


@app.route("/api/owner/heist", methods=["POST"])
@owner_only
def api_owner_heist_save():
    data = request.get_json(silent=True) or {}
    if data.get("reset"):
        new = dict(HEIST_DEFAULTS)
    else:
        new = {}
        for key, label in (("wait", "Join time"), ("run", "Start wait"), ("cooldown", "Cooldown")):
            lo, hi = HEIST_RANGE[key]
            v = data.get(key)
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                return jsonify({"success": False, "error": f"{label} must be a whole number from {lo} to {hi} seconds."}), 400
            new[key] = v
    if db_settings is None:
        return jsonify({"success": False, "error": "The database is not connected."}), 503
    try:
        db_settings.update_one({"_id": "heist_config"}, {"$set": new}, upsert=True)
    except Exception as e:
        print(f"[Heist Config Save Error] {e}")
        return jsonify({"success": False, "error": "Could not save. Please try again."}), 500
    return jsonify({"success": True, "heist": new})


# ----- Owner Panel: Rebrand -----
@app.route("/api/owner/brand")
@owner_only
def api_owner_brand():
    brand.refresh(force=True)
    return jsonify({"brand": brand.get(), "default": brand.DEFAULT, "limits": brand.LIMITS})


@app.route("/api/owner/brand", methods=["POST"])
@owner_only
def api_owner_brand_save():
    data = request.get_json(silent=True) or {}
    if data.get("reset"):
        brand.reset()
        return jsonify({"success": True, "brand": brand.get()})
    cfg, err = brand.validate(data)
    if err:
        return jsonify({"success": False, "error": err}), 400
    try:
        brand.save(cfg)
    except Exception as e:
        print(f"[Brand Save Error] {e}")
        return jsonify({"success": False, "error": "Could not save. Please try again."}), 500
    return jsonify({"success": True, "brand": brand.get()})


def sec_revoke_share(share_id: str):
    cfg = sec_config()
    if not cfg["triggers"]["share_abuse"]["on"]:
        return
    if cfg["dry_run"]:
        sec_log("share_abuse", 1, f"DRY RUN: would turn off share link {share_id}.", "dry-run", share_id)
        return
    try:
        if db_designs is not None:
            db_designs.update_one({"share_id": share_id}, {"$unset": {"share_id": ""}, "$set": {"on_profile": False}})
        else:
            for d in LOCAL_DESIGNS:
                if d.get("share_id") == share_id:
                    d.pop("share_id", None)
                    d["on_profile"] = False
        sec_log("share_abuse", 1, f"Share link {share_id} was opened too often.", "link turned off", share_id)
        if sec_count("strike:share_abuse", 600) >= 3:
            sec_raise(2, "share_abuse", SEC_TRIGGERS["share_abuse"]["label"] + ".", f"Link {share_id}")
    except Exception as e:
        print(f"[Security Revoke Error] {e}")


# ----- Owner Panel: Security tab -----
@app.route("/api/owner/security")
@owner_only
def api_owner_security():
    now = datetime.now(timezone.utc).timestamp()
    state = dict(sec_state())
    state.pop("_id", None)
    blocks = [{"kind": d["kind"], "value": d["value"], "label": d.get("label", ""), "reason": d.get("reason", ""), "trigger": d.get("trigger", ""),
               "minutes": max(1, int((float(d["until"]) - now) // 60) + 1)} for d in sec_blocks().values()]
    events = LOCAL_SEC["log"][:50]
    if db_sec_log is not None:
        try:
            events = [{k: v for k, v in e.items() if k != "_id"} for e in db_sec_log.find({}).sort("at", -1).limit(50)]
        except Exception as e:
            print(f"[Security Events Error] {e}")
    return jsonify({"level": int(state.get("level", 0)), "level_name": SEC_LEVEL_NAMES[int(state.get("level", 0))], "state": state,
                    "config": sec_config(), "triggers": {k: {"label": v["label"], "level": v["level"], "block_min": v["block_min"]} for k, v in SEC_TRIGGERS.items()},
                    "levels": SEC_LEVEL_NAMES, "blocks": blocks, "events": events, "now": now})


@app.route("/api/owner/security/config", methods=["POST"])
@owner_only
def api_owner_security_config():
    if not request.is_json:
        return jsonify({"success": False, "error": "Bad request."}), 400
    data = request.get_json(silent=True) or {}
    cfg = sec_config()
    try:
        if isinstance(data.get("enabled"), bool):
            cfg["enabled"] = data["enabled"]
        if isinstance(data.get("dry_run"), bool):
            cfg["dry_run"] = data["dry_run"]
        if "auto_unlock_minutes" in data:
            mins = int(data["auto_unlock_minutes"])
            if not 1 <= mins <= 1440:
                return jsonify({"success": False, "error": "Auto-unlock must be from 1 to 1440 minutes."}), 400
            cfg["auto_unlock_minutes"] = mins
        for name, t in (data.get("triggers") or {}).items():
            if name in cfg["triggers"] and isinstance(t, dict):
                if isinstance(t.get("on"), bool):
                    cfg["triggers"][name]["on"] = t["on"]
                if "threshold" in t:
                    th = int(t["threshold"])
                    if not 1 <= th <= 1000000:
                        return jsonify({"success": False, "error": "A number is out of range."}), 400
                    cfg["triggers"][name]["threshold"] = th
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Please type numbers only."}), 400
    sec_save_config(cfg)
    sec_log("config", 0, "Security settings changed by the owner.", "owner", str(session["user"]["id"]))
    return jsonify({"success": True})


@app.route("/api/owner/security/level", methods=["POST"])
@owner_only
def api_owner_security_level():
    if not request.is_json:
        return jsonify({"success": False, "error": "Bad request."}), 400
    try:
        level = int((request.get_json(silent=True) or {}).get("level"))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Choose a level from 0 to 4."}), 400
    if not 0 <= level <= 4:
        return jsonify({"success": False, "error": "Choose a level from 0 to 4."}), 400
    sec_set_level(level, "manual", "Set by the owner in the Owner Panel." if level else "Unlocked by the owner in the Owner Panel.", by=str(session["user"]["id"]))
    return jsonify({"success": True})


@app.route("/api/owner/security/unblock", methods=["POST"])
@owner_only
def api_owner_security_unblock():
    data = request.get_json(silent=True) or {}
    kind, value = str(data.get("kind", "")), str(data.get("value", ""))
    if kind not in ("ip", "user") or not value or len(value) > 64:
        return jsonify({"success": False, "error": "Bad request."}), 400
    sec_unblock(kind, value)
    sec_log("unblock", 0, f"Unblocked {kind} {value}.", "owner", value)
    return jsonify({"success": True})


# ----- Owner Panel: Shop prices (Discord shop items and website cosmetics) -----
@app.route("/api/owner/shop")
@owner_only
def api_owner_shop():
    load_cosmetic_prices(force=True)
    discord_items = []
    if db_shop is not None:
        try:
            for it in db_shop.find({}, {"_id": 0}):
                discord_items.append({"key": str(it.get("key", "")), "name": str(it.get("name", "Item")), "price": int(it.get("price", 0)),
                                      "stock": "Unlimited" if it.get("stock") is None else int(it.get("stock", 0)), "sold": int(it.get("sold", 0))})
            discord_items.sort(key=lambda i: (i["price"], i["name"].lower()))
        except Exception as e:
            print(f"[Owner Shop Error] {e}")
    cosmetics = [{"id": it["id"], "kind": it["kind"], "name": it["name"], "price": it["price"], "default": shop_items.base_price(it["id"]),
                  "changed": it["id"] in shop_items.OVERRIDES} for it in shop_items.catalog()]
    return jsonify({"discord": discord_items, "cosmetics": cosmetics})


@app.route("/api/owner/shop/price", methods=["POST"])
@owner_only
def api_owner_shop_price():
    data = request.get_json(silent=True) or {}
    source, key = str(data.get("source", "")), str(data.get("key", ""))
    reset = data.get("price") in (None, "")
    price = None
    if not reset:
        try:
            price = int(data.get("price"))
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "Please type a whole number."}), 400
        if not 1 <= price <= 1000000:
            return jsonify({"success": False, "error": "The price must be from 1 to 1,000,000."}), 400
    if source == "discord":
        if db_shop is None or reset:
            return jsonify({"success": False, "error": "Type a new price for this item."}), 400
        res = db_shop.update_one({"key": key}, {"$set": {"price": price}})
        if not getattr(res, "matched_count", 1):
            return jsonify({"success": False, "error": "This item does not exist anymore."}), 404
        return jsonify({"success": True})
    if source == "cosmetic":
        if shop_items.base_price(key) is None:
            return jsonify({"success": False, "error": "This item does not exist."}), 404
        if db_settings is None:
            return jsonify({"success": False, "error": "The database is not connected."}), 503
        load_cosmetic_prices(force=True)
        prices = dict(shop_items.OVERRIDES)
        if reset:
            prices.pop(key, None)
        else:
            prices[key] = price
        db_settings.update_one({"_id": "shop_prices"}, {"$set": {"prices": prices}}, upsert=True)
        shop_items.set_overrides(prices)
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Bad request."}), 400


@app.route("/api/live")
def api_live():
    """Small status check the page asks for every few seconds, so owner changes show up without a refresh."""
    user = session.get("user") or {}
    uid = str(user.get("id", ""))
    personal = ((_web_user(uid) or {}).get("theme_mode") if uid else "") or ""
    m = get_maintenance()
    resp = jsonify({
        "theme": personal if personal in ("dark", "light") else get_site_theme(),
        "free_mode": get_free_mode(),
        "skin": _equipped(uid)["skin"] if uid else "",
        "premium": bool(uid) and is_user_premium(uid),
        "maintenance": {"on": bool(m["on"]) and not is_owner(), "message": m["message"] or DEFAULT_MAINTENANCE_TEXT},
    })
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/")
def index():
    user = session.get("user")
    if user and is_blocked_user(user.get("id")):
        return "<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'><body style='font-family:monospace;background:#060f1c;color:#e6f0fa;padding:24px'><h2>Access Blocked</h2><p>Your account is blocked from Template Engine. If you think this is a mistake, contact the staff.</p>", 403
    roblox_user = session.get("roblox_user")
    is_premium = False
    if user and user.get("id") and not _web_user(user["id"]):
        remember_web_user(user["id"], user.get("username"), user.get("username"), user.get("avatar"))  # for people who logged in before
    personal_theme = ((_web_user(user["id"]) or {}).get("theme_mode") if user and user.get("id") else "") or ""
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
        site_theme=(personal_theme if personal_theme in ("dark", "light") else get_site_theme()),
        skin=(_equipped(str(user["id"]))["skin"] if user and user.get("id") else ""),
        skin_css=shop_items.skin_css() + "\n" + shop_items.name_color_css(),
        free_mode=get_free_mode(),
        pending_share=pending_share,
        is_owner=bool(user and str(user.get('id', '')) in OWNER_IDS),
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
    if target_server_id in get_blacklist()["guilds"]:
        return jsonify({"success": False, "error": "This server is blocked from using Template Engine."}), 403
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
        "footer": {"text": brand.apply("Template Engine Infrastructure Log")},
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
