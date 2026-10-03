"""On-demand pack voice channels named for the dino you are spawned as.

Join the hub VC (or Pack VC on the member panel). The bot creates or reuses
a channel for that species. Linked members can talk there like any normal
voice channel. Empty pack channels are deleted.
"""

from __future__ import annotations

import asyncio
import json
import os
import time

import discord

from primeval_gate import LINKED_ROLE_NAME, MEMBER_ROLE_NAME, is_steam_linked
from primeval_species import emoji as species_emoji

G = {}
STATE_PATH = "primeval_voice.json"
CATEGORY_NAME = "Pack Voice"
HUB_NAME = "Create pack VC"
_LOCKS = {}
_COOLDOWN = {}
_task = None
_ATTACHED = set()


def bind(g):
    G.clear()
    G.update(g)


def _fn(name, default=None):
    return G.get(name, default)


def _lock_for(guild_id):
    lock = _LOCKS.get(guild_id)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[guild_id] = lock
    return lock


def _load():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            data.setdefault("category_id", 0)
            data.setdefault("hub_id", 0)
            data.setdefault("channels", {})
            return data
    except Exception:
        pass
    return {"category_id": 0, "hub_id": 0, "channels": {}}


def _save(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)
        handle.write("\n")
    os.replace(tmp, STATE_PATH)


def _role_named(guild, name):
    if guild is None:
        return None
    want = str(name).strip().lower()
    for role in guild.roles:
        if str(role.name).strip().lower() == want:
            return role
    return None


def _steam_for_discord(user_id):
    getter = _fn("get_linked_steam_id")
    if getter:
        try:
            steam = getter(int(user_id))
            if steam:
                return str(steam).strip()
        except Exception:
            pass
    links = G.get("STEAM_LINKS") or {}
    steam = links.get(int(user_id)) if str(user_id).isdigit() else None
    if steam is None:
        steam = links.get(str(user_id))
    return str(steam).strip() if steam else ""


def _channel_name(species):
    species = str(species or "").strip() or "Dino"
    mark = (species_emoji() or {}).get(species) or "🦕"
    return f"{mark} {species}"[:100]


def _member_voice_overwrites(guild):
    everyone = guild.default_role
    member = _role_named(guild, MEMBER_ROLE_NAME)
    linked = _role_named(guild, LINKED_ROLE_NAME)
    overwrites = {
        everyone: discord.PermissionOverwrite(
            view_channel=True,
            connect=False,
            speak=False,
        ),
    }
    if member:
        overwrites[member] = discord.PermissionOverwrite(
            view_channel=True,
            connect=False,
            speak=False,
        )
    if linked:
        overwrites[linked] = discord.PermissionOverwrite(
            view_channel=True,
            connect=True,
            speak=True,
            stream=True,
            use_voice_activation=True,
            mute_members=False,
            deafen_members=False,
            move_members=False,
            manage_channels=False,
            manage_permissions=False,
            priority_speaker=False,
        )
    me = guild.me
    if me:
        overwrites[me] = discord.PermissionOverwrite(
            view_channel=True,
            connect=True,
            speak=True,
            move_members=True,
            manage_channels=True,
            manage_permissions=True,
        )
    return overwrites


async def _get_channel(guild, channel_id):
    if not channel_id:
        return None
    channel = guild.get_channel(int(channel_id))
    if channel is not None:
        return channel
    try:
        fetched = await guild.fetch_channel(int(channel_id))
    except Exception:
        return None
    return fetched


async def _sync_overwrites(channel, overwrites):
    # Pack channels keep whatever overwrites they were created with. If the
    # Member/Linked roles did not exist yet at creation time (e.g. a startup
    # race with primeval_gate, or the roles got recreated/renamed later), the
    # channel is permanently stuck with no one able to connect but the bot.
    # Re-apply current overwrites every time we touch the layout so that
    # fixes to roles heal existing channels instead of requiring a manual
    # Discord-side permission edit or a channel delete/recreate.
    if channel is None:
        return
    try:
        if channel.overwrites != overwrites:
            await channel.edit(overwrites=overwrites, reason="Primeval pack voice permission sync")
    except Exception as exc:
        print(f"[VOICE] overwrite sync failed for {getattr(channel, 'name', channel)}: {exc}")


async def ensure_layout(guild):
    if guild is None:
        return None, None
    state = _load()
    overwrites = _member_voice_overwrites(guild)
    category = await _get_channel(guild, state.get("category_id"))
    if category is None or not isinstance(category, discord.CategoryChannel):
        for cand in guild.categories:
            if str(cand.name).strip().lower() == CATEGORY_NAME.lower():
                category = cand
                break
    if category is None:
        category = await guild.create_category(
            CATEGORY_NAME,
            overwrites=overwrites,
            reason="Primeval pack voice",
        )
    else:
        await _sync_overwrites(category, overwrites)
    hub = await _get_channel(guild, state.get("hub_id"))
    if hub is None or not isinstance(hub, discord.VoiceChannel) or hub.category_id != category.id:
        for cand in category.voice_channels:
            if str(cand.name).strip().lower() == HUB_NAME.lower():
                hub = cand
                break
    if hub is None:
        hub = await guild.create_voice_channel(
            HUB_NAME,
            category=category,
            overwrites=overwrites,
            reason="Primeval pack voice hub",
        )
    else:
        await _sync_overwrites(hub, overwrites)
    kept = {}
    for species, cid in list((state.get("channels") or {}).items()):
        channel = await _get_channel(guild, cid)
        if isinstance(channel, discord.VoiceChannel):
            await _sync_overwrites(channel, overwrites)
            kept[str(species)] = channel.id
    state["category_id"] = category.id
    state["hub_id"] = hub.id
    state["channels"] = kept
    _save(state)
    return category, hub


async def _species_for_member(member):
    if member is None:
        return "", "Could not read your Discord user."
    if not is_steam_linked(member.id):
        return "", "Link Steam on the member panel before using pack voice."
    steam = _steam_for_discord(member.id)
    if not steam:
        return "", "Link Steam on the member panel before using pack voice."
    try:
        import primeval_tally

        species = await primeval_tally.species_for_steam(steam)
    except Exception as exc:
        print(f"[VOICE] species lookup failed: {exc}")
        species = ""
    if not species:
        cache = G.get("LIVE_BY_STEAM") or {}
        if steam not in cache:
            return "", "We do not see you on the Isle yet. Spawn in, then join Create pack VC."
        return "", "Spawn as a dino first, then join Create pack VC."
    return species, ""


async def _ensure_species_channel(guild, species):
    category, hub = await ensure_layout(guild)
    state = _load()
    existing = await _get_channel(guild, (state.get("channels") or {}).get(species))
    if isinstance(existing, discord.VoiceChannel):
        return existing
    want = _channel_name(species)
    for cand in category.voice_channels:
        if cand.id == hub.id:
            continue
        if str(cand.name).strip().lower() == want.lower():
            state.setdefault("channels", {})[species] = cand.id
            _save(state)
            return cand
    channel = await guild.create_voice_channel(
        want,
        category=category,
        overwrites=_member_voice_overwrites(guild),
        reason="Primeval pack voice " + species,
    )
    state.setdefault("channels", {})[species] = channel.id
    _save(state)
    return channel


async def place_member(member):
    guild = getattr(member, "guild", None)
    if guild is None:
        return False, "Use this in the Fallen Earth server."
    now = time.time()
    last = float(_COOLDOWN.get(member.id) or 0)
    if now - last < 8:
        return False, "Wait a few seconds, then try again."
    _COOLDOWN[member.id] = now
    async with _lock_for(guild.id):
        species, err = await _species_for_member(member)
        if err:
            return False, err
        channel = await _ensure_species_channel(guild, species)
        try:
            await member.move_to(channel, reason="Primeval pack voice")
        except discord.HTTPException as exc:
            text = str(exc).lower()
            if getattr(exc, "code", None) == 40032 or "not connected to voice" in text:
                return False, (
                    f"Join **{HUB_NAME}** under **{CATEGORY_NAME}**. "
                    f"We will move you into **{channel.name}**."
                )
            print(f"[VOICE] move failed: {exc}")
            return False, "Could not move you into pack voice."
        return True, f"Joined **{channel.name}**."


async def _delete_if_empty(channel):
    if not isinstance(channel, discord.VoiceChannel):
        return
    state = _load()
    if int(channel.id) == int(state.get("hub_id") or 0):
        return
    tracked = {
        int(cid): species
        for species, cid in (state.get("channels") or {}).items()
        if cid
    }
    if channel.id not in tracked:
        return
    if len(channel.members) > 0:
        return
    species = tracked.get(channel.id)
    try:
        await channel.delete(reason="Primeval pack voice empty")
    except Exception as exc:
        print(f"[VOICE] delete failed: {exc}")
        return
    if species:
        state.get("channels", {}).pop(str(species), None)
        _save(state)


async def sweep_empty(guild):
    if guild is None:
        return
    async with _lock_for(guild.id):
        state = _load()
        for species, cid in list((state.get("channels") or {}).items()):
            channel = await _get_channel(guild, cid)
            if channel is None:
                state.get("channels", {}).pop(str(species), None)
                continue
            if len(channel.members) == 0:
                try:
                    await channel.delete(reason="Primeval pack voice empty")
                except Exception:
                    pass
                state.get("channels", {}).pop(str(species), None)
        _save(state)


async def handle_voice(member, before, after):
    if member is None or getattr(member, "bot", False):
        return
    guild = getattr(member, "guild", None)
    if guild is None:
        return
    state = _load()
    hub_id = int(state.get("hub_id") or 0)
    after_id = int(getattr(after.channel, "id", 0) or 0)
    before_ch = before.channel
    if after_id and after_id == hub_id:
        ok, msg = await place_member(member)
        if not ok:
            try:
                await member.move_to(None, reason="Primeval pack voice")
            except Exception:
                pass
            try:
                await member.send(msg)
            except Exception:
                pass
        return
    if before_ch is not None:
        await _delete_if_empty(before_ch)


def attach(bot):
    if id(bot) in _ATTACHED:
        return
    _ATTACHED.add(id(bot))

    @bot.listen("on_voice_state_update")
    async def _voice_update(member, before, after):
        try:
            await handle_voice(member, before, after)
        except Exception as exc:
            print(f"[VOICE] update failed: {exc}")

    @bot.listen("on_ready")
    async def _voice_ready():
        try:
            for guild in bot.guilds:
                await ensure_layout(guild)
            print("[VOICE] pack voice ready")
        except Exception as exc:
            print(f"[VOICE] ready failed: {exc}")


def start(bot):
    global _task
    if _task is not None and _task.is_running():
        return _task

    from discord.ext import tasks

    @tasks.loop(seconds=45)
    async def voice_tick():
        try:
            for guild in bot.guilds:
                await sweep_empty(guild)
        except Exception as exc:
            print(f"[VOICE] sweep failed: {exc}")

    @voice_tick.before_loop
    async def _wait():
        await bot.wait_until_ready()
        await asyncio.sleep(6)
        for guild in bot.guilds:
            try:
                await ensure_layout(guild)
            except Exception as exc:
                print(f"[VOICE] layout failed: {exc}")

    _task = voice_tick
    voice_tick.start()
    print("[VOICE] pack voice sweeper started")
    return voice_tick


async def from_panel(interaction: discord.Interaction):
    member = interaction.user
    if not isinstance(member, discord.Member):
        await interaction.response.send_message("Use this in the Fallen Earth server.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    ok, msg = await place_member(member)
    await interaction.followup.send(msg, ephemeral=True)


def register_slash(bot):
    names = {cmd.name for cmd in bot.tree.get_commands()}
    if "pack_vc" in names:
        return

    @bot.tree.command(name="pack_vc", description="Join a voice channel for the dino you are spawned as")
    async def pack_vc(interaction: discord.Interaction):
        await from_panel(interaction)
