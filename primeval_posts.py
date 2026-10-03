"""Staff bot posts: announcements, events, and emergency Isle-down alerts.

/bot_post opens a form. The bot publishes a formatted embed to #announcements
or #events (or a channel you pick). Unscheduled shutdowns post to the staff
log with a Relaunch button and skip the 00/06/12/18 Pacific restart window.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone

import discord
from discord import app_commands, ui

_CHANNEL_URL_RE = re.compile(
    r"https?://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/channels/\d+/(\d+)(?:/\d+)?",
    re.I,
)
_HASH_NAME_RE = re.compile(r"(?<!<)#([a-z0-9_-]{1,100})(?![a-z0-9_-])", re.I)
_CONTENT_LIMIT = 2000

G = {}

STAFF_LOG_FALLBACK_ID = 1539044183417819226
STAFF_LOG_CHANNEL_ID = 1539044183417819226
HEALTH_CHANNEL_ID = 1541157110262399076
RELAUNCH_CUSTOM_ID = "pi_isle_relaunch"

_RELAUNCH_LOCK = asyncio.Lock()
_LAST_RELAUNCH_AT = 0.0
_RELAUNCH_COOLDOWN = 45

KIND_META = {
    "announcement": {
        "label": "Announcement",
        "emoji": "📢",
        "color": discord.Color.gold(),
        "names": ("announcements", "announcement", "server-announcements"),
    },
    "event": {
        "label": "Event",
        "emoji": "🎉",
        "color": discord.Color.purple(),
        "names": ("events", "event", "isle-events"),
    },
}

ALERT_NAMES = (
    "staff-alerts",
    "isle-alerts",
    "server-alerts",
    "admin-action-log",
    "staff-log",
)


def bind(g):
    G.clear()
    G.update(g)


def find_named_channel(guild, names):
    if guild is None:
        return None
    want = [str(n).strip().lower().lstrip("#") for n in names if n]
    for channel in getattr(guild, "text_channels", []) or []:
        slug = str(getattr(channel, "name", "") or "").strip().lower()
        if slug in want:
            return channel
    for channel in getattr(guild, "text_channels", []) or []:
        slug = str(getattr(channel, "name", "") or "").strip().lower()
        if any(name in slug for name in want):
            return channel
    return None


async def _fetch(bot, channel_id):
    if not channel_id:
        return None
    channel = bot.get_channel(int(channel_id)) if bot else None
    if channel is not None:
        return channel
    try:
        return await bot.fetch_channel(int(channel_id))
    except Exception:
        return None


def default_channel(guild, kind):
    meta = KIND_META.get(kind) or {}
    return find_named_channel(guild, meta.get("names") or ())


async def staff_alert_channel(bot, guild=None):
    log_channel = await _fetch(bot, STAFF_LOG_CHANNEL_ID)
    if log_channel is not None:
        return log_channel
    if guild is None and bot is not None:
        health = bot.get_channel(HEALTH_CHANNEL_ID)
        guild = getattr(health, "guild", None)
        if guild is None:
            guilds = list(getattr(bot, "guilds", []) or [])
            guild = guilds[0] if guilds else None
    found = find_named_channel(guild, ALERT_NAMES)
    if found is not None:
        return found
    return await _fetch(bot, STAFF_LOG_FALLBACK_ID)


def _channel_rank(channel):
    kind = getattr(channel, "type", None)
    value = getattr(kind, "value", kind)
    try:
        value = int(value)
    except Exception:
        value = 99
    if value in (0, 5):
        return 0
    if value == 15:
        return 1
    return 2


def _channel_index(guild):
    index = {}
    if guild is None:
        return index
    channels = list(getattr(guild, "channels", None) or [])
    threads = list(getattr(guild, "threads", None) or [])
    for channel in channels + threads:
        slug = str(getattr(channel, "name", "") or "").strip().lower()
        if not slug:
            continue
        prev = index.get(slug)
        if prev is None or _channel_rank(channel) < _channel_rank(prev):
            index[slug] = channel
    return index


def linkify_discord_refs(text, guild):
    """Turn #channel-name and Discord channel URLs into clickable <#id> mentions."""
    raw = str(text or "")
    if not raw:
        return raw

    def from_url(match):
        return f"<#{match.group(1)}>"

    out = _CHANNEL_URL_RE.sub(from_url, raw)
    names = _channel_index(guild)

    def from_hash(match):
        slug = match.group(1).lower()
        channel = names.get(slug)
        if channel is None:
            return match.group(0)
        return f"<#{channel.id}>"

    return _HASH_NAME_RE.sub(from_hash, out)


def _split_content(text, limit=_CONTENT_LIMIT):
    text = str(text or "")
    if len(text) <= limit:
        return text, ""
    cut = text.rfind("\n", 0, limit)
    if cut < limit // 2:
        cut = limit
    return text[:cut].rstrip(), text[cut:].lstrip()


def _icon_url(obj):
    guild = getattr(obj, "guild", None) or obj
    icon = getattr(guild, "icon", None)
    if icon is None:
        return ""
    return str(getattr(icon, "url", "") or "")


def build_post_embed(kind, title, body, author=None, guild=None):
    meta = KIND_META.get(kind) or KIND_META["announcement"]
    headline = str(title or "").strip() or meta["label"]
    desc = str(body or "").strip()[:4096]
    embed = discord.Embed(
        title=f"{meta['emoji']}  {headline}",
        color=meta["color"],
        timestamp=datetime.now(timezone.utc),
    )
    if desc:
        embed.description = desc
    icon = _icon_url(guild) or _icon_url(author)
    author_name = f"Fallen Earth · {meta['label']}"
    if icon:
        embed.set_author(name=author_name, icon_url=icon)
        embed.set_thumbnail(url=icon)
    else:
        embed.set_author(name=author_name)
    who = ""
    if author is not None:
        who = str(getattr(author, "display_name", None) or getattr(author, "name", "") or "")
    footer = "Posted by staff via Fallen Earth Elder"
    if who:
        footer = f"Posted by {who} · Fallen Earth Elder"
    embed.set_footer(text=footer)
    return embed


async def publish(channel, embed, content="", mention_here=False):
    if channel is None:
        return None
    text = str(content or "").strip()
    if mention_here:
        text = "@here\n" + text if text else "@here"
    kwargs = {
        "embed": embed,
        "allowed_mentions": discord.AllowedMentions(
            everyone=bool(mention_here),
            users=True,
            roles=False,
            replied_user=False,
        ),
    }
    if text:
        kwargs["content"] = text
    return await channel.send(**kwargs)


async def publish_staff_post(channel, kind, title, body, author=None, guild=None, mention_here=False):
    guild = guild or getattr(channel, "guild", None) or getattr(author, "guild", None)
    linked = linkify_discord_refs(str(body or "").strip(), guild)
    content, overflow = _split_content(linked)
    embed = build_post_embed(kind, title, overflow, author, guild)
    return await publish(channel, embed, content=content, mention_here=mention_here)


class PostModal(ui.Modal):
    def __init__(self, kind, channel):
        meta = KIND_META.get(kind) or KIND_META["announcement"]
        super().__init__(title=f"{meta['label']} post"[:45])
        self.kind = kind
        self.channel = channel
        self.headline = ui.TextInput(
            label="Title",
            placeholder="Short headline players will see",
            max_length=120,
            required=True,
        )
        self.body = ui.TextInput(
            label="Post body",
            style=discord.TextStyle.paragraph,
            placeholder="Type #channel-name (like #events) — the bot turns it into a clickable Discord link.",
            max_length=3900,
            required=True,
        )
        self.add_item(self.headline)
        self.add_item(self.body)

    async def on_submit(self, interaction: discord.Interaction):
        from primeval_panels import can_staff, deny_staff

        if not can_staff(interaction.user, "bot_post"):
            await deny_staff(interaction, "bot_post")
            return
        channel = self.channel
        if channel is None:
            channel = default_channel(interaction.guild, self.kind)
        if channel is None:
            await interaction.response.send_message(
                "Could not find the destination channel. Run `/bot_post` and pick a channel.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            message = await publish_staff_post(
                channel,
                self.kind,
                str(self.headline.value),
                str(self.body.value),
                interaction.user,
                interaction.guild,
            )
        except Exception as exc:
            await interaction.followup.send(f"Post failed: {exc}", ephemeral=True)
            return
        jump = getattr(message, "jump_url", "") or f"<#{channel.id}>"
        await interaction.followup.send(f"Posted in {channel.mention}: {jump}", ephemeral=True)


class BotPostKindView(ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    async def _open(self, interaction, kind):
        from primeval_panels import can_staff, deny_staff

        if not can_staff(interaction.user, "bot_post"):
            await deny_staff(interaction, "bot_post")
            return
        channel = default_channel(interaction.guild, kind)
        if channel is None:
            names = (KIND_META.get(kind) or {}).get("names") or ("that-channel",)
            await interaction.response.send_message(
                f"Could not find `#{names[0]}` by name. Use `/bot_post` and pick a channel.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(PostModal(kind, channel))

    @ui.button(label="Announcement", emoji="📢", style=discord.ButtonStyle.primary)
    async def announcement(self, interaction: discord.Interaction, button: ui.Button):
        await self._open(interaction, "announcement")

    @ui.button(label="Event", emoji="🎉", style=discord.ButtonStyle.success)
    async def event(self, interaction: discord.Interaction, button: ui.Button):
        await self._open(interaction, "event")

    @ui.button(label="Hunt night", emoji="🦴", style=discord.ButtonStyle.success)
    async def hunt_night(self, interaction: discord.Interaction, button: ui.Button):
        import primeval_events

        await primeval_events.show_staff_controls(interaction)


async def show_post_picker(interaction: discord.Interaction):
    from primeval_panels import can_staff, deny_staff

    if not can_staff(interaction.user, "bot_post"):
        await deny_staff(interaction, "bot_post")
        return
    await interaction.response.send_message(
        "Pick the type. The bot posts to **#announcements** or **#events** as itself. "
        "Hunt night / migration live on the **Events panel**. "
        "Type `#channel-name` in the body and it becomes a clickable Discord channel. "
        "Or use `/bot_post` to choose any channel.",
        view=BotPostKindView(),
        ephemeral=True,
    )


def _panel_state(stats):
    return str((stats or {}).get("state") or "unknown").strip().lower() or "unknown"


def isle_host_down(stats):
    name = _panel_state(stats)
    return name in ("offline", "stopped", "crashed")


def emergency_embed(stats, recovered=False, relaunched_by=None, relaunch_note=""):
    state = str((stats or {}).get("state") or "unknown")
    now = datetime.now(timezone.utc)
    if recovered:
        embed = discord.Embed(
            title="🟢 Isle is back online",
            description=(
                "The Isle host is **running** again.\n"
                f"Panel state: `{state}`"
            ),
            color=discord.Color.green(),
            timestamp=now,
        )
        embed.set_footer(text="Unscheduled shutdown · Fallen Earth Elder")
        return embed
    embed = discord.Embed(
        title="🛑 Unscheduled shutdown",
        description=(
            "Isle went **offline** outside the scheduled "
            "**00:00 / 12:00 / 18:00 Pacific** restart window.\n\n"
            "Use **Relaunch server** only if the host is still down."
        ),
        color=discord.Color.dark_red(),
        timestamp=now,
    )
    embed.add_field(name="Panel", value=f"`{state}`", inline=True)
    embed.add_field(name="Seen", value=f"<t:{int(time.time())}:t>", inline=True)
    embed.add_field(name="Board", value=f"<#{HEALTH_CHANNEL_ID}>", inline=True)
    err = str((stats or {}).get("error") or "").strip()
    if err:
        embed.add_field(name="Panel note", value=err[:200], inline=False)
    bits = []
    if relaunched_by:
        bits.append(f"Sent by {relaunched_by}")
    if relaunch_note:
        bits.append(relaunch_note)
    if bits:
        embed.add_field(name="Relaunch", value="\n".join(bits), inline=False)
    embed.set_footer(text="Staff log · Fallen Earth Elder")
    return embed


def relaunch_view(disabled=False, label="Relaunch server"):
    view = ui.View(timeout=None)
    view.add_item(
        ui.Button(
            label=label,
            style=discord.ButtonStyle.success,
            custom_id=RELAUNCH_CUSTOM_ID,
            disabled=bool(disabled),
        )
    )
    return view


class IsleRelaunchView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(
        label="Relaunch server",
        style=discord.ButtonStyle.success,
        custom_id=RELAUNCH_CUSTOM_ID,
    )
    async def relaunch(self, interaction: discord.Interaction, button: ui.Button):
        await handle_relaunch(interaction)


async def _start_isle_host():
    import primeval_isle

    return await primeval_isle.request_json("POST", "/power", json_body={"signal": "start"})


async def handle_relaunch(interaction: discord.Interaction):
    from primeval_panels import can_staff, deny_staff

    if not can_staff(interaction.user, "relaunch_isle"):
        await deny_staff(interaction, "relaunch_isle")
        return
    await interaction.response.defer(ephemeral=True)
    global _LAST_RELAUNCH_AT
    async with _RELAUNCH_LOCK:
        try:
            import primeval_health_board

            stats = await primeval_health_board.fetch_isle_stats()
        except Exception as exc:
            await interaction.followup.send(f"Could not read the Isle panel: {exc}", ephemeral=True)
            return
        state = _panel_state(stats)
        if state == "running":
            await _edit_down_message(
                interaction.message,
                stats,
                disabled=True,
                label="Already online",
                relaunch_note="Host was already running — start was not sent.",
            )
            await interaction.followup.send(
                "Isle is already **running**. Relaunch was not sent.",
                ephemeral=True,
            )
            return
        if state == "starting":
            await interaction.followup.send(
                "Isle is already **starting**. Wait for it to finish coming up.",
                ephemeral=True,
            )
            return
        if state == "stopping":
            await interaction.followup.send(
                "Isle is still **stopping**. Wait until it is offline, then relaunch.",
                ephemeral=True,
            )
            return
        if not isle_host_down(stats):
            await interaction.followup.send(
                f"Panel state is `{state}`. Relaunch only runs when Isle is still down.",
                ephemeral=True,
            )
            return
        now = time.time()
        wait = _RELAUNCH_COOLDOWN - (now - _LAST_RELAUNCH_AT)
        if wait > 0:
            await interaction.followup.send(
                f"Relaunch was just sent. Wait **{int(wait)}s** before trying again.",
                ephemeral=True,
            )
            return
        status, body = await _start_isle_host()
        _LAST_RELAUNCH_AT = now
        who = interaction.user.mention
        note = f"Panel replied `{status}` · was `{state}`"
        if status not in (200, 204):
            err = ""
            if isinstance(body, dict):
                err = str(body.get("error") or body.get("raw") or "")[:160]
            await _edit_down_message(
                interaction.message,
                stats,
                disabled=False,
                relaunched_by=who,
                relaunch_note=f"Start failed ({status}). {err}".strip(),
            )
            await interaction.followup.send(
                f"Start failed (HTTP {status}). Button is still available if Isle is down.",
                ephemeral=True,
            )
            return
        await _edit_down_message(
            interaction.message,
            stats,
            disabled=False,
            label="Relaunch again if still down",
            relaunched_by=who,
            relaunch_note=note,
        )
        await interaction.followup.send(
            "Start sent to Game Host Bros. Watch the health board — "
            "click again only if Isle is still down after a minute.",
            ephemeral=True,
        )


async def _edit_down_message(message, stats, disabled=False, label="Relaunch server", relaunched_by=None, relaunch_note=""):
    if message is None:
        return
    embed = emergency_embed(
        stats,
        recovered=False,
        relaunched_by=relaunched_by,
        relaunch_note=relaunch_note,
    )
    try:
        await message.edit(embed=embed, view=relaunch_view(disabled=disabled, label=label))
    except Exception as exc:
        print(f"[POSTS] down-alert edit failed: {exc}")


async def settle_down_alert(bot, channel_id, message_id, stats):
    if not channel_id or not message_id:
        return
    channel = await _fetch(bot, channel_id)
    if channel is None:
        return
    try:
        message = await channel.fetch_message(int(message_id))
    except Exception:
        return
    await _edit_down_message(
        message,
        stats,
        disabled=True,
        label="Back online",
        relaunch_note="Host is running again. Button closed.",
    )


async def post_emergency(bot, stats, recovered=False):
    channel = await staff_alert_channel(bot)
    if channel is None:
        print("[POSTS] no staff log channel")
        return None
    embed = emergency_embed(stats, recovered=recovered)
    content = ""
    view = None
    if not recovered:
        content = "@here Unscheduled Isle shutdown — relaunch from this log if it is still down."
        view = IsleRelaunchView()
    try:
        kwargs = {
            "embed": embed,
            "allowed_mentions": discord.AllowedMentions(
                everyone=not recovered,
                users=True,
                roles=False,
                replied_user=False,
            ),
        }
        if content:
            kwargs["content"] = content
        if view is not None:
            kwargs["view"] = view
        message = await channel.send(**kwargs)
        print("[POSTS] emergency", "recovered" if recovered else "down", channel.id)
        return message
    except Exception as exc:
        print(f"[POSTS] emergency failed: {exc}")
        return None


async def post_crash_watch(
    bot,
    *,
    reason: str,
    stats: dict | None,
    previous_uptime_s: float = 0,
    scheduled: bool = False,
    cpu: float | None = None,
    mem_gb: float | None = None,
):
    """Post Isle uptime-cliff / restart notice to the staff (admin) log."""
    channel = await staff_alert_channel(bot)
    if channel is None:
        print("[POSTS] no staff log for crash watch")
        return None
    stats = stats or {}
    state = str(stats.get("state") or "unknown")
    up_s = int((stats.get("uptime_ms") or 0) / 1000)
    now = datetime.now(timezone.utc)
    if scheduled:
        title = "🔄 Isle restart (expected)"
        color = discord.Color.orange()
        description = (
            "Uptime reset while a **scheduled / staff** restart was in progress.\n"
            "No action needed unless the host stays down."
        )
        footer = "Crash watch · expected · Fallen Earth Elder"
    else:
        title = "⚠️ Isle uptime reset"
        color = discord.Color.dark_red()
        description = (
            "Isle uptime dropped outside a known restart window.\n"
            "Check the health board — relaunch from staff log if it stays offline."
        )
        footer = "Crash watch · unexpected · Fallen Earth Elder"
    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=now,
    )
    embed.add_field(name="Reason", value=f"`{str(reason)[:180]}`", inline=False)
    embed.add_field(name="Panel", value=f"`{state}`", inline=True)
    embed.add_field(
        name="Uptime",
        value=f"was `{int(previous_uptime_s)}s` → now `{up_s}s`",
        inline=True,
    )
    embed.add_field(name="Board", value=f"<#{HEALTH_CHANNEL_ID}>", inline=True)
    if cpu is not None:
        embed.add_field(name="CPU (last)", value=f"{cpu:.0f}%", inline=True)
    if mem_gb is not None:
        embed.add_field(name="RAM (last)", value=f"{mem_gb:.2f}G", inline=True)
    embed.set_footer(text=footer)
    try:
        message = await channel.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=False),
        )
        print("[POSTS] crash watch", "scheduled" if scheduled else "unexpected", channel.id)
        return message
    except Exception as exc:
        print(f"[POSTS] crash watch failed: {exc}")
        return None


async def post_director_restart(bot, user, ok, detail=""):
    channel = await staff_alert_channel(bot)
    if channel is None:
        print("[POSTS] no staff log for director restart")
        return None
    who = getattr(user, "mention", None) or str(user)
    embed = discord.Embed(
        title="🔄 Director restart" if ok else "🔄 Director restart failed",
        description=(
            "A Director scheduled an Isle restart from the staff panel. "
            "Players have **5 minutes** to safelog. This is not a clock restart."
            if ok
            else "A Director tried to schedule an Isle restart from the staff panel, and it was not accepted."
        ),
        color=discord.Color.orange() if ok else discord.Color.dark_red(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="By", value=who, inline=True)
    embed.add_field(name="Board", value=f"<#{HEALTH_CHANNEL_ID}>", inline=True)
    if detail:
        embed.add_field(name="Result", value=str(detail)[:300], inline=False)
    embed.set_footer(text="Staff log · Fallen Earth Elder")
    try:
        return await channel.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions(everyone=False, users=True, roles=False),
        )
    except Exception as exc:
        print(f"[POSTS] director restart log failed: {exc}")
        return None


def register_views(bot):
    bot.add_view(IsleRelaunchView())


def register_slash(bot):
    names = {cmd.name for cmd in bot.tree.get_commands()}
    if "bot_post" in names:
        return

    @bot.tree.command(name="bot_post", description="Staff: post a formatted announcement or event as the bot")
    @app_commands.describe(
        kind="Announcement goes to #announcements, event to #events, unless you pick a channel",
        channel="Optional override (any text channel the bot can post in)",
    )
    @app_commands.choices(
        kind=[
            app_commands.Choice(name="Announcement", value="announcement"),
            app_commands.Choice(name="Event", value="event"),
        ]
    )
    async def bot_post(
        interaction: discord.Interaction,
        kind: app_commands.Choice[str],
        channel: discord.TextChannel | None = None,
    ):
        from primeval_panels import can_staff, deny_staff

        if not can_staff(interaction.user, "bot_post"):
            await deny_staff(interaction, "bot_post")
            return
        dest = channel or default_channel(interaction.guild, kind.value)
        if dest is None:
            await interaction.response.send_message(
                "Pick a **channel** — I could not find `#announcements` or `#events` by name.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(PostModal(kind.value, dest))
