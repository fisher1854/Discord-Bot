"""Pin Isle AdminsSteamIDs and write them after a controlled stop.

Evrima reads Game.ini at boot and rewrites it on shutdown. A live RCON
addadmin or a write while the process is up often does not survive. The
health-board restart stops first, applies this list, then starts.
"""

from __future__ import annotations

import asyncio
import json
import re
import time

GAME_INI = "/TheIsle/Saved/Config/WindowsServer/Game.ini"
STATE_PATH = "isle_admins.json"
SECTION = "[/script/theisle.tigamestatebase]"

# Always keep these SteamID64s in Game.ini across bot-driven restarts.
PINNED_ADMINS = (
    "76561198260079568",  # Fisher / host
    "76561198173845697",
    "76561198858154276",
)


def _load_extra():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            rows = data.get("steam_ids") or []
            return [str(x).strip() for x in rows if str(x).strip().startswith("7656")]
    except Exception:
        pass
    return []


def wanted_ids():
    seen = []
    for steam in list(PINNED_ADMINS) + _load_extra():
        steam = str(steam).strip()
        if steam.startswith("7656") and steam not in seen:
            seen.append(steam)
    return seen


def parse_ids(text):
    have = []
    for chunk in re.findall(r"(?im)^AdminsSteamIDs=(.*)$", text or ""):
        for part in re.split(r"[,\s]+", chunk.strip().strip('"')):
            part = part.strip()
            if part.startswith("7656") and part not in have:
                have.append(part)
    return have


def merge_game_ini(text, extra_ids):
    text = text or ""
    have = parse_ids(text)
    add = [sid for sid in extra_ids if sid not in have]
    if not add:
        return text, []
    lines = text.splitlines(keepends=True)
    last = None
    section = None
    for i, line in enumerate(lines):
        if re.match(r"(?i)^\[/script/theisle\.tigamestatebase\]\s*$", line):
            section = i
        if re.match(r"(?i)^AdminsSteamIDs=", line):
            last = i
    nl = "\n"
    if last is not None and "\r\n" in lines[last]:
        nl = "\r\n"
    elif lines and "\r\n" in lines[0]:
        nl = "\r\n"
    insert_at = (last + 1) if last is not None else None
    if insert_at is None and section is not None:
        insert_at = section + 1
    if insert_at is None:
        if text and not text.endswith(("\n", "\r\n")):
            text += nl
        block = SECTION + nl + "".join("AdminsSteamIDs=" + sid + nl for sid in add)
        return text + block, add
    chunk = "".join("AdminsSteamIDs=" + sid + nl for sid in add)
    lines.insert(insert_at, chunk)
    return "".join(lines), add


async def apply_pinned_admins():
    import primeval_isle

    wanted = wanted_ids()
    status, text = await primeval_isle.read_file(GAME_INI)
    if status != 200 or not text:
        print(f"[ISLE ADMINS] read Game.ini failed {status}")
        return False, []
    body, added = merge_game_ini(text, wanted)
    if not added:
        print("[ISLE ADMINS] Game.ini already has pinned Steam IDs")
        return True, []
    wst, _ = await primeval_isle.write_file(GAME_INI, body)
    if wst not in (200, 204):
        print(f"[ISLE ADMINS] write Game.ini failed {wst}")
        return False, []
    status, verify = await primeval_isle.read_file(GAME_INI)
    have = parse_ids(verify if status == 200 else "")
    missing = [sid for sid in wanted if sid not in have]
    print(f"[ISLE ADMINS] wrote {added}; missing={missing}")
    return not missing, added


async def wait_host_down(timeout=180):
    import primeval_health_board

    deadline = time.time() + timeout
    while time.time() < deadline:
        stats = await primeval_health_board.fetch_isle_stats()
        name = str(stats.get("state") or "").strip().lower()
        if name in ("offline", "stopped"):
            await asyncio.sleep(4)
            return True
        await asyncio.sleep(2)
    return False
