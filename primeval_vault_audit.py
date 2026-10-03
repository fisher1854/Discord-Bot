"""Audit trail for dino buy / redeem / store.

Posts to VAULT_AUDIT_CHANNEL_ID. Discord actions go up immediately.
Isle recap.ndjson and results.ndjson are tailed so in-game outcomes
(saved, redeem fail, inject message) show up for research.
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

VAULT_AUDIT_CHANNEL_ID = 1543246319999516692
STATE_PATH = "primeval_vault_audit.json"
LOG_PATH = "primeval_vault_audit.ndjson"
RESULTS_PATH = "/TheIsle/Binaries/Win64/ue4ss/Mods/PrimevalRedeem/Saved/results.ndjson"
RECAP_LOG = "/TheIsle/Binaries/Win64/ue4ss/Mods/PrimevalRedeem/Saved/recap.ndjson"
TICK_SECONDS = 10
STEAM_RE = re.compile(r"7656\d{13}")

RESULT_VERBS = {
    "store",
    "safelog",
    "redeem",
    "cancelstore",
    "apply",
    "grow",
}

KIND_META = {
    "buy": ("🪙 Buy", discord.Color.gold()),
    "buy_fail": ("🪙 Buy failed", discord.Color.red()),
    "redeem": ("♻️ Redeem", discord.Color.green()),
    "redeem_fail": ("♻️ Redeem failed", discord.Color.red()),
    "store": ("📦 Store", discord.Color.blurple()),
    "store_fail": ("📦 Store failed", discord.Color.red()),
    "return_store": ("📦 Return store", discord.Color.teal()),
    "return_store_fail": ("📦 Return store failed", discord.Color.red()),
    "cancelstore": ("📦 Store cancelled", discord.Color.dark_grey()),
    "isle": ("🦕 Isle result", discord.Color.greyple()),
    "trade": ("🃏 Trade", discord.Color.gold()),
    "trade_fail": ("🃏 Trade failed", discord.Color.red()),
    "gamble": ("🎲 Gamble", discord.Color.purple()),
    "gamble_fail": ("🎲 Gamble failed", discord.Color.red()),
    "gamble_test": ("🧪 Gamble test grant", discord.Color.orange()),
    "revive": ("💖 Recovery credit", discord.Color.green()),
    "revive_fail": ("💖 Recovery failed", discord.Color.red()),
    "giveaway": ("🎁 Free-player giveaway", discord.Color.gold()),
    "giveaway_fail": ("🎁 Giveaway failed", discord.Color.red()),
}

_task = None
_STATE_LOCK = asyncio.Lock()
_recent = []


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


def _who(steam="", discord_ids=None, user=None):
    ids = []
    seen = set()
    if user is not None:
        uid = str(getattr(user, "id", "") or "")
        if uid.isdigit():
            ids.append(uid)
            seen.add(uid)
            if not steam:
                steam = _steam_for_discord(uid)
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
        mentions = "unlinked / in-game"
        disc = "`not linked`"
    steam_txt = f"`{steam}`" if steam else "`unknown`"
    return f"{mentions}\nDiscord {disc}\nSteam {steam_txt}"


def _remember_progress(event):
    steam = str((event or {}).get("steam") or "").strip()
    if not steam or not (event or {}).get("ok"):
        return
    verb = str((event or {}).get("verb") or "").lower().strip()
    try:
        import primeval_qol

        if verb in ("store", "buy", "safelog"):
            primeval_qol.mark_progress(steam, seen=True, stored=True)
        elif verb in ("redeem", "apply"):
            primeval_qol.mark_progress(steam, seen=True, redeemed=True)
    except Exception:
        pass


def _seen_recently(finger):
    now = int(time.time())
    global _recent
    _recent = [row for row in _recent if now - row[0] < 20]
    for ts, key in _recent:
        if key == finger:
            return True
    _recent.append((now, finger))
    if len(_recent) > 300:
        _recent = _recent[-220:]
    return False


def _kind_for(verb, ok=True):
    verb = str(verb or "").lower()
    if verb in ("buy", "buy_fail"):
        return "buy" if ok else "buy_fail"
    if verb == "redeem":
        return "redeem" if ok else "redeem_fail"
    if verb in ("store", "safelog"):
        return "store" if ok else "store_fail"
    if verb in ("return_store", "restore_store", "vault_recover"):
        return "return_store" if ok else "return_store_fail"
    if verb in ("cancelstore", "cancelled", "armed_lost"):
        return "cancelstore"
    if verb in ("failed",):
        return "store_fail"
    if verb in (
        "list",
        "unlist",
        "trade",
        "trade_offer",
        "trade_decline",
        "listing_offer",
        "listing_offer_decline",
    ):
        return "trade" if ok else "trade_fail"
    if verb == "gamble_test":
        return "gamble_test" if ok else "gamble_fail"
    if verb == "gamble":
        return "gamble" if ok else "gamble_fail"
    if verb in ("revive", "recovery"):
        return "revive" if ok else "revive_fail"
    if verb in ("giveaway", "free_giveaway"):
        return "giveaway" if ok else "giveaway_fail"
    return "isle"


def build_embed(event):
    verb = str(event.get("verb") or "vault")
    ok = event.get("ok") is not False
    kind = event.get("kind") or _kind_for(verb, ok)
    title, color = KIND_META.get(kind, KIND_META["isle"])
    source = str(event.get("source") or "discord")
    embed = discord.Embed(
        title=title,
        description=f"**{verb}** · {source}",
        color=color,
        timestamp=datetime.fromtimestamp(int(event.get("at") or time.time()), timezone.utc),
    )
    embed.add_field(
        name="Player",
        value=_who(event.get("steam"), event.get("discord_ids"), event.get("user"))[:1024],
        inline=False,
    )
    details = []
    for label, key in (
        ("Species", "species"),
        ("Gender", "gender"),
        ("Growth", "growth"),
        ("Slot", "slot"),
        ("Cards", "cards"),
        ("This life", "life"),
        ("Inherited", "inherited"),
        ("Mutations", "mutations"),
        ("Chance", "chance"),
        ("Roll", "roll"),
        ("Prize", "prize"),
        ("Tokens", "tokens"),
        ("Credits left", "revives"),
        ("Balance", "balance"),
        ("Refunded", "refunded"),
        ("Prime", "prime"),
    ):
        val = event.get(key)
        if val is None or val == "":
            continue
        details.append(f"**{label}:** {val}")
    msg = str(event.get("msg") or "").strip()
    if msg:
        details.append(msg[:900])
    if details:
        embed.add_field(name="Details", value="\n".join(details)[:1024], inline=False)
    extra = str(event.get("extra") or "").strip()
    if extra:
        embed.add_field(name="Log", value=f"`{extra[:1000]}`", inline=False)
    embed.set_footer(text="Fallen Earth vault audit")
    return embed


async def _channel(bot):
    channel = bot.get_channel(VAULT_AUDIT_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(VAULT_AUDIT_CHANNEL_ID)
        except Exception as exc:
            print(f"[VAULT AUDIT] channel missing: {exc}")
            return None
    return channel


async def post(bot, event):
    finger = "|".join(
        [
            str(event.get("verb") or ""),
            str(event.get("steam") or ""),
            str(event.get("slot") or ""),
            str(event.get("msg") or "")[:80],
            str(event.get("source") or ""),
        ]
    )
    if _seen_recently(finger):
        return False
    channel = await _channel(bot)
    if channel is None:
        return False
    try:
        await channel.send(embed=build_embed(event))
        return True
    except Exception as exc:
        print(f"[VAULT AUDIT] post failed: {exc}")
        return False


def _append_local(event):
    row = {}
    for key, val in (event or {}).items():
        if key == "user":
            continue
        if isinstance(val, (str, int, float, bool)) or val is None:
            row[key] = val
        else:
            row[key] = str(val)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=True) + "\n")
    except Exception as exc:
        print(f"[VAULT AUDIT] local log failed: {exc}")


async def note(bot, verb, user=None, steam="", ok=True, **fields):
    steam = str(steam or "").strip()
    if user is not None and not steam:
        steam = _steam_for_discord(getattr(user, "id", None))
    event = {
        "at": int(time.time()),
        "verb": verb,
        "ok": bool(ok),
        "kind": _kind_for(verb, ok),
        "source": fields.pop("source", "discord"),
        "steam": steam,
        "user": user,
        "discord_ids": [str(getattr(user, "id", ""))] if user is not None else [],
        "msg": str(fields.pop("msg", "") or ""),
        "extra": str(fields.pop("extra", "") or ""),
    }
    event.update(fields)
    _remember_progress(event)
    _append_local(event)
    try:
        await post(bot, event)
    except Exception as exc:
        print(f"[VAULT AUDIT] note failed: {exc}")


async def _read_remote(path):
    import primeval_isle

    status, text = await primeval_isle.read_file(path)
    if status != 200:
        return ""
    return str(text or "")


def _parse_result_line(line):
    try:
        row = json.loads(line)
    except Exception:
        return None
    if not isinstance(row, dict):
        return None
    verb = str(row.get("verb") or "").lower().strip()
    if verb not in RESULT_VERBS:
        return None
    ok = row.get("ok") is not False
    msg = str(row.get("msg") or "").strip()
    return {
        "at": int(row.get("ts") or time.time()),
        "verb": verb,
        "ok": ok,
        "kind": _kind_for(verb, ok),
        "source": "isle",
        "steam": str(row.get("steam") or "").strip(),
        "msg": msg,
        "extra": str(row.get("id") or ""),
    }


def _parse_recap_line(line):
    try:
        row = json.loads(line)
    except Exception:
        return None
    if not isinstance(row, dict):
        return None
    status = str(row.get("status") or "").lower().strip()
    if not status:
        return None
    ok = status in ("saved", "ok", "success")
    verb = "store"
    if status in ("cancelled", "canceled"):
        verb = "cancelstore"
    elif status == "armed_lost":
        verb = "cancelstore"
        ok = False
    elif not ok:
        verb = "store"
    growth = row.get("growth")
    growth_txt = ""
    try:
        if growth is not None:
            growth_txt = f"{float(growth) * 100:.0f}%"
    except Exception:
        growth_txt = str(growth)
    prime = ""
    if row.get("primeHave") not in (None, ""):
        prime = f"{row.get('primeHave')}/10"
        flags = str(row.get("primeFlags") or "").strip()
        if flags:
            prime = f"{prime} `{flags}`"
    detail = str(row.get("detail") or "").strip()
    nxt = str(row.get("next") or "").strip()
    msg = " · ".join(p for p in (status, detail, nxt) if p)
    return {
        "at": int(row.get("capturedAt") or time.time()),
        "verb": verb,
        "ok": ok,
        "kind": _kind_for(status if status in ("failed", "cancelled", "armed_lost") else verb, ok),
        "source": "isle recap",
        "steam": str(row.get("steam") or "").strip(),
        "species": row.get("species") or "",
        "gender": row.get("gender") or "",
        "growth": growth_txt,
        "prime": prime,
        "msg": msg,
    }


async def _poll_chunk(bot, path, state, key, parser, skip_existing=True):
    text = await _read_remote(path)
    if not text:
        return 0
    if key not in state:
        state[key] = len(text) if skip_existing else 0
        if skip_existing:
            return 0
    seen = int(state.get(key) or 0)
    if len(text) < seen:
        seen = 0
    chunk = text[seen:]
    state[key] = len(text)
    posted = 0
    for line in chunk.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = parser(line)
        except Exception:
            event = None
        if not event:
            continue
        _remember_progress(event)
        _append_local(event)
        if await post(bot, event):
            posted += 1
    return posted


async def tick(bot):
    async with _STATE_LOCK:
        state = _load_state()
        try:
            await _poll_chunk(bot, RESULTS_PATH, state, "results_bytes", _parse_result_line)
        except Exception as exc:
            print(f"[VAULT AUDIT] results poll failed: {exc}")
        try:
            await _poll_chunk(bot, RECAP_LOG, state, "recap_bytes", _parse_recap_line)
        except Exception as exc:
            print(f"[VAULT AUDIT] recap poll failed: {exc}")
        _save_state(state)


def start(bot):
    global _task
    if _task is not None and _task.is_running():
        return _task

    @tasks.loop(seconds=TICK_SECONDS)
    async def vault_audit_tick():
        try:
            await tick(bot)
        except Exception as exc:
            print(f"[VAULT AUDIT] tick failed: {exc}")

    @vault_audit_tick.before_loop
    async def _wait_ready():
        await bot.wait_until_ready()
        await asyncio.sleep(8)

    _task = vault_audit_tick
    vault_audit_tick.start()
    print("[VAULT AUDIT] buy/redeem/store trail started")
    return vault_audit_tick
