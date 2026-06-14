import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
import json
import os
import yt_dlp

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")
YOUTUBE_HANDLE = "moonaticsmusic"

CONFIG_FILE = "config.json"
SEEN_FILE = "seen.json"
POLL_INTERVAL_MINUTES = 5

# Tab URLs per content type
TABS: dict[str, str] = {
    "video":      f"https://www.youtube.com/@{YOUTUBE_HANDLE}/videos",
    "short":      f"https://www.youtube.com/@{YOUTUBE_HANDLE}/shorts",
    "livestream": f"https://www.youtube.com/@{YOUTUBE_HANDLE}/streams",
    "community":  f"https://www.youtube.com/@{YOUTUBE_HANDLE}/community",
}

DEFAULT_MESSAGES = {
    "video":      "🎬 {role} New video from **Moonatics**!\n**{title}**\n{url}",
    "short":      "🩳 {role} New Short from **Moonatics**!\n**{title}**\n{url}",
    "livestream": "🔴 {role} **Moonatics** is going LIVE!\n**{title}**\n{url}",
    "community":  "📢 {role} New community post from **Moonatics**!\n\n{title}\n\n{url}",
}

# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def load_json(path: str, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path: str, data) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# ---------------------------------------------------------------------------
# yt-dlp scraping (blocking — run in executor)
# ---------------------------------------------------------------------------

def _scrape(url: str, content_type: str) -> list[dict]:
    """
    Full yt-dlp extraction for a YouTube tab — fetches real titles/content for all types.
    """
    opts = {
        "quiet": True,
        "no_warnings": True,
        "playlist_items": "1-10",
        "skip_download": True,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        },
        "extractor_args": {
            "youtube": {
                "player_client": ["web"],
            }
        },
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
        except Exception as e:
            print(f"[yt-dlp] error scraping {content_type}: {e}")
            return []

    if not info:
        return []

    results = []
    for entry in info.get("entries", []):
        if not entry:
            continue
        eid = entry.get("id", "")
        if content_type == "community":
            # Post body text is in 'content', fallback to 'title'
            title = entry.get("content") or entry.get("description") or entry.get("title", "")
            eurl = entry.get("webpage_url") or f"https://www.youtube.com/post/{eid}"
        else:
            title = entry.get("title", "")
            eurl = entry.get("webpage_url") or f"https://www.youtube.com/watch?v={eid}"
        results.append({"id": eid, "title": title, "url": eurl})
    return results


async def scrape_tab(content_type: str) -> list[dict]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _scrape, TABS[content_type], content_type)


# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


# ---------------------------------------------------------------------------
# Notification dispatcher
# ---------------------------------------------------------------------------

async def notify(content_type: str, title: str, url: str) -> None:
    config: dict = load_json(CONFIG_FILE, {})
    for guild_id, channels in config.items():
        for ch_id, settings in channels.items():
            if not settings.get("notify", {}).get(content_type, True):
                continue
            channel = bot.get_channel(int(ch_id))
            if channel is None:
                continue
            role_id = settings.get("role_id")
            role_mention = f"<@&{role_id}>" if role_id else ""
            template = settings.get("messages", {}).get(content_type, DEFAULT_MESSAGES[content_type])
            text = template.format(role=role_mention, title=title, url=url)
            try:
                await channel.send(text)
            except discord.Forbidden:
                print(f"[warn] No send permission in channel {ch_id}")


# ---------------------------------------------------------------------------
# Background polling
# ---------------------------------------------------------------------------

@tasks.loop(minutes=POLL_INTERVAL_MINUTES)
async def poll_youtube():
    seen: dict = load_json(SEEN_FILE, {t: [] for t in TABS})
    changed = False

    for content_type in TABS:
        entries = await scrape_tab(content_type)
        if not entries:
            continue

        current_ids = [e["id"] for e in entries]
        new_entries = [e for e in entries if e["id"] not in seen.get(content_type, [])]

        for entry in reversed(new_entries):  # oldest first
            print(f"[notify] {content_type}: {entry['title']} — {entry['url']}")
            await notify(content_type, entry["title"], entry["url"])

        seen[content_type] = current_ids
        changed = True

    if changed:
        save_json(SEEN_FILE, seen)


@poll_youtube.before_loop
async def before_poll():
    await bot.wait_until_ready()


# ---------------------------------------------------------------------------
# on_ready
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s)")
    except Exception as e:
        print(f"[error] Failed to sync commands: {e}")
    await seed_seen()
    poll_youtube.start()


async def seed_seen():
    """On first run, populate seen.json with current content so we don't spam old posts."""
    if os.path.exists(SEEN_FILE):
        return
    print("[seed] seen.json not found — seeding current content to avoid notification flood...")
    seen = {}
    for content_type in TABS:
        entries = await scrape_tab(content_type)
        seen[content_type] = [e["id"] for e in entries]
        print(f"[seed] {content_type}: {len(entries)} entries cached")
    save_json(SEEN_FILE, seen)
    print("[seed] Done — only new content from this point will trigger notifications")


# ---------------------------------------------------------------------------
# Slash commands — /yt group
# ---------------------------------------------------------------------------

yt = app_commands.Group(name="yt", description="YouTube notification settings")


@yt.command(name="setup", description="Set this channel to receive Moonatics YouTube notifications")
@app_commands.describe(
    role="Role to ping when something is posted (leave empty for no ping)",
    videos="Notify for regular videos",
    shorts="Notify for Shorts",
    livestreams="Notify for livestreams",
    community="Notify for community posts",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def yt_setup(
    interaction: discord.Interaction,
    role: discord.Role | None = None,
    videos: bool = True,
    shorts: bool = True,
    livestreams: bool = True,
    community: bool = True,
):
    config: dict = load_json(CONFIG_FILE, {})
    gid = str(interaction.guild_id)
    cid = str(interaction.channel_id)
    config.setdefault(gid, {})
    existing = config[gid].get(cid, {})

    config[gid][cid] = {
        "role_id": role.id if role else None,
        "notify": {
            "video": videos,
            "short": shorts,
            "livestream": livestreams,
            "community": community,
        },
        "messages": existing.get("messages", DEFAULT_MESSAGES.copy()),
    }
    save_json(CONFIG_FILE, config)

    role_text = role.mention if role else "*(no role)*"
    active = [t for t, v in {"video": videos, "short": shorts, "livestream": livestreams, "community": community}.items() if v]
    await interaction.response.send_message(
        f"✅ Notifications configured!\n"
        f"**Role:** {role_text}\n"
        f"**Types:** {', '.join(active) or 'none'}\n\n"
        f"Use `/yt message` to customise each notification message.",
        ephemeral=True,
    )


@yt.command(name="remove", description="Stop YouTube notifications in this channel")
@app_commands.checks.has_permissions(manage_guild=True)
async def yt_remove(interaction: discord.Interaction):
    config: dict = load_json(CONFIG_FILE, {})
    gid = str(interaction.guild_id)
    cid = str(interaction.channel_id)
    if config.get(gid, {}).pop(cid, None) is None:
        await interaction.response.send_message("This channel had no notifications set up.", ephemeral=True)
        return
    save_json(CONFIG_FILE, config)
    await interaction.response.send_message("✅ Notifications removed from this channel.", ephemeral=True)


@yt.command(name="message", description="Customise the notification message for a content type")
@app_commands.describe(
    content_type="Which content type to customise",
    template="Message template — use {role}, {title}, {url} as placeholders ({title} is post text for community)",
)
@app_commands.choices(content_type=[
    app_commands.Choice(name="Video", value="video"),
    app_commands.Choice(name="Short", value="short"),
    app_commands.Choice(name="Livestream", value="livestream"),
    app_commands.Choice(name="Community post", value="community"),
])
@app_commands.checks.has_permissions(manage_guild=True)
async def yt_message(interaction: discord.Interaction, content_type: str, template: str):
    config: dict = load_json(CONFIG_FILE, {})
    gid = str(interaction.guild_id)
    cid = str(interaction.channel_id)
    if cid not in config.get(gid, {}):
        await interaction.response.send_message("Run `/yt setup` first.", ephemeral=True)
        return
    config[gid][cid].setdefault("messages", DEFAULT_MESSAGES.copy())
    config[gid][cid]["messages"][content_type] = template
    save_json(CONFIG_FILE, config)
    preview = template.format(role="@Role", title="Example Title", url="https://youtu.be/example")
    await interaction.response.send_message(
        f"✅ Updated **{content_type}** template.\n\n**Preview:**\n{preview}",
        ephemeral=True,
    )


@yt.command(name="status", description="Show current notification settings for this channel")
async def yt_status(interaction: discord.Interaction):
    config: dict = load_json(CONFIG_FILE, {})
    gid = str(interaction.guild_id)
    cid = str(interaction.channel_id)
    settings = config.get(gid, {}).get(cid)
    if not settings:
        await interaction.response.send_message("No notifications configured for this channel.", ephemeral=True)
        return
    role_id = settings.get("role_id")
    role_text = f"<@&{role_id}>" if role_id else "*(none)*"
    flags = settings.get("notify", {})
    lines = [f"**Role:** {role_text}", "**Content types:**"]
    for t in ("video", "short", "livestream", "community"):
        icon = "✅" if flags.get(t, True) else "❌"
        lines.append(f"  {icon} {t}")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@yt.command(name="test", description="Send a test notification for a content type")
@app_commands.describe(content_type="Which notification type to test")
@app_commands.choices(content_type=[
    app_commands.Choice(name="Video", value="video"),
    app_commands.Choice(name="Short", value="short"),
    app_commands.Choice(name="Livestream", value="livestream"),
    app_commands.Choice(name="Community post", value="community"),
])
@app_commands.checks.has_permissions(manage_guild=True)
async def yt_test(interaction: discord.Interaction, content_type: str):
    config: dict = load_json(CONFIG_FILE, {})
    gid = str(interaction.guild_id)
    cid = str(interaction.channel_id)
    settings = config.get(gid, {}).get(cid)
    if not settings:
        await interaction.response.send_message("Run `/yt setup` first.", ephemeral=True)
        return
    role_id = settings.get("role_id")
    role_mention = f"<@&{role_id}>" if role_id else ""
    template = settings.get("messages", {}).get(content_type, DEFAULT_MESSAGES[content_type])
    text = template.format(role=role_mention, title="Test Title — Moonatics", url="https://youtu.be/dQw4w9WgXcQ")
    await interaction.response.send_message("Sending test...", ephemeral=True)
    await interaction.channel.send(f"*(test)* {text}")


bot.tree.add_command(yt)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("Set the DISCORD_TOKEN environment variable before running.")
    bot.run(DISCORD_TOKEN)
