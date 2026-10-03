"""Posted Events panel: token event, growth window, hunt night, migration.

Same shape as the token event: ON/OFF presets, no typed RCON. Growth
always restores 1x. Hunt / migration post a filled #events template and
a Discord scheduled event.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import discord
from discord import ui
from discord.ext import tasks

G = {}
STATE_PATH = "primeval_events.json"
_LOCK = threading.RLock()

DEFAULT_GROWTH = 1.0
ANNOUNCE_SECONDS = 30 * 60
GROWTH_REAPPLY_SECONDS = 3 * 60
TICK_SECONDS = 20
HUNT_SECONDS = 2 * 60 * 60
DEFAULT_ZONE = "South Plains"
EVENT_ZONES = (
    "South Plains",
    "Center",
    "North Lake",
    "Watering Hole",
)
HUNT_ZONES = EVENT_ZONES
NIGHT_SECONDS = HUNT_SECONDS
NIGHT_META = {
    "hunt": {
        "label": "Hunt night",
        "action": "hunt_night",
        "title": "Hunt night — {zone}",
        "event_name": "Fallen Earth — Hunt Night",
        "announce_on": "Hunt night: meet at {zone}. 2 hours.",
        "announce_off": "Hunt night is over.",
        "announce_still": "Hunt night still on at {zone}. {left} left.",
        "event_desc": "Meet at {zone}. Fight at the zone. Store before you die if you want the dino back.",
        "body": (
            "**Hunt night is on now**\n"
            "Meet at **{zone}**.\n"
            "Ends <t:{ends}:t> (<t:{ends}:R>).\n\n"
            "Fight at the zone. No killing people on the way in.\n"
            "Store before you die if you want that dino back.\n"
            "Staff are typically on evenings Pacific."
        ),
    },
    "migration": {
        "label": "Migration",
        "action": "hunt_night",
        "title": "Migration — {zone}",
        "event_name": "Fallen Earth — Migration",
        "announce_on": "Migration: travel together to {zone}. 2 hours.",
        "announce_off": "Migration is over.",
        "announce_still": "Migration still on — meet at {zone}. {left} left.",
        "event_desc": "Travel together to {zone}. Do not KoS migrants on the route.",
        "body": (
            "**Migration is on now**\n"
            "Travel together to **{zone}**.\n"
            "Ends <t:{ends}:t> (<t:{ends}:R>).\n\n"
            "This is a group move, not a hunt. Do not KoS people on the route.\n"
            "Good time to get migration Prime credit.\n"
            "Store before you die if you want that dino back.\n"
            "Staff are typically on evenings Pacific."
        ),
    },
}

_task = None
_bot = None
_OP_LOCK = None


def _op_lock():
    global _OP_LOCK
    if _OP_LOCK is None:
        _OP_LOCK = asyncio.Lock()
    return _OP_LOCK


def _night_blank():
    return {
        "enabled": False,
        "zone": DEFAULT_ZONE,
        "started_at": 0,
        "ends_at": 0,
        "started_by": 0,
        "channel_id": 0,
        "message_id": 0,
        "discord_event_id": 0,
        "guild_id": 0,
        "last_announce_at": 0,
    }


def bind(g):
    G.clear()
    G.update(g)


def _fn(name, default=None):
    return G.get(name, default)


def _blank():
    return {
        "growth": {
            "enabled": False,
            "rate": DEFAULT_GROWTH,
            "restore": DEFAULT_GROWTH,
            "started_at": 0,
            "ends_at": 0,
            "started_by": 0,
            "last_apply_at": 0,
            "last_announce_at": 0,
            "restore_failed": False,
        },
        "hunt": _night_blank(),
        "migration": _night_blank(),
        "panel": {
            "channel_id": 0,
            "message_id": 0,
            "guild_id": 0,
            "zone": DEFAULT_ZONE,
        },
        "last_uptime": 0,
    }


def _load():
    with _LOCK:
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                blank = _blank()
                for key, value in blank.items():
                    if key not in data:
                        data[key] = value
                    elif isinstance(value, dict) and isinstance(data[key], dict):
                        for inner, default in value.items():
                            data[key].setdefault(inner, default)
                return data
        except Exception:
            pass
        return _blank()


def _save(state):
    with _LOCK:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2)
            handle.write("\n")
        os.replace(tmp, STATE_PATH)


def _now():
    return int(time.time())


def _rate_label(rate):
    try:
        number = float(rate)
    except Exception:
        return "1x"
    if number == int(number):
        return f"{int(number)}x"
    return f"{number:g}x"


def growth_active(state=None):
    state = state or _load()
    row = state.get("growth") or {}
    return bool(row.get("enabled") and int(row.get("ends_at") or 0) > _now())


def night_active(kind, state=None):
    state = state or _load()
    row = state.get(kind) or {}
    return bool(row.get("enabled") and int(row.get("ends_at") or 0) > _now())


def hunt_active(state=None):
    return night_active("hunt", state)


def migration_active(state=None):
    return night_active("migration", state)


def panel_zone(state=None):
    state = state or _load()
    zone = str((state.get("panel") or {}).get("zone") or DEFAULT_ZONE)
    return zone if zone in EVENT_ZONES else DEFAULT_ZONE


def how_to_line():
    state = _load()
    bits = []
    if growth_active(state):
        row = state["growth"]
        bits.append(
            f"**Growth window ON** — dinos grow **{_rate_label(row.get('rate'))}** "
            f"until <t:{int(row.get('ends_at') or 0)}:R>."
        )
    if hunt_active(state):
        row = state["hunt"]
        bits.append(
            f"**Hunt night ON** — meet at **{row.get('zone') or DEFAULT_ZONE}** "
            f"until <t:{int(row.get('ends_at') or 0)}:R>."
        )
    if migration_active(state):
        row = state["migration"]
        bits.append(
            f"**Migration ON** — travel to **{row.get('zone') or DEFAULT_ZONE}** "
            f"until <t:{int(row.get('ends_at') or 0)}:R>."
        )
    if not bits:
        return ""
    return "\n" + "\n".join(bits) + "\n"


def _left_text(ends_at):
    left = max(0, int(ends_at or 0) - _now())
    minutes = max(1, (left + 59) // 60)
    if minutes >= 60:
        hours = minutes // 60
        mins = minutes % 60
        if mins:
            return f"{hours}h {mins}m"
        return f"{hours}h"
    return f"{minutes} min"


async def _rcon(command):
    send = _fn("send_game_command")
    if not send:
        return False, "no rcon"
    try:
        result = await send(command)
    except Exception as exc:
        return False, str(exc)
    text = str(result or "").strip()
    low = text.lower()
    if any(word in low for word in ("unknown command", "invalid", "failed", "error:")):
        return False, text or "rcon rejected"
    return True, text or "ok"


async def _announce(message):
    ok, result = await _rcon("announce " + message)
    print("[EVENTS] announce", ok, result)
    return ok


async def _events_channel(guild=None, bot=None):
    import primeval_posts

    bot = bot or _bot
    if guild is None and bot is not None:
        guilds = list(getattr(bot, "guilds", []) or [])
        guild = guilds[0] if guilds else None
    return primeval_posts.default_channel(guild, "event"), guild


async def _post_event(title, body, author=None, guild=None, bot=None):
    import primeval_posts

    channel, guild = await _events_channel(guild, bot)
    if channel is None:
        return None, "Could not find #events."
    try:
        message = await primeval_posts.publish_staff_post(
            channel,
            "event",
            title,
            body,
            author,
            guild,
            mention_here=True,
        )
        return message, ""
    except Exception as exc:
        return None, str(exc)


async def _audit(interaction, command, extra="", result=""):
    if interaction is None:
        return
    try:
        from primeval_panels import _audit_game_cmd

        await _audit_game_cmd(interaction, command, extra=extra, result=result)
    except Exception as exc:
        print(f"[EVENTS] audit failed: {exc}")


async def _set_growth_rate(rate):
    rate = float(rate)
    return await _rcon(f"setgrowthmultiplier {rate:g}")


async def _apply_growth(state, force=False):
    row = state["growth"]
    if not growth_active(state):
        return False, "off"
    now = _now()
    last = int(row.get("last_apply_at") or 0)
    if not force and last and now - last < GROWTH_REAPPLY_SECONDS:
        return True, "fresh"
    ok, result = await _set_growth_rate(row.get("rate") or 2.0)
    if ok:
        state = _load()
        row = state["growth"]
        row["last_apply_at"] = now
        row["restore_failed"] = False
        _save(state)
    print("[EVENTS] growth apply", ok, result)
    return ok, result


async def _restore_growth(state, reason="off"):
    state = _load()
    row = state["growth"]
    if not row.get("enabled") and not row.get("restore_failed"):
        return True, "already off"
    restore = float(row.get("restore") or DEFAULT_GROWTH)
    ok, result = await _set_growth_rate(restore)
    if ok:
        state = _load()
        row = state["growth"]
        row["enabled"] = False
        row["restore_failed"] = False
        row["last_apply_at"] = _now()
        _save(state)
        await _announce(
            "Fallen Earth Elder — Growth window over. Speed is back to normal."
        )
        await _post_event(
            "Growth window over",
            "Growth is back to **1x**. The window has ended.",
            bot=_bot,
        )
        print("[EVENTS] growth restored", reason, result)
        return True, result
    state = _load()
    state["growth"]["restore_failed"] = True
    _save(state)
    print("[EVENTS] growth restore failed", reason, result)
    return False, result


async def start_growth(rate, seconds, user_id=0, interaction=None):
    async with _op_lock():
        return await _start_growth(rate, seconds, user_id, interaction)


async def _start_growth(rate, seconds, user_id=0, interaction=None):
    rate = float(rate)
    seconds = int(seconds)
    if rate < 1.0 or rate > 4.0 or seconds < 30 * 60 or seconds > 4 * 60 * 60:
        return False, "That preset is not allowed."
    state = _load()
    row = state["growth"]
    if row.get("enabled") or row.get("restore_failed"):
        if growth_active(state):
            left = _left_text(row.get("ends_at"))
            return False, f"Growth is already ON ({left} left). Turn it OFF first."
        return False, "Still restoring 1x from the last window. Wait a few seconds, then try again."
    ok, result = await _set_growth_rate(rate)
    await _audit(interaction, "setgrowthmultiplier", extra=str(rate), result=result)
    if not ok:
        return False, f"Isle did not take the growth command. Nothing was changed.\n`{result}`"
    now = _now()
    state = _load()
    row = state["growth"]
    row.update({
        "enabled": True,
        "rate": rate,
        "restore": DEFAULT_GROWTH,
        "started_at": now,
        "ends_at": now + seconds,
        "started_by": int(user_id or 0),
        "last_apply_at": now,
        "last_announce_at": now,
        "restore_failed": False,
    })
    _save(state)
    if seconds == 3600:
        span = "1 hour"
    elif seconds % 3600 == 0:
        span = f"{seconds // 3600} hours"
    else:
        span = _left_text(now + seconds)
    await _announce(
        f"Fallen Earth Elder — Growth window ON: {_rate_label(rate)} for {span}. It turns itself off."
    )
    ends_at = now + seconds
    posted, post_err = await _post_event(
        f"Growth window — {_rate_label(rate)}",
        (
            f"**Growth window is on now**\n"
            f"Dinos grow at **{_rate_label(rate)}** for **{span}**.\n"
            f"Ends <t:{ends_at}:t> (<t:{ends_at}:R>).\n\n"
            "It turns itself off and returns to **1x**."
        ),
        author=getattr(interaction, "user", None),
        guild=getattr(interaction, "guild", None),
    )
    extra = ""
    if posted is not None:
        extra = f"\n{getattr(posted, 'jump_url', '')}"
    elif post_err:
        extra = f"\nPosted in-game, but #events failed: {post_err}"
    return True, f"Growth is ON at {_rate_label(rate)} for {span}. It returns to 1x automatically.{extra}"


async def stop_growth(user_id=0, interaction=None):
    async with _op_lock():
        return await _stop_growth(user_id, interaction)


async def _stop_growth(user_id=0, interaction=None):
    state = _load()
    row = state["growth"]
    if not row.get("enabled") and not row.get("restore_failed"):
        return False, "Growth window is already OFF."
    row["started_by"] = int(user_id or row.get("started_by") or 0)
    _save(state)
    ok, result = await _restore_growth(state, reason="manual")
    await _audit(interaction, "setgrowthmultiplier", extra="1", result=result)
    if not ok:
        return False, f"Could not restore 1x. The bot will keep trying.\n`{result}`"
    return True, "Growth window OFF. Speed is 1x."


async def _create_discord_event(guild, kind, zone, ends_at):
    meta = NIGHT_META[kind]
    if guild is None:
        return 0, "no guild"
    start = datetime.now(timezone.utc) + timedelta(minutes=1)
    end = datetime.fromtimestamp(int(ends_at), tz=timezone.utc)
    if end <= start:
        end = start + timedelta(hours=2)
    try:
        event = await guild.create_scheduled_event(
            name=meta["event_name"],
            description=meta["event_desc"].format(zone=zone),
            start_time=start,
            end_time=end,
            privacy_level=discord.PrivacyLevel.guild_only,
            entity_type=discord.EntityType.external,
            location=f"Fallen Earth · {zone}",
        )
        return int(event.id), "ok"
    except Exception as exc:
        return 0, str(exc)


async def _finish_discord_event(bot, guild_id, event_id):
    if not bot or not guild_id or not event_id:
        return
    try:
        guild = bot.get_guild(int(guild_id)) or await bot.fetch_guild(int(guild_id))
        event = await guild.fetch_scheduled_event(int(event_id))
        status = str(getattr(getattr(event, "status", None), "name", "") or "").lower()
        if status == "active":
            await event.end()
        elif status in ("scheduled",):
            await event.cancel()
    except Exception as exc:
        print(f"[EVENTS] event close failed: {exc}")


async def start_night(kind, zone, user_id=0, guild=None, bot=None, interaction=None):
    async with _op_lock():
        return await _start_night(kind, zone, user_id, guild, bot, interaction)


async def _start_night(kind, zone, user_id=0, guild=None, bot=None, interaction=None):
    meta = NIGHT_META.get(kind)
    if not meta:
        return False, "Unknown event."
    zone = str(zone or panel_zone()).strip() or DEFAULT_ZONE
    if zone not in EVENT_ZONES:
        return False, "Pick a zone from the list."
    state = _load()
    row = state.setdefault(kind, _night_blank())
    if night_active(kind, state):
        left = _left_text(row.get("ends_at"))
        return False, f"{meta['label']} is already ON ({left} left). Turn it OFF first."
    now = _now()
    ends_at = now + NIGHT_SECONDS
    message, post_err = await _post_event(
        meta["title"].format(zone=zone),
        meta["body"].format(zone=zone, ends=ends_at),
        author=getattr(interaction, "user", None),
        guild=guild,
        bot=bot,
    )
    if message is None:
        return False, f"{post_err or 'Could not find #events.'} {meta['label']} was not started."
    channel = getattr(message, "channel", None)
    event_id, event_note = await _create_discord_event(guild, kind, zone, ends_at)
    state = _load()
    row = state.setdefault(kind, _night_blank())
    row.update({
        "enabled": True,
        "zone": zone,
        "started_at": now,
        "ends_at": ends_at,
        "started_by": int(user_id or 0),
        "channel_id": int(getattr(channel, "id", 0) or 0),
        "message_id": int(getattr(message, "id", 0) or 0),
        "discord_event_id": int(event_id or 0),
        "guild_id": int(getattr(guild, "id", 0) or 0),
        "last_announce_at": now,
    })
    _save(state)
    await _announce("Fallen Earth Elder — " + meta["announce_on"].format(zone=zone))
    extra = ""
    if not event_id:
        extra = (
            "\nPosted to #events, but the sidebar event was not created "
            f"({event_note}). Grant the bot **Manage Events** if you want that."
        )
    jump = getattr(message, "jump_url", "") or (channel.mention if channel else "")
    return True, f"{meta['label']} ON at **{zone}** for 2 hours.\n{jump}{extra}"


async def stop_night(kind, user_id=0, bot=None):
    async with _op_lock():
        return await _stop_night(kind, user_id, bot)


async def _stop_night(kind, user_id=0, bot=None):
    meta = NIGHT_META.get(kind)
    if not meta:
        return False, "Unknown event."
    state = _load()
    row = state.setdefault(kind, _night_blank())
    if not row.get("enabled"):
        return False, f"{meta['label']} is already OFF."
    bot = bot or _bot
    await _finish_discord_event(bot, row.get("guild_id"), row.get("discord_event_id"))
    state = _load()
    row = state.setdefault(kind, _night_blank())
    row["enabled"] = False
    row["started_by"] = int(user_id or row.get("started_by") or 0)
    _save(state)
    await _announce("Fallen Earth Elder — " + meta["announce_off"])
    await _post_event(
        f"{meta['label']} over",
        f"**{meta['label']} is over.**",
        bot=bot,
    )
    return True, f"{meta['label']} OFF."


async def start_hunt(zone, user_id=0, guild=None, bot=None, interaction=None):
    return await start_night("hunt", zone, user_id, guild, bot, interaction)


async def stop_hunt(user_id=0, bot=None):
    return await stop_night("hunt", user_id, bot)


def _status_line(kind, state, zone):
    meta = NIGHT_META[kind]
    row = state.get(kind) or {}
    if night_active(kind, state):
        return (
            f"**ON** — **{row.get('zone') or zone}** "
            f"until <t:{int(row.get('ends_at') or 0)}:R> "
            f"({_left_text(row.get('ends_at'))} left)"
        )
    return f"**OFF** — next zone **{zone}**"


def events_embed():
    state = _load()
    zone = panel_zone(state)
    growth = state["growth"]
    token_line = "**OFF**"
    try:
        import primeval_opening

        token_line = primeval_opening.panel_line()
    except Exception:
        pass
    if growth_active(state) or growth.get("restore_failed"):
        if growth.get("restore_failed"):
            g_line = "**ON — restore failed, retrying 1x**"
        else:
            g_line = (
                f"**ON** — {_rate_label(growth.get('rate'))} "
                f"until <t:{int(growth.get('ends_at') or 0)}:R> "
                f"({_left_text(growth.get('ends_at'))} left)"
            )
    else:
        g_line = "**OFF** — server growth is 1x"
    any_on = bool(
        token_line.startswith("**ON**")
        or growth_active(state)
        or growth.get("restore_failed")
        or hunt_active(state)
        or migration_active(state)
    )
    embed = discord.Embed(
        title="🌙 Fallen Earth | Events",
        description=(
            f"🎉 **Token event** — {token_line}\n"
            f"🌱 **Growth window** — {g_line}\n"
            f"🦴 **Hunt night** — {_status_line('hunt', state, zone)}\n"
            f"🌿 **Migration** — {_status_line('migration', state, zone)}\n\n"
            "Staff click a toggle. Each button checks your role.\n"
            "**Director** — token event · **Administrator+** — growth · "
            "**Moderator+** — hunt / migration\n"
            "Growth, hunt, and migration post to **#events** with **@here**. "
            "The monthly **free-player token giveaway** also posts here on the 1st. "
            "Growth presets restore **1x**. Hunt and migration last **2 hours** and add a sidebar event."
        ),
        color=discord.Color.green() if any_on else discord.Color.dark_grey(),
    )
    return embed


async def refresh_posted_panel(bot=None, message=None):
    bot = bot or _bot
    embed = events_embed()
    view = EventsPanelView()
    if message is not None:
        try:
            await message.edit(embed=embed, view=view)
            return True
        except Exception as exc:
            print(f"[EVENTS] panel edit failed: {exc}")
            return False
    state = _load()
    panel = state.get("panel") or {}
    channel_id = int(panel.get("channel_id") or 0)
    message_id = int(panel.get("message_id") or 0)
    if not bot or not channel_id or not message_id:
        return False
    try:
        channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        posted = await channel.fetch_message(message_id)
        await posted.edit(embed=embed, view=view)
        return True
    except Exception as exc:
        print(f"[EVENTS] posted panel refresh failed: {exc}")
        return False


def _remember_panel(message):
    if message is None:
        return
    channel = getattr(message, "channel", None)
    guild = getattr(message, "guild", None) or getattr(channel, "guild", None)
    state = _load()
    panel = state.setdefault("panel", {})
    panel["channel_id"] = int(getattr(channel, "id", 0) or 0)
    panel["message_id"] = int(getattr(message, "id", 0) or 0)
    panel["guild_id"] = int(getattr(guild, "id", 0) or 0)
    _save(state)


class EventZoneSelect(ui.Select):
    def __init__(self, current=DEFAULT_ZONE):
        current = current if current in EVENT_ZONES else DEFAULT_ZONE
        super().__init__(
            placeholder="Zone for hunt / migration…",
            min_values=1,
            max_values=1,
            custom_id="pi_ev_zone_sel",
            row=2,
            options=[
                discord.SelectOption(label=name, value=name, default=name == current)
                for name in EVENT_ZONES
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        from primeval_panels import can_staff, deny_staff

        if not can_staff(interaction.user, "hunt_night"):
            await deny_staff(interaction, "hunt_night")
            return
        state = _load()
        state.setdefault("panel", {})["zone"] = self.values[0]
        _save(state)
        await interaction.response.edit_message(embed=events_embed(), view=EventsPanelView())


class EventsPanelView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(EventZoneSelect(panel_zone()))

    async def _gate(self, interaction, action):
        from primeval_panels import can_staff, deny_staff

        if not can_staff(interaction.user, action):
            await deny_staff(interaction, action)
            return False
        return True

    async def _ack(self, interaction, ok, note):
        prefix = "✅ " if ok else "⚠️ "
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        await refresh_posted_panel(interaction.client, interaction.message)
        await interaction.followup.send(prefix + note, ephemeral=True)

    async def _toggle_token(self, interaction, on):
        if not await self._gate(interaction, "token_event"):
            return
        await interaction.response.defer(ephemeral=True)
        import primeval_opening

        ok, note = await primeval_opening.apply_toggle(on, interaction.user.id)
        await self._ack(interaction, ok, note)

    async def _toggle_growth(self, interaction, rate=None, seconds=None, off=False):
        if not await self._gate(interaction, "growth_window"):
            return
        await interaction.response.defer(ephemeral=True)
        if off:
            ok, note = await stop_growth(interaction.user.id, interaction)
        else:
            ok, note = await start_growth(rate, seconds, interaction.user.id, interaction)
        await self._ack(interaction, ok, note)

    async def _toggle_night(self, interaction, kind, off=False):
        if not await self._gate(interaction, "hunt_night"):
            return
        await interaction.response.defer(ephemeral=True)
        if off:
            ok, note = await stop_night(kind, interaction.user.id, interaction.client)
        else:
            ok, note = await start_night(
                kind,
                panel_zone(),
                interaction.user.id,
                interaction.guild,
                interaction.client,
                interaction,
            )
        await self._ack(interaction, ok, note)

    @ui.button(label="Token ON", emoji="🎉", style=discord.ButtonStyle.success, custom_id="pi_ev_token_on", row=0)
    async def token_on(self, interaction: discord.Interaction, button: ui.Button):
        await self._toggle_token(interaction, True)

    @ui.button(label="Token OFF", emoji="🛑", style=discord.ButtonStyle.danger, custom_id="pi_ev_token_off", row=0)
    async def token_off(self, interaction: discord.Interaction, button: ui.Button):
        await self._toggle_token(interaction, False)

    @ui.button(label="Refresh", emoji="🔄", style=discord.ButtonStyle.secondary, custom_id="pi_ev_refresh", row=0)
    async def refresh(self, interaction: discord.Interaction, button: ui.Button):
        from primeval_panels import can_staff, deny_staff

        if not (
            can_staff(interaction.user, "hunt_night")
            or can_staff(interaction.user, "growth_window")
            or can_staff(interaction.user, "token_event")
        ):
            await deny_staff(interaction, "hunt_night")
            return
        await interaction.response.edit_message(embed=events_embed(), view=EventsPanelView())

    @ui.button(label="2x / 2h", emoji="🌱", style=discord.ButtonStyle.success, custom_id="pi_ev_g22", row=1)
    async def grow_2x_2h(self, interaction: discord.Interaction, button: ui.Button):
        await self._toggle_growth(interaction, 2.0, 2 * 60 * 60)

    @ui.button(label="2x / 1h", emoji="🌱", style=discord.ButtonStyle.secondary, custom_id="pi_ev_g21", row=1)
    async def grow_2x_1h(self, interaction: discord.Interaction, button: ui.Button):
        await self._toggle_growth(interaction, 2.0, 1 * 60 * 60)

    @ui.button(label="3x / 2h", emoji="🌱", style=discord.ButtonStyle.primary, custom_id="pi_ev_g32", row=1)
    async def grow_3x_2h(self, interaction: discord.Interaction, button: ui.Button):
        await self._toggle_growth(interaction, 3.0, 2 * 60 * 60)

    @ui.button(label="Growth OFF", emoji="🛑", style=discord.ButtonStyle.danger, custom_id="pi_ev_g_off", row=1)
    async def grow_off(self, interaction: discord.Interaction, button: ui.Button):
        await self._toggle_growth(interaction, off=True)

    @ui.button(label="Hunt 2h", emoji="🦴", style=discord.ButtonStyle.success, custom_id="pi_ev_hunt_on", row=3)
    async def hunt_on(self, interaction: discord.Interaction, button: ui.Button):
        await self._toggle_night(interaction, "hunt")

    @ui.button(label="Hunt OFF", emoji="🛑", style=discord.ButtonStyle.danger, custom_id="pi_ev_hunt_off", row=3)
    async def hunt_off(self, interaction: discord.Interaction, button: ui.Button):
        await self._toggle_night(interaction, "hunt", off=True)

    @ui.button(label="Migration 2h", emoji="🌿", style=discord.ButtonStyle.success, custom_id="pi_ev_mig_on", row=4)
    async def mig_on(self, interaction: discord.Interaction, button: ui.Button):
        await self._toggle_night(interaction, "migration")

    @ui.button(label="Migration OFF", emoji="🛑", style=discord.ButtonStyle.danger, custom_id="pi_ev_mig_off", row=4)
    async def mig_off(self, interaction: discord.Interaction, button: ui.Button):
        await self._toggle_night(interaction, "migration", off=True)


async def post_events_panel(interaction: discord.Interaction):
    from primeval_panels import can_staff, deny_staff

    if not can_staff(interaction.user, "post_events_panel"):
        if not (
            can_staff(interaction.user, "hunt_night")
            or can_staff(interaction.user, "growth_window")
            or can_staff(interaction.user, "token_event")
        ):
            await deny_staff(interaction, "post_events_panel")
            return
    message = await interaction.response.send_message(embed=events_embed(), view=EventsPanelView())
    posted = None
    try:
        posted = await interaction.original_response()
    except Exception:
        posted = message
    _remember_panel(posted)


async def show_staff_controls(interaction: discord.Interaction):
    await post_events_panel(interaction)


def register_views(bot):
    bot.add_view(EventsPanelView())


def register_slash(bot):
    names = {cmd.name for cmd in bot.tree.get_commands()}
    if "events_panel" not in names:

        @bot.tree.command(name="events_panel", description="Post the Events panel (token, growth, hunt, migration)")
        async def events_panel(interaction: discord.Interaction):
            await post_events_panel(interaction)

    if "isle_events" not in names:

        @bot.tree.command(name="isle_events", description="Post the Events panel")
        async def isle_events(interaction: discord.Interaction):
            await post_events_panel(interaction)


async def on_isle_observed(running, uptime_s=0):
    uptime_s = int(uptime_s or 0)
    state = _load()
    last = int(state.get("last_uptime") or 0)
    rebooted = bool(running and last > 240 and uptime_s < 180)
    state["last_uptime"] = uptime_s if running else 0
    _save(state)
    if rebooted and growth_active(state):
        await _apply_growth(state, force=True)
        await _announce(
            "Fallen Earth Elder — Growth window still ON after the restart."
        )


async def _maybe_announce_active():
    state = _load()
    now = _now()
    if growth_active(state):
        row = state["growth"]
        last = float(row.get("last_announce_at") or 0)
        if now - last >= ANNOUNCE_SECONDS:
            row["last_announce_at"] = now
            _save(state)
            await _announce(
                f"Fallen Earth Elder — Growth window still ON: "
                f"{_rate_label(row.get('rate'))} until it auto-off "
                f"({_left_text(row.get('ends_at'))} left)."
            )
    for kind, meta in NIGHT_META.items():
        if not night_active(kind, state):
            continue
        row = state[kind]
        last = float(row.get("last_announce_at") or 0)
        if now - last < ANNOUNCE_SECONDS:
            continue
        row["last_announce_at"] = now
        _save(state)
        await _announce(
            "Fallen Earth Elder — "
            + meta["announce_still"].format(
                zone=row.get("zone") or DEFAULT_ZONE,
                left=_left_text(row.get("ends_at")),
            )
        )


async def tick(bot=None):
    bot = bot or _bot
    state = _load()
    changed = False
    if state["growth"].get("enabled") and not growth_active(state):
        async with _op_lock():
            await _restore_growth(state, reason="expired")
        changed = True
    elif growth_active(state):
        await _apply_growth(state)
    for kind in NIGHT_META:
        row = state.get(kind) or {}
        if row.get("enabled") and not night_active(kind, state):
            await stop_night(kind, bot=bot)
            changed = True
    await _maybe_announce_active()
    if changed:
        await refresh_posted_panel(bot)


def start(bot):
    global _task, _bot
    _bot = bot
    if _task is not None and _task.is_running():
        return _task

    @tasks.loop(seconds=TICK_SECONDS)
    async def events_tick():
        try:
            await tick(bot)
        except Exception as exc:
            print(f"[EVENTS] tick failed: {exc}")

    @events_tick.before_loop
    async def _wait_ready():
        await bot.wait_until_ready()
        await asyncio.sleep(10)

    _task = events_tick
    events_tick.start()
    print("[EVENTS] events panel started")
    return events_tick
