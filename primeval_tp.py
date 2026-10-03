"""Discord teleport-to-friend: search linked players, accept via DM, Isle stand-still TP."""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time

import discord
from discord import ui

from primeval_panels import (
    ISLE_SAVED,
    _fn,
    _isle_file,
    _need_steam,
    _queue_cmd_flag,
)
from primeval_species import diet as species_diet

STATE_PATH = "primeval_tp.json"
_STATE_LOCK = threading.Lock()
COOLDOWN = 10 * 60
REQUEST_TTL = 60
LIVE_DIR = ISLE_SAVED + "/live"
TP_DIR = ISLE_SAVED + "/tp"


def _load():
    with _STATE_LOCK:
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {"friends": {}, "blocks": {}, "cooldown": {}, "requests": {}}


def _save(state):
    with _STATE_LOCK:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2)
            handle.write("\n")
        os.replace(tmp, STATE_PATH)


def _uid(user):
    return str(getattr(user, "id", user))


def _diet(species):
    return species_diet(species)


TP_MIN_TARGET_HP = 100


def _health_points(live):
    if not live:
        return None
    raw = live.get("health")
    if raw is None:
        return None
    try:
        hp = float(raw)
    except (TypeError, ValueError):
        return None
    max_hp = live.get("maxHealth")
    try:
        max_hp = float(max_hp) if max_hp is not None else None
    except (TypeError, ValueError):
        max_hp = None
    if hp <= 1.5 and (max_hp is None or max_hp <= 1.5):
        return hp * 100.0
    return hp


def _target_hp_ok(live):
    hp = _health_points(live)
    if hp is None:
        return True, ""
    if hp < TP_MIN_TARGET_HP:
        return False, "They are below 100 HP. Teleport is blocked until they heal."
    return True, ""


def _species_ok(from_sp, to_sp):
    fd, td = _diet(from_sp), _diet(to_sp)
    if fd == "carni":
        if td != "carni":
            return False, "Carni cannot teleport to Herb/Omni."
        a = str(from_sp or "").lower()
        b = str(to_sp or "").lower()
        if a and b and a != b and a not in b and b not in a:
            return False, "Carni can only teleport to the same carnivore species."
        return True, ""
    if td == "carni":
        return False, "Herb/Omni cannot teleport to carnivores."
    return True, ""


def _blocked(state, a, b):
    a, b = str(a), str(b)
    return b in (state.get("blocks") or {}).get(a, []) or a in (state.get("blocks") or {}).get(b, [])


def _cooldown_left(state, uid):
    until = float((state.get("cooldown") or {}).get(str(uid)) or 0)
    left = int(until - time.time())
    return max(0, left)


def _linked_map():
    links = _fn("STEAM_LINKS") or {}
    getter = _fn("get_linked_steam_id")
    out = {}
    keys = list(links.keys()) if isinstance(links, dict) else []
    for key in keys:
        uid = str(key)
        if not uid.isdigit():
            continue
        steam = links.get(key) or links.get(int(uid) if uid.isdigit() else uid)
        if getter:
            try:
                steam = getter(int(uid)) or steam
            except Exception:
                pass
        steam = str(steam or "").strip()
        if steam:
            out[uid] = steam
    return out


async def _live(steam):
    await _queue_cmd_flag("tpinfo", steam)
    path = LIVE_DIR + "/" + str(steam) + ".json"
    for _ in range(8):
        await asyncio.sleep(0.7)
        status, text = await _isle_file("GET", path)
        if status != 200 or not text or not str(text).strip().startswith("{"):
            continue
        try:
            data = json.loads(text)
        except Exception:
            continue
        captured = int(data.get("capturedAt") or 0)
        if captured >= int(time.time()) - 20:
            return data
    return None


async def _tp_status(req_id):
    status, text = await _isle_file("GET", TP_DIR + "/" + str(req_id) + ".json")
    if status != 200 or not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _rules_embed():
    return discord.Embed(
        title="📍 Teleport",
        description=(
            "Choose an option:\n\n"
            "• **Search:** teleport request to any verified user (they must accept).\n"
            "• **Friends:** quick-pick from your friend list.\n\n"
            "Blocks are respected automatically.\n\n"
            "**Rules:**\n"
            "• Herb/Omni → can teleport to Herb/Omni (any species).\n"
            "• Carni → only to Carni of the same species.\n"
            "• Carni ↔ Herbi is blocked\n"
            "• The player you teleport **to** must have **at least 100 HP**. "
            "If they are below that (or drop below it during the 60s hold), the TP is canceled.\n\n"
            "After accept, both players stand still **60s**. **10 minute** cooldown after a successful TP."
        ),
        color=discord.Color.teal(),
    )


class TpMenuView(ui.View):
    def __init__(self):
        super().__init__(timeout=180)

    @ui.button(label="Search", emoji="🔎", style=discord.ButtonStyle.primary)
    async def search(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message(
            "Search a **linked** member to teleport to.",
            view=TpSearchView(),
            ephemeral=True,
        )

    @ui.button(label="Friends", emoji="👥", style=discord.ButtonStyle.secondary)
    async def friends(self, interaction: discord.Interaction, button: ui.Button):
        await show_friends(interaction)

    @ui.button(label="Add friend", emoji="➕", style=discord.ButtonStyle.success)
    async def addf(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message(
            "Pick a linked member to save on your teleport friend list.",
            view=TpAddFriendView(),
            ephemeral=True,
        )

    @ui.button(label="Block", emoji="🚫", style=discord.ButtonStyle.danger)
    async def block(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message(
            "Pick someone to block from sending you teleport requests.",
            view=TpBlockView(),
            ephemeral=True,
        )


class TpSearchView(ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(TpUserSelect("request"))


class TpAddFriendView(ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(TpUserSelect("friend"))


class TpBlockView(ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(TpUserSelect("block"))


class TpUserSelect(ui.UserSelect):
    def __init__(self, mode):
        super().__init__(placeholder="Search people in this server…", min_values=1, max_values=1)
        self.mode = mode

    async def callback(self, interaction: discord.Interaction):
        target = self.values[0]
        if self.mode == "friend":
            await add_friend(interaction, target)
            return
        if self.mode == "block":
            await add_block(interaction, target)
            return
        await send_tp_request(interaction, target)


class FriendPickSelect(ui.Select):
    def __init__(self, options):
        super().__init__(placeholder="Friends…", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        uid = self.values[0]
        member = interaction.guild.get_member(int(uid)) if interaction.guild and uid.isdigit() else None
        if member is None:
            await interaction.response.send_message("That friend is not in the server.", ephemeral=True)
            return
        await send_tp_request(interaction, member)


class FriendPickView(ui.View):
    def __init__(self, options):
        super().__init__(timeout=120)
        self.add_item(FriendPickSelect(options))


class TpDecisionButton(ui.DynamicItem[ui.Button], template=r"pi_tp:(?P<rid>[a-z0-9]+):(?P<act>ok|no|blk)"):
    def __init__(self, req_id, act):
        labels = {"ok": "Accept", "no": "Decline", "blk": "Block"}
        styles = {
            "ok": discord.ButtonStyle.success,
            "no": discord.ButtonStyle.secondary,
            "blk": discord.ButtonStyle.danger,
        }
        super().__init__(
            ui.Button(
                label=labels.get(act, act),
                style=styles.get(act, discord.ButtonStyle.secondary),
                custom_id=f"pi_tp:{req_id}:{act}",
            )
        )
        self.req_id = req_id
        self.act = act

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["rid"], match["act"])

    async def callback(self, interaction: discord.Interaction):
        await handle_decision(interaction, self.req_id, self.act)


class TpDecisionView(ui.View):
    def __init__(self, req_id):
        super().__init__(timeout=None)
        self.add_item(TpDecisionButton(req_id, "ok"))
        self.add_item(TpDecisionButton(req_id, "no"))
        self.add_item(TpDecisionButton(req_id, "blk"))


async def open_tp_menu(interaction: discord.Interaction):
    steam = await _need_steam(interaction)
    if not steam:
        return
    await interaction.response.send_message(embed=_rules_embed(), view=TpMenuView(), ephemeral=True)


async def show_friends(interaction: discord.Interaction):
    state = _load()
    ids = [str(x) for x in (state.get("friends") or {}).get(_uid(interaction.user), [])]
    if not ids:
        await interaction.response.send_message(
            "Your friend list is empty. Use **Add friend** after you find someone with Search.",
            ephemeral=True,
        )
        return
    options = []
    for uid in ids[:25]:
        member = interaction.guild.get_member(int(uid)) if interaction.guild and uid.isdigit() else None
        name = member.display_name if member else f"User {uid}"
        options.append(discord.SelectOption(label=name[:100], value=uid, description="Linked friend"))
    await interaction.response.send_message("Pick a friend to request a teleport.", view=FriendPickView(options), ephemeral=True)


async def add_friend(interaction, target):
    if target.bot or target.id == interaction.user.id:
        await interaction.response.send_message("Pick a real linked member.", ephemeral=True)
        return
    links = _linked_map()
    if str(target.id) not in links:
        await interaction.response.send_message("They have not linked Steam.", ephemeral=True)
        return
    state = _load()
    friends = state.setdefault("friends", {})
    mine = [str(x) for x in friends.get(_uid(interaction.user), [])]
    if str(target.id) not in mine:
        mine.append(str(target.id))
        friends[_uid(interaction.user)] = mine
        _save(state)
    await interaction.response.send_message(f"Added {target.mention} to your teleport friends.", ephemeral=True)


async def add_block(interaction, target):
    state = _load()
    blocks = state.setdefault("blocks", {})
    mine = [str(x) for x in blocks.get(_uid(interaction.user), [])]
    if str(target.id) not in mine:
        mine.append(str(target.id))
        blocks[_uid(interaction.user)] = mine
        _save(state)
    await interaction.response.send_message(
        f"Blocked {target.mention}. They cannot send you teleport requests.",
        ephemeral=True,
    )


async def send_tp_request(interaction, target):
    if target.bot:
        await interaction.response.send_message("You cannot teleport to a bot.", ephemeral=True)
        return
    if target.id == interaction.user.id:
        await interaction.response.send_message("You cannot teleport to yourself.", ephemeral=True)
        return
    links = _linked_map()
    from_steam = links.get(_uid(interaction.user))
    to_steam = links.get(str(target.id))
    getter = _fn("get_linked_steam_id")
    if getter:
        from_steam = getter(interaction.user.id) or from_steam
        to_steam = getter(target.id) or to_steam
    if not from_steam:
        await interaction.response.send_message("Link Steam before requesting a teleport.", ephemeral=True)
        return
    if not to_steam:
        await interaction.response.send_message("That person has not linked Steam.", ephemeral=True)
        return
    state = _load()
    if _blocked(state, interaction.user.id, target.id):
        await interaction.response.send_message("That teleport is blocked.", ephemeral=True)
        return
    left = _cooldown_left(state, interaction.user.id)
    if left > 0:
        mins, secs = divmod(left, 60)
        await interaction.response.send_message(
            f"Teleport cooldown: **{mins}m {secs:02d}s** left.",
            ephemeral=True,
        )
        return
    await interaction.response.defer(ephemeral=True)
    from_live = await _live(from_steam)
    to_live = await _live(to_steam)
    if not (from_live and from_live.get("spawned")):
        await interaction.followup.send("You must be spawned in-game to request a teleport.", ephemeral=True)
        return
    if not (to_live and to_live.get("spawned")):
        await interaction.followup.send("They are not spawned in-game right now.", ephemeral=True)
        return
    ok, why = _species_ok(from_live.get("species"), to_live.get("species"))
    if not ok:
        await interaction.followup.send(
            f"Blocked by diet rules. You are **{from_live.get('species')}** ({_diet(from_live.get('species'))}); "
            f"they are **{to_live.get('species')}** ({_diet(to_live.get('species'))}). {why}",
            ephemeral=True,
        )
        return
    ok, why = _target_hp_ok(to_live)
    if not ok:
        await interaction.followup.send(why, ephemeral=True)
        return
    state = _load()  # reload: state above is stale after the slow _live() awaits
    req_id = f"t{int(time.time())}{str(interaction.user.id)[-3:]}"
    req_id = re.sub(r"[^a-z0-9]", "", req_id.lower())[:24]
    state.setdefault("requests", {})[req_id] = {
        "from_id": str(interaction.user.id),
        "to_id": str(target.id),
        "from_steam": str(from_steam),
        "to_steam": str(to_steam),
        "expires": int(time.time()) + REQUEST_TTL,
        "status": "pending",
        "guild_id": str(getattr(interaction.guild, "id", "") or ""),
    }
    _save(state)
    embed = discord.Embed(
        title="📍 Teleport Request",
        description=(
            f"{interaction.user.mention} wants to teleport to you.\n\n"
            f"They are **{from_live.get('species')}**. You are **{to_live.get('species')}**.\n\n"
            "Accept to allow the teleport.\n"
            "You must stay at **100+ HP** or the TP is canceled.\n"
            "Expires in 60 seconds."
        ),
        color=discord.Color.teal(),
    )
    try:
        await target.send(embed=embed, view=TpDecisionView(req_id))
    except Exception:
        state = _load()
        (state.get("requests") or {}).pop(req_id, None)
        _save(state)
        await interaction.followup.send(
            "Could not DM them. They need to allow DMs from server members.",
            ephemeral=True,
        )
        return
    await interaction.followup.send(
        f"Teleport request sent to {target.mention}. They have **60 seconds** to accept.",
        ephemeral=True,
    )
    _spawn(_expire_request(interaction.client, req_id))


async def _expire_request(bot, req_id):
    await asyncio.sleep(REQUEST_TTL + 1)
    state = _load()
    req = (state.get("requests") or {}).get(req_id)
    if not req or req.get("status") != "pending":
        return
    req["status"] = "expired"
    _save(state)
    try:
        user = await bot.fetch_user(int(req["from_id"]))
        await user.send("Teleport request expired (no accept within 60 seconds).")
    except Exception:
        pass


async def handle_decision(interaction, req_id, act):
    state = _load()
    req = (state.get("requests") or {}).get(req_id)
    if not req:
        await interaction.response.send_message("That teleport request is gone.", ephemeral=True)
        return
    if str(interaction.user.id) != str(req.get("to_id")):
        await interaction.response.send_message("This request is not for you.", ephemeral=True)
        return
    if req.get("status") != "pending" or time.time() > float(req.get("expires") or 0) + 2:
        await interaction.response.send_message("This request expired.", ephemeral=True)
        return
    if act == "no":
        req["status"] = "declined"
        _save(state)
        await interaction.response.edit_message(content="Request declined.", embed=None, view=None)
        await _tell(interaction.client, req["from_id"], "Your teleport request was declined.")
        return
    if act == "blk":
        blocks = state.setdefault("blocks", {})
        mine = [str(x) for x in blocks.get(_uid(interaction.user), [])]
        if str(req["from_id"]) not in mine:
            mine.append(str(req["from_id"]))
            blocks[_uid(interaction.user)] = mine
        req["status"] = "blocked"
        _save(state)
        await interaction.response.edit_message(content="Blocked. Future requests from them are ignored.", embed=None, view=None)
        await _tell(interaction.client, req["from_id"], "Your teleport request was declined.")
        return
    req["status"] = "hold"
    _save(state)
    await interaction.response.edit_message(
        content=(
            "Accept to allow the teleport.\n"
            "Teleport started. Both players must stand still for 60s.\n"
            "If either player moves, or you drop below 100 HP, the request will be canceled."
        ),
        embed=None,
        view=None,
    )
    await _tell(
        interaction.client,
        req["from_id"],
        "Teleport started. Both players must stand still for 60s.\n"
        "If either player moves, or your friend drops below 100 HP, the request will be canceled.",
    )
    ok, err = await _queue_cmd_flag("tpstart", req["from_steam"], f"{req['to_steam']} {req_id}")
    if not ok:
        state = _load()  # state is stale after the awaits above
        req = (state.get("requests") or {}).get(req_id) or req
        req["status"] = "fail"
        _save(state)
        await _tell(interaction.client, req["from_id"], f"Isle did not start the teleport: {err}")
        await _tell(interaction.client, req["to_id"], f"Isle did not start the teleport: {err}")
        return
    _spawn(_watch_hold(interaction.client, req_id))


_TASKS = set()


def _spawn(coro):
    # strong ref so the event loop cannot garbage-collect the task mid-run
    task = asyncio.create_task(coro)
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


async def _tell(bot, uid, text):
    try:
        user = await bot.fetch_user(int(uid))
        await user.send(text)
    except Exception:
        pass


async def _watch_hold(bot, req_id):
    state = _load()
    req = (state.get("requests") or {}).get(req_id) or {}
    final = None
    for _ in range(90):
        await asyncio.sleep(1)
        row = await _tp_status(req_id)
        if not row:
            continue
        status = str(row.get("status") or "")
        if status in ("ok", "moved", "fail", "cancel"):
            final = row
            break
    state = _load()
    req = (state.get("requests") or {}).get(req_id) or req
    if final and str(final.get("status")) == "ok":
        req["status"] = "done"
        state.setdefault("cooldown", {})[str(req.get("from_id"))] = time.time() + COOLDOWN
        _save(state)
        await _tell(bot, req.get("from_id"), "Teleport executed.")
        await _tell(bot, req.get("to_id"), "Teleport executed.")
        return
    msg = (final or {}).get("msg") or "Teleport was canceled."
    req["status"] = str((final or {}).get("status") or "fail")
    _save(state)
    await _tell(bot, req.get("from_id"), msg)
    await _tell(bot, req.get("to_id"), msg)


def register_views(bot):
    try:
        bot.add_dynamic_items(TpDecisionButton)
    except Exception:
        pass
    try:
        bot.loop.create_task(_maybe_push_lua())
    except Exception:
        pass


async def _maybe_push_lua():
    marker = "_isle_tp_pushed"
    src = "_isle_push_main.lua"
    if os.path.exists(marker) or not os.path.exists(src):
        return
    try:
        with open(src, "r", encoding="utf-8") as handle:
            body = handle.read()
    except Exception as exc:
        print("[TP] lua read failed", exc)
        return
    if "startFriendTp" not in body:
        return
    if not body.endswith("\n"):
        body += "\n"
    path = "/TheIsle/Binaries/Win64/ue4ss/Mods/PrimevalRedeem/Scripts/main.lua"
    status, text = await _isle_file("POST", path, body)
    print("[TP] push lua", status, str(text)[:120])
    if status in (200, 204):
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write(str(int(time.time())))

