"""Patreon shop sync for Primeval Island.

Patreon Discord role sync tells the bot who is in Hatchling / Juvenile / Apex.
The bot then:

- credits first-join bonus + the first bi-weekly token drop immediately
- credits the bi-weekly drop every 14 days while the role is still present
- credits a free 2-token stipend every 14 days to every Steam-linked member
- credits one-time Extra hosting gifts from Patreon charge polling
  (HTTPS webhooks are optional; Patreon rejects http:// URLs),
  not from Discord roles. Each grant is posted to a staff ledger channel.

Members must: join a tier or buy a gift on Patreon, connect Discord on Patreon,
and link Steam in Discord. Tokens land on the Steam wallet.

Hook from Primeval_Island_Bot.py:

    import primeval_patreon
    primeval_patreon.bind(globals())
    primeval_patreon.attach(bot)
    primeval_patreon.register_views(bot)
    primeval_patreon.register_slash(bot)
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
import aiohttp
import discord
from discord import app_commands, ui

from primeval_panels import deny_staff, staff_rank

G = {}

STATE_PATH = "primeval_patreon.json"
TOKEN_PATH = "patreon_creator_token.txt"
SECRET_PATH = "patreon_webhook_secret.txt"
PATREON_URL = "https://www.patreon.com/c/PrimevalIsland"
PATREON_API = "https://www.patreon.com/api/oauth2/v2"
CAMPAIGN_ID = os.environ.get("PATREON_CAMPAIGN_ID", "16666253")
WEBHOOK_PORT = int(os.environ.get("PATREON_WEBHOOK_PORT", "8095") or "8095")
POLL_SECONDS = 300
BIWEEK_SECONDS = 14 * 24 * 60 * 60
TICK_SECONDS = 120
FREE_TOKENS = 2

MISSION = (
    "Fallen Earth is a community The Isle server built on three promises: "
    "**fun**, **inclusivity**, and **fairness above all**. Everyone is welcome. "
    "The same rules bind every player, including staff. Donations keep the island "
    "alive -- they never buy rank, immunity, or a pass to break the rules. "
    "Admin abuse is not a perk. It is a ban."
)

WHY_DONATE = (
    "This island is not a product. It is a living world we keep running for "
    "people who still want a fair hunt. Every dollar goes to **server rent**, "
    "**uptime and maintenance**, and **building what comes next** -- better bots, "
    "smoother tools, and the projects that make Primeval actually feel like a "
    "home instead of another empty Evrima box.\n\n"
    "If you have ever queued for a full server, gotten a ticket answered, used "
    "the shop, or just wanted a place that does not sell staff ranks, you have "
    "already used what donations pay for. Help keep the lights on. Help us "
    "build more. Play for free either way -- but if the island has given you "
    "something, this is how you give it back."
)

DISCLAIMER = (
    "These are **voluntary donation tiers**, not a store selling The Isle. "
    "Afterthought LLC owns The Isle. We are not affiliated with or endorsed by "
    "them. Donations fund **server rent**, **maintenance**, **further community "
    "projects**, and **bot development**. Thank-you credits have "
    "**no cash value**, are not a purchase of Afterthought content, and can never "
    "unlock a species or power that free players cannot also obtain by playing. "
    "Donors follow the exact same rules as everyone else."
)

ROLE_HATCHLING = "Hatchling"
ROLE_JUVENILE = "Juvenile"
ROLE_APEX = "Apex"
ROLE_TOPUP_5 = "Token Top-up 5"
ROLE_TOPUP_15 = "Token Top-up 15"

TIERS = {
    "hatchling": {
        "role": ROLE_HATCHLING,
        "price": 3,
        "tokens": 4,
        "bonus": 8,
        "revives": 1,
        "pop_cap": False,
        "label": "Hatchling donation",
        "color": discord.Color.from_rgb(120, 180, 90),
    },
    "juvenile": {
        "role": ROLE_JUVENILE,
        "price": 5,
        "tokens": 10,
        "bonus": 10,
        "revives": 2,
        "pop_cap": False,
        "label": "Juvenile donation",
        "color": discord.Color.from_rgb(80, 160, 210),
    },
    "apex": {
        "role": ROLE_APEX,
        "price": 10,
        "tokens": 15,
        "bonus": 15,
        "revives": 4,
        "pop_cap": False,
        "label": "Apex donation",
        "color": discord.Color.from_rgb(210, 150, 70),
    },
}

TOPUPS = {
    "top5": {"role": ROLE_TOPUP_5, "tokens": 5, "price": 5, "cents": 500},
    "top15": {"role": ROLE_TOPUP_15, "tokens": 15, "price": 15, "cents": 1500},
}

CENTS_TO_TOPUP = {500: "top5", 1500: "top15"}
PAID_STATUSES = {"paid"}
FAIL_STATUSES = {"declined", "fraud", "refunded", "refunded by patreon", "refund pending", "deleted"}

_ATTACHED = set()
_HTTP_STARTED = set()
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
            data.setdefault("members", {})
            data.setdefault("topups", {})
            data.setdefault("role_ids", {})
            data.setdefault("ledger", {})
            data.setdefault("marks", {})
            data.setdefault("settings", {})
            data.setdefault("free", {})
            return data
    except Exception:
        pass
    return {
        "members": {},
        "topups": {},
        "role_ids": {},
        "ledger": {},
        "marks": {},
        "settings": {},
        "free": {},
    }


def _save(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)
        handle.write("\n")
    os.replace(tmp, STATE_PATH)


def _read_secret_file(path, env_name):
    value = (os.environ.get(env_name) or "").strip()
    if value:
        return value
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except Exception:
        return ""


def _creator_token():
    return _read_secret_file(TOKEN_PATH, "PATREON_CREATOR_TOKEN")


def _webhook_secret():
    return _read_secret_file(SECRET_PATH, "PATREON_WEBHOOK_SECRET")


def _api_enabled():
    return bool(_creator_token())


def _new_ledger_id(state):
    n = 0
    while True:
        lid = f"p{int(time.time()):x}{n}"[-12:]
        if lid not in state.get("ledger", {}):
            return lid
        n += 1


def _log_channel_id(state=None):
    state = state or _load()
    raw = (state.get("settings") or {}).get("log_channel_id") or os.environ.get("PATREON_LOG_CHANNEL_ID")
    try:
        return int(raw or 0)
    except Exception:
        return 0


async def _log_channel(bot, state=None):
    cid = _log_channel_id(state)
    if not cid:
        return None
    channel = bot.get_channel(cid)
    if channel:
        return channel
    try:
        return await bot.fetch_channel(cid)
    except Exception:
        return None


def _steam_for(user_id):
    getter = _fn("get_linked_steam_id")
    if not getter or not user_id:
        return None
    try:
        steam = getter(int(user_id))
    except Exception:
        steam = None
    return str(steam).strip() if steam else None


def _credit(steam, amount, reason, user_id=None):
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
    result = None
    if credit:
        try:
            result = credit(steam, uid, amount)
        except TypeError:
            try:
                result = credit(steam, amount, reason)
            except TypeError:
                try:
                    result = credit(steam, amount)
                except Exception as exc:
                    print("[PATREON] credit failed", reason, exc)
                    credit = None
        except Exception as exc:
            print("[PATREON] credit failed", reason, exc)
            return False, 0
        if credit and result is not False and result is not None:
            if isinstance(result, (int, float)):
                return True, int(result)
            if getter:
                try:
                    return True, int(getter(steam) or 0)
                except Exception:
                    return True, 0
            return True, 0
    if not getter or not setter:
        print("[PATREON] no steam wallet credit function bound")
        return False, 0
    try:
        current = int(getter(steam) or 0)
    except Exception as exc:
        print("[PATREON] balance read failed", exc)
        return False, 0
    target = current + amount
    try:
        result = setter(steam, uid, target)
    except TypeError:
        try:
            result = setter(steam, target)
        except Exception as exc:
            print("[PATREON] credit failed", reason, exc)
            return False, 0
    except Exception as exc:
        print("[PATREON] credit failed", reason, exc)
        return False, 0
    if result is False or result is None:
        return False, 0
    if isinstance(result, (int, float)):
        return True, int(result)
    return True, target


def _status_norm(value):
    return str(value or "").strip().lower()


def _discord_id_from_user(user_obj):
    if not isinstance(user_obj, dict):
        return ""
    attrs = user_obj.get("attributes") or user_obj
    social = attrs.get("social_connections") or {}
    discord_info = social.get("discord") or {}
    uid = discord_info.get("user_id") or discord_info.get("id")
    return str(uid).strip() if uid else ""


def _included_by_type(payload):
    out = {}
    for item in payload.get("included") or []:
        if not isinstance(item, dict):
            continue
        out.setdefault(item.get("type"), {})[str(item.get("id"))] = item
    return out


def _member_snapshot(member_obj, included):
    attrs = member_obj.get("attributes") or {}
    rel = ((member_obj.get("relationships") or {}).get("user") or {}).get("data") or {}
    user_id = str(rel.get("id") or "")
    users = included.get("user") or {}
    user = users.get(user_id) or {}
    return {
        "member_id": str(member_obj.get("id") or ""),
        "patreon_user_id": user_id,
        "discord_id": _discord_id_from_user(user),
        "lifetime": int(attrs.get("lifetime_support_cents") or 0),
        "entitled": int(attrs.get("currently_entitled_amount_cents") or 0),
        "last_charge_date": str(attrs.get("last_charge_date") or ""),
        "last_charge_status": str(attrs.get("last_charge_status") or ""),
        "next_charge_date": str(attrs.get("next_charge_date") or ""),
        "patron_status": str(attrs.get("patron_status") or ""),
    }


def _classify_charge(prev, snap):
    status = _status_norm(snap.get("last_charge_status"))
    delta = int(snap.get("lifetime") or 0) - int((prev or {}).get("lifetime") or 0)
    charge_changed = snap.get("last_charge_date") != (prev or {}).get("last_charge_date")
    status_changed = status != _status_norm((prev or {}).get("last_charge_status"))
    next_moved = bool(snap.get("next_charge_date")) and snap.get("next_charge_date") != (prev or {}).get(
        "next_charge_date"
    )
    if not charge_changed and not status_changed and delta <= 0:
        return None
    if status in FAIL_STATUSES:
        return {
            "kind": "failed_charge",
            "status": "declined" if status == "declined" else "failed",
            "cents": max(delta, 0),
            "tokens": 0,
            "note": f"Patreon last_charge_status={snap.get('last_charge_status')}",
        }
    if status and status not in PAID_STATUSES and not delta:
        return None
    if delta <= 0:
        return None
    entitled = int(snap.get("entitled") or 0)
    membership_like = entitled > 0 and abs(delta - entitled) <= 25 and next_moved
    top_key = CENTS_TO_TOPUP.get(delta)
    if membership_like:
        return {
            "kind": "membership_charge",
            "status": "logged",
            "cents": delta,
            "tokens": 0,
            "note": "Monthly membership charge (roles still handle bi-weekly tokens).",
        }
    if top_key:
        pack = TOPUPS[top_key]
        return {
            "kind": top_key,
            "status": "pending",
            "cents": delta,
            "tokens": int(pack["tokens"]),
            "note": f"Extra hosting gift ${pack['price']} detected from lifetime cents.",
        }
    return {
        "kind": "unknown_charge",
        "status": "needs_review",
        "cents": delta,
        "tokens": 0,
        "note": f"Unmatched +{delta} cents. Staff must map this to 5 or 15 tokens or ignore.",
    }


def _ledger_embed(row):
    color = {
        "credited": discord.Color.green(),
        "pending": discord.Color.orange(),
        "needs_review": discord.Color.gold(),
        "failed": discord.Color.red(),
        "declined": discord.Color.dark_red(),
        "logged": discord.Color.blurple(),
        "skipped": discord.Color.dark_grey(),
    }.get(row.get("status"), discord.Color.light_grey())
    discord_id = row.get("discord_id") or ""
    who = f"<@{discord_id}>" if discord_id else "Discord not connected on Patreon"
    steam = row.get("steam") or "Steam not linked in Discord"
    embed = discord.Embed(
        title=f"Patreon ledger `{row.get('id')}`",
        description=row.get("note") or "",
        color=color,
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Status", value=str(row.get("status") or "unknown"), inline=True)
    embed.add_field(name="Kind",         value=str(row.get("kind") or "-"), inline=True)
    embed.add_field(name="Source", value=str(row.get("source") or "-"), inline=True)
    embed.add_field(name="Discord", value=who, inline=True)
    embed.add_field(name="Steam", value=f"`{steam}`" if steam and " " not in str(steam) else str(steam), inline=True)
    embed.add_field(
        name="Amount",
        value=f"{int(row.get('cents') or 0)} cents -> {int(row.get('tokens') or 0)} tokens",
        inline=True,
    )
    embed.add_field(name="Patreon user", value=str(row.get("patreon_user_id") or "-"), inline=True)
    embed.add_field(name="Member id", value=str(row.get("patreon_member_id") or "-"), inline=True)
    embed.add_field(name="Charge", value=str(row.get("last_charge_date") or "-")[:32], inline=True)
    embed.add_field(name="Charge status", value=str(row.get("last_charge_status") or "-"), inline=True)
    if row.get("error"):
        embed.add_field(name="Error", value=str(row["error"])[:500], inline=False)
    if row.get("staff_id"):
        embed.add_field(name="Staff", value=f"<@{row['staff_id']}>", inline=True)
    embed.set_footer(text="Search this channel for the ledger id. Do not credit twice.")
    return embed


class PatreonLedgerButton(ui.DynamicItem[ui.Button], template=r"pi_pat:(?P<act>credit|top5|top15|skip):(?P<lid>[a-z0-9]+)"):
    def __init__(self, ledger_id, act):
        labels = {
            "credit": ("Credit now", discord.ButtonStyle.success),
            "top5": ("Treat as $5 gift", discord.ButtonStyle.primary),
            "top15": ("Treat as $15 gift", discord.ButtonStyle.primary),
            "skip": ("Ignore", discord.ButtonStyle.secondary),
        }
        label, style = labels[act]
        super().__init__(
            ui.Button(label=label, style=style, custom_id=f"pi_pat:{act}:{ledger_id}")
        )
        self.ledger_id = ledger_id
        self.act = act

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["lid"], match["act"])

    async def callback(self, interaction: discord.Interaction):
        if staff_rank(interaction.user) < 2:
            await interaction.response.send_message("Moderator or higher.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        async with _STATE_LOCK:
            state = _load()
            row = (state.get("ledger") or {}).get(self.ledger_id)
            if not row:
                await interaction.followup.send("Ledger row missing.", ephemeral=True)
                return
            # stale buttons on an already-settled row must not re-credit it
            if row.get("status") in ("credited", "skipped"):
                await interaction.followup.send(
                    f"`{self.ledger_id}` is already **{row.get('status')}**.", ephemeral=True
                )
                return
            row["staff_id"] = interaction.user.id
            if self.act == "skip":
                row["status"] = "skipped"
                row["note"] = (row.get("note") or "") + " Staff ignored this charge."
            elif self.act in ("top5", "top15"):
                pack = TOPUPS[self.act]
                row["kind"] = self.act
                row["tokens"] = int(pack["tokens"])
                row["cents"] = int(pack["cents"])
                row["status"] = "pending"
                row["note"] = f"Staff mapped to Extra hosting gift ${pack['price']}."
                await _try_credit_ledger(interaction.client, state, row)
            else:
                await _try_credit_ledger(interaction.client, state, row)
            state["ledger"][self.ledger_id] = row
            _save(state)
        await _refresh_ledger_message(interaction.client, row)
        await interaction.followup.send(f"`{self.ledger_id}` is now **{row.get('status')}**.", ephemeral=True)


def _ledger_view(row):
    view = ui.View(timeout=None)
    lid = row.get("id")
    status = row.get("status")
    if status in ("pending", "failed", "needs_review") and int(row.get("tokens") or 0) > 0:
        view.add_item(PatreonLedgerButton(lid, "credit"))
    if status == "needs_review":
        view.add_item(PatreonLedgerButton(lid, "top5"))
        view.add_item(PatreonLedgerButton(lid, "top15"))
    if status in ("pending", "failed", "needs_review"):
        view.add_item(PatreonLedgerButton(lid, "skip"))
    return view


async def _refresh_ledger_message(bot, row):
    channel = await _log_channel(bot)
    if not channel or not row.get("log_message_id"):
        return
    try:
        msg = await channel.fetch_message(int(row["log_message_id"]))
        await msg.edit(embed=_ledger_embed(row), view=_ledger_view(row))
    except Exception:
        pass


async def _post_ledger(bot, state, row):
    channel = await _log_channel(bot, state)
    if not channel:
        print("[PATREON] ledger", row.get("id"), row.get("status"), "no log channel")
        return
    try:
        msg = await channel.send(embed=_ledger_embed(row), view=_ledger_view(row))
        row["log_message_id"] = msg.id
        row["log_channel_id"] = channel.id
    except Exception as exc:
        print("[PATREON] ledger post failed", exc)


async def _try_credit_ledger(bot, state, row):
    tokens = int(row.get("tokens") or 0)
    if tokens <= 0:
        return
    if row.get("status") == "credited":
        return
    discord_id = row.get("discord_id")
    steam = row.get("steam") or (_steam_for(discord_id) if discord_id else None)
    if steam:
        row["steam"] = steam
    if not discord_id:
        row["status"] = "pending"
        row["error"] = "Patron has not connected Discord on Patreon."
        return
    if not steam:
        row["status"] = "pending"
        row["error"] = "Discord is connected, but Steam is not linked in this server."
        return
    ok, bal = _credit(steam, tokens, f"patreon ledger {row.get('id')}", discord_id)
    if not ok:
        row["status"] = "failed"
        row["error"] = "Steam wallet credit failed."
        return
    row["status"] = "credited"
    row["credited_at"] = int(time.time())
    row["error"] = ""
    row["note"] = (row.get("note") or "") + f" Credited {tokens} tokens (balance {bal})."
    await _dm(
        bot,
        discord_id,
        f"Extra hosting gift applied: **{tokens}** tokens. Balance: **{bal}**. Ledger `{row.get('id')}`.",
    )


async def ingest_member_snapshot(bot, snap, source, event=""):
    if not snap.get("patreon_user_id") and not snap.get("member_id"):
        return None
    async with _STATE_LOCK:
        return await _ingest_member_snapshot_unlocked(bot, snap, source, event)


async def _ingest_member_snapshot_unlocked(bot, snap, source, event=""):
    if not snap.get("patreon_user_id") and not snap.get("member_id"):
        return None
    state = _load()
    key = snap.get("patreon_user_id") or snap.get("member_id")
    marks = state.setdefault("marks", {})
    prev = marks.get(key)
    if prev is None:
        marks[key] = {
            "lifetime": snap.get("lifetime") or 0,
            "last_charge_date": snap.get("last_charge_date") or "",
            "last_charge_status": snap.get("last_charge_status") or "",
            "next_charge_date": snap.get("next_charge_date") or "",
            "entitled": snap.get("entitled") or 0,
            "discord_id": snap.get("discord_id") or "",
        }
        _save(state)
        return None
    classified = _classify_charge(prev, snap)
    marks[key] = {
        "lifetime": snap.get("lifetime") or 0,
        "last_charge_date": snap.get("last_charge_date") or "",
        "last_charge_status": snap.get("last_charge_status") or "",
        "next_charge_date": snap.get("next_charge_date") or "",
        "entitled": snap.get("entitled") or 0,
        "discord_id": snap.get("discord_id") or prev.get("discord_id") or "",
    }
    if not classified:
        _save(state)
        return None
    fingerprint = f"{key}:{snap.get('last_charge_date')}:{classified['cents']}:{classified['kind']}"
    for existing in (state.get("ledger") or {}).values():
        if existing.get("fingerprint") == fingerprint:
            _save(state)
            return existing
    row = {
        "id": _new_ledger_id(state),
        "fingerprint": fingerprint,
        "source": source,
        "event": event,
        "kind": classified["kind"],
        "status": classified["status"],
        "note": classified["note"],
        "cents": classified["cents"],
        "tokens": classified["tokens"],
        "patreon_user_id": snap.get("patreon_user_id") or "",
        "patreon_member_id": snap.get("member_id") or "",
        "discord_id": snap.get("discord_id") or prev.get("discord_id") or "",
        "steam": _steam_for(snap.get("discord_id") or prev.get("discord_id")) if (snap.get("discord_id") or prev.get("discord_id")) else "",
        "last_charge_date": snap.get("last_charge_date") or "",
        "last_charge_status": snap.get("last_charge_status") or "",
        "created_at": int(time.time()),
        "error": "",
    }
    if row["status"] == "pending":
        await _try_credit_ledger(bot, state, row)
    state.setdefault("ledger", {})[row["id"]] = row
    # persist right after crediting so a later failure cannot cause a re-credit
    _save(state)
    await _post_ledger(bot, state, row)
    _save(state)
    return row


async def _patreon_get(session, path, token, params=None):
    url = path if str(path).startswith("http") else f"{PATREON_API}{path}"
    headers = {"Authorization": f"Bearer {token}", "User-Agent": "PrimevalIslandBot"}
    async with session.get(url, headers=headers, params=params) as resp:
        text = await resp.text()
        if resp.status != 200:
            raise RuntimeError(f"Patreon GET {resp.status}: {text[:200]}")
        return json.loads(text)


async def poll_patreon_members(bot):
    token = _creator_token()
    if not token:
        return
    params = {
        "include": "user,currently_entitled_tiers",
        "fields[member]": "last_charge_date,last_charge_status,lifetime_support_cents,currently_entitled_amount_cents,patron_status,next_charge_date",
        "fields[user]": "social_connections",
        "page[count]": "100",
    }
    path = f"/campaigns/{CAMPAIGN_ID}/members"
    async with aiohttp.ClientSession() as session:
        while path:
            payload = await _patreon_get(session, path, token, params if "?" not in str(path) else None)
            included = _included_by_type(payload)
            for member_obj in payload.get("data") or []:
                if member_obj.get("type") != "member":
                    continue
                snap = _member_snapshot(member_obj, included)
                try:
                    await ingest_member_snapshot(bot, snap, "poll")
                except Exception as exc:
                    print("[PATREON] ingest failed", exc)
            nxt = ((payload.get("links") or {}).get("next") or "")
            path = nxt
            params = None
            await asyncio.sleep(1)
    async with _STATE_LOCK:
        state = _load()
        dirty = False
        for row in (state.get("ledger") or {}).values():
            if row.get("status") != "pending":
                continue
            if row.get("discord_id") and not row.get("steam"):
                row["steam"] = _steam_for(row["discord_id"]) or ""
            before = row.get("status")
            err = row.get("error")
            await _try_credit_ledger(bot, state, row)
            if row.get("status") != before or row.get("error") != err:
                dirty = True
                await _refresh_ledger_message(bot, row)
        if dirty:
            _save(state)


def _verify_patreon_sig(body, signature, secret):
    if not secret or not signature:
        return False
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.md5).hexdigest()
    return hmac.compare_digest(digest, str(signature).strip())


async def handle_patreon_webhook(bot, body, headers):
    secret = _webhook_secret()
    sig = headers.get("X-Patreon-Signature") or headers.get("x-patreon-signature")
    if not _verify_patreon_sig(body, sig, secret):
        return 401, {"ok": False, "error": "bad signature"}
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return 400, {"ok": False, "error": "invalid json"}
    event = headers.get("X-Patreon-Event") or headers.get("x-patreon-event") or ""
    data = payload.get("data") or {}
    included = _included_by_type(payload)
    if data.get("type") == "member":
        snap = _member_snapshot(data, included)
        row = await ingest_member_snapshot(bot, snap, "webhook", event)
        return 200, {"ok": True, "ledger": (row or {}).get("id")}
    return 200, {"ok": True, "ignored": data.get("type") or "none"}


async def start_webhook_http(bot):
    if not _webhook_secret():
        print("[PATREON] no webhook secret; HTTPS Patreon webhooks skipped (poll handles gifts)")
        return
    if id(bot) in _HTTP_STARTED:
        return
    from aiohttp import web

    async def health(_request):
        return web.json_response({"ok": True, "service": "primeval-patreon"})

    async def webhook(request):
        body = await request.read()
        headers = request.headers
        try:
            status, payload = await handle_patreon_webhook(bot, body, headers)
        except Exception as exc:
            print("[PATREON] webhook handler failed", exc)
            return web.json_response({"ok": False}, status=500)
        return web.json_response(payload, status=status)

    app = web.Application()
    app.router.add_get("/patreon/health", health)
    app.router.add_post("/patreon/webhook", webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT)
        await site.start()
    except OSError as exc:
        print("[PATREON] webhook port", WEBHOOK_PORT, "failed", exc)
        return
    _HTTP_STARTED.add(id(bot))
    print(f"[PATREON] webhook listening on 0.0.0.0:{WEBHOOK_PORT}/patreon/webhook")


def _role_on(member, name):
    want = str(name).strip().lower()
    for role in getattr(member, "roles", []) or []:
        if str(role.name).strip().lower() == want:
            return True
    return False


def _active_tier_key(member):
    for key in ("apex", "juvenile", "hatchling"):
        if _role_on(member, TIERS[key]["role"]):
            return key
    return None


def revives_left_for(member, state=None):
    if member is None or not isinstance(member, discord.Member):
        return 0
    if not _active_tier_key(member):
        return 0
    state = state or _load()
    row = (state.get("members") or {}).get(str(member.id), {})
    try:
        return max(0, int(row.get("revives_left") or 0))
    except Exception:
        return 0


def perk_status_text(member, steam):
    tier = _active_tier_key(member) if isinstance(member, discord.Member) else None
    label = TIERS[tier]["label"] if tier else "free player"
    left = revives_left_for(member)
    lines = [
        f"**{label}** · Steam `{steam}`",
        (
            f"Recovery credits: **{left}** left this month"
            if tier
            else "Recovery credits: **0** — Hatchling / Juvenile / Apex donations grant these"
        ),
    ]
    if tier:
        lines.append("Use **Use recovery credit** while downed in-game. Standing up may still spend the credit.")
    return "\n".join(lines)


def _revive_failed(result):
    low = str(result or "").strip().lower()
    if low in ("no rcon", "none"):
        return True
    return any(bit in low for bit in ("not found", "unknown player", "invalid", "failed", "error"))


async def _audit_perk(interaction, verb, steam="", ok=True, **fields):
    try:
        import primeval_vault_audit

        await primeval_vault_audit.note(
            interaction.client,
            verb,
            interaction.user,
            steam=steam,
            ok=ok,
            **fields,
        )
    except Exception as exc:
        print("[VAULT AUDIT] perk note failed", exc)


async def use_recovery_credit(interaction):
    member = interaction.user
    if interaction.guild and not isinstance(member, discord.Member):
        member = interaction.guild.get_member(interaction.user.id) or member
    if not isinstance(member, discord.Member):
        await interaction.followup.send("Use this in the Fallen Earth server.", ephemeral=True)
        return
    steam = _steam_for(member.id)
    if not steam:
        await interaction.followup.send(
            "Link Steam first (`Link Steam` on the member panel).",
            ephemeral=True,
        )
        return
    tier = _active_tier_key(member)
    if not tier:
        await _audit_perk(
            interaction, "revive", steam=steam, ok=False,
            msg="No donation tier — recovery credit refused",
        )
        await interaction.followup.send(
            "Recovery credits come with Hatchling / Juvenile / Apex donations. "
            "They revive **you** — they do not make a stronger dino.",
            ephemeral=True,
        )
        return
    async with _STATE_LOCK:
        state = _load()
        await _sync_member_unlocked(interaction.client, member, state, persist=False)
        row = state.setdefault("members", {}).setdefault(str(member.id), {})
        try:
            left = max(0, int(row.get("revives_left") or 0))
        except Exception:
            left = 0
        if left <= 0:
            _save(state)
            await _audit_perk(
                interaction, "revive", steam=steam, ok=False,
                revives=0, msg="No recovery credits left this month",
            )
            await interaction.followup.send(
                "No recovery credits left this month. They reset on the 1st.",
                ephemeral=True,
            )
            return
        send = _fn("send_game_command")
        result = await send(f"revive {steam}") if send else "no rcon"
        if not send or _revive_failed(result):
            _save(state)
            await _audit_perk(
                interaction, "revive", steam=steam, ok=False,
                revives=left, extra=str(result or ""),
                msg="Recovery credit not spent — Isle revive failed",
            )
            await interaction.followup.send(
                f"Isle did not take the revive (`{result}`). Credit was **not** spent. "
                "Be in-game, then try again.",
                ephemeral=True,
            )
            return
        row["revives_left"] = left - 1
        row["steam"] = steam
        _save(state)
        remaining = int(row["revives_left"])
    await _audit_perk(
        interaction, "revive", steam=steam, ok=True,
        revives=remaining, extra=str(result or ""),
        msg=f"Recovery credit spent; {remaining} left this month",
    )
    await interaction.followup.send(
        f"Recovery sent for Steam `{steam}`. **{remaining}** credit"
        f"{'' if remaining == 1 else 's'} left this month.\n"
        f"Isle: `{result}`",
        ephemeral=True,
    )


async def _dm(bot, user_id, text):
    try:
        user = bot.get_user(int(user_id)) or await bot.fetch_user(int(user_id))
        await user.send(text)
    except Exception:
        pass


def _shop_blocked(user_id, steam):
    try:
        from primeval_tickets import shop_block_reason

        blocked, _ = shop_block_reason(user_id, steam)
        return bool(blocked)
    except Exception:
        return False


def _linked_members(guild):
    seen = set()
    out = []
    links = G.get("STEAM_LINKS") or {}
    getter = _fn("get_linked_steam_id")
    for member in guild.members:
        if getattr(member, "bot", False):
            continue
        steam = None
        if getter:
            try:
                steam = getter(member.id)
            except Exception:
                steam = None
        if not steam:
            steam = links.get(member.id)
            if steam is None:
                steam = links.get(str(member.id))
            if isinstance(steam, dict):
                steam = steam.get("steam")
        steam = str(steam or "").strip()
        if not steam or steam in seen:
            continue
        seen.add(steam)
        out.append((member, steam))
    return out


async def _grant_free_unlocked(bot, member, steam, state):
    if member is None or not steam:
        return False
    if _shop_blocked(member.id, steam):
        return False
    now = int(time.time())
    bucket = state.setdefault("free", {})
    row = bucket.setdefault(str(steam), {})
    last = int(row.get("last_drop") or 0)
    if last and last + BIWEEK_SECONDS > now:
        return False
    ok, bal = _credit(steam, FREE_TOKENS, "free biweekly stipend", member.id)
    if not ok:
        return False
    row["last_drop"] = now
    row["discord_id"] = str(member.id)
    bucket[str(steam)] = row
    print(f"[STIPEND] +{FREE_TOKENS} steam={steam} discord={member.id} bal={bal}")
    await _dm(
        bot,
        member.id,
        f"Island stipend: **{FREE_TOKENS}** tokens (free, every 2 weeks). Balance: **{bal}**.",
    )
    return True


async def grant_free_stipend_for(bot, member):
    """Credit the free 2-token drop as soon as Steam is linked, then every 14 days."""
    if member is None or getattr(member, "bot", False):
        return
    steam = _steam_for(member.id)
    if not steam:
        return
    async with _STATE_LOCK:
        state = _load()
        await _grant_free_unlocked(bot, member, steam, state)
        _save(state)


async def sync_member(bot, member, state=None):
    if member is None or getattr(member, "bot", False):
        return
    persist = state is None
    if persist:
        async with _STATE_LOCK:
            await _sync_member_unlocked(bot, member, _load(), persist=True)
        return
    await _sync_member_unlocked(bot, member, state, persist=False)


async def _sync_member_unlocked(bot, member, state, persist):
    uid = str(member.id)
    steam = _steam_for(member.id)
    now = int(time.time())
    members = state.setdefault("members", {})
    row = members.setdefault(uid, {})
    row["discord_id"] = uid
    if steam:
        row["steam"] = steam

    tier_key = _active_tier_key(member)
    if tier_key:
        meta = TIERS[tier_key]
        prev = row.get("tier")
        first = not bool(row.get("joined_at"))
        if first and steam:
            amount = int(meta["tokens"]) + int(meta["bonus"])
            ok, bal = _credit(steam, amount, f"patreon first {tier_key}", member.id)
            if ok:
                row.pop("pending_first", None)
                row["joined_at"] = now
                row["tier"] = tier_key
                row["last_drop"] = now
                row["bonus_paid"] = True
                row["revives_month"] = time.strftime("%Y-%m")
                row["revives_left"] = int(meta["revives"])
                await _dm(
                    bot,
                    member.id,
                    f"Welcome to **{meta['label']}**. First drop: **{amount}** tokens "
                    f"({meta['tokens']} + {meta['bonus']} bonus). Balance: **{bal}**.",
                )
        elif first and not steam:
            row["pending_first"] = tier_key
        elif steam and row.get("pending_first"):
            pending = row.pop("pending_first")
            pmeta = TIERS.get(pending) or meta
            amount = int(pmeta["tokens"]) + int(pmeta["bonus"])
            ok, bal = _credit(steam, amount, f"patreon first {pending}", member.id)
            if ok:
                row["joined_at"] = now
                row["tier"] = pending
                row["last_drop"] = now
                row["bonus_paid"] = True
                await _dm(
                    bot,
                    member.id,
                    f"Steam linked. First **{pmeta['label']}** drop: **{amount}** tokens. Balance: **{bal}**.",
                )
            else:
                row["pending_first"] = pending
        elif prev != tier_key:
            row["tier"] = tier_key
            row["revives_month"] = time.strftime("%Y-%m")
            row["revives_left"] = int(meta["revives"])
        elif steam and int(row.get("last_drop") or 0) + BIWEEK_SECONDS <= now:
            ok, bal = _credit(steam, int(meta["tokens"]), f"patreon biweekly {tier_key}", member.id)
            if ok:
                row["last_drop"] = now
                await _dm(
                    bot,
                    member.id,
                    f"**{meta['label']}** bi-weekly drop: **{meta['tokens']}** tokens. Balance: **{bal}**.",
                )
        month = time.strftime("%Y-%m")
        if row.get("revives_month") != month:
            row["revives_month"] = month
            row["revives_left"] = int(meta["revives"])
    else:
        if row.get("tier"):
            row["ended_at"] = now
            row["tier"] = None

    held = row.setdefault("topup_held", {})
    for key, pack in TOPUPS.items():
        if _api_enabled():
            continue
        has_role = _role_on(member, pack["role"])
        was_held = bool(held.get(key))
        if has_role and not was_held:
            if not steam:
                row["pending_topup"] = int(row.get("pending_topup") or 0) + int(pack["tokens"])
            else:
                ok, bal = _credit(steam, int(pack["tokens"]), f"patreon {key}", member.id)
                if not ok:
                    continue  # leave held unset so the credit is retried next sync
                await _dm(
                    bot,
                    member.id,
                    f"Token top-up: **{pack['tokens']}** tokens. Balance: **{bal}**.",
                )
            held[key] = True
        elif not has_role:
            held[key] = False

    pending_top = int(row.get("pending_topup") or 0)
    if pending_top and steam:
        ok, bal = _credit(steam, pending_top, "patreon pending topup", member.id)
        if ok:
            row["pending_topup"] = 0
            await _dm(
                bot,
                member.id,
                f"Token top-up: **{pending_top}** tokens. Balance: **{bal}**.",
            )

    members[uid] = row
    if persist:
        _save(state)


async def ensure_roles(guild):
    names = [TIERS[k]["role"] for k in TIERS] + [p["role"] for p in TOPUPS.values()]
    existing = {str(r.name).lower(): r for r in guild.roles}
    created = []
    for name in names:
        if name.lower() in existing:
            continue
        try:
            role = await guild.create_role(
                name=name,
                mentionable=True,
                reason="Primeval Patreon shop roles",
            )
            existing[name.lower()] = role
            created.append(name)
        except Exception as exc:
            print("[PATREON] role create failed", name, exc)
    return created


def shop_embed():
    embed = discord.Embed(
        title="Fallen Earth - Support the island",
        description=(
            f"{MISSION}\n\n"
            f"{WHY_DONATE}\n\n"
            f"{DISCLAIMER}\n\n"
            f"Pick a **donation tier** on Patreon, **connect Discord** there, and **link Steam** "
            f"here. Thank-you credits land on your Steam wallet. You can play the whole server "
            f"without donating.\n\n"
            f"[Donate on Patreon]({PATREON_URL})"
        ),
        color=discord.Color.gold(),
        url=PATREON_URL,
    )
    embed.add_field(
        name="Free island stipend",
        value=(
            f"Every **Steam-linked** member gets **{FREE_TOKENS}** community credits "
            "every **2 weeks**, automatically. First drop lands when you link. "
            "Donations are extra thank-yous — never required."
        ),
        inline=False,
    )
    embed.add_field(
        name="Hatchling donation - $3 / month",
        value=(
            "The spark that pays the rent. Thank-you for keeping servers online.\n"
            "- **4** community credits every 2 weeks\n"
            "- **+8** first-time thank-you (**12** on day one)\n"
            "- **1** recovery credit / month"
        ),
        inline=True,
    )
    embed.add_field(
        name="Juvenile donation - $5 / month",
        value=(
            "Funds upkeep *and* the next bot features. Same rules. Same island.\n"
            "- **10** community credits every 2 weeks\n"
            "- **+10** first-time thank-you (**20** on day one)\n"
            "- **2** recovery credits / month"
        ),
        inline=True,
    )
    embed.add_field(
        name="Apex donation - $10 / month",
        value=(
            "The backbone of rent, maintenance, and new projects. Still no rank and no immunity.\n"
            "- **15** community credits every 2 weeks\n"
            "- **+15** first-time thank-you (**30** on day one)\n"
            "- **4** recovery credits / month"
        ),
        inline=True,
    )
    embed.add_field(
        name="Extra hosting gift (optional)",
        value=(
            "**$5** -> 5 community credits / **$15** -> 15 community credits. "
            "One-time gift toward rent, maintenance, and bot work -- not a purchase of dinosaurs. "
            "Connect Discord on Patreon and link Steam here. The bot credits the Steam wallet "
            "from the Patreon charge (no extra Discord role). Staff get a searchable ledger post."
        ),
        inline=False,
    )
    embed.add_field(
        name="Free-player monthly giveaway",
        value=(
            "Every **Steam-linked** member **without** a donation role is in a monthly token draw. "
            "One winner, posted in **#events**. Default prize is **5–20** tokens. "
            "Last month's winner sits the next draw out. Donors are not in the pool on purpose."
        ),
        inline=False,
    )
    embed.add_field(
        name="Recovery credits",
        value=(
            "Monthly recovery credits revive **you** while downed (same command staff use). "
            "They are not a stronger dino. Spend one from this panel. "
            "They reset on the 1st of each month."
        ),
        inline=False,
    )
    embed.add_field(
        name="What donations never buy",
        value=(
            "Staff rank / rule immunity / admin commands / queue skip / skins / "
            "species the free roster does not have / a stronger dino than anyone else can grow / "
            "the right to abuse players or staff."
        ),
        inline=False,
    )
    embed.set_footer(
        text="Free stipend: 2 credits / 14 days while Steam is linked. Patreon bills monthly. Donor credits arrive every 14 days while the donation stays active. Not affiliated with Afterthought LLC."
    )
    return embed


class ShopPanelView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="Refresh my perks", style=discord.ButtonStyle.primary, custom_id="pi_pat_refresh")
    async def refresh(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        member = interaction.user
        if interaction.guild and not isinstance(member, discord.Member):
            member = interaction.guild.get_member(interaction.user.id) or member
        if not isinstance(member, discord.Member):
            await interaction.followup.send("Use this in the Fallen Earth server.", ephemeral=True)
            return
        await sync_member(interaction.client, member)
        steam = _steam_for(interaction.user.id)
        if not steam:
            await interaction.followup.send(
                "Link Steam first (`Link Steam` on the member panel). Then press refresh again.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(perk_status_text(member, steam), ephemeral=True)

    @ui.button(label="Use recovery credit", style=discord.ButtonStyle.success, custom_id="pi_pat_revive")
    async def revive(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        await use_recovery_credit(interaction)


async def post_shop_panel(interaction: discord.Interaction):
    if interaction.guild and staff_rank(interaction.user) < 2:
        await deny_staff(interaction, "broadcast")
        return
    view = ShopPanelView()
    view.add_item(ui.Button(label="Open donation page", url=PATREON_URL))
    await interaction.response.send_message(embed=shop_embed(), view=view)


def attach(bot):
    if id(bot) in _ATTACHED:
        return
    _ATTACHED.add(id(bot))

    @bot.listen("on_member_update")
    async def _patreon_roles(before, after):
        try:
            await sync_member(bot, after)
        except Exception as exc:
            print("[PATREON] member update failed", exc)

    @bot.listen("on_ready")
    async def _patreon_ready():
        try:
            for guild in bot.guilds:
                await ensure_roles(guild)
        except Exception as exc:
            print("[PATREON] ready roles failed", exc)

    from discord.ext import tasks

    @tasks.loop(seconds=TICK_SECONDS)
    async def _patreon_tick():
        try:
            async with _STATE_LOCK:
                state = _load()
                try:
                    for guild in bot.guilds:
                        for member, steam in _linked_members(guild):
                            await _grant_free_unlocked(bot, member, steam, state)
                        for member in guild.members:
                            if member.bot:
                                continue
                            if _active_tier_key(member) or any(_role_on(member, p["role"]) for p in TOPUPS.values()):
                                await sync_member(bot, member, state=state)
                finally:
                    # always persist: tokens already credited in memory must not be re-granted next tick
                    _save(state)
        except Exception as exc:
            print("[PATREON] tick failed", exc)

    @_patreon_tick.before_loop
    async def _wait():
        await bot.wait_until_ready()
        await asyncio.sleep(8)

    @tasks.loop(seconds=POLL_SECONDS)
    async def _patreon_poll():
        try:
            await poll_patreon_members(bot)
        except Exception as exc:
            print("[PATREON] poll failed", exc)

    @_patreon_poll.before_loop
    async def _wait_poll():
        await bot.wait_until_ready()
        await asyncio.sleep(20)

    if _api_enabled():
        _patreon_poll.start()
        print("[PATREON] charge poll started")
    else:
        print("[PATREON] no creator token; shop gifts stay manual / role fallback")

    @bot.listen("on_ready")
    async def _patreon_http():
        try:
            await start_webhook_http(bot)
        except Exception as exc:
            print("[PATREON] webhook server failed", exc)

    _patreon_tick.start()
    print("[PATREON] shop sync started")


def register_views(bot):
    bot.add_view(ShopPanelView())
    try:
        bot.add_dynamic_items(PatreonLedgerButton)
    except Exception:
        pass


def register_slash(bot):
    existing = {cmd.name for cmd in bot.tree.get_commands()}
    if "patreon_shop" not in existing:

        @bot.tree.command(name="patreon_shop", description="Post the public donation / support panel")
        async def patreon_shop(interaction: discord.Interaction):
            await post_shop_panel(interaction)

    if "setup_patreon" not in existing:

        @bot.tree.command(
            name="setup_patreon",
            description="Create Hatchling / Juvenile / Apex / top-up Discord roles",
        )
        async def setup_patreon(interaction: discord.Interaction):
            if staff_rank(interaction.user) < 4:
                await interaction.response.send_message(
                    "Administrator or higher can set up Patreon roles.", ephemeral=True
                )
                return
            if not interaction.guild:
                await interaction.response.send_message("Use this in the server.", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            created = await ensure_roles(interaction.guild)
            extra = ", ".join(created) if created else "all roles already exist"
            await interaction.followup.send(
                "Patreon donation roles ready: Hatchling, Juvenile, Apex. "
                f"Created: {extra}. Map membership roles in Patreon -> Apps -> Discord. "
                "Extra hosting gifts credit Steam from Patreon polling (about every 5 minutes) "
                "and are logged in the channel set by `/patreon_log`. "
                "`patreon_creator_token.txt` must sit next to the bot. "
                "A webhook secret is not required. Donations never grant staff rank.",
                ephemeral=True,
            )

    if "patreon_log" not in existing:

        @bot.tree.command(
            name="patreon_log",
            description="Set this channel as the searchable Patreon payment ledger",
        )
        async def patreon_log(interaction: discord.Interaction):
            if staff_rank(interaction.user) < 4:
                await interaction.response.send_message(
                    "Administrator or higher can set the ledger channel.", ephemeral=True
                )
                return
            if not interaction.channel:
                await interaction.response.send_message("Use this in a staff channel.", ephemeral=True)
                return
            async with _STATE_LOCK:
                state = _load()
                state.setdefault("settings", {})["log_channel_id"] = interaction.channel.id
                _save(state)
            await interaction.response.send_message(
                f"Patreon ledger will post here: {interaction.channel.mention}. "
                "Every gift, declined charge, and unmatched amount gets a message with a ledger id.",
                ephemeral=True,
            )

    if "patreon_topup" not in existing:

        @bot.tree.command(
            name="patreon_topup",
            description="Manually credit a $5 or $15 hosting gift and log it",
        )
        @app_commands.describe(
            member="Who receives the tokens",
            gift="Hosting gift size",
            reason="Why -- receipt, ticket id, or what went wrong",
        )
        @app_commands.choices(
            gift=[
                app_commands.Choice(name="$5 / 5 tokens", value="5"),
                app_commands.Choice(name="$15 / 15 tokens", value="15"),
            ]
        )
        async def patreon_topup(
            interaction: discord.Interaction,
            member: discord.Member,
            gift: app_commands.Choice[str],
            reason: str,
        ):
            if staff_rank(interaction.user) < 2:
                await interaction.response.send_message("Moderator or higher.", ephemeral=True)
                return
            key = "top5" if gift.value == "5" else "top15"
            pack = TOPUPS[key]
            await interaction.response.defer(ephemeral=True)
            async with _STATE_LOCK:
                state = _load()
                steam = _steam_for(member.id)
                row = {
                    "id": _new_ledger_id(state),
                    "fingerprint": f"staff:{member.id}:{int(time.time())}:{key}",
                    "source": "staff",
                    "event": "slash",
                    "kind": key,
                    "status": "pending",
                    "note": f"Staff override: {reason[:300]}",
                    "cents": int(pack["cents"]),
                    "tokens": int(pack["tokens"]),
                    "patreon_user_id": "",
                    "patreon_member_id": "",
                    "discord_id": str(member.id),
                    "steam": steam or "",
                    "last_charge_date": "",
                    "last_charge_status": "staff",
                    "created_at": int(time.time()),
                    "staff_id": interaction.user.id,
                    "error": "",
                }
                await _try_credit_ledger(interaction.client, state, row)
                state.setdefault("ledger", {})[row["id"]] = row
                await _post_ledger(interaction.client, state, row)
                _save(state)
            await interaction.followup.send(
                f"Ledger `{row['id']}` **{row['status']}** for {member.mention} "
                f"({pack['tokens']} tokens).",
                ephemeral=True,
            )
