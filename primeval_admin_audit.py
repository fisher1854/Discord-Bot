"""Watch in-game / RCON admin commands and post a staff audit trail.

Posts to AUDIT_CHANNEL_ID. Who used the command and who it hit, with
Discord + Steam IDs when we can resolve them.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone

import discord
from discord.ext import tasks

G = {}

AUDIT_CHANNEL_ID = 1542333399052845257
STATE_PATH = "primeval_admin_audit.json"
ISLE_LOG = "/TheIsle/Saved/Logs/TheIsle.log"
AUDIT_NDJSON = "/TheIsle/Binaries/Win64/ue4ss/Mods/PrimevalRedeem/Saved/admin_audit.ndjson"
TICK_SECONDS = 8
STEAM_RE = re.compile(r"7656\d{13}")
LOG_CMD_RE = re.compile(
    r"LogTheIsleCommandData:.*?((?:RCON|Admin)\s+)?Command Used \[([^\]]+)\]\s*:\s*(.*)$",
    re.I,
)
NOISE_COMMANDS = {
    "get player list",
    "getplayerlist",
    "playerlist",
    "get player data",
    "getplayerdata",
    "get queue status",
    "getqueuestatus",
    "server details",
    "serverdetails",
    "get playables",
    "getplayables",
}
BOT_ANNOUNCE_HINTS = (
    "server restart in",
    "fallen earth restart in",
    "last call",
    "primeval island is restarting",
    "fallen earth is restarting",
    "welcome to primeval",
    "token event:",
    "opening event:",
    "growth window",
    "hunt night",
    "migration",
)
SOURCE_LABELS = {
    "chat": "In-game chat",
    "hook": "In-game admin panel",
    "rcon": "RCON",
    "discord": "Discord staff panel",
    "redeem": "Discord redeem (auto grow)",
}
HOT_COMMANDS = {
    "slay", "kill", "kick", "ban", "banplayer", "unban", "heal", "grow",
    "goto", "bring", "teleport", "tp", "revive", "god", "fly", "ghost",
    "setgrowth", "setgrowthmultiplier", "addadmin", "setgroup", "wipe", "wipecorpses",
}

_task = None
_STATE_LOCK = asyncio.Lock()
_recent = []
_recent_redeems = []
_held_log = []


def bind(g):
    G.clear()
    G.update(g)
    try:
        import primeval_isle

        primeval_isle.bind(g)
    except Exception:
        pass


def _fn(name, default=None):
    return G.get(name, default)


def _load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)
        handle.write("\n")
    os.replace(tmp, STATE_PATH)


def _norm_cmd(name):
    return re.sub(r"[^a-z0-9]+", "", str(name or "").lower())


def _is_noise(command, extra=""):
    key = str(command or "").strip().lower()
    if key in NOISE_COMMANDS or _norm_cmd(key) in {_norm_cmd(n) for n in NOISE_COMMANDS}:
        return True
    blob = f"{key} {extra or ''}".lower()
    if "announce" in key and any(h in blob for h in BOT_ANNOUNCE_HINTS):
        return True
    return False


def _steams_in(text):
    found, seen = [], set()
    for match in STEAM_RE.finditer(str(text or "")):
        steam = match.group(0)
        if steam not in seen:
            seen.add(steam)
            found.append(steam)
    return found


def _discord_ids_for_steam(steam):
    steam = str(steam or "").strip()
    if not steam:
        return []
    links = G.get("STEAM_LINKS") or {}
    found, seen = [], set()
    if isinstance(links, dict):
        for key, value in links.items():
            sid = value.get("steam") if isinstance(value, dict) else value
            if str(sid).strip() != steam:
                continue
            uid = str(key).strip()
            if not uid.isdigit() or uid in seen:
                continue
            seen.add(uid)
            found.append(uid)
    return found


def _steam_for_discord(user_id):
    getter = _fn("get_linked_steam_id")
    if getter and user_id:
        try:
            steam = getter(int(user_id))
            if steam:
                return str(steam).strip()
        except Exception:
            pass
    links = G.get("STEAM_LINKS") or {}
    if not isinstance(links, dict):
        return ""
    steam = links.get(int(user_id)) if str(user_id).isdigit() else None
    if steam is None:
        steam = links.get(str(user_id))
    if isinstance(steam, dict):
        steam = steam.get("steam")
    return str(steam or "").strip()


def _who_block(steam="", discord_ids=None, fallback="unknown"):
    steam = str(steam or "").strip()
    ids = []
    seen = set()
    for raw in discord_ids or []:
        uid = str(raw).strip()
        if uid.isdigit() and uid not in seen:
            seen.add(uid)
            ids.append(uid)
    if steam:
        for uid in _discord_ids_for_steam(steam):
            if uid not in seen:
                seen.add(uid)
                ids.append(uid)
    if ids:
        mentions = " ".join(f"<@{uid}>" for uid in ids)
        disc = "\n".join(f"`{uid}`" for uid in ids)
    else:
        mentions = fallback
        disc = "`not linked`"
    steam_txt = f"`{steam}`" if steam else "`unknown`"
    return f"{mentions}\nDiscord {disc}\nSteam {steam_txt}"


def _finger(command, actor, target, extra=""):
    return "|".join(
        [
            _norm_cmd(command),
            str(actor or ""),
            str(target or ""),
            str(extra or "")[:48],
        ]
    )


def _seen_recently(finger, now=None):
    now = int(now or time.time())
    global _recent
    _recent = [row for row in _recent if now - row[0] < 25]
    for ts, key in _recent:
        if key == finger:
            return True
    _recent.append((now, finger))
    if len(_recent) > 250:
        _recent = _recent[-200:]
    return False


def _note_redeem(event, now=None):
    if str(event.get("source") or "") != "redeem":
        return
    now = int(now or event.get("at") or time.time())
    steam = str(event.get("target_steam") or "")
    if not steam:
        return
    _recent_redeems.append((now, steam))
    while len(_recent_redeems) > 80:
        _recent_redeems.pop(0)


def _is_redeem_echo(event, now=None):
    """Isle log still attributes ServerGrow to the player. Drop those echoes."""
    if str(event.get("source") or "") == "redeem":
        return False
    key = _norm_cmd(event.get("command"))
    if not any(part in key for part in ("grow", "hunger", "nutrient")):
        return False
    now = int(now or event.get("at") or time.time())
    steams = {
        str(event.get("actor_steam") or ""),
        str(event.get("target_steam") or ""),
    }
    steams.update(_steams_in(event.get("extra") or ""))
    _recent_redeems[:] = [row for row in _recent_redeems if now - row[0] < 90]
    return any(steam and steam in steams for _, steam in _recent_redeems)


def _embed_color(command):
    key = _norm_cmd(command)
    if any(hot in key for hot in ("slay", "kill", "kick", "ban", "wipe")):
        return discord.Color.red()
    if any(hot in key for hot in HOT_COMMANDS):
        return discord.Color.gold()
    return discord.Color.blurple()


def build_embed(event):
    command = str(event.get("command") or "unknown")
    source = SOURCE_LABELS.get(event.get("source"), str(event.get("source") or "unknown"))
    extra = str(event.get("extra") or "").strip()
    actor_steam = str(event.get("actor_steam") or "")
    target_steam = str(event.get("target_steam") or "")
    actor_ids = event.get("actor_discord_ids") or []
    target_ids = event.get("target_discord_ids") or []
    actor_fallback = str(event.get("actor_fallback") or "unknown")
    target_fallback = str(event.get("target_fallback") or "none / server-wide")
    when = int(event.get("at") or time.time())
    is_redeem = str(event.get("source") or "") == "redeem"
    extra_bits = dict(
        part.split("=", 1) for part in extra.split() if "=" in part
    )
    species = extra_bits.get("species") or ""
    title = "🎟️ Redeem auto-grow"
    if is_redeem and species:
        title = f"🎟️ Redeem auto-grow · {species}"
    embed = discord.Embed(
        title=title if is_redeem else "🛡️ Admin command",
        description=f"**{command}**\nSource: {source}",
        color=discord.Color.greyple() if is_redeem else _embed_color(command),
        timestamp=datetime.fromtimestamp(when, timezone.utc),
    )
    who = (
        "**PrimevalRedeem**\nDiscord shop auto-grow\nnot a player admin command"
        if is_redeem
        else _who_block(actor_steam, actor_ids, actor_fallback)
    )
    embed.add_field(
        name="Who used it",
        value=who[:1024],
        inline=True,
    )
    embed.add_field(
        name="Used on",
        value=_who_block(target_steam, target_ids, target_fallback)[:1024],
        inline=True,
    )
    if extra:
        embed.add_field(name="Details", value=f"`{extra[:1000]}`", inline=False)
    embed.set_footer(
        text="Fallen Earth redeem audit" if is_redeem else "Fallen Earth admin audit"
    )
    return embed


async def _channel(bot):
    channel = bot.get_channel(AUDIT_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(AUDIT_CHANNEL_ID)
        except Exception as exc:
            print(f"[AUDIT] channel missing: {exc}")
            return None
    return channel


async def post_event(bot, event):
    finger = _finger(
        event.get("command"),
        event.get("actor_steam") or ",".join(event.get("actor_discord_ids") or []),
        event.get("target_steam") or event.get("target_fallback"),
        event.get("extra"),
    )
    if _seen_recently(finger, event.get("at")):
        return False
    if _is_redeem_echo(event, event.get("at")):
        return False
    _note_redeem(event, event.get("at"))
    channel = await _channel(bot)
    if channel is None:
        return False
    try:
        await channel.send(embed=build_embed(event))
        return True
    except Exception as exc:
        print(f"[AUDIT] post failed: {exc}")
        return False


def _event_from_parts(command, source, actor_steam="", target_steam="", extra="", actor_user=None, at=None):
    actor_ids = []
    actor_fallback = "unknown"
    if actor_user is not None:
        actor_ids = [str(getattr(actor_user, "id", "") or "")]
        actor_fallback = getattr(actor_user, "mention", None) or str(actor_user)
        if not actor_steam:
            actor_steam = _steam_for_discord(actor_ids[0])
    elif actor_steam:
        actor_fallback = "in-game admin (unlinked)" if not _discord_ids_for_steam(actor_steam) else "unknown"
    else:
        actor_fallback = "RCON / unknown user"
    target_ids = _discord_ids_for_steam(target_steam) if target_steam else []
    target_fallback = "none / server-wide"
    if target_steam and not target_ids:
        target_fallback = "in-game player (unlinked)"
    elif extra and not target_steam:
        target_fallback = extra[:80] or "none / server-wide"
    return {
        "at": int(at or time.time()),
        "command": command,
        "source": source,
        "actor_steam": actor_steam,
        "target_steam": target_steam,
        "extra": extra,
        "actor_discord_ids": [uid for uid in actor_ids if uid.isdigit()],
        "target_discord_ids": target_ids,
        "actor_fallback": actor_fallback,
        "target_fallback": target_fallback,
    }


async def note_discord(bot, user, command, target="", extra="", result=""):
    command = str(command or "").strip() or "command"
    target = str(target or "").strip()
    extra = str(extra or "").strip()
    if result:
        extra = (extra + (" | " if extra else "") + f"result: {str(result)[:160]}").strip()
    if _is_noise(command, extra):
        return
    steams = _steams_in(target) or _steams_in(extra)
    target_steam = steams[0] if steams else (target if STEAM_RE.fullmatch(target) else "")
    event = _event_from_parts(
        command,
        "discord",
        target_steam=target_steam,
        extra=extra or target,
        actor_user=user,
    )
    if not event["target_steam"] and target and not STEAM_RE.fullmatch(target):
        event["target_fallback"] = target
    await post_event(bot, event)


async def _read_remote(path):
    import primeval_isle

    status, text = await primeval_isle.read_file(path)
    if status != 200:
        return status, ""
    return status, str(text or "")


def _parse_log_line(line):
    match = LOG_CMD_RE.search(line or "")
    if not match:
        return None
    kind = (match.group(1) or "").strip().lower()
    command = (match.group(2) or "").strip()
    extra = (match.group(3) or "").strip()
    if _is_noise(command, extra):
        return None
    source = "rcon" if kind.startswith("rcon") else "hook"
    steams = _steams_in(line)
    actor_steam = ""
    target_steam = ""
    if source == "hook" and len(steams) >= 2:
        actor_steam, target_steam = steams[0], steams[1]
    elif len(steams) == 1:
        if source == "rcon":
            target_steam = steams[0]
        else:
            actor_steam = steams[0]
    elif len(steams) >= 2:
        target_steam = steams[0]
    return _event_from_parts(command, source, actor_steam, target_steam, extra)


def _parse_ndjson_row(row):
    command = str(row.get("command") or "").strip()
    extra = str(row.get("extra") or "").strip()
    if not command or _is_noise(command, extra):
        return None
    actor = str(row.get("actor") or "").strip()
    target = str(row.get("target") or "").strip()
    actor_steam = actor if STEAM_RE.fullmatch(actor) else ""
    target_steam = target if STEAM_RE.fullmatch(target) else (_steams_in(target)[0] if _steams_in(target) else "")
    event = _event_from_parts(
        command,
        str(row.get("source") or "chat"),
        actor_steam,
        target_steam,
        extra,
        at=row.get("at"),
    )
    if not event["target_steam"] and target:
        event["target_fallback"] = target
    if not event["actor_steam"] and actor:
        event["actor_fallback"] = actor
    return event


async def _poll_chunk(bot, path, state, key, parser, skip_existing=False):
    status, text = await _read_remote(path)
    if status != 200 or not text:
        return 0
    if key not in state:
        if skip_existing:
            state[key] = len(text)
            return 0
        state[key] = 0
    seen = int(state.get(key) or 0)
    if len(text) < seen:
        seen = 0
    chunk = text[seen:]
    state[key] = len(text)
    posted = 0
    for line in chunk.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = parser(line)
        except Exception:
            event = None
        if not event:
            continue
        if await post_event(bot, event):
            posted += 1
    return posted


def _parse_ndjson_line(line):
    if not line.startswith("{"):
        return None
    try:
        row = json.loads(line)
    except Exception:
        return None
    if not isinstance(row, dict):
        return None
    return _parse_ndjson_row(row)


def _parse_log_line_held(line):
    event = _parse_log_line(line)
    if event is None:
        return None
    if _is_redeem_echo(event):
        return None
    key = _norm_cmd(event.get("command"))
    if any(part in key for part in ("grow", "hunger", "nutrient")):
        _held_log.append((int(event.get("at") or time.time()), event))
        return None
    return event


async def _flush_held_log(bot):
    now = int(time.time())
    kept = []
    for ts, event in _held_log:
        if _is_redeem_echo(event, now):
            continue
        if now - ts >= 12:
            await post_event(bot, event)
        else:
            kept.append((ts, event))
    _held_log[:] = kept


async def tick(bot):
    async with _STATE_LOCK:
        state = _load_state()
        try:
            await _poll_chunk(bot, AUDIT_NDJSON, state, "ndjson_bytes", _parse_ndjson_line)
        except Exception as exc:
            print(f"[AUDIT] ndjson poll failed: {exc}")
        try:
            await _poll_chunk(
                bot, ISLE_LOG, state, "isle_log_bytes", _parse_log_line_held, skip_existing=True
            )
        except Exception as exc:
            print(f"[AUDIT] isle log poll failed: {exc}")
        try:
            await _flush_held_log(bot)
        except Exception as exc:
            print(f"[AUDIT] held log flush failed: {exc}")
        _save_state(state)


def start(bot):
    global _task
    if _task is not None and _task.is_running():
        return _task

    @tasks.loop(seconds=TICK_SECONDS)
    async def audit_tick():
        try:
            await tick(bot)
        except Exception as exc:
            print(f"[AUDIT] tick failed: {exc}")

    @audit_tick.before_loop
    async def _wait_ready():
        await bot.wait_until_ready()
        await asyncio.sleep(6)

    _task = audit_tick
    audit_tick.start()
    print("[AUDIT] in-game admin command watch started")
    return audit_tick
