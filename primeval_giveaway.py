"""Monthly token giveaway for Steam-linked free players.

Auto-draws after the calendar month rolls. Winner is posted in #events
so it is public that the island supports people who play for free.
Donors (Hatchling / Juvenile / Apex) are not in the pool.
"""

from __future__ import annotations

import asyncio
import calendar
import json
import os
import random
import time
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import tasks

from primeval_panels import can_staff, deny_staff

G = {}

STATE_PATH = "primeval_giveaway.json"
MIN_PRIZE = 5
MAX_PRIZE = 20
ADMIN_PRIZE_CAP = 100
DIRECTOR_PRIZE_CAP = 10000
TICK_SECONDS = 60

_ATTACHED = set()
_task = None
_STATE_LOCK = asyncio.Lock()


def bind(g):
    G.clear()
    G.update(g)


def _fn(name, default=None):
    return G.get(name, default)


def _load():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            data.setdefault("settings", {})
            data.setdefault("draws", {})
            return data
    except Exception:
        pass
    return {"settings": {}, "draws": {}, "bootstrapped": False}


def _save(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)
        handle.write("\n")
    os.replace(tmp, STATE_PATH)


def _month_key(when=None):
    when = when or datetime.now()
    return when.strftime("%Y-%m")


def _prev_month(month):
    year, mon = [int(part) for part in str(month).split("-")[:2]]
    mon -= 1
    if mon < 1:
        mon = 12
        year -= 1
    return f"{year:04d}-{mon:02d}"


def _next_month(month):
    year, mon = [int(part) for part in str(month).split("-")[:2]]
    mon += 1
    if mon > 12:
        mon = 1
        year += 1
    return f"{year:04d}-{mon:02d}"


def _month_label(month):
    try:
        year, mon = [int(part) for part in str(month).split("-")[:2]]
        return f"{calendar.month_name[mon]} {year}"
    except Exception:
        return str(month)


def _is_patron(member):
    try:
        from primeval_patreon import _active_tier_key

        return bool(_active_tier_key(member))
    except Exception:
        return False


def _steam_for(user_id):
    getter = _fn("get_linked_steam_id")
    if not getter or not user_id:
        return None
    try:
        steam = getter(int(user_id))
    except Exception:
        return None
    return str(steam).strip() if steam else None


def _credit(steam, amount, user_id=None):
    try:
        from primeval_patreon import _credit

        return _credit(steam, amount, "free player monthly giveaway", user_id)
    except Exception as exc:
        print("[GIVEAWAY] credit failed", exc)
        return False, 0


async def _audit(bot, verb, user=None, steam="", ok=True, **fields):
    try:
        import primeval_vault_audit

        await primeval_vault_audit.note(
            bot, verb, user, steam=steam, ok=ok, **fields
        )
    except Exception as exc:
        print("[GIVEAWAY] audit failed", exc)


def _eligible_members(guild, state, month):
    prev = (state.get("draws") or {}).get(_prev_month(month)) or {}
    skip_ids = {str(prev.get("winner_discord") or "").strip()}
    skip_steams = {str(prev.get("winner_steam") or "").strip()}
    skip_ids.discard("")
    skip_steams.discard("")
    out = []
    seen = set()
    for member in getattr(guild, "members", []) or []:
        if getattr(member, "bot", False):
            continue
        if _is_patron(member):
            continue
        if str(member.id) in skip_ids:
            continue
        steam = _steam_for(member.id)
        if not steam or steam in seen or steam in skip_steams:
            continue
        seen.add(steam)
        out.append((member, steam))
    return out


def _override_amount(state):
    settings = state.get("settings") or {}
    try:
        amount = int(settings.get("override_amount") or 0)
    except Exception:
        amount = 0
    return amount if amount > 0 else 0


def _prize_for(state, month):
    amount = _override_amount(state)
    if amount > 0:
        return amount, True
    return random.randint(MIN_PRIZE, MAX_PRIZE), False


def _consume_override(state, month=None):
    settings = state.setdefault("settings", {})
    settings.pop("override_amount", None)
    settings.pop("override_month", None)
    settings.pop("override_by", None)


async def _post_events(bot, title, body, guild=None):
    import primeval_events

    return await primeval_events._post_event(title, body, guild=guild, bot=bot)


def _winner_body(month, member, amount, pool_n, forced):
    prize_bit = (
        f"**{amount}** tokens (staff-set prize this month)"
        if forced
        else f"**{amount}** tokens (rolled 5–20)"
    )
    return (
        f"**{_month_label(month)} free-player token giveaway**\n\n"
        f"Winner: {member.mention}\n"
        f"Prize: {prize_bit}\n"
        f"Eligible linked free players this draw: **{pool_n}**\n\n"
        "This draw is only for people who play for free and have Steam linked. "
        "Donation roles are not in the pool. Last month's winner sits this one out.\n"
        "Next draw is the 1st. Play for free either way."
    )


def _empty_body(month, reason):
    return (
        f"**{_month_label(month)} free-player token giveaway**\n\n"
        f"The monthly draw ran. Nobody won this time: {reason}\n\n"
        "Steam-linked members without a Hatchling / Juvenile / Apex role are in the pool. "
        "Last month's winner sits one draw out. Donors are not entered on purpose."
    )


def _pick_guild(bot):
    guilds = list(getattr(bot, "guilds", []) or [])
    return guilds[0] if guilds else None


async def run_draw(bot, *, force=False, actor=None):
    month = _month_key()
    async with _STATE_LOCK:
        state = _load()
        if not state.get("bootstrapped"):
            state["bootstrapped"] = True
            if not force:
                state["drawn_month"] = month
                _save(state)
                print("[GIVEAWAY] bootstrapped; first auto-draw waits for the next month")
                return False, "bootstrapped"
        if not force and state.get("drawn_month") == month:
            return False, "already drawn"
        guild = _pick_guild(bot)
        if guild is None:
            return False, "no guild"
        pool = _eligible_members(guild, state, month)
        if not pool:
            reason = "no Steam-linked free players were eligible"
            message, err = await _post_events(
                bot,
                "Free-player token giveaway",
                _empty_body(month, reason),
                guild=guild,
            )
            state["drawn_month"] = month
            state.setdefault("draws", {})[month] = {
                "winner_discord": "",
                "winner_steam": "",
                "amount": 0,
                "pool": 0,
                "at": int(time.time()),
                "empty": True,
                "forced": bool(force),
            }
            _save(state)
            await _audit(
                bot, "giveaway", actor, ok=True,
                msg=f"Empty pool for {month}: {reason}",
            )
            if message is None:
                return False, err or "empty pool; #events post failed"
            return True, f"empty pool posted in #events ({err})" if err else "empty pool posted"
        member, steam = random.choice(pool)
        amount, forced = _prize_for(state, month)
        ok, bal = _credit(steam, amount, member.id)
        if not ok:
            await _audit(
                bot, "giveaway", member, steam=steam, ok=False,
                tokens=amount, msg=f"Wallet credit failed for {month}",
            )
            return False, "wallet credit failed"
        body = _winner_body(month, member, amount, len(pool), forced)
        message, err = await _post_events(
            bot,
            "Free-player token giveaway",
            body,
            guild=guild,
        )
        state["drawn_month"] = month
        state.setdefault("draws", {})[month] = {
            "winner_discord": str(member.id),
            "winner_steam": steam,
            "amount": int(amount),
            "pool": len(pool),
            "balance": int(bal or 0),
            "at": int(time.time()),
            "forced": bool(forced),
            "staff_set": bool(forced),
            "rerun": bool(force),
            "message_id": int(getattr(message, "id", 0) or 0),
        }
        _consume_override(state, month)
        _save(state)
        await _audit(
            bot, "giveaway", member, steam=steam, ok=True,
            tokens=amount, balance=bal,
            msg=f"{month} winner {member.id} +{amount} (pool {len(pool)})",
        )
        try:
            await member.send(
                f"You won this month's Fallen Earth free-player giveaway: **{amount}** tokens. "
                f"Balance: **{bal}**. Posted in #events."
            )
        except Exception:
            pass
        if message is None:
            return True, f"credited {member} +{amount}, but #events post failed: {err}"
        return True, f"credited {member} +{amount} and posted in #events"


async def maybe_auto_draw(bot):
    month = _month_key()
    async with _STATE_LOCK:
        state = _load()
        if not state.get("bootstrapped"):
            state["bootstrapped"] = True
            state["drawn_month"] = month
            _save(state)
            print("[GIVEAWAY] bootstrapped; first auto-draw waits for the next month")
            return
        if state.get("drawn_month") == month:
            return
    ok, msg = await run_draw(bot, force=False)
    print("[GIVEAWAY] auto", ok, msg)


def _status_text(guild):
    state = _load()
    month = _month_key()
    draws = state.get("draws") or {}
    row = draws.get(month) or {}
    settings = state.get("settings") or {}
    override_month = str(settings.get("override_month") or "")
    try:
        override_amt = int(settings.get("override_amount") or 0)
    except Exception:
        override_amt = 0
    pool = _eligible_members(guild, state, month) if guild else []
    lines = [
        f"**{_month_label(month)}**",
        f"Drawn: **{'yes' if state.get('drawn_month') == month else 'no'}**",
        f"Eligible now: **{len(pool)}** linked free players",
    ]
    if override_amt > 0:
        when = f" (`{override_month}`)" if override_month else ""
        lines.append(f"Pinned prize: **{override_amt}** tokens — this overrides the 5–20 roll{when}")
    else:
        lines.append(f"Prize: random **{MIN_PRIZE}–{MAX_PRIZE}** (no staff pin)")
    if row.get("winner_discord"):
        lines.append(
            f"This month's winner: <@{row['winner_discord']}> · **{row.get('amount') or 0}** tokens"
        )
    elif row.get("empty"):
        lines.append("This month's draw ran with an empty pool.")
    prev = draws.get(_prev_month(month)) or {}
    if prev.get("winner_discord"):
        lines.append(f"Sitting out (won last month): <@{prev['winner_discord']}>")
    return "\n".join(lines)


def attach(bot):
    if id(bot) in _ATTACHED:
        return
    _ATTACHED.add(id(bot))


def start(bot):
    global _task
    attach(bot)
    if _task is not None and _task.is_running():
        return _task

    @tasks.loop(seconds=TICK_SECONDS)
    async def giveaway_tick():
        try:
            await maybe_auto_draw(bot)
        except Exception as exc:
            print("[GIVEAWAY] tick failed", exc)

    @giveaway_tick.before_loop
    async def _wait():
        await bot.wait_until_ready()
        await asyncio.sleep(20)

    _task = giveaway_tick
    giveaway_tick.start()
    print("[GIVEAWAY] monthly free-player draw started")
    return giveaway_tick


def register_slash(bot):
    existing = {cmd.name for cmd in bot.tree.get_commands()}
    if "free_giveaway_prize" not in existing:

        @bot.tree.command(
            name="free_giveaway_prize",
            description="Pin this month's free-player giveaway prize (Admin 1-100, Director over 100)",
        )
        @app_commands.describe(amount="Token amount for the next undrawn month")
        async def free_giveaway_prize(interaction: discord.Interaction, amount: int):
            if not can_staff(interaction.user, "free_giveaway"):
                await deny_staff(interaction, "free_giveaway")
                return
            if amount < 1:
                await interaction.response.send_message("Amount must be at least 1.", ephemeral=True)
                return
            if amount > ADMIN_PRIZE_CAP and not can_staff(interaction.user, "free_giveaway_uncapped"):
                await interaction.response.send_message(
                    f"Administrator cap is **{ADMIN_PRIZE_CAP}**. Only Director can set more.",
                    ephemeral=True,
                )
                return
            if amount > DIRECTOR_PRIZE_CAP:
                await interaction.response.send_message(
                    f"Hard cap is **{DIRECTOR_PRIZE_CAP}**.", ephemeral=True
                )
                return
            month = _month_key()
            async with _STATE_LOCK:
                state = _load()
                real = (state.get("draws") or {}).get(month) or {}
                already_paid = bool(real.get("winner_discord") or real.get("empty"))
                target = _next_month(month) if already_paid else month
                settings = state.setdefault("settings", {})
                settings["override_amount"] = int(amount)
                settings["override_month"] = target
                settings["override_by"] = str(interaction.user.id)
                _save(state)
            await interaction.response.send_message(
                f"Pinned **{amount}** tokens. The next free-player giveaway uses this "
                f"instead of the 5–20 roll (`{target}`). Winner still posts in **#events**.",
                ephemeral=True,
            )

    if "free_giveaway_status" not in existing:

        @bot.tree.command(
            name="free_giveaway_status",
            description="Show this month's free-player giveaway status",
        )
        async def free_giveaway_status(interaction: discord.Interaction):
            if not can_staff(interaction.user, "free_giveaway"):
                await deny_staff(interaction, "free_giveaway")
                return
            await interaction.response.send_message(
                _status_text(interaction.guild), ephemeral=True
            )

    if "free_giveaway_run" not in existing:

        @bot.tree.command(
            name="free_giveaway_run",
            description="Director: run this month's free-player giveaway now (posts in #events)",
        )
        async def free_giveaway_run(interaction: discord.Interaction):
            if not can_staff(interaction.user, "free_giveaway_run"):
                await deny_staff(interaction, "free_giveaway_run")
                return
            await interaction.response.defer(ephemeral=True)
            ok, msg = await run_draw(interaction.client, force=True, actor=interaction.user)
            await interaction.followup.send(
                ("Ran. " if ok else "Did not finish. ") + str(msg),
                ephemeral=True,
            )
