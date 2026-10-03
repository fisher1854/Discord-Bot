"""Live species tally + store-lock Discord posts.

Reads Isle census.json (UE4SS) when present; otherwise RCON playerlist
and getplayerdata. Old loop used tokens.json online flags and a bad
locks channel ID, so in-game dinos never appeared.
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

from primeval_species import aliases as species_aliases, playable as playable_species

G = {}
POPULATION_CHANNEL_ID = 1540827821067477064
LOCKS_CHANNEL_ID = 1541014100971360326
STATE_PATH = "primeval_tally.json"
TICK_SECONDS = 120
RENAME_EVERY = 15 * 60
CENSUS_MAX_AGE = 90
CENSUS_PATH = "/TheIsle/Binaries/Win64/ue4ss/Mods/PrimevalRedeem/Saved/census.json"
STEAM_RE = re.compile(r"7656\d{13}")
ALIASES = species_aliases()
PLAYABLE_SPECIES = playable_species()
_task = None
_STATE_LOCK = asyncio.Lock()


def bind(g):
    G.clear()
    G.update(g)
    try:
        import primeval_isle

        primeval_isle.bind(g)
    except Exception:
        pass
    try:
        import primeval_qol

        primeval_qol.bind(g)
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


def _norm_species_key(raw):
    key = re.sub(r"[^a-z]", "", str(raw or "").lower())
    if key.startswith("bp"):
        key = key[2:]
    for suffix in ("character", "playerpawn", "pawn"):
        if key.endswith(suffix) and len(key) > len(suffix) + 4:
            key = key[: -len(suffix)]
    return key


def _known_species(key):
    key = _norm_species_key(key)
    if not key:
        return ""
    if key in ALIASES:
        return ALIASES[key]
    for alias, name in ALIASES.items():
        if len(alias) >= 6 and (alias in key or key in alias):
            return name
    for name in PLAYABLE_SPECIES:
        if _norm_species_key(name) == key:
            return name
    # Unrecognized text (admin pawns, malformed RCON output, etc.) is never a
    # real species — return "" (unknown) instead of fabricating a display
    # name. Matching against LIVE_HEADCOUNT's own keys was also removed: it
    # let one bad entry "confirm" itself and any similar-looking future junk,
    # which is how the tally kept accumulating corrupted entries.
    return ""


def canonical_species(raw):
    text = str(raw or "").strip()
    if not text:
        return ""
    class_match = re.search(r"\bClass\s*:\s*([A-Za-z]+)", text, re.I)
    if class_match:
        found = _known_species(class_match.group(1))
        if found:
            return found
    bp = re.search(r"BP_([A-Za-z]+)", text)
    if bp:
        found = _known_species(bp.group(1))
        if found:
            return found
    if len(text) <= 40 and "\n" not in text and "PlayerData" not in text:
        return _known_species(text)
    return ""


async def _read_census_file():
    import primeval_isle

    data = await primeval_isle.read_census()
    if not data:
        return None
    captured = int(data.get("capturedAt") or 0)
    if captured < int(time.time()) - CENSUS_MAX_AGE:
        return None
    return data


def _parse_playerlist(blob):
    low = str(blob or "").lower()
    if "auth failed" in low or "connection error" in low:
        return None
    steams, seen = [], set()
    for match in STEAM_RE.finditer(str(blob or "")):
        steam = match.group(0)
        if steam not in seen:
            seen.add(steam)
            steams.append(steam)
    return steams


def _parse_species_blob(blob):
    text = str(blob or "")
    low = text.lower()
    if "auth failed" in low or "connection error" in low:
        return ""
    return canonical_species(text)


def _empty_counts():
    # Only ever seed known playable species. Previously this also re-added
    # whatever keys already existed in LIVE_HEADCOUNT, which is how a single
    # corrupted entry kept reappearing forever.
    return {name: 0 for name in PLAYABLE_SPECIES}


def _apply_counts(counts):
    # Rebuild LIVE_HEADCOUNT from scratch each tick instead of mutating the
    # old dict in place. The old code kept every key it had ever seen (only
    # zeroing stale ones, never removing them), so any garbage species name
    # that slipped in even once would persist and clutter the tally forever.
    live = {name: int(counts.get(name, 0) or 0) for name in PLAYABLE_SPECIES}
    G["LIVE_HEADCOUNT"] = live
    return live


async def _census_from_rcon():
    send = _fn("send_game_command")
    if not send:
        return None
    try:
        blob = await send("playerlist")
    except Exception:
        return None
    steams = _parse_playerlist(blob)
    if steams is None:
        return None
    players = []
    for steam in steams[:20]:
        species = ""
        try:
            species = _parse_species_blob(await send("getplayerdata " + steam))
        except Exception:
            species = ""
        players.append({"steam": steam, "spawned": True, "species": species})
    return {"capturedAt": int(time.time()), "online": len(steams), "players": players}


def _counts_from_census(census):
    counts = _empty_counts()
    spawned = unknown = 0
    for row in census.get("players") or []:
        if not row.get("spawned", True):
            continue
        spawned += 1
        name = canonical_species(row.get("species") or "")
        if not name or name not in counts:
            unknown += 1
            continue
        counts[name] = int(counts.get(name, 0) or 0) + 1
    online = int(census.get("online") or len(census.get("players") or []) or 0)
    return counts, online, spawned, unknown


def _index_players(census):
    by_steam = {}
    steams = []
    seen = set()
    for row in census.get("players") or []:
        steam = str((row or {}).get("steam") or "").strip()
        if not STEAM_RE.fullmatch(steam) or steam in seen:
            continue
        seen.add(steam)
        steams.append(steam)
        if row.get("spawned", True):
            by_steam[steam] = canonical_species(row.get("species") or "")
        else:
            by_steam[steam] = ""
    G["LIVE_BY_STEAM"] = by_steam
    return steams


async def collect_census():
    source = "game census"
    census = await _read_census_file()
    if census is None:
        census = await _census_from_rcon()
        source = "RCON playerlist"
    if census is None:
        G["LIVE_BY_STEAM"] = {}
        return _empty_counts(), 0, 0, 0, "unavailable", []
    counts, online, spawned, unknown = _counts_from_census(census)
    _apply_counts(counts)
    steams = _index_players(census)
    return counts, online, spawned, unknown, source, steams


async def species_for_steam(steam):
    steam = str(steam or "").strip()
    if not STEAM_RE.fullmatch(steam):
        return ""
    census = await _read_census_file()
    if census:
        _index_players(census)
    return str((G.get("LIVE_BY_STEAM") or {}).get(steam) or "")


def _stamp(now):
    return f"Last updated <t:{now}:f> (<t:{now}:R>)"


def build_tally_embed(counts, online, spawned, unknown, source, now):
    total = sum(int(v or 0) for v in counts.values())
    lines = []
    for name in sorted(counts, key=lambda n: (-int(counts[n] or 0), str(n))):
        n = int(counts.get(name, 0) or 0)
        mark = "*" if n else " "
        lines.append(f"`{mark}` **{name}** `{n}`")
    body = "\n".join(lines) or "No tracked species yet."
    extra = f"\nUnparsed species: **{unknown}**" if unknown else ""
    embed = discord.Embed(
        title="Fallen Earth - Live tally",
        description=(
            f"{_stamp(now)}\nPlayers connected: **{online}**\n"
            f"Spawned dinos: **{spawned}**\nTotal counted: **{total}**{extra}"
        ),
        color=discord.Color.dark_green(),
        timestamp=datetime.fromtimestamp(now, tz=timezone.utc),
    )
    embed.add_field(name="Species", value=body[:1020], inline=False)
    embed.set_footer(text=f"Source: {source} - refreshes every 2 minutes")
    return embed


def build_locks_embed(counts, source, now):
    caps = G.get("SPECIES_CAPS") or {}
    lines = []
    for dino, cap in caps.items():
        cap = int(cap or 0)
        n = int(counts.get(dino, 0) or 0)
        tag = "LOCKED" if n >= cap else "OPEN"
        lines.append(f"**{tag}** {dino} `{n}/{cap}`")
    embed = discord.Embed(
        title="Fallen Earth - Point store locks",
        description=(
            f"{_stamp(now)}\nCapped species lock at capacity and reopen below cap.\n"
            "Join the **waitlist** below — first in line gets a ping when a slot opens (10 minutes)."
        ),
        color=discord.Color.orange(),
        timestamp=datetime.fromtimestamp(now, tz=timezone.utc),
    )
    embed.add_field(name="Store status", value=("\n".join(lines) or "No capped species configured.")[:1020], inline=False)
    embed.set_footer(text=f"Source: {source} - refreshes every 2 minutes")
    return embed


async def _upsert(bot, channel_id, embed, state, key):
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception as exc:
            print(f"[TALLY] channel {channel_id} missing: {exc}")
            return state
    msg_id = int(state.get(key) or 0)
    message = None
    if msg_id:
        try:
            message = await channel.fetch_message(msg_id)
        except Exception:
            message = None
    view = None
    if channel_id == LOCKS_CHANNEL_ID:
        try:
            import primeval_qol

            view = primeval_qol.locks_wait_view()
        except Exception:
            view = None
    if message is None:
        message = await channel.send(embed=embed, view=view)
        state[key] = message.id
    else:
        await message.edit(embed=embed, view=view)
    return state


def _channel_slug(name):
    return re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")[:100]


async def _maybe_rename_population(channel, online, now, state):
    last_rename = float(state.get("last_rename_at") or 0)
    if now - last_rename < RENAME_EVERY:
        return
    want = f"Players Online: {int(online)}"[:100]
    current = _channel_slug(getattr(channel, "name", ""))
    if current == _channel_slug(want):
        state["last_rename_at"] = now
        return
    state["last_rename_at"] = now
    try:
        await channel.edit(name=want, reason="live player count")
    except discord.HTTPException as exc:
        retry = float(getattr(exc, "retry_after", 0) or 0)
        wait = max(RENAME_EVERY, retry + 30)
        state["last_rename_at"] = now - RENAME_EVERY + wait
        print(f"[TALLY] rename skipped ({exc.status}): backing off {wait:.0f}s")


async def tick(bot):
    async with _STATE_LOCK:
        counts, online, spawned, unknown, source, steams = await collect_census()
        now = int(time.time())
        state = _load_state()
    try:
        import primeval_opening

        await primeval_opening.on_online(bot, steams, now)
    except Exception as exc:
        print(f"[OPENING] tick failed: {exc}")
    try:
        state = await _upsert(bot, POPULATION_CHANNEL_ID, build_tally_embed(counts, online, spawned, unknown, source, now), state, "tally_message_id")
    except Exception as exc:
        print(f"[TALLY] population post failed: {exc}")
    try:
        state = await _upsert(bot, LOCKS_CHANNEL_ID, build_locks_embed(counts, source, now), state, "locks_message_id")
    except Exception as exc:
        print(f"[TALLY] locks post failed: {exc}")
    try:
        channel = bot.get_channel(POPULATION_CHANNEL_ID) or await bot.fetch_channel(POPULATION_CHANNEL_ID)
        await _maybe_rename_population(channel, online, now, state)
    except Exception as exc:
        print(f"[TALLY] rename failed: {exc}")
    try:
        import primeval_qol

        primeval_qol.note_online(steams)
        state = await primeval_qol.on_census(bot, counts, state) or state
    except Exception as exc:
        print(f"[QOL] census hook failed: {exc}")
    state["last_tick"] = now
    state["last_source"] = source
    async with _STATE_LOCK:
        _save_state(state)
    print(f"[TALLY] {source} online={online} spawned={spawned} counts={counts}")


def start(bot):
    global _task
    if _task is not None and _task.is_running():
        return _task

    @tasks.loop(seconds=TICK_SECONDS)
    async def tally_tick():
        try:
            await tick(bot)
        except Exception as exc:
            print(f"[TALLY] tick failed: {exc}")

    @tally_tick.before_loop
    async def _wait_ready():
        await bot.wait_until_ready()
        await asyncio.sleep(4)

    _task = tally_tick
    tally_tick.start()
    print("[TALLY] live census posts started")
    return tally_tick


def register_slash(bot):
    if "tally_refresh" in {cmd.name for cmd in bot.tree.get_commands()}:
        return

    @bot.tree.command(name="tally_refresh", description="Refresh live tally and store-lock posts now")
    async def tally_refresh(interaction: discord.Interaction):
        rank = 0
        try:
            import primeval_panels
            rank = primeval_panels.staff_rank(interaction.user)
        except Exception:
            rank = 0
        perms = getattr(interaction.user, "guild_permissions", None)
        if rank < 2 and not (perms and perms.manage_guild):
            await interaction.response.send_message("Staff can refresh the live tally.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await tick(interaction.client)
        await interaction.followup.send("Live tally and lock posts refreshed.", ephemeral=True)
