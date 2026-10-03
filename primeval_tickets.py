"""Player report tickets + staff verdict workflow for Primeval Island.

Hook from Primeval_Island_Bot.py the same way as primeval_panels:

    import primeval_tickets
    primeval_tickets.bind(globals())
    primeval_tickets.register_views(bot)
    primeval_tickets.register_slash(bot)

Public panels: /report_panel and /appeal_panel
Staff verdict panel posts itself into VERDICT_CHANNEL_ID.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from datetime import timedelta

import discord
from discord import ui
from discord import app_commands
from primeval_panels import (
    STAFF_ROLE_RANKS,
    deny_staff,
    staff_rank,
)

_RANK_NAMES = {
    1: "Helper",
    2: "Moderator",
    3: "Administrator",
    4: "Administrator / Senior Administrator",
    5: "Director",
}

G = {}

STATE_PATH = "primeval_cases.json"
_STATE_LOCK = threading.Lock()

NOTIFY_CHANNEL_ID = 1541419323698974841
VERDICT_CHANNEL_ID = 1541419899082244136
LOG_CHANNEL_ID = 1539044183417819226

KIND_INGAME_BUG = "ingame_bug"
KIND_BOT = "bot_issue"
KIND_DISCORD = "discord_report"
KIND_INGAME_RULE = "ingame_rule"
KIND_APPEAL = "appeal"
KIND_ADMIN_ABUSE = "admin_abuse"

KIND_META = {
    KIND_INGAME_BUG: {
        "label": "In-game bug",
        "emoji": "🐛",
        "needs_accused": False,
        "track": None,
        "color": discord.Color.gold(),
    },
    KIND_BOT: {
        "label": "Discord bot issue",
        "emoji": "🤖",
        "needs_accused": False,
        "track": None,
        "color": discord.Color.blurple(),
    },
    KIND_DISCORD: {
        "label": "Discord rule break",
        "emoji": "📛",
        "needs_accused": True,
        "track": "discord",
        "color": discord.Color.orange(),
    },
    KIND_INGAME_RULE: {
        "label": "In-game rule break",
        "emoji": "⚔️",
        "needs_accused": True,
        "track": "ingame",
        "color": discord.Color.red(),
    },
    KIND_APPEAL: {
        "label": "Appeal judgement",
        "emoji": "⚖️",
        "needs_accused": False,
        "track": "appeal",
        "color": discord.Color.teal(),
    },
    KIND_ADMIN_ABUSE: {
        "label": "Admin abuse",
        "emoji": "🚨",
        "needs_accused": False,
        "track": "abuse",
        "color": discord.Color.dark_red(),
    },
}

# Lowest staff rank that may APPLY this verdict. Lower ranks may recommend it.
VERDICT_RANKS = {
    "dismiss": 1,
    "resolve": 1,
    "dc1": 2,
    "dc2": 2,
    "dc3": 3,
    "ig1": 2,
    "ig2": 2,
    "ig3": 4,
    "ig4": 4,
    "ig5": 5,
    "uphold": 4,
    "grant": 4,
}

VERDICT_LABELS = {
    "dismiss": "Dismiss / no action",
    "resolve": "Mark resolved",
    "dc1": "Discord strike 1",
    "dc2": "Discord strike 2",
    "dc3": "Discord strike 3 — server ban",
    "ig1": "In-game strike 1 — shop + events lock",
    "ig2": "In-game strike 2 — shop + events lock",
    "ig3": "In-game strike 3 — 24h timeout",
    "ig4": "In-game strike 4 — 48h timeout",
    "ig5": "In-game strike 5 — Fallen Earth ban",
    "uphold": "Deny appeal — original verdict stands",
    "grant": "Grant appeal — undo original punishment",
}

_CASE_ID_RE = re.compile(r"^[a-z0-9]{6,16}$")


def bind(g):
    G.clear()
    G.update(g)
    try:
        import primeval_suggestions

        primeval_suggestions.bind(g)
    except Exception:
        pass
    try:
        import primeval_patreon

        primeval_patreon.bind(g)
    except Exception:
        pass


def _fn(name, default=None):
    return G.get(name, default)


def _load_state():
    with _STATE_LOCK:
        # A transient read failure must not look like "no data": the next save
        # would overwrite every case/profile with an empty file.
        for attempt in range(3):
            try:
                with open(STATE_PATH, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
                if isinstance(data, dict):
                    data.setdefault("cases", {})
                    data.setdefault("profiles", {})
                    return data
                break
            except FileNotFoundError:
                return {"cases": {}, "profiles": {}}
            except Exception:
                time.sleep(0.05)
        try:
            os.replace(STATE_PATH, STATE_PATH + ".corrupt")
        except Exception:
            pass
        return {"cases": {}, "profiles": {}}


def _save_state(state):
    with _STATE_LOCK:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2)
            handle.write("\n")
        os.replace(tmp, STATE_PATH)


def _new_case_id():
    # Random suffix: two tickets in the same second used to share one ID and overwrite each other.
    return f"t{int(time.time()):x}{secrets.token_hex(2)}"


def _save_case(case):
    """Persist one case on top of the freshest state (callers may have awaited since loading)."""
    state = _load_state()
    state.setdefault("cases", {})[case["id"]] = case
    _save_state(state)


_VERDICTS_INFLIGHT = set()


def _steam_for(user_id):
    getter = _fn("get_linked_steam_id")
    if not getter or not user_id:
        return None
    try:
        steam = getter(int(user_id))
    except Exception:
        steam = None
    return str(steam).strip() if steam else None


def _discord_ids_for_steam(steam):
    if not steam:
        return []
    steam = str(steam).strip()
    links = G.get("STEAM_LINKS") or {}
    found = []
    seen = set()
    for key, value in links.items():
        if str(value).strip() != steam:
            continue
        uid = str(key).strip()
        if uid in seen:
            continue
        seen.add(uid)
        found.append(uid)
    return found


def _profile_keys(discord_id=None, steam=None):
    keys = []
    if discord_id:
        keys.append("d:" + str(discord_id))
    if steam:
        keys.append("s:" + str(steam))
        for uid in _discord_ids_for_steam(steam):
            keys.append("d:" + str(uid))
    return keys


def _merged_profile(state, discord_id=None, steam=None):
    profiles = state.setdefault("profiles", {})
    merged = {
        "ingame_strikes": 0,
        "discord_strikes": 0,
        "shop_blocked": False,
        "events_blocked": False,
        "history": [],
        "steam": str(steam or "") or None,
        "discord_ids": [str(discord_id)] if discord_id else [],
    }
    for key in _profile_keys(discord_id, steam):
        row = profiles.get(key) or {}
        merged["ingame_strikes"] = max(int(merged["ingame_strikes"]), int(row.get("ingame_strikes") or 0))
        merged["discord_strikes"] = max(int(merged["discord_strikes"]), int(row.get("discord_strikes") or 0))
        merged["shop_blocked"] = bool(merged["shop_blocked"] or row.get("shop_blocked"))
        merged["events_blocked"] = bool(merged["events_blocked"] or row.get("events_blocked"))
        for cid in row.get("history") or []:
            if cid not in merged["history"]:
                merged["history"].append(cid)
        if row.get("steam"):
            merged["steam"] = str(row.get("steam"))
        for uid in row.get("discord_ids") or []:
            if str(uid) not in merged["discord_ids"]:
                merged["discord_ids"].append(str(uid))
    if discord_id and str(discord_id) not in merged["discord_ids"]:
        merged["discord_ids"].append(str(discord_id))
    if steam:
        merged["steam"] = str(steam)
    return merged


def _write_profile(state, profile, discord_id=None, steam=None):
    profiles = state.setdefault("profiles", {})
    payload = {
        "ingame_strikes": int(profile.get("ingame_strikes") or 0),
        "discord_strikes": int(profile.get("discord_strikes") or 0),
        "shop_blocked": bool(profile.get("shop_blocked")),
        "events_blocked": bool(profile.get("events_blocked")),
        "history": list(profile.get("history") or []),
        "steam": profile.get("steam"),
        "discord_ids": list(profile.get("discord_ids") or []),
    }
    for key in _profile_keys(discord_id, steam or payload.get("steam")):
        profiles[key] = dict(payload)


def shop_block_reason(user_id, steam=None):
    steam = steam or _steam_for(user_id)
    state = _load_state()
    profile = _merged_profile(state, user_id, steam)
    strikes = int(profile.get("ingame_strikes") or 0)
    if profile.get("shop_blocked") or strikes >= 1:
        return (
            True,
            f"Your in-game standing is **{strikes} strike(s)**. The dino shop and events are locked.",
        )
    return False, ""


def events_blocked(user_id, steam=None):
    blocked, _ = shop_block_reason(user_id, steam)
    if blocked:
        return True
    steam = steam or _steam_for(user_id)
    profile = _merged_profile(_load_state(), user_id, steam)
    return bool(profile.get("events_blocked"))


def _mention_role_for_rank(guild, rank):
    if not guild:
        return f"**{_RANK_NAMES.get(rank, 'staff')}**"
    mentions = []
    seen = set()
    for role in guild.roles:
        name = str(role.name).strip().lower()
        if STAFF_ROLE_RANKS.get(name) != rank:
            continue
        if role.id in seen:
            continue
        seen.add(role.id)
        mentions.append(role.mention)
    if mentions:
        return " ".join(mentions)
    return f"**{_RANK_NAMES.get(rank, 'staff')}**"


def _staff_overwrites(guild, reporter, extras=None):
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_channels=True,
            manage_messages=True,
            embed_links=True,
            attach_files=True,
        ),
        reporter: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            embed_links=True,
        ),
    }
    for role in guild.roles:
        name = str(role.name).strip().lower()
        if name in STAFF_ROLE_RANKS:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
                manage_messages=True,
            )
    for member in extras or []:
        if member is None:
            continue
        overwrites[member] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
        )
    return overwrites


def _party_line(user_id, steam=None, fallback_name=""):
    uid = str(user_id or "").strip()
    steam = steam or (_steam_for(uid) if uid.isdigit() else None)
    mention = f"<@{uid}>" if uid.isdigit() else (fallback_name or "unknown")
    disc = f"`{uid}`" if uid else "`unlinked`"
    steam_txt = f"`{steam}`" if steam else "`not linked`"
    return f"{mention}\nDiscord {disc}\nSteam {steam_txt}"


def _case_history_lines(state, profile, limit=8):
    lines = []
    cases = state.get("cases") or {}
    for cid in list(profile.get("history") or [])[-limit:]:
        row = cases.get(cid) or {}
        kind = KIND_META.get(row.get("kind"), {}).get("label", row.get("kind") or "?")
        verdict = VERDICT_LABELS.get(row.get("verdict") or "", row.get("verdict") or "open")
        when = row.get("created_at") or 0
        stamp = f"<t:{int(when)}:d>" if when else "unknown date"
        lines.append(f"• `{cid}` {kind} — {verdict} ({stamp})")
    return lines


def _staff_only(interaction):
    return staff_rank(interaction.user) >= 1


def _role_names(member):
    names = set()
    for role in getattr(member, "roles", None) or []:
        names.add(str(getattr(role, "name", "")).strip().lower())
    return names


def _is_director(member):
    if member is None:
        return False
    if "director" in _role_names(member):
        return True
    perms = getattr(member, "guild_permissions", None)
    return bool(perms and perms.administrator)


def _is_senior_or_director(member):
    if _is_director(member):
        return True
    return "senior administrator" in _role_names(member)


def _is_administrator_or_higher(member):
    if _is_senior_or_director(member):
        return True
    if "administrator" in _role_names(member):
        return True
    return staff_rank(member) >= 4


def _can_claim_case(member, case):
    if (case or {}).get("kind") == KIND_ADMIN_ABUSE:
        return _is_senior_or_director(member)
    if (case or {}).get("kind") == KIND_APPEAL:
        return _is_administrator_or_higher(member)
    return staff_rank(member) >= 1


def _can_apply_appeal(member, case):
    claimed = str((case or {}).get("claimed_by") or "")
    uid = str(getattr(member, "id", "") or "")
    if not claimed:
        return False
    if claimed == uid:
        return _can_claim_case(member, case)
    return _is_senior_or_director(member)


def _standing_snapshot(profile):
    return {
        "ingame_strikes": int(profile.get("ingame_strikes") or 0),
        "discord_strikes": int(profile.get("discord_strikes") or 0),
        "shop_blocked": bool(profile.get("shop_blocked")),
        "events_blocked": bool(profile.get("events_blocked")),
        "steam": profile.get("steam"),
    }


def _guess_standing_before(origin, profile):
    saved = origin.get("standing_before") if isinstance(origin, dict) else None
    if isinstance(saved, dict) and ("ingame_strikes" in saved or "discord_strikes" in saved):
        return {
            "ingame_strikes": int(saved.get("ingame_strikes") or 0),
            "discord_strikes": int(saved.get("discord_strikes") or 0),
            "shop_blocked": bool(saved.get("shop_blocked")),
            "events_blocked": bool(saved.get("events_blocked")),
            "steam": saved.get("steam") or profile.get("steam"),
        }
    ig = int(profile.get("ingame_strikes") or 0)
    dc = int(profile.get("discord_strikes") or 0)
    act = str((origin or {}).get("verdict") or "")
    if act.startswith("ig") and act[2:].isdigit():
        n = int(act[2:])
        ig = max(0, min(ig, n) - 1) if ig >= n else max(0, ig - 1)
    if act.startswith("dc") and act[2:].isdigit():
        n = int(act[2:])
        dc = max(0, min(dc, n) - 1) if dc >= n else max(0, dc - 1)
    return {
        "ingame_strikes": ig,
        "discord_strikes": dc,
        "shop_blocked": ig >= 1,
        "events_blocked": ig >= 1,
        "steam": profile.get("steam"),
    }


async def _channel(bot, channel_id):
    channel = bot.get_channel(int(channel_id))
    if channel is not None:
        return channel
    try:
        return await bot.fetch_channel(int(channel_id))
    except Exception:
        return None


def _ticket_embed(case):
    meta = KIND_META.get(case.get("kind") or "", {})
    embed = discord.Embed(
        title=f"{meta.get('emoji', '🎫')} {meta.get('label', 'Report')} · `{case.get('id')}`",
        description=str(case.get("details") or "No details given.")[:4000],
        color=meta.get("color") or discord.Color.dark_grey(),
    )
    embed.add_field(name="Reporter", value=_party_line(case.get("reporter_id"), case.get("reporter_steam")), inline=True)
    if case.get("accused_id") or case.get("accused_steam") or case.get("accused_name"):
        embed.add_field(
            name="Reported party",
            value=_party_line(case.get("accused_id"), case.get("accused_steam"), case.get("accused_name") or ""),
            inline=True,
        )
    else:
        embed.add_field(name="Reported party", value="n/a (bug / bot ticket)", inline=True)
    extra = str(case.get("accused_steam_manual") or "").strip()
    if extra:
        embed.add_field(name="Steam given in report", value=f"`{extra}`", inline=False)
    if case.get("origin_id"):
        embed.add_field(name="Appealing case", value=f"`{case.get('origin_id')}`", inline=False)
    claimed = case.get("claimed_by")
    if claimed:
        embed.add_field(name="Claimed by", value=f"<@{claimed}>", inline=True)
    embed.set_footer(text="Staff can add extra members with the picker below. Reporter cannot see the verdict channel.")
    return embed


def _prior_embed(case, profile, history_lines):
    track = KIND_META.get(case.get("kind") or "", {}).get("track")
    ig = int(profile.get("ingame_strikes") or 0)
    dc = int(profile.get("discord_strikes") or 0)
    desc = (
        f"Case `{case.get('id')}` · {KIND_META.get(case.get('kind'), {}).get('label')}\n"
        f"Accused Discord `{case.get('accused_id') or 'n/a'}` · Steam `{case.get('accused_steam') or 'n/a'}`\n\n"
        f"**In-game strikes:** {ig}/5\n"
        f"**Discord strikes:** {dc}/3\n"
        f"**Shop/events lock:** {'yes' if profile.get('shop_blocked') or ig in (1, 2) else 'no'}"
    )
    if track == "ingame":
        nxt = min(ig + 1, 5)
        desc += f"\n**If a strike is added now:** {VERDICT_LABELS.get('ig' + str(nxt), nxt)}"
    elif track == "discord":
        nxt = min(dc + 1, 3)
        desc += f"\n**If a strike is added now:** {VERDICT_LABELS.get('dc' + str(nxt), nxt)}"
    elif track in ("appeal", "abuse"):
        if case.get("origin_id"):
            desc += f"\n**Linked case:** `{case.get('origin_id')}`"
        else:
            desc += "\n**In-game / no case** — power-abuse report"
        desc += f"\n**Claim rule:** {'Senior Administration + Directors only' if case.get('kind') == KIND_ADMIN_ABUSE else 'Administration or higher'}"
    embed = discord.Embed(title="Prior offenses", description=desc, color=discord.Color.dark_teal())
    embed.add_field(
        name="History",
        value="\n".join(history_lines) if history_lines else "No prior cases on file.",
        inline=False,
    )
    return embed


def _verdict_embed(case, profile, pending=None):
    meta = KIND_META.get(case.get("kind") or "", {})
    ig = int(profile.get("ingame_strikes") or 0)
    dc = int(profile.get("discord_strikes") or 0)
    lines = [
        f"**Case** `{case.get('id')}`",
        f"**Type** {meta.get('label')}",
        f"**Ticket** <#{case.get('ticket_channel_id')}>" if case.get("ticket_channel_id") else "**Ticket** n/a",
        f"**Reporter** {_party_line(case.get('reporter_id'), case.get('reporter_steam'))}",
    ]
    if case.get("accused_id") or case.get("accused_steam") or case.get("accused_name"):
        lines.append(f"**Accused** {_party_line(case.get('accused_id'), case.get('accused_steam'), case.get('accused_name') or '')}")
    lines.append(f"**Standing** in-game {ig}/5 · Discord {dc}/3")
    if case.get("origin_id"):
        lines.append(f"**Original case** `{case.get('origin_id')}` · {VERDICT_LABELS.get(case.get('origin_verdict'), case.get('origin_verdict') or 'unknown')}")
    claimed = case.get("claimed_by")
    if case.get("kind") in (KIND_APPEAL, KIND_ADMIN_ABUSE):
        if claimed:
            lines.append(f"**Claimed by** <@{claimed}>")
        else:
            rule = "Senior Administration + Directors" if case.get("kind") == KIND_ADMIN_ABUSE else "Administration or higher"
            lines.append(f"**Unclaimed** — {rule} must claim before a verdict")
    recs = case.get("recommendations") or []
    if recs:
        pretty = []
        for row in recs[-6:]:
            pretty.append(
                f"• <@{row.get('user_id')}> ({_RANK_NAMES.get(int(row.get('rank') or 0), 'staff')}) → "
                f"{VERDICT_LABELS.get(row.get('act'), row.get('act'))}"
            )
        lines.append("**Recommendations**\n" + "\n".join(pretty))
    if pending:
        need = int(VERDICT_RANKS.get(pending, 5))
        lines.append(
            f"**Waiting on** {_RANK_NAMES.get(need, 'higher staff')} to apply "
            f"**{VERDICT_LABELS.get(pending, pending)}**."
        )
    if case.get("verdict"):
        lines.append(f"**Final verdict** {VERDICT_LABELS.get(case.get('verdict'), case.get('verdict'))}")
        if case.get("applied_by"):
            lines.append(f"**Applied by** <@{case.get('applied_by')}>")
    embed = discord.Embed(
        title="Admin verdict",
        description="\n".join(lines)[:4000],
        color=discord.Color.dark_gold() if not case.get("verdict") else discord.Color.green(),
    )
    details = str(case.get("details") or "")[:1000]
    if details:
        embed.add_field(name="Report", value=details, inline=False)
    return embed


def _log_embed(case, profile, applied):
    meta = KIND_META.get(case.get("kind") or "", {})
    embed = discord.Embed(
        title=f"Case log `{case.get('id')}`",
        description=str(case.get("details") or "")[:1500] or "—",
        color=discord.Color.dark_grey(),
    )
    embed.add_field(name="Type", value=meta.get("label", case.get("kind")), inline=True)
    embed.add_field(
        name="Opened",
        value=f"<t:{int(case.get('created_at') or time.time())}:F>",
        inline=True,
    )
    embed.add_field(
        name="Closed",
        value=f"<t:{int(case.get('closed_at') or time.time())}:F>",
        inline=True,
    )
    embed.add_field(name="Reporter", value=_party_line(case.get("reporter_id"), case.get("reporter_steam")), inline=True)
    embed.add_field(
        name="Accused",
        value=_party_line(case.get("accused_id"), case.get("accused_steam"), case.get("accused_name") or "")
        if (case.get("accused_id") or case.get("accused_steam") or case.get("accused_name"))
        else "n/a",
        inline=True,
    )
    handlers = case.get("handlers") or []
    if handlers:
        embed.add_field(
            name="Staff involved",
            value="\n".join(
                f"<@{h.get('user_id')}> `{h.get('user_id')}` · {_RANK_NAMES.get(int(h.get('rank') or 0), '?')}"
                for h in handlers
            )[:1000],
            inline=False,
        )
    recs = case.get("recommendations") or []
    if recs:
        embed.add_field(
            name="Recommendations",
            value="\n".join(
                f"<@{r.get('user_id')}> → {VERDICT_LABELS.get(r.get('act'), r.get('act'))}"
                for r in recs
            )[:1000],
            inline=False,
        )
    embed.add_field(
        name="Verdict",
        value=VERDICT_LABELS.get(case.get("verdict"), case.get("verdict") or "none"),
        inline=True,
    )
    embed.add_field(name="Actions applied", value=applied or "none", inline=False)
    embed.add_field(
        name="Standing after",
        value=(
            f"In-game `{int(profile.get('ingame_strikes') or 0)}/5` · "
            f"Discord `{int(profile.get('discord_strikes') or 0)}/3` · "
            f"Shop lock `{bool(profile.get('shop_blocked'))}`"
        ),
        inline=False,
    )
    embed.add_field(name="Search keys", value=(
        f"case:{case.get('id')} "
        f"origin:{case.get('origin_id') or 'none'} "
        f"discord:{case.get('accused_id') or case.get('reporter_id')} "
        f"steam:{case.get('accused_steam') or case.get('reporter_steam') or 'none'}"
    ), inline=False)
    return embed


def _origin_research_embed(appeal, origin, profile, history_lines):
    origin = origin or {}
    act = origin.get("verdict")
    standing = _guess_standing_before(origin, profile)
    desc = (
        f"**Appeal** `{appeal.get('id')}` · {KIND_META.get(appeal.get('kind'), {}).get('label')}\n"
        f"**Appellant** Discord `{appeal.get('reporter_id')}` · Steam `{appeal.get('reporter_steam') or 'n/a'}`\n"
        f"**Original case** `{origin.get('id') or appeal.get('origin_id')}`\n"
        f"**Original type** {KIND_META.get(origin.get('kind'), {}).get('label', origin.get('kind') or '?')}\n"
        f"**Original verdict** {VERDICT_LABELS.get(act, act or 'none')}\n"
        f"**Applied by** <@{origin.get('applied_by')}> `{origin.get('applied_by') or 'n/a'}`\n"
        f"**Applied** {origin.get('applied_text') or 'n/a'}\n"
        f"**Standing now** in-game {int(profile.get('ingame_strikes') or 0)}/5 · Discord {int(profile.get('discord_strikes') or 0)}/3\n"
        f"**Standing before original verdict** in-game {standing.get('ingame_strikes')}/5 · "
        f"Discord {standing.get('discord_strikes')}/3 · shop lock `{standing.get('shop_blocked')}`"
    )
    embed = discord.Embed(
        title="Appeal research",
        description=desc[:4000],
        color=discord.Color.dark_teal(),
    )
    report = str(origin.get("details") or "")[:1000]
    if report:
        embed.add_field(name="Original report", value=report, inline=False)
    appeal_why = str(appeal.get("details") or "")[:1000]
    if appeal_why:
        embed.add_field(name="Appeal statement", value=appeal_why, inline=False)
    handlers = origin.get("handlers") or []
    if handlers:
        embed.add_field(
            name="Original staff",
            value="\n".join(
                f"<@{h.get('user_id')}> `{h.get('user_id')}` · {_RANK_NAMES.get(int(h.get('rank') or 0), '?')}"
                for h in handlers
            )[:1000],
            inline=False,
        )
    embed.add_field(
        name="History",
        value="\n".join(history_lines) if history_lines else "No prior cases on file.",
        inline=False,
    )
    embed.add_field(
        name="Search keys",
        value=(
            f"appeal:{appeal.get('id')} origin:{origin.get('id') or appeal.get('origin_id')} "
            f"discord:{appeal.get('reporter_id')} steam:{appeal.get('reporter_steam') or 'none'}"
        ),
        inline=False,
    )
    return embed


def _verdict_actions(kind, case=None):
    if kind in (KIND_INGAME_BUG, KIND_BOT):
        return ["resolve", "dismiss"]
    if kind == KIND_DISCORD:
        return ["dismiss", "dc1", "dc2", "dc3"]
    if kind == KIND_INGAME_RULE:
        return ["dismiss", "ig1", "ig2", "ig3", "ig4", "ig5"]
    if kind == KIND_APPEAL:
        return ["uphold", "grant"]
    if kind == KIND_ADMIN_ABUSE:
        if case and case.get("origin_id"):
            return ["uphold", "grant"]
        return ["resolve", "dismiss"]
    return ["dismiss"]


class VerdictButton(ui.DynamicItem[ui.Button], template=r"pi_vrd:(?P<cid>[a-z0-9]+):(?P<act>[a-z0-9]+)"):
    def __init__(self, case_id, act):
        style = discord.ButtonStyle.secondary
        if act == "dismiss" or act == "resolve" or act == "grant":
            style = discord.ButtonStyle.success
        elif act in ("dc3", "ig5", "uphold"):
            style = discord.ButtonStyle.danger
        elif act.startswith("ig") or act.startswith("dc"):
            style = discord.ButtonStyle.primary
        super().__init__(
            ui.Button(
                label=VERDICT_LABELS.get(act, act)[:80],
                style=style,
                custom_id=f"pi_vrd:{case_id}:{act}",
            )
        )
        self.case_id = case_id
        self.act = act

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["cid"], match["act"])

    async def callback(self, interaction: discord.Interaction):
        await handle_verdict(interaction, self.case_id, self.act)


def _verdict_view(case_id, kind, disabled=False, claimed=False, case=None):
    view = ui.View(timeout=None)
    if kind in (KIND_APPEAL, KIND_ADMIN_ABUSE):
        claim = ClaimButton(case_id)
        claim.item.disabled = bool(disabled or claimed)
        view.add_item(claim)
    for act in _verdict_actions(kind, case):
        button = VerdictButton(case_id, act)
        button.item.disabled = disabled
        view.add_item(button)
    return view


class ClaimButton(ui.DynamicItem[ui.Button], template=r"pi_clm:(?P<cid>[a-z0-9]+)"):
    def __init__(self, case_id):
        super().__init__(
            ui.Button(
                label="Claim ticket",
                style=discord.ButtonStyle.primary,
                custom_id=f"pi_clm:{case_id}",
            )
        )
        self.case_id = case_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["cid"])

    async def callback(self, interaction: discord.Interaction):
        await handle_claim(interaction, self.case_id)


class TicketControlView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.select(
        cls=ui.UserSelect,
        placeholder="Staff: add another member to this ticket…",
        min_values=1,
        max_values=1,
        custom_id="pi_tix_add",
        row=0,
    )
    async def add_member(self, interaction: discord.Interaction, select: ui.UserSelect):
        if not _staff_only(interaction):
            await interaction.response.send_message("Only staff can add members.", ephemeral=True)
            return
        member = select.values[0]
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("This only works inside a ticket channel.", ephemeral=True)
            return
        try:
            await channel.set_permissions(
                member,
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
            )
        except Exception as exc:
            await interaction.response.send_message(f"Could not add that member: {exc}", ephemeral=True)
            return
        state = _load_state()
        case = _case_for_channel(state, channel.id)
        if case:
            extras = case.setdefault("extra_members", [])
            uid = str(member.id)
            if uid not in extras:
                extras.append(uid)
            _touch_handler(case, interaction.user)
            state["cases"][case["id"]] = case
            _save_state(state)
        await interaction.response.send_message(f"Added {member.mention} to this ticket.", ephemeral=True)
        try:
            await channel.send(f"{member.mention} was added by {interaction.user.mention}.")
        except Exception:
            pass

    @ui.button(label="Claim ticket", style=discord.ButtonStyle.primary, custom_id="pi_tix_claim", row=1)
    async def claim_ticket(self, interaction: discord.Interaction, button: ui.Button):
        state = _load_state()
        case = _case_for_channel(state, getattr(interaction.channel, "id", 0))
        if not case:
            await interaction.response.send_message("This channel is not an open case.", ephemeral=True)
            return
        await handle_claim(interaction, case.get("id"))

    @ui.button(label="Close ticket", style=discord.ButtonStyle.danger, custom_id="pi_tix_close", row=2)
    async def close_ticket(self, interaction: discord.Interaction, button: ui.Button):
        if not _staff_only(interaction):
            await interaction.response.send_message("Only staff can close tickets.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        channel = interaction.channel
        state = _load_state()
        case = _case_for_channel(state, getattr(channel, "id", 0))
        if case and not case.get("verdict") and KIND_META.get(case.get("kind"), {}).get("track"):
            await interaction.followup.send(
                "This ticket still needs a verdict in the admin panel before it can close.",
                ephemeral=True,
            )
            return
        if case:
            case["status"] = "closed"
            case["closed_at"] = int(time.time())
            _touch_handler(case, interaction.user)
            state["cases"][case["id"]] = case
            _save_state(state)
        try:
            await channel.send("Ticket closed. Channel will be locked.")
            await channel.set_permissions(interaction.guild.default_role, view_channel=False)
            if not channel.name.startswith("closed-"):
                await channel.edit(name=f"closed-{channel.name}"[:95])
        except Exception:
            pass
        await interaction.followup.send("Closed.", ephemeral=True)


def _case_for_channel(state, channel_id):
    for case in (state.get("cases") or {}).values():
        if int(case.get("ticket_channel_id") or 0) == int(channel_id or 0):
            return case
    return None


def _touch_handler(case, user):
    handlers = case.setdefault("handlers", [])
    uid = str(user.id)
    rank = staff_rank(user)
    for row in handlers:
        if str(row.get("user_id")) == uid:
            row["rank"] = rank
            row["name"] = str(getattr(user, "display_name", user))
            return
    handlers.append({"user_id": uid, "rank": rank, "name": str(getattr(user, "display_name", user))})


def _resolve_named_member(guild, raw):
    needle = str(raw or "").strip()
    if guild is None or not needle:
        return None
    if needle.isdigit():
        found = guild.get_member(int(needle))
        if found is not None:
            return found
    low = needle.lower().lstrip("@")
    exact, partial = [], []
    for member in getattr(guild, "members", []) or []:
        names = [
            str(getattr(member, "name", "") or ""),
            str(getattr(member, "display_name", "") or ""),
            str(getattr(member, "global_name", "") or ""),
            str(getattr(member, "nick", "") or ""),
        ]
        lowered = [n.lower() for n in names if n]
        if low in lowered:
            exact.append(member)
        elif any(low in n for n in lowered):
            partial.append(member)
    if len(exact) == 1:
        return exact[0]
    if not exact and len(partial) == 1:
        return partial[0]
    return None


async def open_ticket(interaction, kind, details, accused=None, steam_manual="", name_manual=""):
    guild = interaction.guild
    if guild is None:
        await interaction.followup.send("Reports only work inside the Fallen Earth server.", ephemeral=True)
        return
    meta = KIND_META[kind]
    reporter = interaction.user
    typed = str(name_manual or "").strip()
    if accused is None and typed:
        accused = _resolve_named_member(guild, typed)
    accused_id = str(accused.id) if accused is not None else ""
    accused_steam = _steam_for(accused_id) if accused_id else ""
    if steam_manual and re.fullmatch(r"\d{17}", steam_manual):
        accused_steam = accused_steam or steam_manual
    elif typed and re.fullmatch(r"\d{17}", typed):
        accused_steam = accused_steam or typed
    reporter_steam = _steam_for(reporter.id)
    if meta.get("needs_accused") or kind == KIND_ADMIN_ABUSE:
        if accused is None and not typed and not accused_steam:
            await interaction.followup.send(
                "Name who you are reporting — pick them from the list, or type their in-game / Discord name.",
                ephemeral=True,
            )
            return
    state = _load_state()
    for existing in (state.get("cases") or {}).values():
        if (
            existing.get("status") == "open"
            and str(existing.get("reporter_id")) == str(reporter.id)
            and existing.get("kind") == kind
        ):
            chan_id = existing.get("ticket_channel_id")
            await interaction.followup.send(
                f"You already have an open **{meta['label']}** ticket: <#{chan_id}>",
                ephemeral=True,
            )
            return

    case_id = _new_case_id()
    notify_channel = await _channel(interaction.client, NOTIFY_CHANNEL_ID)
    category = getattr(notify_channel, "category", None) if notify_channel else None
    extras = []
    if accused is not None and meta["needs_accused"]:
        # Accused is NOT added by default; staff may add them if needed.
        pass
    try:
        channel = await guild.create_text_channel(
            name=f"report-{meta['emoji']}-{reporter.name}"[:95],
            category=category,
            overwrites=_staff_overwrites(guild, reporter, extras),
            topic=f"Primeval ticket {case_id} · {meta['label']}",
            reason=f"Report ticket {case_id}",
        )
    except Exception as exc:
        await interaction.followup.send(f"Could not open a private ticket channel: {exc}", ephemeral=True)
        return

    # State may have changed while the channel was being created; don't save a stale snapshot.
    state = _load_state()
    case = {
        "id": case_id,
        "kind": kind,
        "status": "open",
        "created_at": int(time.time()),
        "reporter_id": str(reporter.id),
        "reporter_name": str(reporter),
        "reporter_steam": reporter_steam,
        "accused_id": accused_id,
        "accused_name": (str(accused) if accused is not None else typed),
        "accused_steam": accused_steam,
        "accused_steam_manual": steam_manual,
        "details": details,
        "ticket_channel_id": channel.id,
        "verdict_message_id": None,
        "notify_message_id": None,
        "log_message_id": None,
        "recommendations": [],
        "handlers": [],
        "pending_act": None,
        "verdict": None,
        "extra_members": [],
    }
    state.setdefault("cases", {})[case_id] = case
    profile = _merged_profile(state, accused_id or None, accused_steam or None)
    if accused_id or accused_steam:
        hist = profile.setdefault("history", [])
        if case_id not in hist:
            hist.append(case_id)
        _write_profile(state, profile, accused_id or None, accused_steam or None)
    _save_state(state)

    history_lines = _case_history_lines(state, profile)
    try:
        await channel.send(
            content=f"{reporter.mention} — staff can see this channel. Do not ping everyone; wait here.",
            embed=_ticket_embed(case),
            view=TicketControlView(),
        )
    except Exception:
        pass
    verdict_channel = await _channel(interaction.client, VERDICT_CHANNEL_ID)
    if kind == KIND_ADMIN_ABUSE:
        claim_rule = "Senior Administration and Directors must claim this."
        try:
            await channel.send(f"{reporter.mention} — {claim_rule}")
        except Exception:
            pass
        if notify_channel and (accused_id or accused_steam):
            try:
                msg = await notify_channel.send(
                    content=f"Admin-abuse report `{case_id}` (no linked case)",
                    embed=_prior_embed(case, profile, history_lines),
                )
                case["notify_message_id"] = msg.id
            except Exception:
                pass
        if verdict_channel:
            ping = _mention_role_for_rank(guild, 5) + " " + " ".join(
                role.mention for role in guild.roles
                if str(role.name).strip().lower() == "senior administrator"
            )
            try:
                vmsg = await verdict_channel.send(
                    content=f"{ping} — claim this ticket, then mark resolved or dismiss.",
                    embed=_verdict_embed(case, profile),
                    view=_verdict_view(case_id, kind, case=case),
                )
                case["verdict_message_id"] = vmsg.id
            except Exception:
                pass
    elif meta.get("track") and (accused_id or accused_steam):
        await channel.send(
            "Prior-offense check was sent to staff only. It is not shown here so the reporter cannot see another player's record."
        )
        if notify_channel:
            try:
                msg = await notify_channel.send(
                    content=f"Prior-offense scan for case `{case_id}`",
                    embed=_prior_embed(case, profile, history_lines),
                )
                case["notify_message_id"] = msg.id
            except Exception:
                pass
        if verdict_channel:
            try:
                vmsg = await verdict_channel.send(
                    content="Select a verdict. If your rank cannot apply it, the next admin tier is pinged.",
                    embed=_verdict_embed(case, profile),
                    view=_verdict_view(case_id, kind, case=case),
                )
                case["verdict_message_id"] = vmsg.id
            except Exception:
                pass
    else:
        if verdict_channel:
            try:
                vmsg = await verdict_channel.send(
                    content=(
                        "Select a verdict. No linked account was found for the reported party, so strikes cannot be recorded — recommend dismiss or resolve manually."
                        if meta.get("track")
                        else "Bug / bot ticket — mark resolved when done."
                    ),
                    embed=_verdict_embed(case, profile),
                    view=_verdict_view(case_id, kind, case=case),
                )
                case["verdict_message_id"] = vmsg.id
            except Exception:
                pass
    _save_case(case)
    await interaction.followup.send(f"Opened private ticket {channel.mention} (`{case_id}`).", ephemeral=True)


class ReportModal(ui.Modal):
    def __init__(self, kind, accused=None, name_required=False):
        meta = KIND_META[kind]
        super().__init__(title=meta["label"][:45])
        self.kind = kind
        self.accused = accused
        self.who = None
        if name_required or (accused is None and meta.get("needs_accused")):
            self.who = ui.TextInput(
                label="Who are you reporting?",
                placeholder="In-game name, Discord name, or SteamID64",
                max_length=80,
                required=True,
            )
            self.add_item(self.who)
        detail_kwargs = {
            "label": "What did they do?" if kind == KIND_ADMIN_ABUSE else "What happened?",
            "style": discord.TextStyle.paragraph,
            "max_length": 1500,
            "required": True,
        }
        if kind == KIND_ADMIN_ABUSE:
            detail_kwargs["placeholder"] = "Slay, grow, god, teleport, revive, or other in-game power abuse."
        self.details = ui.TextInput(**detail_kwargs)
        self.add_item(self.details)
        self.steam = None
        if kind in (KIND_INGAME_RULE, KIND_ADMIN_ABUSE):
            self.steam = ui.TextInput(
                label="Accused SteamID64 (if you know it)",
                required=False,
                max_length=17,
                min_length=0,
                placeholder="7656119… optional",
            )
            self.add_item(self.steam)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        steam_manual = str(self.steam.value).strip() if self.steam is not None else ""
        name_manual = str(self.who.value).strip() if self.who is not None else ""
        await open_ticket(
            interaction,
            self.kind,
            str(self.details.value or "").strip(),
            accused=self.accused,
            steam_manual=steam_manual,
            name_manual=name_manual,
        )


class AccusedPickView(ui.View):
    def __init__(self, kind):
        super().__init__(timeout=180)
        self.kind = kind
        self.accused = None

    @ui.select(
        cls=ui.UserSelect,
        placeholder="Who are you reporting? (linked Discord users)",
        min_values=0,
        max_values=1,
    )
    async def pick(self, interaction: discord.Interaction, select: ui.UserSelect):
        self.accused = select.values[0] if select.values else None
        if self.accused is None:
            await interaction.response.send_message(
                "No one picked. Use **Type a name** if they are not in this list.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"Reporting {self.accused.mention}. Press **Continue** and describe what happened.",
            ephemeral=True,
        )

    @ui.button(label="Continue", style=discord.ButtonStyle.primary)
    async def cont(self, interaction: discord.Interaction, button: ui.Button):
        if self.accused is None:
            await interaction.response.send_message(
                "Pick someone from the list, or press **Type a name** if they are not linked / not on Discord.",
                ephemeral=True,
            )
            return
        if self.accused.id == interaction.user.id:
            await interaction.response.send_message("You cannot report yourself.", ephemeral=True)
            return
        await interaction.response.send_modal(ReportModal(self.kind, self.accused))

    @ui.button(label="Type a name", style=discord.ButtonStyle.secondary)
    async def type_name(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(ReportModal(self.kind, accused=None, name_required=True))


class ReportPanelView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.select(
        placeholder="Open a private report ticket…",
        min_values=1,
        max_values=1,
        custom_id="pi_rpt_kind",
        options=[
            discord.SelectOption(
                label="In-game bug",
                description="Map, dinos, crashes, gameplay bugs",
                value=KIND_INGAME_BUG,
                emoji="🐛",
            ),
            discord.SelectOption(
                label="Discord bot issue",
                description="Shop, panels, tokens, commands",
                value=KIND_BOT,
                emoji="🤖",
            ),
            discord.SelectOption(
                label="Discord rule break",
                description="A user breaking Discord rules",
                value=KIND_DISCORD,
                emoji="📛",
            ),
            discord.SelectOption(
                label="In-game rule break",
                description="A player breaking Fallen Earth rules",
                value=KIND_INGAME_RULE,
                emoji="⚔️",
            ),
        ],
    )
    async def kind(self, interaction: discord.Interaction, select: ui.Select):
        choice = select.values[0]
        meta = KIND_META[choice]
        if meta["needs_accused"]:
            await interaction.response.send_message(
                "Pick the Discord user you are reporting, **or** press **Type a name** "
                "if they are not in the list (not linked / not on Discord).",
                view=AccusedPickView(choice),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(ReportModal(choice))


def report_embed():
    return discord.Embed(
        title="Fallen Earth · Reports",
        description=(
            "Open a **private ticket** with staff. Only you and staff can see it; "
            "staff may add extra members if they need statements.\n\n"
            "🐛 **In-game bug** — gameplay / map / dino problems\n"
            "🤖 **Discord bot issue** — shop, panels, tokens\n"
            "📛 **Discord rule break** — a user breaking Discord rules (3-strike ladder)\n"
            "⚔️ **In-game rule break** — a player breaking Fallen Earth rules (5-strike ladder)\n\n"
            "Steam and Discord IDs are pulled automatically when they are linked. "
            "If the person is not in the dropdown, use **Type a name** and write their in-game or Discord name."
        ),
        color=discord.Color.dark_gold(),
    )


async def post_report_panel(interaction: discord.Interaction):
    if interaction.guild and staff_rank(interaction.user) < 2:
        await deny_staff(interaction, "broadcast")
        return
    await interaction.response.send_message(embed=report_embed(), view=ReportPanelView())


def appeal_embed():
    return discord.Embed(
        title="Fallen Earth · Appeals",
        description=(
            "Appeal a punishment or report **admin abuse**. A private ticket opens with staff.\n\n"
            "⚖️ **Appeal judgement** — pick the case, explain why it should be reversed. "
            "Administration or higher must claim it.\n"
            "🚨 **Admin abuse** — Senior Administration and Directors only. "
            "Mishandled ticket **or** in-game power abuse (slay, grow, god, teleport, etc.).\n\n"
            "Staff research (history + the exact original verdict) is posted to the staff log. "
            "If the appeal is granted, the original punishment is undone and the ticket closes."
        ),
        color=discord.Color.teal(),
    )


async def post_appeal_panel(interaction: discord.Interaction):
    if interaction.guild and staff_rank(interaction.user) < 2:
        await deny_staff(interaction, "broadcast")
        return
    await interaction.response.send_message(embed=appeal_embed(), view=AppealPanelView())


class AppealPanelView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.select(
        placeholder="Open an appeal ticket…",
        min_values=1,
        max_values=1,
        custom_id="pi_apl_kind",
        options=[
            discord.SelectOption(
                label="Appeal judgement",
                description="Ask Administration to undo a punishment",
                value=KIND_APPEAL,
                emoji="⚖️",
            ),
            discord.SelectOption(
                label="Admin abuse",
                description="Ticket mishandled, or in-game power abuse",
                value=KIND_ADMIN_ABUSE,
                emoji="🚨",
            ),
        ],
    )
    async def kind(self, interaction: discord.Interaction, select: ui.Select):
        choice = select.values[0]
        state = _load_state()
        if choice == KIND_ADMIN_ABUSE:
            rows = _appealable_cases(state, interaction.user.id, abuse=True)
            await interaction.response.send_message(
                "If this is about a **ticket verdict**, pick that case and press **Continue with case**.\n"
                "To report **in-game admin power abuse** (slay, grow, god, teleport, revive, etc.) "
                "with no ticket, press **Report without a case**.",
                view=AbusePickView(rows),
                ephemeral=True,
            )
            return
        rows = _appealable_cases(state, interaction.user.id, abuse=False)
        if not rows:
            await interaction.response.send_message(
                "No matching cases were found on your Discord/Steam record to appeal.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "Pick the case you are appealing, then continue.",
            view=AppealCasePickView(choice, rows),
            ephemeral=True,
        )


class AbusePickView(ui.View):
    def __init__(self, rows):
        super().__init__(timeout=180)
        self.origin_id = None
        if rows:
            options = []
            for row in rows:
                cid = str(row.get("id") or "")
                if not cid:
                    continue
                label = f"{cid} · {KIND_META.get(row.get('kind'), {}).get('label', row.get('kind') or '?')}"[:100]
                verd = VERDICT_LABELS.get(row.get("verdict"), row.get("verdict") or "open")
                options.append(discord.SelectOption(label=label, description=str(verd)[:100], value=cid[:100]))
            if options:
                select = ui.Select(
                    placeholder="Optional: linked ticket case",
                    min_values=0,
                    max_values=1,
                    options=options[:25],
                )

                async def picked(interaction: discord.Interaction):
                    self.origin_id = select.values[0] if select.values else None
                    if not self.origin_id:
                        await interaction.response.send_message("No case selected.", ephemeral=True)
                        return
                    await interaction.response.send_message(
                        f"Linked `{self.origin_id}`. Press **Continue with case**, "
                        "or **Report without a case** for in-game power abuse.",
                        ephemeral=True,
                    )

                select.callback = picked
                self.add_item(select)

    @ui.button(label="Continue with case", style=discord.ButtonStyle.primary)
    async def with_case(self, interaction: discord.Interaction, button: ui.Button):
        if not self.origin_id:
            await interaction.response.send_message(
                "Pick a case first, or press **Report without a case** for in-game power abuse.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(AppealModal(KIND_ADMIN_ABUSE, self.origin_id))

    @ui.button(label="Report without a case", style=discord.ButtonStyle.danger)
    async def no_case(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message(
            "Who abused admin powers? Pick them from the list, or press **Type a name** "
            "if they are not linked / not on Discord.",
            view=AccusedPickView(KIND_ADMIN_ABUSE),
            ephemeral=True,
        )


class AppealCasePickView(ui.View):
    def __init__(self, kind, rows):
        super().__init__(timeout=180)
        self.kind = kind
        self.origin_id = None
        options = []
        for row in rows:
            cid = str(row.get("id") or "")
            if not cid:
                continue
            label = f"{cid} · {KIND_META.get(row.get('kind'), {}).get('label', row.get('kind') or '?')}"[:100]
            verd = VERDICT_LABELS.get(row.get("verdict"), row.get("verdict") or "open")
            options.append(discord.SelectOption(label=label, description=str(verd)[:100], value=cid[:100]))
        if not options:
            options.append(discord.SelectOption(label="No cases", value="none"))
        select = ui.Select(placeholder="Which case?", min_values=1, max_values=1, options=options)

        async def picked(interaction: discord.Interaction):
            self.origin_id = select.values[0]
            if self.origin_id == "none":
                await interaction.response.send_message("No case selected.", ephemeral=True)
                return
            await interaction.response.send_message(
                f"Appealing `{self.origin_id}`. Press **Continue** and explain.",
                ephemeral=True,
            )

        select.callback = picked
        self.add_item(select)

    @ui.button(label="Continue", style=discord.ButtonStyle.primary)
    async def cont(self, interaction: discord.Interaction, button: ui.Button):
        if not self.origin_id or self.origin_id == "none":
            await interaction.response.send_message("Pick a case first.", ephemeral=True)
            return
        await interaction.response.send_modal(AppealModal(self.kind, self.origin_id))


class AppealModal(ui.Modal, title="Appeal statement"):
    def __init__(self, kind, origin_id):
        super().__init__()
        self.kind = kind
        self.origin_id = origin_id
        self.details = ui.TextInput(
            label="Why should this verdict change?",
            style=discord.TextStyle.paragraph,
            max_length=1500,
            required=True,
        )
        self.add_item(self.details)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await open_appeal_ticket(interaction, self.kind, self.origin_id, str(self.details.value or "").strip())


async def open_appeal_ticket(interaction, kind, origin_id, details):
    guild = interaction.guild
    if guild is None:
        await interaction.followup.send("Appeals only work inside the Fallen Earth server.", ephemeral=True)
        return
    state = _load_state()
    origin = (state.get("cases") or {}).get(origin_id)
    if not origin:
        await interaction.followup.send("That case is not on file.", ephemeral=True)
        return
    reporter = interaction.user
    for existing in (state.get("cases") or {}).values():
        if (
            existing.get("status") == "open"
            and existing.get("kind") == kind
            and str(existing.get("reporter_id")) == str(reporter.id)
        ):
            await interaction.followup.send(
                f"You already have an open appeal: <#{existing.get('ticket_channel_id')}>",
                ephemeral=True,
            )
            return
    if origin.get("open_appeal_id"):
        other = (state.get("cases") or {}).get(origin.get("open_appeal_id")) or {}
        if other.get("status") == "open":
            await interaction.followup.send(
                f"Case `{origin_id}` already has an open appeal: <#{other.get('ticket_channel_id')}>",
                ephemeral=True,
            )
            return
    meta = KIND_META[kind]
    case_id = _new_case_id()
    notify_channel = await _channel(interaction.client, NOTIFY_CHANNEL_ID)
    category = getattr(notify_channel, "category", None) if notify_channel else None
    try:
        channel = await guild.create_text_channel(
            name=f"appeal-{reporter.name}"[:95],
            category=category,
            overwrites=_staff_overwrites(guild, reporter),
            topic=f"Primeval appeal {case_id} · origin {origin_id}",
            reason=f"Appeal ticket {case_id}",
        )
    except Exception as exc:
        await interaction.followup.send(f"Could not open a private appeal channel: {exc}", ephemeral=True)
        return
    # Reload: state may have changed while the channel was being created.
    state = _load_state()
    origin = (state.get("cases") or {}).get(origin_id) or origin
    accused_id = origin.get("accused_id") or str(reporter.id)
    accused_steam = origin.get("accused_steam") or _steam_for(reporter.id)
    case = {
        "id": case_id,
        "kind": kind,
        "status": "open",
        "created_at": int(time.time()),
        "reporter_id": str(reporter.id),
        "reporter_name": str(reporter),
        "reporter_steam": _steam_for(reporter.id),
        "accused_id": accused_id,
        "accused_name": origin.get("accused_name") or "",
        "accused_steam": accused_steam,
        "details": details,
        "origin_id": origin_id,
        "origin_verdict": origin.get("verdict"),
        "ticket_channel_id": channel.id,
        "verdict_message_id": None,
        "notify_message_id": None,
        "log_message_id": None,
        "recommendations": [],
        "handlers": [],
        "pending_act": None,
        "verdict": None,
        "claimed_by": None,
        "extra_members": [],
    }
    origin["open_appeal_id"] = case_id
    state.setdefault("cases", {})[case_id] = case
    state["cases"][origin_id] = origin
    profile = _merged_profile(state, accused_id, accused_steam)
    hist = profile.setdefault("history", [])
    if case_id not in hist:
        hist.append(case_id)
    _write_profile(state, profile, accused_id, accused_steam)
    _save_state(state)
    history_lines = _case_history_lines(state, profile)
    claim_rule = (
        "Senior Administration and Directors must claim this."
        if kind == KIND_ADMIN_ABUSE
        else "Administration or higher must claim this before a verdict."
    )
    try:
        await channel.send(
            content=f"{reporter.mention} — {claim_rule} Do not ping everyone; wait here.",
            embed=_ticket_embed(case),
            view=TicketControlView(),
        )
    except Exception:
        pass
    if notify_channel:
        try:
            msg = await notify_channel.send(
                content=f"Appeal research for `{case_id}` (origin `{origin_id}`)",
                embed=_origin_research_embed(case, origin, profile, history_lines),
            )
            case["notify_message_id"] = msg.id
        except Exception:
            pass
    verdict_channel = await _channel(interaction.client, VERDICT_CHANNEL_ID)
    if verdict_channel:
        ping = (
            _mention_role_for_rank(guild, 5) + " " + " ".join(
                role.mention for role in guild.roles
                if str(role.name).strip().lower() == "senior administrator"
            )
            if kind == KIND_ADMIN_ABUSE
            else _mention_role_for_rank(guild, 4)
        )
        try:
            vmsg = await verdict_channel.send(
                content=f"{ping} — claim this appeal, then choose a verdict.",
                embed=_verdict_embed(case, profile),
                view=_verdict_view(case_id, kind, case=case),
            )
            case["verdict_message_id"] = vmsg.id
        except Exception:
            pass
    _save_case(case)
    await interaction.followup.send(f"Opened private appeal {channel.mention} (`{case_id}`).", ephemeral=True)


async def _apply_timeout(guild, user_id, hours, reason):
    member = guild.get_member(int(user_id)) if user_id else None
    if member is None:
        try:
            member = await guild.fetch_member(int(user_id))
        except Exception:
            return "could not find Discord member for timeout"
    try:
        until = discord.utils.utcnow() + timedelta(hours=int(hours))
        await member.timeout(until, reason=reason)
        return f"Discord timeout {hours}h until {until.isoformat()}"
    except Exception as exc:
        return f"timeout failed: {exc}"


async def _apply_discord_ban(guild, user_id, reason):
    try:
        user = discord.Object(id=int(user_id))
        await guild.ban(user, reason=reason, delete_message_seconds=0)
        return "banned from Discord"
    except Exception as exc:
        return f"Discord ban failed: {exc}"


async def _apply_game_ban(steam, reason, staff=None, bot=None):
    send = _fn("send_game_command")
    if not send or not steam:
        return "no RCON or no Steam ID for game ban"
    try:
        result = await send(f"ban {steam} perm {reason}")
        if staff is not None and bot is not None:
            try:
                import primeval_admin_audit

                await primeval_admin_audit.note_discord(bot, staff, "ban", steam, reason, result)
            except Exception:
                pass
        return f"game ban: {result}"
    except Exception as exc:
        return f"game ban failed: {exc}"


async def _clear_timeout(guild, user_id, reason):
    if not user_id:
        return "no Discord ID for timeout clear"
    member = guild.get_member(int(user_id)) if guild else None
    if member is None:
        try:
            member = await guild.fetch_member(int(user_id))
        except Exception:
            return "could not find Discord member to clear timeout"
    try:
        await member.timeout(None, reason=reason)
        return "Discord timeout cleared"
    except Exception as exc:
        return f"clear timeout failed: {exc}"


async def _discord_unban(guild, user_id, reason):
    if not user_id:
        return "no Discord ID to unban"
    try:
        await guild.unban(discord.Object(id=int(user_id)), reason=reason)
        return "Discord ban lifted"
    except Exception as exc:
        return f"Discord unban failed: {exc}"


async def _game_unban(steam, reason, staff=None, bot=None):
    """Bypasses dynamic dictionary caches by calling the global framework dispatcher directly."""
    if not steam:
        return "no Steam ID for game unban"
        
    try:
        clean_steam = "".join(c for c in str(steam) if c.isdigit()).strip()
        
        # Trigger our core framework dispatcher attached directly to the live running bot instance
        if bot and hasattr(bot, "execute_raw_rcon"):
            result_str = await bot.execute_raw_rcon(f"unban {clean_steam}")
            
            if staff is not None:
                try:
                    import primeval_admin_audit
                    await primeval_admin_audit.note_discord(bot, staff, "unban", clean_steam, f"FE_Lift_{reason}", result_str)
                except Exception:
                    pass
            return f"game unban: {result_str}"
            
        return "game unban failed: Framework dispatcher link unreachable"
    except Exception as exc:
        return f"game unban failed: {exc}"



async def _lock_ticket_channel(channel, note):
    if channel is None:
        return
    try:
        await channel.send(note)
        guild = getattr(channel, "guild", None)
        if guild is not None:
            await channel.set_permissions(guild.default_role, view_channel=False)
        if not channel.name.startswith("closed-"):
            await channel.edit(name=f"closed-{channel.name}"[:95])
    except Exception:
        pass


def _appealable_cases(state, user_id, abuse=False):
    uid = str(user_id)
    steam = _steam_for(uid)
    rows = []
    for case in (state.get("cases") or {}).values():
        if case.get("kind") in (KIND_APPEAL, KIND_ADMIN_ABUSE):
            continue
        if case.get("open_appeal_id") and (state.get("cases") or {}).get(case.get("open_appeal_id"), {}).get("status") == "open":
            continue
        party_accused = str(case.get("accused_id") or "") == uid
        if steam and str(case.get("accused_steam") or "") == str(steam):
            party_accused = True
        party_any = party_accused or str(case.get("reporter_id") or "") == uid
        if abuse:
            if not party_any:
                continue
        else:
            if not party_accused:
                continue
            if not case.get("verdict") or case.get("verdict") in ("dismiss", "resolve", "grant", "uphold"):
                continue
        rows.append(case)
    rows.sort(key=lambda row: int(row.get("closed_at") or row.get("created_at") or 0))
    return rows[-25:]


async def handle_claim(interaction, case_id):
    if not _CASE_ID_RE.match(str(case_id or "")):
        await interaction.response.send_message("Unknown case.", ephemeral=True)
        return
    state = _load_state()
    case = (state.get("cases") or {}).get(case_id)
    if not case:
        await interaction.response.send_message("That case is no longer on file.", ephemeral=True)
        return
    if case.get("kind") not in (KIND_APPEAL, KIND_ADMIN_ABUSE):
        await interaction.response.send_message("Only appeal tickets are claimed this way.", ephemeral=True)
        return
    if case.get("verdict"):
        await interaction.response.send_message("This appeal already has a verdict.", ephemeral=True)
        return
    if not _can_claim_case(interaction.user, case):
        need = "Senior Administration or a Director" if case.get("kind") == KIND_ADMIN_ABUSE else "Administration or higher"
        await interaction.response.send_message(f"You cannot claim this appeal. Needs **{need}**.", ephemeral=True)
        return
    existing = str(case.get("claimed_by") or "")
    uid = str(interaction.user.id)
    if existing and existing != uid and not _is_senior_or_director(interaction.user):
        await interaction.response.send_message(
            f"Already claimed by <@{existing}>. Senior Administration or a Director can take it over.",
            ephemeral=True,
        )
        return
    case["claimed_by"] = uid
    case["claimed_at"] = int(time.time())
    _touch_handler(case, interaction.user)
    state["cases"][case_id] = case
    _save_state(state)
    await interaction.response.send_message(f"You claimed `{case_id}`.", ephemeral=True)
    profile = _merged_profile(state, case.get("accused_id") or case.get("reporter_id"), case.get("accused_steam") or case.get("reporter_steam"))
    vchan = await _channel(interaction.client, VERDICT_CHANNEL_ID)
    if vchan and case.get("verdict_message_id"):
        try:
            vmsg = await vchan.fetch_message(int(case["verdict_message_id"]))
            await vmsg.edit(
                embed=_verdict_embed(case, profile),
                view=_verdict_view(case_id, case.get("kind"), claimed=True, case=case),
            )
        except Exception:
            pass
    ticket = await _channel(interaction.client, case.get("ticket_channel_id") or 0)
    if ticket:
        try:
            await ticket.send(f"{interaction.user.mention} claimed this appeal.")
        except Exception:
            pass


def _next_rank(have, need):
    if have >= need:
        return need
    return min(5, max(need, have + 1))


async def handle_verdict(interaction, case_id, act):
    # Two staff clicking at once would otherwise both pass the "no verdict yet" check and apply it twice.
    if case_id in _VERDICTS_INFLIGHT:
        await interaction.response.send_message("A verdict is already being applied to this case.", ephemeral=True)
        return
    _VERDICTS_INFLIGHT.add(case_id)
    try:
        await _handle_verdict_locked(interaction, case_id, act)
    finally:
        _VERDICTS_INFLIGHT.discard(case_id)


async def _handle_verdict_locked(interaction, case_id, act):
    if not _staff_only(interaction):
        await interaction.response.send_message("Only staff can use the verdict panel.", ephemeral=True)
        return
    if not _CASE_ID_RE.match(str(case_id or "")) or act not in VERDICT_LABELS:
        await interaction.response.send_message("Unknown verdict control.", ephemeral=True)
        return
    need = int(VERDICT_RANKS.get(act, 5))
    have = staff_rank(interaction.user)
    state = _load_state()
    case = (state.get("cases") or {}).get(case_id)
    if not case:
        await interaction.response.send_message("That case is no longer on file.", ephemeral=True)
        return
    if case.get("verdict"):
        await interaction.response.send_message(
            f"Already closed with **{VERDICT_LABELS.get(case.get('verdict'), case.get('verdict'))}**.",
            ephemeral=True,
        )
        return
    if case.get("kind") == KIND_APPEAL or (case.get("kind") == KIND_ADMIN_ABUSE and case.get("origin_id")):
        await _handle_appeal_verdict(interaction, state, case, act)
        return
    if case.get("kind") == KIND_ADMIN_ABUSE:
        if not case.get("claimed_by"):
            await interaction.response.send_message("This ticket must be claimed before a verdict.", ephemeral=True)
            return
        if not _can_apply_appeal(interaction.user, case):
            await interaction.response.send_message(
                "Only the staff member who claimed this ticket (or Senior Administration / Directors) can apply the verdict.",
                ephemeral=True,
            )
            return
    _touch_handler(case, interaction.user)
    case.setdefault("recommendations", []).append(
        {
            "user_id": str(interaction.user.id),
            "rank": have,
            "act": act,
            "at": int(time.time()),
        }
    )
    profile = _merged_profile(state, case.get("accused_id") or None, case.get("accused_steam") or None)

    if have < need:
        ping_rank = _next_rank(have, need)
        case["pending_act"] = act
        state["cases"][case_id] = case
        _save_state(state)
        mention = _mention_role_for_rank(interaction.guild, ping_rank)
        await interaction.response.send_message(
            f"Recorded your recommendation. **{_RANK_NAMES.get(have, 'your rank')}** cannot apply "
            f"**{VERDICT_LABELS[act]}** (needs **{_RANK_NAMES.get(need)}**). Pinging {mention}.",
            ephemeral=True,
        )
        try:
            await interaction.message.edit(embed=_verdict_embed(case, profile, pending=act))
            await interaction.channel.send(
                f"{mention} — {interaction.user.mention} recommended **{VERDICT_LABELS[act]}** on `{case_id}`. "
                f"Confirm with the panel above."
            )
        except Exception:
            pass
        return

    await interaction.response.defer(ephemeral=True)
    if not case.get("standing_before"):
        case["standing_before"] = _standing_snapshot(profile)
    applied = await _execute_verdict(
        interaction.guild, case, profile, act, staff=interaction.user, bot=interaction.client
    )
    case["verdict"] = act
    case["applied_by"] = str(interaction.user.id)
    case["applied_at"] = int(time.time())
    case["pending_act"] = None
    case["status"] = "closed"
    case["closed_at"] = int(time.time())
    case["applied_text"] = applied
    state = _load_state()  # execute awaited; don't overwrite changes made meanwhile
    _write_profile(state, profile, case.get("accused_id") or None, case.get("accused_steam") or None)
    state["cases"][case_id] = case
    _save_state(state)

    try:
        await interaction.message.edit(
            content=f"Verdict applied by {interaction.user.mention}.",
            embed=_verdict_embed(case, profile),
            view=_verdict_view(case_id, case.get("kind"), disabled=True, case=case),
        )
    except Exception:
        pass

    ticket = await _channel(interaction.client, case.get("ticket_channel_id") or 0)
    if ticket:
        try:
            await ticket.send(
                f"Staff reached a verdict: **{VERDICT_LABELS.get(act)}**.\n"
                "This ticket will stay open until staff close it."
            )
        except Exception:
            pass

    log_channel = await _channel(interaction.client, LOG_CHANNEL_ID)
    if log_channel:
        try:
            msg = await log_channel.send(embed=_log_embed(case, profile, applied))
            case["log_message_id"] = msg.id
            _save_case(case)
        except Exception:
            pass
    await interaction.followup.send(f"Applied **{VERDICT_LABELS[act]}**. {applied}", ephemeral=True)


async def _execute_verdict(guild, case, profile, act, staff=None, bot=None):
    bits = []
    reason = f"Primeval case {case.get('id')}"
    accused_id = case.get("accused_id")
    steam = case.get("accused_steam") or profile.get("steam")

    if act == "dismiss" or act == "resolve":
        bits.append("no punishment")
        return "; ".join(bits)

    if act.startswith("dc"):
        n = int(act[2:])
        profile["discord_strikes"] = max(int(profile.get("discord_strikes") or 0), n)
        bits.append(f"Discord strikes set to {profile['discord_strikes']}/3")
        if n >= 3 and accused_id:
            bits.append(await _apply_discord_ban(guild, accused_id, reason + " Discord strike 3"))

    if act.startswith("ig"):
        n = int(act[2:])
        profile["ingame_strikes"] = max(int(profile.get("ingame_strikes") or 0), n)
        bits.append(f"in-game strikes set to {profile['ingame_strikes']}/5")
        if n >= 1:
            profile["shop_blocked"] = True
            profile["events_blocked"] = True
            bits.append("dino shop + events locked")
        if n == 3 and accused_id:
            bits.append(await _apply_timeout(guild, accused_id, 24, reason + " in-game strike 3"))
        if n == 4 and accused_id:
            bits.append(await _apply_timeout(guild, accused_id, 48, reason + " in-game strike 4"))
        if n >= 5:
            bits.append(await _apply_game_ban(steam, reason + " in-game strike 5", staff=staff, bot=bot))

    return "; ".join(bits) if bits else "recorded"


async def _undo_original_punishment(guild, origin, profile, staff=None, bot=None):
    origin = origin or {}
    act = str(origin.get("verdict") or "")
    accused_id = origin.get("accused_id")
    steam = origin.get("accused_steam") or profile.get("steam")
    before = _guess_standing_before(origin, profile)
    profile["ingame_strikes"] = int(before.get("ingame_strikes") or 0)
    profile["discord_strikes"] = int(before.get("discord_strikes") or 0)
    profile["shop_blocked"] = bool(before.get("shop_blocked")) or int(profile["ingame_strikes"]) >= 1
    profile["events_blocked"] = bool(before.get("events_blocked")) or int(profile["ingame_strikes"]) >= 1
    if int(profile["ingame_strikes"]) <= 0:
        profile["shop_blocked"] = False
        profile["events_blocked"] = False
    bits = [
        f"restored standing to in-game {profile['ingame_strikes']}/5 · Discord {profile['discord_strikes']}/3"
    ]
    reason = f"Primeval appeal of {origin.get('id')}"
    if act in ("ig3", "ig4"):
        bits.append(await _clear_timeout(guild, accused_id, reason))
    if act == "dc3":
        bits.append(await _discord_unban(guild, accused_id, reason))
    if act == "ig5":
        bits.append(await _game_unban(steam, reason, staff=staff, bot=bot))
    origin["punishment_undone"] = True
    origin["undone_at"] = int(time.time())
    return "; ".join(bits)


async def _handle_appeal_verdict(interaction, state, case, act):
    if act not in ("grant", "uphold"):
        await interaction.response.send_message("Use **Grant appeal** or **Deny appeal** on this panel.", ephemeral=True)
        return
    if not case.get("claimed_by"):
        await interaction.response.send_message("This appeal must be claimed before a verdict.", ephemeral=True)
        return
    if not _can_apply_appeal(interaction.user, case):
        await interaction.response.send_message(
            "Only the staff member who claimed this appeal (or Senior Administration / Directors) can apply the verdict.",
            ephemeral=True,
        )
        return
    if not _can_claim_case(interaction.user, case) and not _is_senior_or_director(interaction.user):
        await interaction.response.send_message("Your rank cannot finish this appeal.", ephemeral=True)
        return
    have = staff_rank(interaction.user)
    _touch_handler(case, interaction.user)
    case.setdefault("recommendations", []).append(
        {
            "user_id": str(interaction.user.id),
            "rank": have,
            "act": act,
            "at": int(time.time()),
        }
    )
    origin = (state.get("cases") or {}).get(case.get("origin_id") or "") or {}
    profile = _merged_profile(
        state,
        origin.get("accused_id") or case.get("reporter_id"),
        origin.get("accused_steam") or case.get("reporter_steam"),
    )
    await interaction.response.defer(ephemeral=True)
    if act == "grant":
        applied = await _undo_original_punishment(
            interaction.guild, origin, profile, staff=interaction.user, bot=interaction.client
        )
        state = _load_state()  # undo awaited; start from fresh state before saving
        if origin.get("id"):
            origin["appeal_result"] = "grant"
            origin["open_appeal_id"] = None
            state["cases"][origin["id"]] = origin
        _write_profile(
            state,
            profile,
            origin.get("accused_id") or case.get("reporter_id"),
            origin.get("accused_steam") or case.get("reporter_steam"),
        )
    else:
        applied = "original verdict stands; no punishment undone"
        if origin.get("id"):
            origin["appeal_result"] = "uphold"
            origin["open_appeal_id"] = None
            state["cases"][origin["id"]] = origin
    case["verdict"] = act
    case["applied_by"] = str(interaction.user.id)
    case["applied_at"] = int(time.time())
    case["pending_act"] = None
    case["status"] = "closed"
    case["closed_at"] = int(time.time())
    case["applied_text"] = applied
    state["cases"][case["id"]] = case
    _save_state(state)
    try:
        await interaction.message.edit(
            content=f"Appeal verdict by {interaction.user.mention}.",
            embed=_verdict_embed(case, profile),
            view=_verdict_view(case["id"], case.get("kind"), disabled=True, claimed=True, case=case),
        )
    except Exception:
        pass
    ticket = await _channel(interaction.client, case.get("ticket_channel_id") or 0)
    if act == "grant":
        await _lock_ticket_channel(
            ticket,
            f"Appeal **granted** by {interaction.user.mention}. Original punishment on `{case.get('origin_id')}` was undone. Ticket closed.",
        )
    elif ticket:
        try:
            await ticket.send(
                f"Appeal **denied** by {interaction.user.mention}. Original verdict on `{case.get('origin_id')}` stands.\n"
                "Staff can close this ticket when statements are finished."
            )
        except Exception:
            pass
    log_channel = await _channel(interaction.client, LOG_CHANNEL_ID)
    if log_channel:
        try:
            msg = await log_channel.send(embed=_log_embed(case, profile, applied))
            case["log_message_id"] = msg.id
            _save_case(case)
        except Exception:
            pass
    await interaction.followup.send(f"Applied **{VERDICT_LABELS[act]}**. {applied}", ephemeral=True)

class AdminPunishmentModal(ui.Modal):
    def __init__(self, strike_tier: str, label: str):
        super().__init__(title=f"Apply Punishment: {label}")
        self.strike_tier = strike_tier
        self.label = label
        
        self.user_id_input = ui.TextInput(
            label="Discord User ID",
            placeholder="e.g., 451932369897484123",
            min_length=15,
            max_length=25,
            required=True
        )
        self.reason_input = ui.TextInput(
            label="Reason for Punishment",
            style=discord.TextStyle.paragraph,
            placeholder="Specify rule broken, ticket reference, or coordinates...",
            required=True
        )
        self.add_item(self.user_id_input)
        self.add_item(self.reason_input)

    async def on_submit(self, interaction: discord.Interaction):
        # 1. Validate Target User ID
        try:
            target_id = int(self.user_id_input.value.strip())
        except ValueError:
            await interaction.response.send_message("❌ Invalid Discord ID format. Must be numeric digits only.", ephemeral=True)
            return

        # 2. Staff Rank Permission Shield Validation Check
        s_rank = staff_rank(interaction.user)
        required_rank = VERDICT_RANKS.get(self.strike_tier, 2)
        if s_rank < required_rank:
            await interaction.response.send_message(
                f"❌ Unauthorized. Your staff tier ({_RANK_NAMES.get(s_rank, 'Unknown')}) "
                f"cannot apply a {self.label}. Requires **{_RANK_NAMES.get(required_rank)}** or higher.",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        # 3. Pull Backend Profiles & Sync Changes directly onto local database layout
        state = _load_state()
        steam_id = _steam_for(target_id)
        profile = _merged_profile(state, discord_id=target_id, steam=steam_id)
        
        case_id = _new_case_id()
        
        # --- WIPE LAYER INTERACTION MODIFIER ---
        if self.strike_tier == "wipe":
            profile["discord_strikes"] = 0
            profile["ingame_strikes"] = 0
            profile["shop_blocked"] = False
            profile["events_blocked"] = False
            profile["history"] = []
            current_strikes = 0
            track_name = "Cleared History"
        elif self.strike_tier.startswith("dc"):
            profile["discord_strikes"] = int(profile.get("discord_strikes") or 0) + 1
            current_strikes = profile["discord_strikes"]
            track_name = "Discord Strikes"
        else:
            profile["ingame_strikes"] = int(profile.get("ingame_strikes") or 0) + 1
            current_strikes = profile["ingame_strikes"]
            track_name = "In-game Strikes"
            if self.strike_tier in ["ig1", "ig2"]:
                profile["shop_blocked"] = True
                profile["events_blocked"] = True

        case_log = {
            "case_id": case_id,
            "verdict": self.strike_tier,
            "reason": self.reason_input.value,
            "staff_id": interaction.user.id,
            "timestamp": int(time.time())
        }
        
        # Keep tracking case logs natively for historical tracking, skip appending list index on global clear
        if self.strike_tier != "wipe":
            profile.setdefault("history", []).append(case_id)
            
        state["cases"][case_id] = case_log
        
        _write_profile(state, profile, discord_id=target_id, steam=steam_id)
        _save_state(state)

        # ======================================================================
        # 🔑 FIXED: STEAM ID JSON EXTRACTION LOADER (RESOLVES NAMEERROR)
        # ======================================================================
        clean_steam = None
        
        try:
            if os.path.exists("steam_links.json"):
                with open("steam_links.json", "r", encoding="utf-8") as sl_file:
                    links_data = json.load(sl_file)
                    if str(target_id) in links_data:
                        clean_steam = str(links_data[str(target_id)]).strip()
        except Exception as sl_err:
            print(f"[PUNISH PANEL JSON ERROR] Failed reading steam_links.json: {sl_err}")

        # Fallback to standard core variables if JSON lookup didn't yield a value
        if not clean_steam and steam_id:
            clean_steam = "".join(char for char in str(steam_id) if char.isdigit()).strip()

        # ======================================================================
        # 🔌 🎯 EVRIMA MANAGER MODE: PURE SYSTEM DISPATCHER SHIELD (BARE COMMAND)
        # ======================================================================
        if clean_steam and len(clean_steam) >= 15:
            # Grab our fresh core framework dispatcher directly out of the live bot client object
            rcon_dispatcher = getattr(interaction.client, "execute_raw_rcon", None)
            
            if rcon_dispatcher and callable(rcon_dispatcher):
                try:
                    # Modelled exactly like Evrima Manager terminal outputs:
                    if self.strike_tier == "wipe":
                        await rcon_dispatcher(f"unban {clean_steam}")
                    elif self.strike_tier in ["ig3", "ig4", "ig5"]:
                        await rcon_dispatcher(f"ban {clean_steam}")
                    elif self.strike_tier in ["ig1", "ig2"]:
                        await rcon_dispatcher(f"kick {clean_steam}")
                        
                except Exception as rcon_err:
                    print(f"[PUNISH PANEL RCON ERROR] Dispatcher routing transmission failed: {rcon_err}")
            else:
                # Direct fallback routing link if dispatcher failed binding sequence
                rcon_pipe = G.get("queue_isle_cmd_flag") or G.get("queue_verb")
                if rcon_pipe and callable(rcon_pipe):
                    if self.strike_tier == "wipe":
                        rcon_pipe(clean_steam, f"unban {clean_steam}")
                    elif self.strike_tier in ["ig3", "ig4", "ig5"]:
                        rcon_pipe(clean_steam, f"ban {clean_steam}")
                    elif self.strike_tier in ["ig1", "ig2"]:
                        rcon_pipe(clean_steam, f"kick {clean_steam}")

            # Independent logging execution layer (keeps your detailed audit logs perfect!)
            try:
                import primeval_admin_audit
                
                # Assign dynamic audit verb for backend logs
                audit_action_verb = "kick"
                if self.strike_tier == "wipe":
                    audit_action_verb = "unban"
                elif self.strike_tier in ["ig3", "ig4", "ig5"]:
                    audit_action_verb = "ban"
                    
                await primeval_admin_audit.note_discord(
                    interaction.client, 
                    interaction.user, 
                    audit_action_verb, 
                    clean_steam, 
                    f"FE_Case_{case_id}_{self.label}", 
                    "Success"
                )
            except Exception:
                pass
        else:
            print(f"[PUNISH PANEL RCON WARN] Could not trigger game command for Discord ID {target_id}. No linked Steam ID found in steam_links.json.")
            try:
                audit_channel = interaction.guild.get_channel(LOG_CHANNEL_ID)
                if audit_channel:
                    msg_type_string = "Unban" if self.strike_tier == "wipe" else "Ban"
                    await audit_channel.send(f"⚠️ **RCON {msg_type_string} Skipped:** Discord user <@{target_id}> does not have a linked Steam account in `steam_links.json`.")
            except Exception:
                pass

        # ======================================================================
        # 📥 DISCORD LOGGING AND CONFIRMATION DISPATCHER
        # ======================================================================
        # Select custom layout aesthetics depending on clear versus punishment
        embed_color_accent = discord.Color.orange()
        if self.strike_tier == "wipe":
            embed_color_accent = discord.Color.green()
        elif "Perm" in self.label:
            embed_color_accent = discord.Color.red()

        verdict_embed = discord.Embed(
            title=f"⚖️ STAFF VERDICT LOG | Case #{case_id}",
            color=embed_color_accent,
            timestamp=discord.utils.utcnow()
        )
        verdict_embed.add_field(name="Target User", value=f"<@{target_id}>\nID: `{target_id}`", inline=True)
        verdict_embed.add_field(name="Enforcing Staff", value=f"{interaction.user.mention}", inline=True)
        
        if self.strike_tier == "wipe":
            verdict_embed.add_field(name="Action Enforced", value=f"**{self.label}**\nTotal History State: `Wiped & Cleared`", inline=False)
        else:
            verdict_embed.add_field(name="Action Enforced", value=f"**{self.label}**\nTotal {track_name}: `{current_strikes}`", inline=False)
            
        verdict_embed.add_field(name="Reason Given", value=f"```{self.reason_input.value}```", inline=False)
        verdict_embed.set_footer(text=f"Fallen Earth Enforcement Matrix • Case ID {case_id}")
        
        # --- FIXED DOUBLE CHANNEL LOGGING LOOP ---
        channels_to_notify = [VERDICT_CHANNEL_ID, LOG_CHANNEL_ID]
        for cid in channels_to_notify:
            try:
                ch = interaction.guild.get_channel(cid) or await interaction.guild.fetch_channel(cid)
                if ch:
                    await ch.send(embed=verdict_embed)
            except Exception as e:
                print(f"[PUNISH PANEL DISCORD ERROR] Failed sending log to channel ID {cid}: {e}")

        await interaction.followup.send(content=f"✅ Successfully processed {self.label} action wrapper.",
         ephemeral=True
         )






class StaffPunishmentControlView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    # --- ROW 0: IN-GAME SYSTEM TRACK ---
    @ui.button(label="IG Strike 1", style=discord.ButtonStyle.secondary, custom_id="fe_punish_ig_1", emoji="🎮", row=0)
    async def ig_one(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(AdminPunishmentModal(strike_tier="ig1", label="IG Strike 1"))

    @ui.button(label="IG Strike 2", style=discord.ButtonStyle.secondary, custom_id="fe_punish_ig_2", emoji="🔇", row=0)
    async def ig_two(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(AdminPunishmentModal(strike_tier="ig2", label="IG Strike 2"))

    @ui.button(label="IG Strike 3 (24h)", style=discord.ButtonStyle.secondary, custom_id="fe_punish_ig_3", emoji="⏳", row=0)
    async def ig_three(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(AdminPunishmentModal(strike_tier="ig3", label="IG Strike 3 (24h)"))

    @ui.button(label="IG Strike 4 (48h)", style=discord.ButtonStyle.secondary, custom_id="fe_punish_ig_4", emoji="🛑", row=0)
    async def ig_four(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(AdminPunishmentModal(strike_tier="ig4", label="IG Strike 4 (48h)"))

    @ui.button(label="IG Strike 5 (Perm)", style=discord.ButtonStyle.danger, custom_id="fe_punish_ig_5", emoji="🔨", row=0)
    async def ig_five(self, interaction: discord.Interaction, button: ui.Button):
        # Shortened label from 51 characters down to 28 characters total!
        await interaction.response.send_modal(AdminPunishmentModal(strike_tier="ig5", label="IG Strike 5 (Perm Ban)"))

    # --- ROW 1: DISCORD SYSTEM TRACK ---
    @ui.button(label="DC Strike 1", style=discord.ButtonStyle.primary, custom_id="fe_punish_dc_1", emoji="💬", row=1)
    async def dc_one(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(AdminPunishmentModal(strike_tier="dc1", label="DC Strike 1"))

    @ui.button(label="DC Strike 2", style=discord.ButtonStyle.primary, custom_id="fe_punish_dc_2", emoji="⚠️", row=1)
    async def dc_two(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(AdminPunishmentModal(strike_tier="dc2", label="DC Strike 2"))

    @ui.button(label="DC Strike 3 (Perm)", style=discord.ButtonStyle.danger, custom_id="fe_punish_dc_3", emoji="🚨", row=1)
    async def dc_three(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(AdminPunishmentModal(strike_tier="dc3", label="DC Strike 3 (Perm Ban)"))

    # --- ROW 2: MANAGEMENT ACTIONS ---
    @ui.button(label="Undo / Remove Punishment Case", style=discord.ButtonStyle.success, custom_id="fe_punish_undo_final", emoji="🔄", row=2)
    async def undo_punishment(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(AdminUndoModal())

    @discord.ui.button(
        label="Wipe Player Records & Unban", 
        style=discord.ButtonStyle.danger, 
        emoji="🧽",
        custom_id="fallen_earth_global_wipe_btn",
        row=2  # Adjust row placement index (0-4) to fit nicely within your panel's current layout
    )
    async def trigger_wipe_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 1. Staff Rank Permission Shield Validation Check
        s_rank = staff_rank(interaction.user)
        required_rank = VERDICT_RANKS.get("wipe", 3)  # Adjust default fallback integer to your highest rank value
        
        if s_rank < required_rank:
            await interaction.response.send_message(
                f"❌ Unauthorized. Your staff tier ({_RANK_NAMES.get(s_rank, 'Unknown')}) "
                f"cannot execute data wipes. Requires **{_RANK_NAMES.get(required_rank, 'Management')}**.",
                ephemeral=True
            )
            return

        # 2. Launch the dynamic modal passing 'wipe' as the structural tier configuration element
        await interaction.response.send_modal(
            AdminPunishmentModal(strike_tier="wipe", label="Wipe All History & Unban")
        )



@app_commands.command(name="punishment_panel", description="Spawns the operational staff button action dashboard panel.")
@app_commands.checks.has_permissions(administrator=True)
async def punishment_panel(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🛡️ FALLEN EARTH | ADMINISTRATIVE ACTION MATRIX",
        description=(
            "Use this secure dashboard matrix to apply disciplinary strike profiles against players directly.\n\n"
            "**Instructions:**\n"
            "1️⃣ Press the corresponding action index button target tier below.\n"
            "2️⃣ Enter the user's explicit Discord Snowflake ID mapping profile.\n"
            "3️⃣ Document an administrative justification reasoning log brief detail tracking account."
        ),
        color=discord.Color.dark_red()
    )
    embed.set_footer(text="Fallen Earth Staff Utility Board Console Access Control Enabled")
    
    # Send layout interface matrix block attaching custom interactive buttons view stack
    await interaction.response.send_message(embed=embed, view=StaffPunishmentControlView())

def register_views(bot):
    # ⚖️ Register the Fallen Earth persistent punishment panel view
    bot.add_view(StaffPunishmentControlView())
    
    bot.add_view(ReportPanelView())
    bot.add_view(AppealPanelView())
    bot.add_view(TicketControlView())
    try:
        bot.add_dynamic_items(VerdictButton)
        bot.add_dynamic_items(ClaimButton)
    except Exception:
        pass
    try:
        import primeval_suggestions

        primeval_suggestions.register_views(bot)
    except Exception:
        pass
    try:
        import primeval_patreon

        primeval_patreon.register_views(bot)
        primeval_patreon.attach(bot)
    except Exception:
        pass
    try:
        import primeval_qol

        primeval_qol.register_views(bot)
    except Exception:
        pass


class AdminUndoModal(ui.Modal):
    def __init__(self):
        super().__init__(title="Reverse / Remove Punishment")
        
        self.case_id_input = ui.TextInput(
            label="Case ID to Undo",
            placeholder="e.g., t6a7b2c",
            min_length=6,
            max_length=16,
            required=True
        )
        self.reason_input = ui.TextInput(
            label="Reason for Removal / Appeal Grant",
            style=discord.TextStyle.paragraph,
            placeholder="Provide context on why this strike is being removed...",
            required=True
        )
        self.add_item(self.case_id_input)
        self.add_item(self.reason_input)

    async def on_submit(self, interaction: discord.Interaction):
        case_id = self.case_id_input.value.strip().lower()

        # 1. Staff Rank Permission Shield Validation Check
        # Reversing a case maps to 'grant', which requires Admin/Senior Admin (Rank 4) in your backend
        s_rank = staff_rank(interaction.user)
        required_rank = VERDICT_RANKS.get("grant", 4)
        if s_rank < required_rank:
            await interaction.response.send_message(
                f"❌ Unauthorized. Reversing cases requires **{_RANK_NAMES.get(required_rank)}** permissions.",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        # 2. Database Lookup
        state = _load_state()
        cases = state.get("cases") or {}
        
        if case_id not in cases:
            await interaction.followup.send(f"❌ Case ID `{case_id}` could not be found in the database records.", ephemeral=True)
            return
            
        old_case = cases[case_id]
        
        # Prevent double-undo scenarios
        if old_case.get("verdict") == "grant" or old_case.get("reversed"):
            await interaction.followup.send(f"❌ Case ID `{case_id}` has already been reversed or is an appeal grant itself.", ephemeral=True)
            return

        # 3. Locate User linked to that Case ID
        target_user_id = None
        profiles = state.get("profiles") or {}
        
        # Scrape profiles database to find which user owned this historical case reference link
        for p_key, p_data in profiles.items():
            if case_id in (p_data.get("history") or []):
                if p_key.startswith("d:"):
                    target_user_id = int(p_key[2:])
                    break

        if not target_user_id:
            await interaction.followup.send(f"❌ Could not trace a Discord user profile linked to Case ID `{case_id}`.", ephemeral=True)
            return

        # 4. Process Profile Metric Reductions
        steam_id = _steam_for(target_user_id)
        profile = _merged_profile(state, discord_id=target_user_id, steam=steam_id)
        original_verdict = str(old_case.get("verdict") or "")
        # Only strike verdicts have strikes to remove; dismiss/resolve/uphold would wrongly decrement in-game strikes.
        if not (original_verdict[:2] in ("ig", "dc") and original_verdict[2:].isdigit()):
            await interaction.followup.send(
                f"❌ Case `{case_id}` has no strike to undo (verdict: `{original_verdict or 'none'}`).",
                ephemeral=True,
            )
            return

        # Decrement tracks safely based on the original mistake action framework type rules
        if original_verdict.startswith("dc"):
            profile["discord_strikes"] = max(0, int(profile.get("discord_strikes") or 1) - 1)
            current_strikes = profile["discord_strikes"]
            track_name = "Discord Strikes"
        else:
            profile["ingame_strikes"] = max(0, int(profile.get("ingame_strikes") or 1) - 1)
            current_strikes = profile["ingame_strikes"]
            track_name = "In-game Strikes"
            
            # Automatically restore access profiles if user drops completely back down to clean standing
            if current_strikes == 0:
                profile["shop_blocked"] = False
                profile["events_blocked"] = False

        # Mark original case file as globally defunct/reversed inside json array sequences 
        old_case["reversed"] = True
        old_case["reversed_by"] = interaction.user.id
        old_case["reversed_reason"] = self.reason_input.value
        
        # Register a brand new closing tracking element link action
        new_case_id = _new_case_id()
        reversal_log = {
            "case_id": new_case_id,
            "verdict": "grant",
            "reason": f"Undid Case #{case_id}: {self.reason_input.value}",
            "staff_id": interaction.user.id,
            "timestamp": int(time.time())
        }
        profile.setdefault("history", []).append(new_case_id)
        state["cases"][new_case_id] = reversal_log

        _write_profile(state, profile, discord_id=target_user_id, steam=steam_id)
        _save_state(state)

        # ======================================================================
        # 🔌 🎮 LIVE NATIVE GAME UNBAN REMOVAL PIPELINE (PASTE IT HERE!)
        # ======================================================================
        if steam_id and original_verdict in ["ig3", "ig4", "ig5"]:
            try:
                reason_str = f"Reversal_Of_Case_{case_id}"
                await _game_unban(steam_id, reason_str, staff=interaction.user, bot=interaction.client)
            except Exception as e:
                print(f"[PUNISH PANEL UNBAN EXCEPTION] Failed lifting in-game restriction: {e}")

        if original_verdict == "dc3":
            await _discord_unban(interaction.guild, target_user_id, f"Reversal_Of_Case_{case_id}")

        # 5. Broadcast Reversal Payload to Verdict Display Window Dashboards
        undo_embed = discord.Embed(
            title=f"🔄 PUNISHMENT REMOVED / REVERSED | Case #{new_case_id}",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow()
        )

        undo_embed.add_field(name="Target User", value=f"<@{target_user_id}>\nID: `{target_user_id}`", inline=True)
        undo_embed.add_field(name="Reversing Staff", value=f"{interaction.user.mention}", inline=True)
        undo_embed.add_field(name="Action Cleared", value=f"Undid Case #{case_id} (`{original_verdict}`)\nNew {track_name}: `{current_strikes}`", inline=False)
        undo_embed.add_field(name="Reason for Removal", value=f"```{self.reason_input.value}```", inline=False)
        undo_embed.set_footer(text=f"Fallen Earth Enforcement Matrix • Reversal Ref #{new_case_id}")

        channels_to_notify = [VERDICT_CHANNEL_ID, LOG_CHANNEL_ID]
        for cid in channels_to_notify:
            try:
                ch = interaction.guild.get_channel(cid) or await interaction.guild.fetch_channel(cid)
                if ch:
                    await ch.send(embed=undo_embed)
            except Exception as e:
                print(f"[PUNISH PANEL REVERSAL EXCEPTION] Failed logging into channel {cid}: {e}")

        await interaction.followup.send(
            f"✅ **Punishment Successfully Reversed!**\nCase ID `{case_id}` has been cleared. Target <@{target_user_id}> now has `{current_strikes}` active {track_name}.",
            ephemeral=True
        )


def register_slash(bot):
    existing = {cmd.name for cmd in bot.tree.get_commands()}
    
# ⚖️ Fallen Earth Punishment Panel Command Addition
    if "punishment_panel" not in existing:
        @bot.tree.command(name="punishment_panel", description="Spawns the operational staff button action dashboard panel")
        @app_commands.checks.has_permissions(administrator=True)
        async def punishment_panel_cmd(interaction: discord.Interaction):
            embed = discord.Embed(
                title="🛡️ FALLEN EARTH | ADMINISTRATIVE ACTION MATRIX",
                description=(
                    "Use this secure control interface to apply disciplinary profile steps across system tracks.\n\n"
                    "🎮 **IN-GAME STRIKE TRACK (Row 1):**\n"
                    "• **Strike 1 & 2:** Dino Shop + Events Access Locked.\n"
                    "• **Strike 3:** 24-Hour Server Ban *(Auto-Lifts)*.\n"
                    "• **Strike 4:** 48-Hour Server Ban *(Auto-Lifts)*.\n"
                    "• **Strike 5:** Permanent In-Game Account Blacklist.\n\n"
                    "💬 **DISCORD STRIKE TRACK (Row 2):**\n"
                    "• **Strike 1:** Server Warning Profile Entry.\n"
                    "• **Strike 2:** Temporary Chat Mute Restriction.\n"
                    "• **Strike 3:** Permanent Discord Server Ban.\n\n"
                    "🔄 **REVERSING CONSEQUESNCES (Row 3):**\n"
                    "Click the green **Undo** button and type in the unique **Case ID** (e.g., `t6b1a2c`) from the log records to wipe a mistaken penalty file step."
                ),
                color=discord.Color.dark_red()
            )
            embed.set_footer(text="Fallen Earth Staff Utility Board Console Access Control Enabled")
            await interaction.response.send_message(embed=embed, view=StaffPunishmentControlView())

    if "report_panel" not in existing:
        # ... (rest of your existing panels remain below) ...


        @bot.tree.command(name="report_panel", description="Post the public report ticket panel")
        async def report_panel(interaction: discord.Interaction):
            await post_report_panel(interaction)

    if "appeal_panel" not in existing:

        @bot.tree.command(name="appeal_panel", description="Post the public appeal ticket panel")
        async def appeal_panel(interaction: discord.Interaction):
            await post_appeal_panel(interaction)

    try:
        import primeval_suggestions

        primeval_suggestions.register_slash(bot)
    except Exception:
        pass
    try:
        import primeval_patreon

        primeval_patreon.register_slash(bot)
    except Exception:
        pass
    try:
        import primeval_tally

        primeval_tally.register_slash(bot)
    except Exception:
        pass
