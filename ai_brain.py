import os
import json
import re
import asyncio
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field
from google import genai
from google.genai import types
import aiohttp


# --- SCHEMAS ---
class ChannelPermissionsConfig(BaseModel):
    view: bool = Field(default=True, description="Whether users can view the channel")
    send: bool = Field(default=True, description="Whether users can send messages in the channel")
    embed: bool = Field(default=True, description="Whether links will embed automatically")
    attach: bool = Field(default=True, description="Whether users can upload files/attachments")


class RoleConfig(BaseModel):
    name: str = Field(description="Role name, e.g. Owner, Admin, VIP, or Member")
    color: str = Field(default="#8b5cf6", description="Hex color code for the role, e.g. #f1c40f")


class ChannelConfig(BaseModel):
    name: str = Field(description="Clean channel name formatted for Discord without leading emojis, e.g. mod-chat or general")
    type: str = Field(description="Channel type: 'text', 'voice', or 'announcement'")
    topic: Optional[str] = Field(default="", description="Brief channel topic/purpose")
    emoji: Optional[str] = Field(default="💬", description="Single emoji representing the channel")
    read_only: bool = Field(default=False, description="True if only admins/bots should speak (e.g. announcements/rules)")
    permissions: Optional[ChannelPermissionsConfig] = Field(
        default_factory=ChannelPermissionsConfig,
        description="Fine-grained channel permissions for view, send, embed, and attach"
    )


class CategoryConfig(BaseModel):
    name: str = Field(description="Category header name without emojis, e.g. INFORMATION or COMMUNITY")
    emoji: Optional[str] = Field(default="📁", description="Single emoji representing the category header")
    channels: List[ChannelConfig] = Field(description="List of channels inside this category")


class ServerLayoutSchema(BaseModel):
    server_name: str = Field(description="Suggested server name based on prompt")
    description: str = Field(description="A brief description of the server theme")
    roles: List[RoleConfig] = Field(description="List of roles configured for this server layout")
    categories: List[CategoryConfig] = Field(description="List of categories in structured order")


# --- GEMINI MODELS FALLBACK SEQUENCE ---
# Old models (1.5, 2.0) are retired and return 404, so only current ones are listed.
# You can override with the env var GEMINI_MODELS="model-a,model-b".
GEMINI_MODELS = [m.strip() for m in os.getenv("GEMINI_MODELS", "").split(",") if m.strip()] or [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-flash-latest",
    "gemini-flash-lite-latest",
]


def short_error(e: Exception) -> str:
    """Short one-line error text for messages."""
    return " ".join(str(e).split())[:160]


def is_busy_error(e: Exception) -> bool:
    t = str(e)
    return any(x in t for x in ("429", "503", "500", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "overloaded"))


# --- HELPER FUNCTIONS ---
def strip_emojis(text: str) -> str:
    """Removes emojis from strings while strictly preserving regular alphanumeric character text."""
    if not text:
        return ""
    # Use accurate 32-bit unicode surrogate ranges to strip emojis without affecting ASCII text
    clean = re.sub(r'[\U00010000-\U0010ffff\u2600-\u27ff\u2300-\u23ff]', '', text)
    return clean.strip()


def calculate_account_age(discord_id: str) -> str:
    """Calculates Discord account age in days/years from a snowflake ID."""
    try:
        snowflake = int(discord_id)
        timestamp_ms = (snowflake >> 22) + 1420070400000
        created_at = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc)
        now = datetime.now(timezone.utc)
        
        days_old = (now - created_at).days
        if days_old >= 365:
            years = days_old // 365
            rem_days = days_old % 365
            return f"{years} yr{'' if years == 1 else 's'}, {rem_days} day{'' if rem_days == 1 else 's'} ({days_old} days total)"
        return f"{days_old} day{'' if days_old == 1 else 's'}"
    except Exception:
        return "Unknown"


IS_COMPONENTS_V2 = 32768  # message flag: the message is built from components (cards with thin separator lines)


def log_components(title: str, items, color: int, file_name: Optional[str] = None) -> list:
    """Clean log card as raw component JSON: title, thin separator line between every item. No emojis."""
    inner = [{"type": 10, "content": f"### {title}"[:300]}]
    for label, value in items:
        value = str(value)
        if len(value) > 80 or "\n" in value:
            if not value.startswith(("<", "`")):
                value = f"```\n{value[:900]}\n```"
        elif not value.startswith(("<", "`", "http")):
            value = f"`{value}`"
        inner.append({"type": 14, "divider": True, "spacing": 1})
        inner.append({"type": 10, "content": f"**{label}**\n{value}"[:1200]})
    if file_name:
        inner.append({"type": 14, "divider": True, "spacing": 1})
        inner.append({"type": 13, "file": {"url": f"attachment://{file_name}"}})
    inner.append({"type": 10, "content": f"-# <t:{int(datetime.now(timezone.utc).timestamp())}:F>"})
    return [{"type": 17, "accent_color": color, "components": inner}]


async def send_webhook_log(
    webhook_url: str,
    title: str,
    user_info: Dict[str, Any],
    action_details: Dict[str, Any],
    color: int = 0x8b5cf6
):
    """Dispatches a structured, clean webhook log to Discord with clear user metrics."""
    if not webhook_url:
        return

    user_id = str(user_info.get("id", "0"))
    username = user_info.get("username", "Unknown User")
    mention = f"<@{user_id}>" if user_id != "0" else "@unknown"
    account_age = calculate_account_age(user_id) if user_id != "0" else "Unknown"

    items = [
        ("User", mention),
        ("Username", username),
        ("User ID", user_id),
        ("Account Age", account_age),
    ]
    for key, val in action_details.items():
        items.append((str(key), val))

    payload = {
        "username": "Studio Logger",
        "avatar_url": "https://cdn.discordapp.com/embed/avatars/0.png",
        "flags": IS_COMPONENTS_V2,
        "components": log_components(title, items, color),
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(webhook_url, params={"with_components": "true"}, json=payload) as resp:
                pass
    except Exception as e:
        print(f"Failed to send Discord webhook log: {e}")


# --- AI GENERATION MAIN FUNCTION ---
async def generate_server_layout(
    prompt: str,
    channel_separator: str = "│",
    server_id: str = "",
    server_link: str = "",
    user_info: Optional[Dict[str, Any]] = None,
    webhook_url: Optional[str] = None
) -> dict:
    """Uses Gemini AI to convert natural language prompt into a structured Discord Server Layout, with fallback for model errors."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY environment variable is not set.")

    client = genai.Client(api_key=api_key)

    system_instruction = (
        "You are an expert Discord Community Infrastructure Architect. Your goal is to design a clean, logical, "
        "and well-structured Discord server based on the user's requirements.\n"
        "Guidelines:\n"
        "1. Organize the server into clear, functional categories (e.g., WELCOME, GENERAL, GAMING, VOICE).\n"
        "2. Provide a clean category name without emojis and a separate emoji field for categories.\n"
        "3. Define default roles with realistic hex colors (e.g., Owner, Admin, Mod, VIP, Member).\n"
        "4. Keep channel names lower-case, concise, and WITHOUT emojis in the name field (e.g. 'general', 'rules').\n"
        "5. Provide a single representative emoji in the emoji field for each channel.\n"
        "6. Mark announcement, rules, or info channels as read_only=True and set set=False in permissions for read-only channels."
    )

    user_query = f"""
    Design a complete Discord server layout for the following theme/request:
    "{prompt}"

    Apply the separator character '{channel_separator}' appropriately where needed for visual layout formatting.
    """

    layout_data = None
    last_error = None
    successful_model = None

    all_errors = []
    for model_name in GEMINI_MODELS:
        for attempt in range(2):  # retry once if the model is busy
            try:
                print(f"Attempting generation using model: {model_name}...")
                response = await asyncio.to_thread(
                    client.models.generate_content,
                    model=model_name,
                    contents=user_query,
                    config=types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        response_mime_type="application/json",
                        response_schema=ServerLayoutSchema,
                        temperature=0.7,
                    ),
                )
                layout_data = json.loads(response.text)
                successful_model = model_name
                break
            except Exception as e:
                last_error = e
                print(f"[{model_name}] Generation failed: {e}")
                if attempt == 0 and is_busy_error(e):
                    await asyncio.sleep(2)
                    continue
                all_errors.append(f"{model_name}: {short_error(e)}")
                break
        if layout_data:
            break

    if not layout_data:
        if webhook_url and user_info:
            await send_webhook_log(
                webhook_url=webhook_url,
                title="AI Generation Failed (All Models)",
                user_info=user_info,
                action_details={
                    "Prompt": prompt,
                    "Error": " | ".join(all_errors)[:1000]
                },
                color=0xf43f5e
            )
        return {
            "success": False,
            "error": "All Gemini models failed. " + " | ".join(all_errors)
        }

    # Process layout formatting
    sep = channel_separator.strip() if channel_separator else ""
    for category in layout_data.get("categories", []):
        cat_name = category.get("name", "")
        cleaned_cat = strip_emojis(cat_name)
        category["name"] = cleaned_cat if cleaned_cat else cat_name
        
        for channel in category.get("channels", []):
            raw_name = channel.get("name") or channel.get("clean_name") or channel.get("title") or ""
            clean_name = strip_emojis(raw_name)
            if not clean_name:
                clean_name = raw_name.lower().replace(" ", "-")
                
            emoji = channel.get("emoji", "💬")
            channel["name"] = clean_name

            # Ensure channel permissions align with read_only state
            if channel.get("read_only"):
                if "permissions" not in channel or not channel["permissions"]:
                    channel["permissions"] = {"view": True, "send": False, "embed": True, "attach": True}
                else:
                    channel["permissions"]["send"] = False

            if sep and emoji:
                channel["formatted_name"] = f"{emoji} {sep} {clean_name}"
            elif emoji:
                channel["formatted_name"] = f"{emoji} {clean_name}"
            else:
                channel["formatted_name"] = clean_name

    layout_data["build_meta"] = {
        "channel_separator": channel_separator,
        "target_server_id": server_id.strip(),
        "target_server_link": server_link.strip(),
        "prompt_used": prompt,
        "model_used": successful_model
    }

    if webhook_url and user_info:
        await send_webhook_log(
            webhook_url=webhook_url,
            title="AI Server Layout Generated",
            user_info=user_info,
            action_details={
                "Prompt": prompt,
                "Model Used": successful_model,
                "Target Server ID": server_id or "Not Provided",
                "Categories Built": len(layout_data.get("categories", [])),
                "Roles Created": len(layout_data.get("roles", []))
            },
            color=0x8b5cf6
        )

    return {
        "success": True,
        "data": layout_data
    }


# --- AI SERVER CONSULTANT (/analyze) ---
class AnalysisProblem(BaseModel):
    title: str = Field(description="Short name of the problem, e.g. 'Everyone can mention @everyone'")
    detail: str = Field(description="1-2 simple sentences: what is wrong and why it matters. Name the real channels or roles.")
    severity: str = Field(description="One of: high, medium, low")


class AnalysisSuggestion(BaseModel):
    title: str = Field(description="Short action title, e.g. 'Add a rules channel'")
    detail: str = Field(description="1-2 simple sentences: exactly what to do. Name the real channels or roles.")


class ServerAnalysisSchema(BaseModel):
    score: int = Field(description="Overall server quality from 0 to 100. Be honest, not generous.")
    summary: str = Field(description="2-3 simple sentences about the overall state of the server")
    strengths: List[str] = Field(description="Up to 5 things the server already does well")
    problems: List[AnalysisProblem] = Field(description="Up to 6 problems, most serious first")
    suggestions: List[AnalysisSuggestion] = Field(description="Up to 8 concrete improvements, most useful first")


async def analyze_server(summary: Dict[str, Any], findings: List[str]) -> dict:
    """AI server consultant: reads a summary of a Discord server and suggests improvements."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return {"success": False, "error": "GEMINI_API_KEY is not set."}

    client = genai.Client(api_key=api_key)

    system_instruction = (
        "You are an expert Discord community consultant. You review the structure of a Discord server "
        "and give honest, specific, practical advice.\n"
        "Rules:\n"
        "1. Use the real channel, category and role names from the data. Never invent channels that are already there.\n"
        "2. Judge organization, naming, permissions and security, onboarding (rules, welcome, roles), "
        "moderation setup (logs, mod channels), community features and missing basics.\n"
        "3. The 'detected_issues' list was found by automatic checks. Use it, explain it, and add what it missed.\n"
        "4. Write in short, simple English. No markdown, no emojis.\n"
        "5. Be honest with the score. A normal untouched server is around 40-60."
    )
    user_query = (
        "Analyze this Discord server and suggest improvements.\n\n"
        f"SERVER DATA (JSON):\n{json.dumps(summary, ensure_ascii=False)[:24000]}\n\n"
        f"DETECTED ISSUES:\n{json.dumps(findings, ensure_ascii=False)}"
    )

    last_error = None
    all_errors = []
    for model_name in GEMINI_MODELS:
        try:
            response = await asyncio.to_thread(
                client.models.generate_content,
                model=model_name,
                contents=user_query,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    response_mime_type="application/json",
                    response_schema=ServerAnalysisSchema,
                    temperature=0.4,
                ),
            )
            data = json.loads(response.text)
            data["score"] = max(0, min(100, int(data.get("score", 50))))
            return {"success": True, "data": data, "model": model_name}
        except Exception as e:
            last_error = e
            all_errors.append(f"{model_name}: {short_error(e)}")
            print(f"[Analyze] {model_name} failed: {e}. Trying next model...")
    return {"success": False, "error": "All AI models failed. " + " | ".join(all_errors)}
