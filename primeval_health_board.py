"""Live Discord health board + Pacific clock Isle restart schedule.

Call bind(globals()) after GHB/RCON helpers exist, register_slash(bot),
then start(bot) from on_ready.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from discord.ext import tasks

G = {}

HEALTH_CHANNEL_ID = 1541157110262399076
RESTART_EVERY_SECONDS = 6 * 60 * 60
RESTART_HOURS = (0, 12, 18)
SKIP_AFTER_BOOT_SECONDS = 45 * 60
SKIP_IF_NEXT_WITHIN = 90 * 60
SCHEDULE_TZ_NAME = "America/Los_Angeles"
SCHEDULE_KIND = "clock_pacific"
BOARD_TICK_SECONDS = 90
WARN_10 = 10 * 60
WARN_5 = 5 * 60
WARN_2 = 2 * 60
CLOSE_SECONDS = 15 * 60
STATE_PATH = "isle_health_board.json"
BOARD_TITLE = "Fallen Earth | Server Board"
# In-game RCON banners (clock + Director). 5 min first, 2 min last call.
MSG_WARN_5 = (
    "Fallen Earth restart in 5 minutes. Finish fights and find a safe log-out."
)
MSG_WARN_2 = (
    "LAST CALL — Fallen Earth restart in 2 minutes. Safe log now; restart is imminent."
)
MSG_RESTART_NOW = (
    "Fallen Earth is restarting now. Hold tight — we'll be back shortly."
)

_task = None
_clock_warn_task = None
_STATE_LOCK = asyncio.Lock()


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


def _fmt_duration(seconds):
    seconds = max(0, int(seconds or 0))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _light(level):
    if level == "stop":
        return "🛑"
    if level == "warn":
        return "🟡"
    return "🟢"


def _embed_color(level):
    if level == "stop":
        return discord.Color.dark_red()
    if level == "warn":
        return discord.Color.gold()
    return discord.Color.green()


def _metric_light(ok, warn):
    if not ok:
        return "🛑"
    if warn:
        return "🟡"
    return "🟢"


async def _ghb(method, path, json_body=None, data_body=None):
    import primeval_isle

    if json_body is not None:
        return await primeval_isle.request_json(method, path, json_body=json_body)
    status, text = await primeval_isle.request(method, path, body=data_body)
    if status == 0:
        return status, {"error": text}
    parsed = {}
    if text:
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = {"raw": str(text)[:400]}
    return status, parsed


async def fetch_isle_stats():
    status, resources = await _ghb("GET", "/resources")
    _st, details = await _ghb("GET", "")
    attrs = (resources or {}).get("attributes") or {}
    res = attrs.get("resources") or {}
    limits = ((details or {}).get("attributes") or {}).get("limits") or {}
    state = str(attrs.get("current_state") or "unknown")
    uptime_ms = int(res.get("uptime") or 0)
    mem_bytes = int(res.get("memory_bytes") or 0)
    cpu_abs = float(res.get("cpu_absolute") or 0)
    mem_limit_mb = int(limits.get("memory") or 0)
    cpu_limit = float(limits.get("cpu") or 0)
    mem_pct = 0.0
    if mem_limit_mb > 0:
        mem_pct = (mem_bytes / (mem_limit_mb * 1024 * 1024)) * 100.0
    cpu_pct = cpu_abs
    if cpu_limit > 0:
        cpu_pct = (cpu_abs / cpu_limit) * 100.0
    return {
        "ok": status in (200, 204) and not attrs.get("is_suspended"),
        "http": status,
        "state": state,
        "uptime_ms": uptime_ms,
        "mem_bytes": mem_bytes,
        "mem_limit_mb": mem_limit_mb,
        "mem_pct": mem_pct,
        "cpu_pct": cpu_pct,
        "error": (resources or {}).get("error"),
    }


def _parse_player_count(blob):
    if not blob:
        return None
    low = str(blob).lower()
    if "auth failed" in low or "connection error" in low or "missing" in low:
        return None
    lines = []
    for raw in str(blob).replace("\x00", "\n").splitlines():
        line = raw.strip()
        if not line:
            continue
        head = line.lower()
        if head.startswith(("player", "name", "steam", "command", "---", "index", "id")):
            continue
        if "executed" in head or "no response" in head:
            continue
        lines.append(line)
    if not lines:
        return 0
    return len(lines)


async def fetch_players():
    send = _fn("send_game_command")
    if send:
        try:
            blob = await send("playerlist")
            n = _parse_player_count(blob)
            if n is not None:
                return n, blob
        except Exception:
            pass
    head = G.get("LIVE_HEADCOUNT") or {}
    if isinstance(head, dict) and head:
        return sum(int(v or 0) for v in head.values() if str(v).isdigit() or isinstance(v, (int, float))), "headcount"
    return None, ""


def _schedule_tz():
    try:
        return ZoneInfo(SCHEDULE_TZ_NAME)
    except Exception:
        return timezone(timedelta(hours=-7))


def _next_clock_restart(after_ts):
    tz = _schedule_tz()
    start = datetime.fromtimestamp(after_ts, tz=tz)
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    for day_offset in range(0, 3):
        base = day + timedelta(days=day_offset)
        for hour in RESTART_HOURS:
            cand = base.replace(hour=hour, minute=0, second=0, microsecond=0)
            ts = cand.timestamp()
            if ts > after_ts + 0.5:
                return ts
    return after_ts + RESTART_EVERY_SECONDS


def _ensure_schedule(state, uptime_s, running):
    now = time.time()
    nxt = float(state.get("next_restart_at") or 0)
    last_up = float(state.get("last_uptime_s") or 0)
    reboot = running and last_up > 180 and uptime_s < 120
    if reboot:
        state["warned_10"] = False
        state["warned_5"] = False
        state["warned_2"] = False
        state["restarting"] = False
        state["last_boot_at"] = now
        nxt = _next_clock_restart(now)
    elif state.get("schedule_kind") != SCHEDULE_KIND or nxt <= 0:
        nxt = _next_clock_restart(now)
    elif nxt < now - 90 and not state.get("restarting"):
        nxt = _next_clock_restart(now)
    # Manual / crash reboot close to a clock slot: skip that bounce.
    if running and uptime_s < SKIP_AFTER_BOOT_SECONDS and nxt > now:
        if (nxt - now) <= SKIP_IF_NEXT_WITHIN:
            skipped = nxt
            nxt = _next_clock_restart(nxt)
            state["warned_10"] = False
            state["warned_5"] = False
            state["warned_2"] = False
            state["restarting"] = False
            print(
                f"[HEALTH BOARD] skipped clock restart at {int(skipped)}; "
                f"uptime {_fmt_duration(uptime_s)}, next {int(nxt)}"
            )
    state["schedule_kind"] = SCHEDULE_KIND
    state["next_restart_at"] = nxt
    state["last_uptime_s"] = uptime_s
    return state


def _level_for(stats, remaining, running):
    if not running or not stats.get("ok"):
        return "stop"
    if remaining <= CLOSE_SECONDS:
        return "stop"
    cpu = float(stats.get("cpu_pct") or 0)
    mem = float(stats.get("mem_pct") or 0)
    if remaining <= 30 * 60 or cpu >= 90 or mem >= 90:
        return "warn"
    return "ok"


def build_embed(stats, players, remaining, level):
    running = str(stats.get("state") or "") == "running"
    light = _light(level)
    uptime_s = int((stats.get("uptime_ms") or 0) / 1000)
    mem_gb = (stats.get("mem_bytes") or 0) / (1024 ** 3)
    mem_lim = (stats.get("mem_limit_mb") or 0) / 1024.0
    cpu = float(stats.get("cpu_pct") or 0)
    mem_pct = float(stats.get("mem_pct") or 0)
    restart_unix = int(time.time() + max(0, remaining))
    status_line = f"{light} **Online**" if running else f"🛑 **{str(stats.get('state') or 'offline').title()}**"
    if remaining <= CLOSE_SECONDS:
        status_line = f"🛑 **Restart window** — world restarts in {_fmt_duration(remaining)}"
    players_txt = "—" if players is None else str(players)
    cpu_l = _metric_light(running, cpu >= 85)
    ram_l = _metric_light(running, mem_pct >= 85)
    rst_l = "🛑" if remaining <= CLOSE_SECONDS else ("🟡" if remaining <= 30 * 60 else "🟢")
    embed = discord.Embed(
        title=f"{light} {BOARD_TITLE}",
        description=(
            f"{status_line}\n"
            f"Restarts at **00:00 / 12:00 / 18:00 Pacific**. "
            f"In-game announce at **5 minutes**, then a **2-minute last call**."
        ),
        color=_embed_color(level),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name=f"{light} Status", value=f"`{stats.get('state') or 'unknown'}`", inline=True)
    embed.add_field(name="🟢 Uptime", value=_fmt_duration(uptime_s) if running else "—", inline=True)
    embed.add_field(name="👥 Players", value=players_txt, inline=True)
    embed.add_field(
        name=f"{rst_l} Next restart",
        value=f"in **{_fmt_duration(remaining)}**\n<t:{restart_unix}:t>",
        inline=True,
    )
    embed.add_field(name=f"{cpu_l} CPU", value=f"{cpu:.0f}%", inline=True)
    ram_txt = f"{mem_gb:.1f} GB"
    if mem_lim > 0:
        ram_txt = f"{mem_gb:.1f} / {mem_lim:.1f} GB ({mem_pct:.0f}%)"
    embed.add_field(name=f"{ram_l} RAM", value=ram_txt, inline=True)
    if stats.get("error"):
        embed.add_field(name="Note", value=str(stats.get("error"))[:200], inline=False)
    embed.set_footer(text="Auto-updates every 90s · 00 / 06 / 12 / 18 Pacific")
    return embed


def _director_due_at(state=None):
    row = state if isinstance(state, dict) else _load_state()
    return float(row.get("director_restart_at") or 0)


def _in_scheduled_maintenance(state, remaining):
    if state.get("restarting"):
        return True
    if remaining <= CLOSE_SECONDS:
        return True
    now = time.time()
    due = _director_due_at(state)
    if due and due > now - 2 * 60:
        return True
    sent = float(state.get("restart_sent_at") or 0)
    if sent and (now - sent) < 12 * 60:
        return True
    boot = float(state.get("last_boot_at") or 0)
    if boot and (now - boot) < 8 * 60:
        return True
    return False


def _isle_looks_down(stats):
    if not stats:
        return False
    http = int(stats.get("http") or 0)
    if http not in (200, 204):
        return False
    name = str(stats.get("state") or "").strip().lower()
    return name in ("offline", "stopped", "crashed")


async def _maybe_emergency(bot, stats, remaining, state):
    scheduled = _in_scheduled_maintenance(state, remaining)
    down = _isle_looks_down(stats) and not scheduled
    streak = int(state.get("down_streak") or 0)
    if down:
        streak += 1
    else:
        streak = 0
    state["down_streak"] = streak
    alerted = bool(state.get("emergency_alerted"))
    if down and streak >= 2 and not alerted:
        try:
            import primeval_posts

            message = await primeval_posts.post_emergency(bot, stats, recovered=False)
            if message is not None:
                state["emergency_alerted"] = True
                state["emergency_at"] = time.time()
                state["emergency_channel_id"] = getattr(getattr(message, "channel", None), "id", 0) or 0
                state["emergency_message_id"] = getattr(message, "id", 0) or 0
        except Exception as exc:
            print(f"[HEALTH BOARD] emergency post failed: {exc}")
    elif (not down) and alerted:
        running = str(stats.get("state") or "") == "running"
        if running:
            try:
                import primeval_posts

                await primeval_posts.settle_down_alert(
                    bot,
                    state.get("emergency_channel_id"),
                    state.get("emergency_message_id"),
                    stats,
                )
                await primeval_posts.post_emergency(bot, stats, recovered=True)
            except Exception as exc:
                print(f"[HEALTH BOARD] recovery post failed: {exc}")
            state["emergency_alerted"] = False
            state["emergency_channel_id"] = 0
            state["emergency_message_id"] = 0
    return state


async def _maybe_crash_watch(bot, stats, remaining, state):
    """Post uptime cliffs to staff/admin log (covers restarts that stay 'starting')."""
    uptime_s = float((stats.get("uptime_ms") or 0) / 1000.0)
    state_name = str(stats.get("state") or "").strip().lower()
    last_up = float(state.get("crash_watch_last_up") or 0)
    last_cpu = state.get("crash_watch_last_cpu")
    last_mem = state.get("crash_watch_last_mem_gb")
    cooldown_until = float(state.get("crash_watch_cooldown_until") or 0)
    now = time.time()

    cliff = (
        last_up > 120
        and uptime_s < max(90.0, last_up * 0.25)
        and state_name in {"running", "starting", "offline", "stopped", "crashed", ""}
    )
    hard_down = _isle_looks_down(stats)

    if (cliff or hard_down) and now >= cooldown_until:
        # Offline emergencies already use post_emergency — only add cliff posts here
        # (or hard_down when emergency streak has not fired yet).
        scheduled = (
            _in_scheduled_maintenance(state, remaining)
            or bool(state.get("restarting"))
            or (_director_due_at(state) > 0 and _director_due_at(state) <= now + 120)
        )
        should_post = cliff or (hard_down and not bool(state.get("emergency_alerted")))
        if should_post and not (hard_down and not cliff and bool(state.get("emergency_alerted"))):
            try:
                import primeval_posts

                # Prefer cliff messaging when uptime reset; emergency covers sustained offline.
                if cliff:
                    await primeval_posts.post_crash_watch(
                        bot,
                        reason=(
                            f"uptime_cliff last={last_up:.0f}s now={uptime_s:.0f}s"
                            + (f" state={state_name}" if state_name else "")
                        ),
                        stats=stats,
                        previous_uptime_s=last_up,
                        scheduled=scheduled,
                        cpu=float(last_cpu) if last_cpu is not None else None,
                        mem_gb=float(last_mem) if last_mem is not None else None,
                    )
                    state["crash_watch_cooldown_until"] = now + 90
            except Exception as exc:
                print(f"[HEALTH BOARD] crash watch post failed: {exc}")

    if state_name == "running" and uptime_s > 30:
        state["crash_watch_last_up"] = uptime_s
        try:
            state["crash_watch_last_cpu"] = float(stats.get("cpu_pct") or stats.get("cpu") or 0)
        except (TypeError, ValueError):
            pass
        try:
            mem_b = float(stats.get("mem_bytes") or stats.get("memory_bytes") or 0)
            if mem_b > 0:
                state["crash_watch_last_mem_gb"] = mem_b / 1e9
        except (TypeError, ValueError):
            pass
    elif cliff or hard_down:
        # Keep prior uptime for the next message; clear after cooldown window advances.
        pass
    return state


async def _announce(message):
    """Send an in-game RCON announce. Returns True only on a real send attempt that did not raise."""
    send = _fn("send_game_command")
    if not send:
        print(f"[HEALTH BOARD] announce skipped (no rcon): {message[:80]}")
        return False
    try:
        result = await send("announce " + message)
        print(f"[HEALTH BOARD] announce ok: {message[:80]!r} → {str(result)[:120]!r}")
        return True
    except Exception as exc:
        print(f"[HEALTH BOARD] announce failed: {exc}")
        try:
            result = await send("announce " + message)
            print(f"[HEALTH BOARD] announce retry ok: {message[:80]!r} → {str(result)[:120]!r}")
            return True
        except Exception as exc2:
            print(f"[HEALTH BOARD] announce retry failed: {exc2}")
            return False


async def _warn_once(state, flag, message):
    """Fire an in-game warn at most once per flag. Persist only after a successful announce."""
    async with _STATE_LOCK:
        fresh = _load_state()
        if fresh.get(flag):
            state[flag] = True
            return False
        ok = await _announce(message)
        if ok:
            fresh = _load_state()
            fresh[flag] = True
            _save_state(fresh)
            state[flag] = True
        return ok


_DIRECTOR_RESTART_LOCK = asyncio.Lock()
_director_task = None
DIRECTOR_LEAD_SECONDS = 5 * 60
DIRECTOR_WARN_2 = 2 * 60


async def _restart_isle():
    await _announce(MSG_RESTART_NOW)
    send = _fn("send_game_command")
    if send:
        try:
            await send("save")
            await asyncio.sleep(4)
        except Exception:
            pass
    status, body = await _ghb("POST", "/power", json_body={"signal": "stop"})
    if status not in (200, 204):
        status, body = await _ghb("POST", "/power", json_body={"signal": "restart"})
        return status, body
    try:
        import primeval_isle_admins

        down = await primeval_isle_admins.wait_host_down()
        if down:
            await primeval_isle_admins.apply_pinned_admins()
        else:
            print("[HEALTH BOARD] host did not go offline in time — starting anyway")
    except Exception as exc:
        print(f"[HEALTH BOARD] pinned admin apply failed: {exc}")
    status, body = await _ghb("POST", "/power", json_body={"signal": "start"})
    return status, body


def _ensure_director_task():
    global _director_task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _director_task is not None and not _director_task.done():
        return
    _director_task = loop.create_task(_run_director_restart())


def _ensure_clock_warn_task():
    """Precise 5m / 2m last-call waiter for Pacific clock restarts (board tick is 90s)."""
    global _clock_warn_task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _clock_warn_task is not None and not _clock_warn_task.done():
        return
    state = _load_state()
    due = float(state.get("next_restart_at") or 0)
    if due <= 0:
        return
    left = due - time.time()
    if left > WARN_10 + 90 or left <= 8:
        return
    _clock_warn_task = loop.create_task(_run_clock_restart_warnings())


async def _run_clock_restart_warnings():
    """Sleep until the 5-minute and 2-minute marks, then announce in-game."""
    try:
        while True:
            state = _load_state()
            due = float(state.get("next_restart_at") or 0)
            if due <= 0 or state.get("restarting"):
                return
            left = due - time.time()
            if left <= 8:
                return
            if left > WARN_5 + 2:
                await asyncio.sleep(min(30, left - WARN_5))
                continue
            if left > WARN_2 + 2:
                await _warn_once(state, "warned_5", MSG_WARN_5)
                state = _load_state()
                left = due - time.time()
                await asyncio.sleep(max(1, min(20, left - WARN_2)))
                continue
            await _warn_once(state, "warned_5", MSG_WARN_5)
            await _warn_once(_load_state(), "warned_2", MSG_WARN_2)
            left = due - time.time()
            if left <= 8:
                return
            await asyncio.sleep(max(1, min(10, left - 5)))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        print(f"[HEALTH BOARD] clock warn task failed: {exc}")


async def _fire_director_restart(state=None):
    state = dict(state or _load_state())
    now = time.time()
    stats = await fetch_isle_stats()
    state = _load_state()
    name = str(stats.get("state") or "").strip().lower()
    state["director_restart_at"] = 0
    state["director_warned_5"] = False
    state["director_warned_2"] = False
    if name in ("offline", "stopped", "crashed"):
        _save_state(state)
        print("[HEALTH BOARD] director restart skipped — host already down")
        return False, "Isle was already down when the 5-minute timer ended. Use **Relaunch server**."
    if state.get("restarting"):
        _save_state(state)
        return False, "A restart is already in progress."
    state["restarting"] = True
    state["restart_sent_at"] = now
    _save_state(state)
    status, body = await _restart_isle()
    state = _load_state()
    state["last_restart_http"] = status
    state["director_restart_at"] = 0
    _save_state(state)
    if status not in (200, 204):
        err = ""
        if isinstance(body, dict):
            err = str(body.get("error") or body.get("raw") or "")[:160]
        return False, f"Panel restart failed (HTTP {status}). {err}".strip()
    return True, "Restart sent after the 5-minute safelog window."


async def _run_director_restart():
    while True:
        state = _load_state()
        due = _director_due_at(state)
        if due <= 0:
            return
        left = due - time.time()
        if left > DIRECTOR_WARN_2 + 2:
            await asyncio.sleep(min(30, left - DIRECTOR_WARN_2))
            continue
        if left > 8:
            await _warn_once(state, "director_warned_2", MSG_WARN_2)
            left = due - time.time()
            await asyncio.sleep(max(1, min(15, left - 5)))
            continue
        await _fire_director_restart(state)
        return


async def director_restart():
    """Director panel: 5-minute safelog warning, then GHB restart. Persists across bot restarts."""
    async with _DIRECTOR_RESTART_LOCK:
        now = time.time()
        state = _load_state()
        due = _director_due_at(state)
        if due > now:
            return False, f"A Director restart is already scheduled <t:{int(due)}:R>."
        stats = await fetch_isle_stats()
        name = str(stats.get("state") or "").strip().lower()
        if name in ("offline", "stopped", "crashed"):
            return False, "Isle is already down. Use **Relaunch server** on the staff-log shutdown post."
        if name in ("starting", "stopping"):
            return False, f"Isle is **{name}**. Wait for it to settle, then try again."
        if name and name != "running":
            return False, f"Panel state is `{name or 'unknown'}`. Restart only runs while Isle is up."
        state = _load_state()
        if _director_due_at(state) > time.time():
            return False, f"A Director restart is already scheduled <t:{int(_director_due_at(state))}:R>."
        due = now + DIRECTOR_LEAD_SECONDS
        state["director_restart_at"] = due
        state["director_warned_5"] = False
        state["director_warned_2"] = False
        _save_state(state)
        ok = await _warn_once(state, "director_warned_5", MSG_WARN_5)
        _ensure_director_task()
        if not ok:
            return True, (
                f"Restart scheduled <t:{int(due)}:R> (**5 minutes**), but the in-game "
                "5-minute announce failed — check RCON. The 2-minute last call will still try."
            )
        return True, (
            f"Restart scheduled <t:{int(due)}:R> (**5 minutes**). "
            "Players got the in-game 5-minute announce; last call fires at 2 minutes."
        )


async def upsert_board(bot, embed, state=None):
    state = dict(state or _load_state())
    channel = bot.get_channel(HEALTH_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(HEALTH_CHANNEL_ID)
        except Exception:
            return state
    msg_id = int(state.get("board_message_id") or 0)
    message = None
    if msg_id:
        try:
            message = await channel.fetch_message(msg_id)
        except Exception:
            message = None
    if message is None:
        message = await channel.send(embed=embed)
        state["board_message_id"] = message.id
        fresh = _load_state()
        fresh["board_message_id"] = message.id
        _save_state(fresh)
    else:
        await message.edit(embed=embed)
    return state


_TICK_LOCK = asyncio.Lock()
_SHARED_KEYS = (
    "warned_10", "warned_5", "warned_2", "restarting", "restart_sent_at",
    "last_restart_http", "director_restart_at", "director_warned_5",
    "director_warned_2", "board_message_id", "last_boot_at",
)


async def tick(bot):
    async with _TICK_LOCK:
        await _tick(bot)


async def _tick(bot):
    stats = await fetch_isle_stats()
    players, _raw = await fetch_players()
    state = _load_state()
    running = str(stats.get("state") or "") == "running"
    uptime_s = int((stats.get("uptime_ms") or 0) / 1000)
    state = _ensure_schedule(state, uptime_s, running)
    _save_state(state)
    clock_left = max(0, int(float(state.get("next_restart_at") or 0) - time.time()))
    remaining = clock_left
    level = _level_for(stats, remaining, running)

    if running and clock_left <= WARN_10 and not state.get("warned_10"):
        # Soft early heads-up; 5m + 2m last call are the required banners.
        if await _announce("Fallen Earth restart in 10 minutes. Finish fights and find a safe log-out."):
            fresh = _load_state()
            fresh["warned_10"] = True
            _save_state(fresh)
            state = _load_state()
    if running and clock_left <= WARN_5:
        await _warn_once(state, "warned_5", MSG_WARN_5)
        state = _load_state()
    if running and clock_left <= WARN_2:
        await _warn_once(state, "warned_2", MSG_WARN_2)
        state = _load_state()
    _ensure_clock_warn_task()

    dir_due = _director_due_at(state)
    dir_left = (dir_due - time.time()) if dir_due > 0 else 0
    if dir_due > 0:
        _ensure_director_task()
        if running and dir_left <= DIRECTOR_WARN_2:
            await _warn_once(state, "director_warned_2", MSG_WARN_2)
            state = _load_state()
        if running and dir_left <= 8 and not state.get("restarting"):
            await _fire_director_restart(state)
            state = _load_state()
            dir_due = 0
        elif dir_due > time.time():
            remaining = min(clock_left, max(0, int(dir_left)))
            level = _level_for(stats, remaining, running)

    if running and clock_left <= 8 and not state.get("restarting"):
        state["restarting"] = True
        state["restart_sent_at"] = time.time()
        state["director_restart_at"] = 0
        _save_state(state)
        status, _body = await _restart_isle()
        fresh = _load_state()
        fresh["last_restart_http"] = status
        _save_state(fresh)
        state["last_restart_http"] = status
    elif state.get("restarting") and running and uptime_s > 240:
        sent = float(state.get("restart_sent_at") or 0)
        if time.time() - sent > 180:
            state["restarting"] = False
            _save_state(state)

    try:
        state = await _maybe_emergency(bot, stats, remaining, state)
    except Exception as exc:
        print(f"[HEALTH BOARD] emergency watch failed: {exc}")
    try:
        state = await _maybe_crash_watch(bot, stats, remaining, state)
    except Exception as exc:
        print(f"[HEALTH BOARD] crash watch failed: {exc}")
    try:
        import primeval_events

        await primeval_events.on_isle_observed(running, uptime_s)
    except Exception as exc:
        print(f"[HEALTH BOARD] events hook failed: {exc}")

    embed = build_embed(stats, players, remaining, level)
    try:
        state = await upsert_board(bot, embed, state)
    except Exception as exc:
        print(f"[HEALTH BOARD] edit failed: {exc}")
    state["last_tick"] = time.time()
    fresh = _load_state()
    for key in _SHARED_KEYS:
        if key in fresh:
            state[key] = fresh[key]
        else:
            state.pop(key, None)
    _save_state(state)


def start(bot):
    global _task
    if _task is not None and _task.is_running():
        return _task

    @tasks.loop(seconds=BOARD_TICK_SECONDS)
    async def health_board_tick():
        try:
            await tick(bot)
        except Exception as exc:
            print(f"[HEALTH BOARD] tick failed: {exc}")

    @health_board_tick.before_loop
    async def _wait_ready():
        await bot.wait_until_ready()
        await asyncio.sleep(3)
        _ensure_director_task()
        _ensure_clock_warn_task()

    _task = health_board_tick
    health_board_tick.start()
    print("[HEALTH BOARD] restart board started (in-game 5m + 2m last call)")
    return health_board_tick


def register_slash(bot):
    existing = {cmd.name for cmd in bot.tree.get_commands()}
    if "health_board" in existing:
        return

    @bot.tree.command(name="health_board", description="Post or refresh the live server health board")
    async def health_board(interaction: discord.Interaction):
        rank_fn = None
        try:
            import primeval_panels
            rank_fn = primeval_panels.staff_rank
        except Exception:
            rank_fn = None
        rank = rank_fn(interaction.user) if rank_fn else 0
        perms = getattr(interaction.user, "guild_permissions", None)
        if rank < 3 and not (perms and perms.manage_guild):
            await interaction.response.send_message("Administrator+ can post the health board.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await tick(interaction.client)
        except Exception as exc:
            await interaction.followup.send(f"Health board refresh failed: {exc}", ephemeral=True)
            return
        await interaction.followup.send(f"Health board refreshed in <#{HEALTH_CHANNEL_ID}>.", ephemeral=True)
