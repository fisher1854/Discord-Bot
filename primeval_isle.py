"""Isle file bus: unique inbox JSON files over the Game Host Bros panel API.

Discord never sees the Isle disk. Every store / redeem / TP / census read
goes through this module. Bind it from Primeval_Island_Bot.py:

    import primeval_isle
    primeval_isle.bind(globals())
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid

import aiohttp

G = {}

SAVED = "/TheIsle/Binaries/Win64/ue4ss/Mods/PrimevalRedeem/Saved"
INBOX_DIR = SAVED + "/inbox"
INBOX_LEGACY = SAVED + "/inbox.ndjson"
VAULT_DIR = SAVED + "/vault"
STORED_DIR = SAVED + "/stored"
PRIME_DIR = SAVED + "/prime"
LIVE_DIR = SAVED + "/live"
TP_DIR = SAVED + "/tp"
CENSUS_PATH = SAVED + "/census.json"

_SESSION = None
_SESSION_LOCK = asyncio.Lock()
_WRITE_LOCK = asyncio.Lock()


def bind(g):
    G.clear()
    G.update(g)
    G["queue_isle_cmd_flag"] = queue_verb
    G["queue_isle_inbox"] = queue_inbox
    G["read_isle_prime_json"] = read_prime
    G["read_isle_vault_json"] = read_vault


def _fn(name, default=None):
    return G.get(name, default)


def _key():
    load_key = _fn("load_game_panel_api_key")
    if load_key:
        return str(load_key() or "")
    return str(G.get("GAME_PANEL_API_KEY") or "")


def _base():
    return str(G.get("GAME_HOST_URL") or "https://bropanel.gamehostbros.com").rstrip("/")


def _uuid():
    return str(G.get("GAME_HOST_UUID") or "09914030")


async def _session():
    global _SESSION
    async with _SESSION_LOCK:
        if _SESSION is None or _SESSION.closed:
            _SESSION = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12))
        return _SESSION


async def close():
    global _SESSION
    async with _SESSION_LOCK:
        if _SESSION is not None and not _SESSION.closed:
            await _SESSION.close()
        _SESSION = None


async def request(method, path, body=None, json_body=None):
    key = _key()
    if not key:
        return 0, "missing game panel API key"
    url = _base() + "/api/client/servers/" + _uuid() + path
    headers = {
        "Authorization": "Bearer " + key,
        "Accept": "Application/vnd.pterodactyl.v1+json",
    }
    data = body
    if json_body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(json_body)
    elif body is not None:
        headers["Content-Type"] = "text/plain"
    try:
        session = await _session()
        async with session.request(method, url, headers=headers, data=data) as resp:
            text = await resp.text()
            return resp.status, text
    except Exception as exc:
        return 0, str(exc)


async def request_json(method, path, json_body=None):
    status, text = await request(method, path, json_body=json_body)
    parsed = {}
    if text:
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = {"raw": str(text)[:400]}
    return status, parsed


async def read_file(path):
    from urllib.parse import quote

    return await request("GET", "/files/contents?file=" + quote(path))


async def write_file(path, body):
    from urllib.parse import quote

    async with _WRITE_LOCK:
        return await request("POST", "/files/write?file=" + quote(path), body=body)


async def queue_inbox(payload):
    payload = dict(payload or {})
    payload.setdefault("id", "cmd-" + uuid.uuid4().hex[:12])
    payload.setdefault("ts", int(time.time()))
    line = json.dumps(payload, separators=(",", ":")) + "\n"
    from urllib.parse import quote

    path = quote(INBOX_LEGACY)
    async with _WRITE_LOCK:
        status, existing = await request("GET", "/files/contents?file=" + path)
        if status != 200 or not existing:
            existing = ""
        elif not isinstance(existing, str):
            existing = str(existing)
        if '"errors"' in existing and '"verb"' not in existing:
            existing = ""
        if len(existing) > 200000:
            existing = ""
        wstatus, text = await request(
            "POST",
            "/files/write?file=" + path,
            body=existing + line,
        )
    if wstatus in (200, 204):
        return True, "queued"
    return False, text or f"write {wstatus}"


async def queue_verb(verb, steam, extra=""):
    verb = str(verb or "").lower().strip()
    steam = str(steam or "").strip()
    extra = str(extra or "").strip()
    payload = {
        "id": f"{verb}-{steam[-6:] if steam else 'x'}-{int(time.time() * 1000)}",
        "ts": int(time.time()),
        "verb": verb,
        "steam": steam,
    }
    if verb == "redeem" and extra:
        payload["slot"] = extra
    elif verb == "tpstart" and extra:
        parts = extra.split()
        payload["target"] = parts[0]
        if len(parts) > 1:
            payload["id"] = parts[1]
    elif verb == "tpcancel" and extra:
        payload["id"] = extra
    return await queue_inbox(payload)


async def read_census():
    status, text = await read_file(CENSUS_PATH)
    if status != 200 or not text or not str(text).strip().startswith("{"):
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


async def read_vault(steam):
    steam = str(steam or "").strip()
    for folder in (VAULT_DIR, STORED_DIR):
        status, text = await read_file(folder + "/" + steam + ".json")
        if status == 200 and text and str(text).strip().startswith("{"):
            return text
    return ""


async def read_prime(steam):
    steam = str(steam or "").strip()
    status, text = await read_file(PRIME_DIR + "/" + steam + ".json")
    if status == 200 and text and str(text).strip().startswith("{"):
        return text
    return ""
