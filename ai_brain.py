import os
import json
import re
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
GEMINI_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-1.5-flash"
]


# --- HELPER FUNCTIONS ---
def strip_emojis(text: str) -> str:
    """Removes emojis from channel or category name strings to prevent duplicate visual rendering."""
    return re.sub(r'[\u1F600-\u1F64F\u1F300-\u1F5FF\u1F680-\u1F6FF\u2600-\u26FF\u2700-\u27BF]', '', text or '').strip()


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


async def send_webhook_log(
    webhook_url: str,
    title: str,
    user_info: Dict[str, Any],
    action_details: Dict[str, Any],
    color: int = 0x8b5cf6
):
    """Dispatches a structured, glass-themed webhook log to Discord with clear user metrics."""
    if not webhook_url:
        return

    user_id = str(user_info.get("id", "0"))
    username = user_info.get("username", "Unknown User")
    mention = f"<@{user_id}>" if user_id != "0" else "@unknown"
    account_age = calculate_account_age(user_id) if user_id != "0" else "Unknown"

    user_details_value = (
        f"**User:** {mention}\n"
        f"**Username:** `{username}`\n"
        f"**User ID:** `{user_id}`\n"
        f"**Account Age:** `{account_age}`"
    )

    embed = {
        "title": title,
        "color": color,
        "fields": [
            {
                "name": "👤 User Information",
                "value": user_details_value,
                "inline": False
            }
        ],
        "footer": {
            "text": "Studio Logging System"
        },
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

    for key, val in action_details.items():
        embed["fields"].append({
            "name": f"📌 {key}",
            "value": f"```\n{str(val)[:1000]}\n```" if len(str(val)) > 80 else f"`{val}`",
            "inline": False
        })

    payload = {
        "username": "Studio Logger",
        "avatar_url": "https://cdn.discordapp.com/embed/avatars/0.png",
        "embeds": [embed]
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(webhook_url, json=payload) as resp:
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

    for model_name in GEMINI_MODELS:
        try:
            print(f"Attempting generation using model: {model_name}...")
            response = client.models.generate_content(
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
            err_msg = str(e)
            print(f"[{model_name}] Generation failed: {err_msg}. Retrying with next model...")
            continue

    if not layout_data:
        if webhook_url and user_info:
            await send_webhook_log(
                webhook_url=webhook_url,
                title="⚠️ AI Generation Failed (All Models)",
                user_info=user_info,
                action_details={
                    "Prompt": prompt,
                    "Error": str(last_error)
                },
                color=0xf43f5e
            )
        return {
            "success": False,
            "error": f"All Gemini models failed. Last error: {str(last_error)}"
        }

    # Process layout formatting
    sep = channel_separator.strip() if channel_separator else ""
    for category in layout_data.get("categories", []):
        category["name"] = strip_emojis(category.get("name", ""))
        
        for channel in category.get("channels", []):
            clean_name = strip_emojis(channel.get("name", ""))
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
            title="⚡ AI Server Layout Generated",
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
