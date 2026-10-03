"""Disboard bump reminder. The bot cannot run /disboard bump.

Disboard and Discord both forbid auto-bump. This module watches for a
successful bump from the official Disboard bot, then pings staff when
the 2-hour cooldown is up so a person can bump again.
"""

from __future__ import annotations

import json
import os
import time

import discord
from discord import app_commands
from discord.ext import tasks

G = {}
STATE_PATH = "primeval_disboard.json"
DISBOARD_BOT_ID = 302050872383242240
COOLDOWN_SECONDS = 2 * 60 * 60
REMIND_EVERY = 15 * 60
TICK_SECONDS = 60

_task = None
_ATTACHED = set()


def bind(g):
    G.clear()
    G.update(g)


def _load():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {
        "enabled": False,
        "channel_id": 0,
        "last_bump_at": 0,
        "last_bumper_id": 0,
        "last_remind_at": 0,
    }


def _save(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)
        handle.write("\n")
    os.replace(tmp, STATE_PATH)


def _looks_like_success(message):
    text = str(getattr(message, "content", "") or "").lower()
    if "bump done" in text or "server bumped" in text:
        return True
    if "please wait" in text or "cooldown" in text:
        return False
    for embed in getattr(message, "embeds", None) or []:
        blob = " ".join(
            str(part or "")
            for part in (
                getattr(embed, "title", ""),
                getattr(embed, "description", ""),
                getattr(getattr(embed, "footer", None), "text", ""),
            )
        ).lower()
        if "please wait" in blob or "cooldown" in blob:
            return False
        if "bump" in blob and ("done" in blob or "success" in blob or "bumped" in blob):
            return True
    return False


def _seconds_left(state, now=None):
    now = now if now is not None else time.time()
    last = float(state.get("last_bump_at") or 0)
    if last <= 0:
        return 0
    return max(0, int(COOLDOWN_SECONDS - (now - last)))


async def _post_remind(bot, state):
    channel_id = int(state.get("channel_id") or 0)
    if not channel_id:
        return False
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception:
            return False
    who = int(state.get("last_bumper_id") or 0)
    ping = f"<@{who}> " if who else ""
    left = _seconds_left(state)
    if left > 0:
        return False
    await channel.send(
        ping
        + "Disboard is ready. Type **`/disboard bump`** in this channel.\n"
        "I cannot bump for you — Disboard only counts a real person running that command."
    )
    return True


def attach(bot):
    if id(bot) in _ATTACHED:
        return
    _ATTACHED.add(id(bot))

    @bot.listen("on_message")
    async def _watch_disboard(message):
        if message is None or getattr(message, "author", None) is None:
            return
        if int(getattr(message.author, "id", 0) or 0) != DISBOARD_BOT_ID:
            return
        if not _looks_like_success(message):
            return
        state = _load()
        if not state.get("enabled"):
            return
        want = int(state.get("channel_id") or 0)
        if want and int(getattr(message.channel, "id", 0) or 0) != want:
            return
        ref = getattr(message, "interaction", None) or getattr(message, "interaction_metadata", None)
        bumper = getattr(ref, "user", None) if ref is not None else None
        state["last_bump_at"] = time.time()
        state["last_bumper_id"] = int(getattr(bumper, "id", 0) or 0)
        state["last_remind_at"] = time.time()
        if not state.get("channel_id"):
            state["channel_id"] = int(getattr(message.channel, "id", 0) or 0)
        _save(state)
        print("[DISBOARD] bump recorded")


def register_slash(bot):
    @bot.tree.command(name="disboard", description="Staff: Disboard bump reminders")
    @app_commands.describe(action="Turn reminders on in this channel, off, or check status")
    @app_commands.choices(
        action=[
            app_commands.Choice(name="Remind here", value="here"),
            app_commands.Choice(name="Turn off", value="off"),
            app_commands.Choice(name="Status", value="status"),
        ]
    )
    async def disboard_cmd(interaction: discord.Interaction, action: app_commands.Choice[str]):
        from primeval_panels import can_staff, deny_staff

        if not can_staff(interaction.user, "disboard"):
            await deny_staff(interaction, "disboard")
            return
        state = _load()
        verb = action.value
        if verb == "here":
            state["enabled"] = True
            state["channel_id"] = int(interaction.channel_id or 0)
            _save(state)
            await interaction.response.send_message(
                "Disboard reminders are on in this channel. "
                "Run `/disboard bump` once so I can start the 2-hour clock.",
                ephemeral=True,
            )
            return
        if verb == "off":
            state["enabled"] = False
            _save(state)
            await interaction.response.send_message("Disboard reminders are off.", ephemeral=True)
            return
        left = _seconds_left(state)
        on = "on" if state.get("enabled") else "off"
        ch = int(state.get("channel_id") or 0)
        where = f"<#{ch}>" if ch else "no channel"
        if left:
            when = f"ready <t:{int(time.time() + left)}:R>"
        elif float(state.get("last_bump_at") or 0) <= 0:
            when = "no bump recorded yet — run `/disboard bump`"
        else:
            when = "ready now"
        await interaction.response.send_message(
            f"Reminders **{on}** in {where}. {when}.",
            ephemeral=True,
        )


def start(bot):
    global _task
    attach(bot)
    if _task is not None and _task.is_running():
        return _task

    @tasks.loop(seconds=TICK_SECONDS)
    async def disboard_tick():
        state = _load()
        if not state.get("enabled") or not int(state.get("channel_id") or 0):
            return
        now = time.time()
        if _seconds_left(state, now) > 0:
            return
        last_r = float(state.get("last_remind_at") or 0)
        if last_r and (now - last_r) < REMIND_EVERY:
            return
        try:
            sent = await _post_remind(bot, state)
        except Exception as exc:
            print(f"[DISBOARD] remind failed: {exc}")
            return
        if sent:
            state["last_remind_at"] = now
            _save(state)

    @disboard_tick.before_loop
    async def _wait():
        await bot.wait_until_ready()

    _task = disboard_tick
    _task.start()
    print("[DISBOARD] bump reminders started")
    return _task
