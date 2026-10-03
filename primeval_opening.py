"""Token-event rewards.

Staff toggle (Director): Token event on the staff panel.
When ON:
  - 15 tokens once for people who join Discord during this special and then link Steam
  - 5 tokens per 30 minutes for anyone Steam-linked who is currently on the Isle
  - Isle RCON `announce` every 30 minutes (system banner, not player chat)
Turning ON starts a fresh special (the 15 can pay again for new Discord joiners).
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time

import discord
from discord import ui
from discord.ext import tasks

G = {}
STATE_PATH = "primeval_opening.json"
_LOCK = threading.RLock()

JOIN_BONUS = 15
PLAY_TOKENS = 5
PLAY_SECONDS = 30 * 60
MAX_TICK_CREDIT = 180
NOTICE_SECONDS = 30 * 60
ANNOUNCE_SECONDS = 60 * 60
DISCORD_INVITE = "https://discord.gg/MNnhAQyhkr"
EVENT_INGAME_BODY = (
    "Welcome to Fallen Earth! Opening event: new Discord members who link Steam get 15 tokens once. "
    f"Linked players earn 5 tokens every 30 minutes in-game. Join {DISCORD_INVITE}"
)
EVENT_INGAME_MSG = "Fallen Earth Elder — " + EVENT_INGAME_BODY

_task = None


def bind(g):
    G.clear()
    G.update(g)


def _fn(name, default=None):
    return G.get(name, default)


def event_active(_now=None):
    return bool(_load().get("enabled"))


def panel_line():
    state = _load()
    on = bool(state.get("enabled"))
    eid = int(state.get("event_id") or 0)
    when = int(state.get("toggled_at") or 0)
    special = f"special #{eid}" if eid else "no special yet"
    when_bit = f" · toggled <t:{when}:R>" if when else ""
    if on:
        return f"**ON** — {special}{when_bit}"
    return f"**OFF** — {special}{when_bit}"


async def apply_toggle(on, user_id=0):
    set_enabled(on, user_id)
    await _sync_event_flag()
    if on:
        await _maybe_broadcast(force=True)
    return True, "Token event ON. New special started." if on else "Token event OFF. Payouts stopped."


def how_to_line():
    if not event_active():
        return ""
    return (
        "\n**Token event** — **new Discord members** who **link Steam** get **15 tokens** once. "
        "Anyone already linked earns **5 tokens every 30 minutes** while on the Isle.\n"
    )


def was_seen(steam):
    steam = str(steam or "").strip()
    if not steam:
        return False
    state = _load()
    if steam in (state.get("last_online") or []):
        return True
    seen = state.get("seen_isle") or {}
    if seen.get(_ekey(state, steam)):
        return True
    suffix = f":{steam}"
    return any(str(key) == steam or str(key).endswith(suffix) for key in seen)


def _blank():
    return {
        "enabled": False,
        "event_id": 0,
        "toggled_by": 0,
        "toggled_at": 0,
        "join_bonus": {},
        "play_seconds": {},
        "play_paid": {},
        "seen_isle": {},
        "new_joins": {},
        "last_online": [],
        "last_tick": 0,
        "last_ingame_notice_at": 0,
        "last_ingame_announce_at": 0,
    }


def _eid(state):
    return str(int(state.get("event_id") or 0))


def _ekey(state, steam):
    return f"{_eid(state)}:{steam}"


def _dkey(state, user_id):
    return f"{_eid(state)}:d:{int(user_id)}"


def _load():
    with _LOCK:
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                data.setdefault("enabled", False)
                data.setdefault("event_id", 0)
                data.setdefault("toggled_by", 0)
                data.setdefault("toggled_at", 0)
                data.setdefault("join_bonus", {})
                data.setdefault("play_seconds", {})
                data.setdefault("play_paid", {})
                data.setdefault("seen_isle", {})
                data.setdefault("new_joins", {})
                data.setdefault("last_online", [])
                data.setdefault("last_tick", 0)
                data.setdefault("last_ingame_notice_at", 0)
                data.setdefault("last_ingame_announce_at", 0)
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


def set_enabled(on, user_id=0):
    on = bool(on)
    with _LOCK:
        state = _load()
        was = bool(state.get("enabled"))
        state["enabled"] = on
        state["toggled_by"] = int(user_id or 0)
        state["toggled_at"] = int(time.time())
        if on and not was:
            state["event_id"] = int(state.get("event_id") or 0) + 1
            state["last_online"] = []
            state["last_tick"] = 0
            state["last_ingame_notice_at"] = 0
            state["last_ingame_announce_at"] = 0
        _save(state)
        return {
            "enabled": bool(state.get("enabled")),
            "event_id": int(state.get("event_id") or 0),
            "toggled_by": int(state.get("toggled_by") or 0),
            "toggled_at": int(state.get("toggled_at") or 0),
        }


def _discord_for_steam(steam):
    steam = str(steam or "").strip()
    if not steam:
        return 0
    links = G.get("STEAM_LINKS") or {}
    for key, val in links.items():
        if str(val).strip() == steam:
            try:
                return int(key)
            except Exception:
                continue
    return 0


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


def _credit(steam, amount, reason, user_id=0):
    if not steam or amount <= 0:
        return False, 0
    amount = int(amount)
    try:
        uid = int(user_id or 0)
    except Exception:
        uid = 0
    credit = _fn("credit_steam_tokens") or _fn("add_steam_tokens")
    getter = _fn("get_steam_token_balance")
    setter = _fn("set_steam_token_balance")
    if credit:
        try:
            result = credit(steam, uid, amount)
        except TypeError:
            try:
                result = credit(steam, amount, reason)
            except TypeError:
                result = credit(steam, amount)
        except Exception as exc:
            print("[OPENING] credit failed", reason, exc)
            return False, 0
        if result is False:
            return False, 0
        if isinstance(result, (int, float)):
            return True, int(result)
        if getter:
            try:
                return True, int(getter(steam) or 0)
            except Exception:
                return True, 0
        return True, 0
    if not getter or not setter:
        print("[OPENING] no steam wallet credit function bound")
        return False, 0
    try:
        current = int(getter(steam) or 0)
    except Exception as exc:
        print("[OPENING] balance read failed", exc)
        return False, 0
    target = current + amount
    try:
        result = setter(steam, uid, target)
    except TypeError:
        result = setter(steam, target)
    except Exception as exc:
        print("[OPENING] credit failed", reason, exc)
        return False, 0
    if result is False:
        return False, 0
    if isinstance(result, (int, float)):
        return True, int(result)
    return True, target


async def _tell(bot, user_id, text):
    if not bot or not user_id or not text:
        return
    user = bot.get_user(int(user_id))
    if user is None:
        try:
            user = await bot.fetch_user(int(user_id))
        except Exception:
            return
    try:
        await user.send(text)
    except Exception:
        pass


def _joined_during_event(state, member):
    if member is None:
        return False
    joined = getattr(member, "joined_at", None)
    if joined is None:
        return False
    try:
        ts = float(joined.timestamp())
    except Exception:
        return False
    start = int(state.get("toggled_at") or 0)
    return start > 0 and ts >= (start - 2)


def _is_event_new_member(state, user_id, member=None):
    try:
        uid = int(user_id or 0)
    except Exception:
        return False
    if not uid:
        return False
    if (state.get("new_joins") or {}).get(_dkey(state, uid)):
        return True
    return _joined_during_event(state, member)


def _try_join_bonus(state, steam, user_id, member=None):
    steam = str(steam or "").strip()
    if not steam or not user_id:
        return 0
    if not _is_event_new_member(state, user_id, member):
        return 0
    key = _dkey(state, user_id)
    old_key = _ekey(state, steam)
    if state["join_bonus"].get(key) or state["join_bonus"].get(old_key):
        return 0
    ok, _bal = _credit(steam, JOIN_BONUS, "token-event-join", user_id)
    if not ok:
        return 0
    state["join_bonus"][key] = int(time.time())
    print(f"[OPENING] join bonus +{JOIN_BONUS} discord={int(user_id)}")
    return JOIN_BONUS


def _try_play_payout(state, steam, user_id):
    steam = str(steam or "").strip()
    if not steam or not user_id:
        return 0
    key = _ekey(state, steam)
    seconds = int(state["play_seconds"].get(key) or 0)
    paid = int(state["play_paid"].get(key) or 0)
    due = (seconds // PLAY_SECONDS) - paid
    if due <= 0:
        return 0
    total = due * PLAY_TOKENS
    ok, _bal = _credit(steam, total, "token-event-playtime", user_id)
    if not ok:
        return 0
    state["play_paid"][key] = paid + due
    print(f"[OPENING] playtime +{total} ({due} x {PLAY_SECONDS}s) steam=...{steam[-6:]}")
    return total


def _member_for(bot, user_id):
    try:
        uid = int(user_id or 0)
    except Exception:
        return None
    if not bot or not uid:
        return None
    for guild in getattr(bot, "guilds", []) or []:
        member = guild.get_member(uid)
        if member is not None:
            return member
    return None


async def on_discord_join(member):
    if member is None or getattr(member, "bot", False):
        return
    if not event_active():
        return
    state = _load()
    state.setdefault("new_joins", {})[_dkey(state, member.id)] = int(time.time())
    _save(state)


async def on_online(bot, steams, now=None):
    if not event_active():
        return
    now = int(now or time.time())
    state = _load()
    last_tick = int(state.get("last_tick") or 0)
    prev = set(str(s) for s in (state.get("last_online") or []))
    current = [str(s).strip() for s in (steams or []) if str(s).strip()]
    elapsed = 0
    if last_tick and prev:
        elapsed = now - last_tick
        if elapsed < 0:
            elapsed = 0
        if elapsed > MAX_TICK_CREDIT:
            elapsed = MAX_TICK_CREDIT
    notes = []
    for steam in current:
        key = _ekey(state, steam)
        state["seen_isle"][key] = now
        if steam in prev and elapsed > 0:
            state["play_seconds"][key] = int(state["play_seconds"].get(key) or 0) + elapsed
        user_id = _discord_for_steam(steam)
        if not user_id:
            continue
        member = _member_for(bot, user_id)
        join = _try_join_bonus(state, steam, user_id, member)
        play = _try_play_payout(state, steam, user_id)
        if join:
            notes.append((user_id, f"Token event: **+{join} tokens** for joining Discord and linking Steam."))
        if play:
            notes.append((user_id, f"Token event: **+{play} tokens** for time on the Isle."))
    state["last_online"] = current
    state["last_tick"] = now
    _save(state)
    for user_id, text in notes:
        await _tell(bot, user_id, text)


async def on_steam_linked(interaction):
    if not event_active():
        return
    user = getattr(interaction, "user", None)
    if user is None:
        return
    steam = _steam_for_discord(user.id)
    if not steam:
        return
    state = _load()
    member = user if isinstance(user, discord.Member) else None
    if member is None:
        guild = getattr(interaction, "guild", None)
        if guild is not None:
            member = guild.get_member(user.id)
    join = _try_join_bonus(state, steam, user.id, member)
    play = _try_play_payout(state, steam, user.id)
    _save(state)
    bits = []
    if join:
        bits.append(f"**+{join} tokens** for joining Discord and linking Steam")
    if play:
        bits.append(f"**+{play} tokens** for time already played")
    if bits:
        try:
            await user.send("Token event: " + " and ".join(bits) + ".")
        except Exception:
            pass


def _staff_embed():
    state = _load()
    on = bool(state.get("enabled"))
    eid = int(state.get("event_id") or 0)
    when = int(state.get("toggled_at") or 0)
    who = int(state.get("toggled_by") or 0)
    who_bit = f" by <@{who}>" if who else ""
    when_bit = f" <t:{when}:R>" if when else ""
    special = f"special #{eid}" if eid else "no special started yet"
    return discord.Embed(
        title="🎉 Token event",
        description=(
            f"Status: **{'ON' if on else 'OFF'}** — {special}\n"
            f"Last toggle{who_bit}{when_bit}\n\n"
            f"While ON: **{JOIN_BONUS} tokens** once for **new Discord members who link Steam**, "
            f"**{PLAY_TOKENS} tokens** every 30 minutes for anyone linked and in-game.\n"
            "Isle reminders while ON: **one RCON announce every 30 minutes** "
            "as a system banner (never Local/Global player chat).\n"
            f"Discord {DISCORD_INVITE}.\n"
            "Turning **ON** starts a **new** special (the 15 can pay again for people who join Discord after that).\n"
            "Turning **OFF** stops payouts and in-game reminders immediately."
        ),
        color=discord.Color.green() if on else discord.Color.dark_grey(),
    )


class TokenEventView(ui.View):
    def __init__(self):
        super().__init__(timeout=180)

    async def _gate(self, interaction: discord.Interaction):
        from primeval_panels import can_staff, deny_staff

        if not can_staff(interaction.user, "token_event"):
            await deny_staff(interaction, "token_event")
            return False
        return True

    @ui.button(label="Turn ON", emoji="✅", style=discord.ButtonStyle.success)
    async def turn_on(self, interaction: discord.Interaction, button: ui.Button):
        if not await self._gate(interaction):
            return
        set_enabled(True, interaction.user.id)
        await interaction.response.edit_message(embed=_staff_embed(), view=self)
        await _sync_event_flag()
        await _maybe_broadcast(force=True)

    @ui.button(label="Turn OFF", emoji="🛑", style=discord.ButtonStyle.danger)
    async def turn_off(self, interaction: discord.Interaction, button: ui.Button):
        if not await self._gate(interaction):
            return
        set_enabled(False, interaction.user.id)
        await interaction.response.edit_message(embed=_staff_embed(), view=self)
        await _sync_event_flag()


async def show_staff_controls(interaction: discord.Interaction):
    await interaction.response.send_message(
        embed=_staff_embed(),
        view=TokenEventView(),
        ephemeral=True,
    )


def register_slash(bot):
    names = {cmd.name for cmd in bot.tree.get_commands()}

    if "opening" not in names:
        @bot.tree.command(name="opening", description="Your token-event bonus status")
        async def opening(interaction: discord.Interaction):
            steam = _steam_for_discord(interaction.user.id)
            state = _load()
            if not event_active():
                await interaction.response.send_message(
                    "No token event is running right now.",
                    ephemeral=True,
                )
                return
            if not steam:
                await interaction.response.send_message(
                    "Link Steam on the panel first.\n"
                    "**15 tokens** — only if you joined this Discord during the event, then link.\n"
                    "**5 tokens every 30 minutes** — anyone linked and in-game on the Isle.",
                    ephemeral=True,
                )
                return
            key = _ekey(state, steam)
            dkey = _dkey(state, interaction.user.id)
            claimed = bool(state["join_bonus"].get(dkey))
            seen = bool(state["seen_isle"].get(key))
            seconds = int(state["play_seconds"].get(key) or 0)
            paid = int(state["play_paid"].get(key) or 0)
            toward = seconds % PLAY_SECONDS
            left = PLAY_SECONDS - toward if seen else PLAY_SECONDS
            member = interaction.user if isinstance(interaction.user, discord.Member) else None
            eligible = _is_event_new_member(state, interaction.user.id, member)
            if claimed:
                join_line = "claimed"
            elif eligible:
                join_line = "ready — next Isle tally will pay it"
            else:
                join_line = "not eligible (this 15 is for people who joined Discord during the event)"
            await interaction.response.send_message(
                f"**Token event** is ON\n"
                f"New-member bonus (15): **{join_line}**\n"
                f"Playtime paid: **{paid * PLAY_TOKENS}** tokens ({paid} half-hours)\n"
                f"Next 5 tokens in about **{max(1, left // 60)} min** while you stay on the Isle (linked).",
                ephemeral=True,
            )

    if "token_event" not in names:
        @bot.tree.command(name="token_event", description="Director: turn the token special on or off")
        async def token_event(interaction: discord.Interaction):
            from primeval_panels import can_staff, deny_staff

            if not can_staff(interaction.user, "token_event"):
                await deny_staff(interaction, "token_event")
                return
            await show_staff_controls(interaction)


async def _sync_event_flag():
    try:
        import primeval_isle as isle
        on = event_active()
        body = json.dumps({"enabled": on, "message": EVENT_INGAME_MSG}) + "\n"
        await isle.write_file(isle.SAVED + "/token_event.json", body)
        queue = _fn("queue_isle_inbox") or isle.queue_inbox
        ok, msg = await queue({
            "verb": "eventon" if on else "eventoff",
            "text": EVENT_INGAME_MSG,
        })
        print("[OPENING] event flag", "on" if on else "off", ok, msg)
    except Exception as exc:
        print(f"[OPENING] event flag sync failed: {exc}")


async def _send_ingame_announce():
    send = _fn("send_game_command")
    if not send:
        print("[OPENING] no rcon for Elder announce")
        return
    try:
        await send("announce " + EVENT_INGAME_MSG)
        print("[OPENING] ingame announce as Fallen Earth Elder")
    except Exception as exc:
        print(f"[OPENING] ingame announce failed: {exc}")


async def _maybe_broadcast(force=False):
    if not event_active():
        return
    now = time.time()
    with _LOCK:
        state = _load()
        last_n = float(state.get("last_ingame_notice_at") or 0)
        due_n = force or (now - last_n >= NOTICE_SECONDS)
        if not due_n:
            return
        state["last_ingame_notice_at"] = now
        state["last_ingame_announce_at"] = now
        _save(state)
    await _send_ingame_announce()


def start(bot):
    global _task
    if _task is not None and _task.is_running():
        return _task

    @tasks.loop(seconds=30)
    async def opening_tick():
        try:
            await _maybe_broadcast()
        except Exception as exc:
            print(f"[OPENING] broadcast tick failed: {exc}")

    @opening_tick.before_loop
    async def _wait_ready():
        await bot.wait_until_ready()
        await asyncio.sleep(8)
        try:
            await _sync_event_flag()
        except Exception as exc:
            print(f"[OPENING] startup flag sync failed: {exc}")

    _task = opening_tick
    opening_tick.start()
    print("[OPENING] token-event in-game reminders started")
    return opening_tick
