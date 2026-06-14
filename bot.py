import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
import json
import os
import re
import aiohttp
import xml.etree.ElementTree as ET
import yt_dlp

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")
YOUTUBE_HANDLE = "moonaticsmusic"

CONFIG_FILE = "config.json"
SEEN_FILE = "seen.json"
POLL_INTERVAL_MINUTES = 5

CONTENT_TYPES = ["video", "short", "livestream", "community"]
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"}

# Resolved at startup
channel_id: str = ""

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
# Channel ID resolution
# ---------------------------------------------------------------------------

async def resolve_channel_id(session: aiohttp.ClientSession) -> str:
    """Scrape the channel handle page once to extract the channel ID."""
    url = f"https://www.youtube.com/@{YOUTUBE_HANDLE}"
    async with session.get(url, headers=HEADERS) as r:
        html = await r.text()
    match = re.search(r'"channelId"\s*:\s*"(UC[^"]+)"', html)
    if not match:
        raise ValueError(f"Could not resolve channel ID for @{YOUTUBE_HANDLE}")
    return match.group(1)


# ---------------------------------------------------------------------------
# RSS feed — videos / shorts / livestreams (no bot detection)
# ---------------------------------------------------------------------------

NS = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}

async def fetch_rss(session: aiohttp.ClientSession) -> list[dict]:
    """Fetch the channel RSS feed — returns up to 15 latest uploads with id + title."""
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    async with session.get(url, headers=HEADERS) as r:
        text = await r.text()
    root = ET.fromstring(text)
    results = []
    for entry in root.findall("atom:entry", NS):
        vid_id = entry.findtext("yt:videoId", namespaces=NS) or ""
        title  = entry.findtext("atom:title", namespaces=NS) or ""
        results.append({"id": vid_id, "title": title})
    return results


async def classify_video(session: aiohttp.ClientSession, vid_id: str) -> str:
    """
    Classify a video as 'short', 'livestream', or 'video' by checking
    the /shorts/ URL (redirects to /watch if not a short) and the oEmbed data.
    """
    # Check if it's a short — YouTube returns 200 for valid shorts URLs
    shorts_url = f"https://www.youtube.com/shorts/{vid_id}"
    try:
        async with session.head(shorts_url, headers=HEADERS, allow_redirects=False) as r:
            if r.status == 200:
                return "short"
    except Exception:
        pass

    # Check if it's a livestream via oEmbed (fast, no auth needed)
    oembed_url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={vid_id}&format=json"
    try:
        async with session.get(oembed_url, headers=HEADERS) as r:
            if r.status == 200:
                data = await r.json()
                # Live videos have "live" in the author_name or title — not reliable
                # Fall back: just check if the watch page has isLiveBroadcast
                pass
    except Exception:
        pass

    # Lightweight check for live broadcast in page metadata
    watch_url = f"https://www.youtube.com/watch?v={vid_id}"
    try:
        async with session.get(watch_url, headers=HEADERS) as r:
            chunk = await r.content.read(50000)  # read only first 50KB
            text = chunk.decode("utf-8", errors="ignore")
            if '"isLiveBroadcast":true' in text or '"isLive":true' in text:
                return "livestream"
    except Exception:
        pass

    return "video"


# ---------------------------------------------------------------------------
# Community posts — yt-dlp flat extraction (much lighter, less bot-detected)
# ---------------------------------------------------------------------------

def _scrape_community() -> list[dict]:
    opts = {
        "extract_flat": "in_playlist",
        "quiet": True,
        "no_warnings": True,
        "playlist_items": "1-10",
        "skip_download": True,
        "http_headers": HEADERS,
    }
    url = f"https://www.youtube.com/@{YOUTUBE_HANDLE}/community"
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
        except Exception as e:
            print(f"[yt-dlp] community error: {e}")
            return []
    if not info:
        return []
    results = []
    for entry in info.get("entries", []):
        if not entry:
            continue
        eid = entry.get("id", "")
        title = entry.get("content") or entry.get("description") or entry.get("title", "")
        eurl = entry.get("webpage_url") or f"https://www.youtube.com/post/{eid}"
        results.append({"id": eid, "title": title, "url": eurl})
    return results


async def fetch_community() -> list[dict]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _scrape_community)


# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
http_session: aiohttp.ClientSession | None = None


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
    seen: dict = load_json(SEEN_FILE, {t: [] for t in CONTENT_TYPES})
    changed = False

    # --- Videos / Shorts / Livestreams via RSS ---
    try:
        rss_entries = await fetch_rss(http_session)
    except Exception as e:
        print(f"[rss] error: {e}")
        rss_entries = []

    all_seen_video_ids = (
        seen.get("video", []) + seen.get("short", []) + seen.get("livestream", [])
    )
    new_rss = [e for e in rss_entries if e["id"] not in all_seen_video_ids]

    new_by_type: dict[str, list] = {"video": [], "short": [], "livestream": []}
    for entry in reversed(new_rss):  # oldest first
        ctype = await classify_video(http_session, entry["id"])
        url = (
            f"https://www.youtube.com/shorts/{entry['id']}"
            if ctype == "short"
            else f"https://www.youtube.com/watch?v={entry['id']}"
        )
        print(f"[notify] {ctype}: {entry['title']} — {url}")
        await notify(ctype, entry["title"], url)
        new_by_type[ctype].append(entry["id"])
        changed = True

    # Merge new IDs into seen per type
    for ctype in ("video", "short", "livestream"):
        seen[ctype] = seen.get(ctype, []) + new_by_type[ctype]
        # Keep only IDs still in the RSS feed to avoid unbounded growth
        rss_ids = [e["id"] for e in rss_entries]
        seen[ctype] = [i for i in seen[ctype] if i in rss_ids or i in new_by_type[ctype]]

    # --- Community posts via yt-dlp flat ---
    try:
        posts = await fetch_community()
    except Exception as e:
        print(f"[community] error: {e}")
        posts = []

    new_posts = [p for p in posts if p["id"] not in seen.get("community", [])]
    for post in reversed(new_posts):
        print(f"[notify] community: {post['url']}")
        await notify("community", post["title"], post["url"])
        changed = True
    if posts:
        seen["community"] = [p["id"] for p in posts]

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
    global http_session, channel_id
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    http_session = aiohttp.ClientSession()
    try:
        channel_id = await resolve_channel_id(http_session)
        print(f"[youtube] Resolved channel ID: {channel_id}")
    except Exception as e:
        print(f"[error] Could not resolve channel ID: {e}")
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
    print("[seed] Seeding current content to avoid notification flood...")
    seen: dict = {t: [] for t in CONTENT_TYPES}
    try:
        rss_entries = await fetch_rss(http_session)
        seen["video"] = [e["id"] for e in rss_entries]
        print(f"[seed] videos/shorts/livestreams: {len(rss_entries)} entries cached")
    except Exception as e:
        print(f"[seed] RSS error: {e}")
    try:
        posts = await fetch_community()
        seen["community"] = [p["id"] for p in posts]
        print(f"[seed] community: {len(posts)} entries cached")
    except Exception as e:
        print(f"[seed] community error: {e}")
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
