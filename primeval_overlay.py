"""Private HTTP API for the Primeval player overlay.

The module has no import-time side effects.  ``bind`` supplies the bot's
existing helpers, ``register_slash`` adds the one-time-code command, and
``start`` starts aiohttp only when all required configuration is present.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import ssl
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

try:
    from aiohttp import web
except ImportError:  # Keep the Discord bot importable on an unconfigured host.
    web = None


G: dict[str, Any] = {}
SAVED = "/TheIsle/Binaries/Win64/ue4ss/Mods/PrimevalRedeem/Saved"
SNAPSHOT_PATH = SAVED + "/overlay_snapshot.json"
POI_PATH = Path(__file__).with_name("primeval_overlay_pois.json")
CONFIG_PATH = Path(__file__).with_name("primeval_overlay_config.json")
SECRET_PATH = Path(__file__).with_name("overlay_signing_secret.txt")

CODE_TTL = 300
SESSION_TTL = 8 * 60 * 60
POLL_SECONDS = 3.0
STALE_SECONDS = 90.0
MAX_SNAPSHOT_BYTES = 2_000_000

_SERVICE: "OverlayService | None" = None
_START_TASK: asyncio.Task | None = None
_REGISTERED: set[int] = set()
_POI_CACHE: tuple[float, dict[str, Any]] | None = None
_CONFIG_CACHE: tuple[float, dict[str, Any]] | None = None


def bind(g: dict[str, Any] | None) -> None:
    G.clear()
    G.update(g or {})


def _setting(name: str, default: str = "") -> str:
    value = G.get(name)
    if value is None or value == "":
        value = os.environ.get(name)
    if value is None or value == "":
        global _CONFIG_CACHE
        try:
            mtime = CONFIG_PATH.stat().st_mtime
            if _CONFIG_CACHE is None or _CONFIG_CACHE[0] != mtime:
                parsed = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                _CONFIG_CACHE = (mtime, parsed if isinstance(parsed, dict) else {})
            value = _CONFIG_CACHE[1].get(name)
        except (OSError, ValueError, json.JSONDecodeError):
            value = None
    if value is None or value == "":
        value = default
    return str(value or "").strip()


def _origins() -> frozenset[str]:
    raw = _setting("PRIMEVAL_OVERLAY_ORIGINS")
    return frozenset(
        item.strip().rstrip("/")
        for item in raw.split(",")
        if item.strip() and item.strip() != "*"
    )


def _overlay_secret() -> str:
    source = _setting("PRIMEVAL_OVERLAY_SECRET")
    if source:
        return source
    try:
        return SECRET_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _signing_key() -> bytes | None:
    """Derive a purpose-separated key from an already configured secret."""
    source = _overlay_secret()
    if not source:
        for name in ("DISCORD_TOKEN", "BOT_TOKEN"):
            source = _setting(name)
            if source:
                break
    if not source:
        for name in ("TOKEN", "DISCORD_BOT_TOKEN"):
            value = G.get(name)
            if value:
                source = str(value)
                break
    if not source:
        return None
    return hmac.new(
        source.encode("utf-8"),
        b"primeval-overlay/session-signing/v1",
        hashlib.sha256,
    ).digest()


def _linked_steam(discord_id: int) -> str:
    getter = G.get("get_linked_steam_id")
    value: Any = None
    if callable(getter):
        try:
            value = getter(discord_id)
        except Exception:
            return ""
    else:
        links = G.get("STEAM_LINKS") or {}
        if isinstance(links, dict):
            value = links.get(discord_id, links.get(str(discord_id)))
            if isinstance(value, dict):
                value = value.get("steam")
    steam = str(value or "").strip()
    return steam if len(steam) == 17 and steam.isdigit() else ""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


class SessionSigner:
    def __init__(self, key: bytes, ttl: int = SESSION_TTL):
        if not key:
            raise ValueError("signing key is required")
        self.key = bytes(key)
        self.ttl = max(30, min(int(ttl), 24 * 60 * 60))

    def issue(self, discord_id: int, steam: str, now: int | None = None) -> str:
        now = int(time.time() if now is None else now)
        payload = {
            "v": 1,
            "uid": str(int(discord_id)),
            "steam": str(steam),
            "iat": now,
            "exp": now + self.ttl,
            "jti": secrets.token_urlsafe(12),
        }
        body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
        signature = _b64(hmac.new(self.key, body.encode("ascii"), hashlib.sha256).digest())
        return body + "." + signature

    def verify(self, token: str, now: int | None = None) -> dict[str, Any] | None:
        try:
            body, supplied = token.split(".", 1)
            expected = _b64(hmac.new(self.key, body.encode("ascii"), hashlib.sha256).digest())
            if not hmac.compare_digest(supplied, expected):
                return None
            payload = json.loads(_unb64(body))
            now = int(time.time() if now is None else now)
            if (
                payload.get("v") != 1
                or int(payload.get("iat", 0)) > now + 30
                or int(payload.get("exp", 0)) <= now
                or int(payload.get("exp", 0)) - int(payload.get("iat", 0)) > 24 * 60 * 60
                or not str(payload.get("uid", "")).isdigit()
                or len(str(payload.get("steam", ""))) != 17
                or not str(payload.get("steam", "")).isdigit()
                or not str(payload.get("jti", ""))
            ):
                return None
            return payload
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None


@dataclass
class OneTimeCode:
    digest: str
    discord_id: int
    steam: str
    expires: float


class CodeStore:
    def __init__(self, ttl: int = CODE_TTL):
        self.ttl = max(30, min(int(ttl), 600))
        self._codes: dict[str, OneTimeCode] = {}
        self._by_user: dict[int, str] = {}

    @staticmethod
    def _digest(code: str) -> str:
        return hashlib.sha256(code.encode("ascii")).hexdigest()

    def issue(self, discord_id: int, steam: str, now: float | None = None) -> str:
        now = time.time() if now is None else now
        old = self._by_user.pop(int(discord_id), None)
        if old:
            self._codes.pop(old, None)
        code = "-".join(
            ("".join(str(secrets.randbelow(10)) for _ in range(4)) for _ in range(2))
        )
        digest = self._digest(code)
        self._codes[digest] = OneTimeCode(digest, int(discord_id), str(steam), now + self.ttl)
        self._by_user[int(discord_id)] = digest
        self.prune(now)
        return code

    def consume(self, code: str, now: float | None = None) -> OneTimeCode | None:
        now = time.time() if now is None else now
        normalized = str(code or "").strip().replace(" ", "")
        digest = self._digest(normalized)
        row = self._codes.pop(digest, None)
        if row:
            self._by_user.pop(row.discord_id, None)
        if not row or row.expires <= now:
            return None
        return row

    def prune(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        for digest, row in list(self._codes.items()):
            if row.expires <= now:
                self._codes.pop(digest, None)
                self._by_user.pop(row.discord_id, None)


class SlidingRateLimiter:
    def __init__(self, limit: int, window: float):
        self.limit = max(1, int(limit))
        self.window = max(1.0, float(window))
        self.events: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        cutoff = now - self.window
        if len(self.events) > 512:
            for stale in [k for k, b in self.events.items() if not b or b[-1] <= cutoff]:
                del self.events[stale]
        bucket = self.events[str(key)]
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= self.limit:
            return False
        bucket.append(now)
        return True


class SnapshotCache:
    """One poller and one in-memory copy shared by every request."""

    def __init__(
        self,
        fetch: Callable[[], Awaitable[tuple[int, str]]],
        poll_seconds: float = POLL_SECONDS,
        stale_seconds: float = STALE_SECONDS,
    ):
        self.fetch = fetch
        self.poll_seconds = max(0.25, float(poll_seconds))
        self.stale_seconds = max(self.poll_seconds, float(stale_seconds))
        self.snapshot: dict[str, Any] | None = None
        self.updated_at = 0.0
        self.task: asyncio.Task | None = None

    async def refresh(self) -> bool:
        status, raw = await self.fetch()
        if status != 200 or not isinstance(raw, str) or len(raw) > MAX_SNAPSHOT_BYTES:
            return False
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return False
        if not isinstance(parsed, dict):
            return False
        self.snapshot = parsed
        self.updated_at = time.monotonic()
        return True

    async def run(self) -> None:
        try:
            while True:
                try:
                    await self.refresh()
                except Exception:
                    pass
                await asyncio.sleep(self.poll_seconds)
        except asyncio.CancelledError:
            raise

    def start(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run(), name="primeval-overlay-poller")

    async def close(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.task = None

    def get(self) -> dict[str, Any] | None:
        if (
            self.snapshot is None
            or not self.updated_at
            or time.monotonic() - self.updated_at > self.stale_seconds
        ):
            return None
        return self.snapshot


PLAYER_FIELDS = frozenset(
    {
        "steam",
        "steam_id",
        "steamId",
        "x",
        "y",
        "z",
        "yaw",
        "online",
        "species",
        "growth",
        "gender",
        "health",
        "maxHealth",
        "health_percent",
        "stamina",
        "stamina_percent",
        "hunger",
        "thirst",
        "oxygen",
        "bleeding",
        "fracture",
        "venom",
        "sickness",
        "position",
        "location",
        "coordinates",
        "zone",
        "biome",
        "direction",
        "heading",
        "velocity",
        "temperature",
        "comfort",
        "diet",
        "diet_slots",
        "mutations",
        "prime",
        "prime_progress",
        "capturedAt",
        "generatedAt",
        "captured_at",
        "updated_at",
    }
)
PRIME_FIELDS = frozenset(
    {
        "eligible",
        "active",
        "progress",
        "current",
        "required",
        "completed",
        "stage",
        "status",
        "species",
        "growth",
        "objectives",
        "have",
        "migration",
        "patrol",
        "conditions",
        "updated_at",
        "captured_at",
    }
)
MANIFEST_FIELDS = frozenset(
    {"version", "map", "map_name", "revision", "updated_at", "captured_at", "features", "assets"}
)
POI_FIELDS = frozenset({"id", "name", "label", "type", "category", "x", "y", "z", "position", "icon"})
STATUS_FIELDS = frozenset(
    {
        "online",
        "name",
        "map",
        "map_name",
        "player_count",
        "capacity",
        "max_players",
        "queue",
        "uptime",
        "restart_at",
        "updated_at",
        "captured_at",
    }
)

_BLOCKED_NESTED_KEYS = frozenset(
    {
        "players",
        "telemetry",
        "roster",
        "raw",
        "credentials",
        "authorization",
        "api_key",
        "token",
        "secret",
        "password",
        "discord_id",
        "ip",
    }
)


def _safe_value(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, list):
        return [_safe_value(item, depth + 1) for item in value[:100]]
    if isinstance(value, dict):
        return {
            str(key)[:64]: _safe_value(item, depth + 1)
            for key, item in list(value.items())[:100]
            if isinstance(key, (str, int)) and str(key).lower() not in _BLOCKED_NESTED_KEYS
        }
    return None


def _only(row: Any, fields: frozenset[str]) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    return {key: _safe_value(row[key]) for key in fields if key in row}


def _only_player(row: Any) -> dict[str, Any]:
    result = _only(row, PLAYER_FIELDS)
    for key in ("prime", "prime_progress"):
        if key in result:
            result[key] = _only(result[key], PRIME_FIELDS)
    return result


def _steam_of(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    for key in ("steam", "steam_id", "steamId", "steamid", "SteamID"):
        value = str(row.get(key) or "").strip()
        if value:
            return _normalize_steam(value)
    return ""


def _normalize_steam(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.search(r"(7656\d{10,})", text)
    if match:
        return match.group(1)
    if text.isdigit() and len(text) >= 15:
        return text
    return ""


def own_player(snapshot: dict[str, Any], steam: str) -> dict[str, Any] | None:
    """Locate exactly one linked player without returning the containing roster."""
    want = _normalize_steam(steam)
    if not want:
        return None
    containers = (snapshot.get("players"), snapshot.get("telemetry"), snapshot.get("player"))
    for container in containers:
        if isinstance(container, dict):
            row = container.get(want) or container.get(steam)
            if isinstance(row, dict):
                return _only_player(row)
            for candidate in container.values():
                if _steam_of(candidate) == want:
                    return _only_player(candidate)
        elif isinstance(container, list):
            for candidate in container:
                if _steam_of(candidate) == want:
                    return _only_player(candidate)
    return None


def manifest_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    manifest = _only(snapshot.get("manifest"), MANIFEST_FIELDS)
    pois = snapshot.get("pois")
    if pois is None and isinstance(snapshot.get("manifest"), dict):
        pois = snapshot["manifest"].get("pois")
    manifest["pois"] = [_only(row, POI_FIELDS) for row in pois[:500]] if isinstance(pois, list) else []
    return manifest


def _poi_data() -> dict[str, Any]:
    global _POI_CACHE
    try:
        mtime = POI_PATH.stat().st_mtime
        if _POI_CACHE is None or _POI_CACHE[0] != mtime:
            parsed = json.loads(POI_PATH.read_text(encoding="utf-8"))
            if not isinstance(parsed, dict) or not isinstance(parsed.get("features"), list):
                return {}
            _POI_CACHE = (mtime, parsed)
        return _POI_CACHE[1]
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _world_to_map(x: Any, y: Any, calibration: dict[str, Any]) -> tuple[float, float] | None:
    try:
        x = float(x)
        y = float(y)
        min_x = float(calibration["minX"])
        max_x = float(calibration["maxX"])
        min_y = float(calibration["minY"])
        max_y = float(calibration["maxY"])
    except (TypeError, ValueError, KeyError):
        return None
    if not all(math.isfinite(value) for value in (x, y, min_x, max_x, min_y, max_y)):
        return None
    if max_x <= min_x or max_y <= min_y:
        return None
    if calibration.get("axisX") == "H":
        u, v = (y - min_y) / (max_y - min_y), (x - min_x) / (max_x - min_x)
    else:
        u, v = (x - min_x) / (max_x - min_x), (y - min_y) / (max_y - min_y)
    if not (-0.02 <= u <= 1.02 and -0.02 <= v <= 1.02):
        return None
    return max(0.0, min(1.0, u)), max(0.0, min(1.0, v))


def _ue_to_map(ue_x: Any, ue_y: Any, calibration: dict[str, Any]) -> tuple[float, float] | None:
    """Map a live Unreal actor location onto Vulnona/theisle.ru axes.

    Empirically (Status Report vs GetActorLocation): Lat = UE Y, Long = UE X.
    Vulnona POI files store (Lat, Long) as (x, y), so swap before calibrating.
    """
    return _world_to_map(ue_y, ue_x, calibration)


def _normalized_geometry(geometry: Any, calibration: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(geometry, dict):
        return None
    kind = str(geometry.get("type") or "")
    if kind == "ellipse":
        center = geometry.get("center")
        point = (
            _world_to_map(center[0], center[1], calibration)
            if isinstance(center, list) and len(center) >= 2
            else None
        )
        if point is None:
            return None
        width = float(calibration["maxY"]) - float(calibration["minY"])
        height = float(calibration["maxX"]) - float(calibration["minX"])
        return {
            "type": "ellipse",
            "u": point[0],
            "v": point[1],
            "radiusU": max(0.0, float(geometry.get("radiusY") or 0) / width),
            "radiusV": max(0.0, float(geometry.get("radiusX") or 0) / height),
            "rotation": float(geometry.get("rotation") or 0),
        }
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list):
        return None
    points = []
    for coordinate in coordinates[:256]:
        if isinstance(coordinate, list) and len(coordinate) >= 2:
            point = _world_to_map(coordinate[0], coordinate[1], calibration)
            if point:
                points.append({"u": point[0], "v": point[1]})
    if not points:
        return None
    return {"type": "polygon" if kind == "polygon" else "point", "points": points}


def public_manifest() -> dict[str, Any]:
    data = _poi_data()
    calibration = data.get("calibration") if isinstance(data.get("calibration"), dict) else {}
    layers: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw in data.get("features", [])[:2000]:
        if not isinstance(raw, dict):
            continue
        point = _world_to_map(raw.get("x"), raw.get("y"), calibration)
        category = str(raw.get("category") or "")
        if point is None or category not in {"migrations", "patrol", "gastroliths", "salt", "landmarks", "water"}:
            continue
        row = {
            "id": str(raw.get("id") or "")[:100],
            "name": str(raw.get("name") or "")[:100],
            "u": point[0],
            "v": point[1],
            "confidence": str(raw.get("confidence") or "")[:32],
            "updatedAt": str(raw.get("updatedAt") or "")[:32],
        }
        geometries = [
            normalized
            for geometry in (raw.get("geometries") or [])[:64]
            if (normalized := _normalized_geometry(geometry, calibration)) is not None
        ]
        if geometries:
            row["geometries"] = geometries
        layers[category].append(row)
    return {
        "schema": "primeval-overlay.manifest",
        "schemaVersion": 1,
        "map": str(data.get("map") or ""),
        "gameBuild": str(data.get("gameBuild") or ""),
        "updatedAt": str(data.get("sourceUpdatedAt") or ""),
        "counts": {key: len(value) for key, value in layers.items()},
        "layers": dict(layers),
        "attribution": {
            "source": "VulnonaMAP",
            "author": "Coco.N / Ciro",
            "url": "https://vulnona.com/game/map/",
            "basemap": "TheIsle.ru",
            "basemapUrl": "https://theisle.ru/en/maps?map=evrima",
        },
    }


def overlay_snapshot_payload(
    snapshot: dict[str, Any],
    steam: str,
    breadcrumbs: deque[dict[str, float]] | None = None,
) -> dict[str, Any]:
    raw = own_player(snapshot, steam)
    data = _poi_data()
    calibration = data.get("calibration") if isinstance(data.get("calibration"), dict) else {}
    species = str((raw or {}).get("species") or "")
    try:
        import primeval_ecology

        diet_hint = primeval_ecology.overlay_diet(species)
        include_ai = primeval_ecology.allow_ai_layer(diet_hint)
    except Exception:
        diet_hint = "unknown"
        include_ai = False

    player = None
    if raw:
        point = _ue_to_map(raw.get("x"), raw.get("y"), calibration)
        if point:
            yaw = float(raw.get("yaw") or 0)
            # UE yaw 0 faces +X (= Status Long / east on the N-up map).
            player = {
                "u": point[0],
                "v": point[1],
                "heading": (yaw + 90.0) % 360.0,
                "species": str(raw.get("species") or "")[:80],
                "growth": raw.get("growth"),
                "health": raw.get("health"),
                "maxHealth": raw.get("maxHealth"),
                "prime": raw.get("prime") if isinstance(raw.get("prime"), dict) else None,
                # Status Report units for HUD cross-check (Lat=UE Y, Long=UE X).
                "lat": (float(raw["y"]) / 1000.0) if raw.get("y") is not None else None,
                "lng": (float(raw["x"]) / 1000.0) if raw.get("x") is not None else None,
            }
            species = str(player.get("species") or species)
            if breadcrumbs is not None:
                previous = breadcrumbs[-1] if breadcrumbs else None
                if previous is None or math.hypot(point[0] - previous["u"], point[1] - previous["v"]) >= 0.0005:
                    breadcrumbs.append({"u": point[0], "v": point[1]})
    # Skip AI ellipse math for herb/unknown — diet gate would drop it anyway.
    ai_points: list[dict[str, Any]] = []
    ai = snapshot.get("ai")
    if (
        include_ai
        and player
        and isinstance(ai, dict)
        and isinstance(ai.get("privacy"), dict)
    ):
        privacy = ai["privacy"]
        cell_size = max(1.0, float(privacy.get("cellSize") or 0))
        min_count = max(1, int(privacy.get("minCount") or 1))
        delay = max(20, int(privacy.get("delaySeconds") or 20))
        # Bound work per poll — privacy cells only, never FindAllOf.
        for cell in (ai.get("cells") or [])[:64]:
            if not isinstance(cell, dict) or int(cell.get("count") or 0) < min_count:
                continue
            x = (float(cell.get("cellX") or 0) + 0.5) * cell_size
            y = (float(cell.get("cellY") or 0) + 0.5) * cell_size
            point = _ue_to_map(x, y, calibration)
            if point:
                lat_span = float(calibration["maxX"]) - float(calibration["minX"])
                lng_span = float(calibration["maxY"]) - float(calibration["minY"])
                # Broad privacy radius: ~75% of the cell, at least ~500m, so the
                # zone shows activity without pin-pointing exact AI positions.
                half = max(cell_size * 0.75, 50000.0)
                if calibration.get("axisX") == "H":
                    radius_u = half / lng_span if lng_span else 0.0
                    radius_v = half / lat_span if lat_span else 0.0
                else:
                    radius_u = half / lat_span if lat_span else 0.0
                    radius_v = half / lng_span if lng_span else 0.0
                ai_points.append(
                    {
                        "u": point[0],
                        "v": point[1],
                        "count": int(cell.get("count") or 0),
                        "distance": math.hypot(x - float(raw["x"]), y - float(raw["y"])) / 100.0,
                        "observedAt": ai.get("observedAt"),
                        "delaySeconds": delay,
                        "geometries": [
                            {
                                "type": "ellipse",
                                "u": point[0],
                                "v": point[1],
                                "radiusU": radius_u,
                                "radiusV": radius_v,
                                "rotation": 0,
                            }
                        ],
                    }
                )
    try:
        import primeval_ecology

        eco = primeval_ecology.client_bundle(species)
    except Exception:
        eco = {
            "diet": diet_hint,
            "worldState": {},
            "layers": {},
            "includeAi": include_ai,
            "allowedLayers": [],
        }
    diet = str(eco.get("diet") or "unknown")
    allowed = list(eco.get("allowedLayers") or [])
    if not allowed:
        try:
            import primeval_ecology as _eco

            allowed = _eco.allowed_layers(diet)
        except Exception:
            allowed = []
    layers: dict[str, Any] = {}
    # Diet gate: herbs never receive AI activity; unknown receives neither AI nor forage.
    if eco.get("includeAi") and ai_points:
        layers["ai"] = ai_points
    for name, points in (eco.get("layers") or {}).items():
        if isinstance(points, list) and points:
            layers[str(name)] = points
    return {
        "schema": "primeval-overlay.client",
        "schemaVersion": 2,
        "generatedAt": snapshot.get("generatedAt"),
        "player": player,
        "breadcrumbs": list(breadcrumbs or []),
        "layers": layers,
        "worldState": eco.get("worldState") if isinstance(eco.get("worldState"), dict) else {},
        "meta": {
            "hasPlayer": player is not None,
            "trackedPlayers": len(snapshot.get("players") or [])
            if isinstance(snapshot.get("players"), list)
            else 0,
            "aiCells": len(layers.get("ai") or []),
            "buildAt": snapshot.get("buildAt"),
            # Last 4 of the session Steam so the client can confirm link match
            # without exposing full IDs of other players.
            "steamTail": str(steam)[-4:] if steam else "",
            "diet": diet,
            "allowedLayers": allowed,
            "ecologySchema": "primeval-ecology.client",
            "ecologySchemaVersion": 1,
        },
    }


def prime_payload(snapshot: dict[str, Any], steam: str, player: dict[str, Any] | None = None) -> dict[str, Any]:
    prime = snapshot.get("prime")
    row: Any = None
    if isinstance(prime, dict):
        row = prime.get(steam)
        if row is None and _steam_of(prime) == steam:
            row = prime
    if row is None and isinstance(player, dict):
        row = player.get("prime_progress", player.get("prime"))
    return _only(row, PRIME_FIELDS)


def status_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    status = snapshot.get("server_status", snapshot.get("server"))
    result = _only(status, STATUS_FIELDS)
    if isinstance(status, dict) and "player_count" not in result:
        players = status.get("players")
        if isinstance(players, (int, float)) and not isinstance(players, bool):
            result["player_count"] = players
    if not result:
        result = _only(snapshot, STATUS_FIELDS)
    if "online" not in result:
        result["online"] = True
    if "player_count" not in result and isinstance(snapshot.get("players"), list):
        result["player_count"] = len(snapshot["players"])
    if "captured_at" not in result and snapshot.get("generatedAt") is not None:
        result["captured_at"] = snapshot.get("generatedAt")
    # A producer must provide an aggregate count; never derive it by exposing a roster.
    return result


class OverlayService:
    def __init__(
        self,
        key: bytes,
        origins: frozenset[str],
        fetch: Callable[[], Awaitable[tuple[int, str]]],
        link_lookup: Callable[[int], str] = _linked_steam,
    ):
        if web is None:
            raise RuntimeError("aiohttp is unavailable")
        if not key or not origins:
            raise ValueError("signing key and restricted origins are required")
        self.origins = origins
        self.link_lookup = link_lookup
        self.signer = SessionSigner(key)
        self.codes = CodeStore()
        self.cache = SnapshotCache(fetch)
        self.revoked: dict[str, int] = {}
        self.ip_limit = SlidingRateLimiter(90, 60)
        self.user_limit = SlidingRateLimiter(60, 60)
        self.exchange_limit = SlidingRateLimiter(8, 300)
        self.trails: dict[str, deque[dict[str, float]]] = defaultdict(lambda: deque(maxlen=240))
        self.app = web.Application(
            middlewares=[self.security_middleware, self.rate_middleware, self.auth_middleware],
            client_max_size=4096,
        )
        self.app.add_routes(
            [
                web.options("/{path:.*}", self.options),
                web.get("/overlay/health", self.health),
                web.post("/v1/auth/exchange", self.exchange),
                web.post("/overlay/login", self.exchange),
                web.post("/v1/auth/revoke", self.revoke),
                web.get("/v1/me/state", self.own_state),
                web.get("/v1/manifest", self.manifest),
                web.get("/overlay/manifest", self.manifest),
                web.get("/overlay/snapshot", self.overlay_snapshot),
                web.get("/v1/me/prime", self.prime),
                web.get("/v1/server/status", self.server_status),
            ]
        )
        self.app.on_startup.append(self._startup)
        self.app.on_cleanup.append(self._cleanup)

    @web.middleware
    async def security_middleware(self, request: web.Request, handler: Callable) -> web.StreamResponse:
        origin = request.headers.get("Origin", "").rstrip("/")
        if origin and origin not in self.origins:
            raise web.HTTPForbidden(text="origin denied")
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            response = exc
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        if origin:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
        return response

    @web.middleware
    async def rate_middleware(self, request: web.Request, handler: Callable) -> web.StreamResponse:
        peer = request.remote or "unknown"
        if not self.ip_limit.allow(peer):
            raise web.HTTPTooManyRequests(text="rate limit exceeded", headers={"Retry-After": "60"})
        return await handler(request)

    @web.middleware
    async def auth_middleware(self, request: web.Request, handler: Callable) -> web.StreamResponse:
        if request.method == "OPTIONS" or request.path in {
            "/v1/auth/exchange",
            "/overlay/login",
            "/overlay/health",
        }:
            return await handler(request)
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer ") or len(header) > 4096:
            raise web.HTTPUnauthorized(text="bearer session required")
        payload = self.signer.verify(header[7:].strip())
        if not payload or payload["jti"] in self.revoked:
            raise web.HTTPUnauthorized(text="invalid session")
        uid = int(payload["uid"])
        if self.link_lookup(uid) != payload["steam"]:
            raise web.HTTPUnauthorized(text="linked account changed")
        if not self.user_limit.allow(str(uid)):
            raise web.HTTPTooManyRequests(text="rate limit exceeded", headers={"Retry-After": "60"})
        request["session"] = payload
        return await handler(request)

    async def _startup(self, _app: web.Application) -> None:
        self.cache.start()

    async def _cleanup(self, _app: web.Application) -> None:
        await self.cache.close()

    async def options(self, request: web.Request) -> web.Response:
        return web.Response(
            status=204,
            headers={
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": "Authorization, Content-Type",
                "Access-Control-Max-Age": "600",
            },
        )

    async def exchange(self, request: web.Request) -> web.Response:
        key = request.remote or "unknown"
        if not self.exchange_limit.allow(key):
            raise web.HTTPTooManyRequests(text="rate limit exceeded", headers={"Retry-After": "300"})
        try:
            body = await request.json()
        except Exception:
            raise web.HTTPBadRequest(text="invalid JSON")
        row = self.codes.consume(str(body.get("code", "")) if isinstance(body, dict) else "")
        if row is None:
            raise web.HTTPUnauthorized(text="invalid or expired code")
        current = self.link_lookup(row.discord_id)
        if not current or current != row.steam:
            raise web.HTTPUnauthorized(text="linked account changed")
        token = self.signer.issue(row.discord_id, row.steam)
        return web.json_response(
            {
                "access_token": token,
                "accessToken": token,
                "token_type": "Bearer",
                "expires_in": self.signer.ttl,
            }
        )

    async def health(self, request: web.Request) -> web.Response:
        del request
        snapshot = self.cache.get()
        poi_counts = {}
        try:
            poi_counts = public_manifest().get("counts") or {}
        except Exception:
            poi_counts = {}
        tracked = 0
        if isinstance(snapshot, dict) and isinstance(snapshot.get("players"), list):
            tracked = len(snapshot["players"])
        return web.json_response(
            {
                "ok": True,
                "service": "primeval-overlay",
                "telemetry": snapshot is not None,
                "trackedPlayers": tracked,
                "poiCounts": poi_counts,
                "poiTotal": int(sum(int(v) for v in poi_counts.values())) if poi_counts else 0,
            }
        )

    async def revoke(self, request: web.Request) -> web.Response:
        payload = request["session"]
        self.revoked[payload["jti"]] = int(payload["exp"])
        now = int(time.time())
        self.revoked = {jti: exp for jti, exp in self.revoked.items() if exp > now}
        return web.json_response({"revoked": True})

    def _snapshot(self) -> dict[str, Any]:
        snapshot = self.cache.get()
        if snapshot is None:
            raise web.HTTPServiceUnavailable(text="telemetry unavailable", headers={"Retry-After": "3"})
        return snapshot

    async def own_state(self, request: web.Request) -> web.Response:
        steam = request["session"]["steam"]
        player = own_player(self._snapshot(), steam)
        return web.json_response({"online": player is not None, "state": player})

    async def manifest(self, request: web.Request) -> web.Response:
        del request
        return web.json_response(public_manifest())

    async def overlay_snapshot(self, request: web.Request) -> web.Response:
        steam = request["session"]["steam"]
        return web.json_response(
            overlay_snapshot_payload(self._snapshot(), steam, self.trails[steam])
        )

    async def prime(self, request: web.Request) -> web.Response:
        snapshot = self._snapshot()
        steam = request["session"]["steam"]
        return web.json_response(prime_payload(snapshot, steam, own_player(snapshot, steam)))

    async def server_status(self, request: web.Request) -> web.Response:
        return web.json_response(status_payload(self._snapshot()))


async def _fetch_snapshot() -> tuple[int, str]:
    import primeval_isle

    if not primeval_isle.G:
        primeval_isle.bind(G)
    return await primeval_isle.read_file(SNAPSHOT_PATH)


def configured() -> bool:
    cert = Path(_setting("PRIMEVAL_OVERLAY_TLS_CERT"))
    key = Path(_setting("PRIMEVAL_OVERLAY_TLS_KEY"))
    if not cert.is_absolute():
        cert = Path(__file__).resolve().parent / cert
    if not key.is_absolute():
        key = Path(__file__).resolve().parent / key
    return (
        web is not None
        and bool(_setting("PRIMEVAL_OVERLAY_PORT"))
        and bool(_origins())
        and _signing_key() is not None
        and cert.is_file()
        and key.is_file()
    )


def issue_code(discord_id: int) -> tuple[str, int] | None:
    if _SERVICE is None:
        return None
    steam = _linked_steam(int(discord_id))
    if not steam:
        return None
    return _SERVICE.codes.issue(int(discord_id), steam), _SERVICE.codes.ttl


def register_slash(bot: Any) -> None:
    if id(bot) in _REGISTERED:
        return
    _REGISTERED.add(id(bot))
    try:
        import discord
    except ImportError:
        return

    @bot.tree.command(name="overlay", description="Get a private one-time Fallen Earth Overlay login code")
    async def overlay_command(interaction: discord.Interaction):
        steam = _linked_steam(interaction.user.id)
        if not steam:
            await interaction.response.send_message(
                "Link Steam first using the member panel, then run `/overlay` again.",
                ephemeral=True,
            )
            return
        result = issue_code(interaction.user.id)
        if result is None:
            await interaction.response.send_message(
                "The Fallen Earth Overlay service is not configured.",
                ephemeral=True,
            )
            return
        code, ttl = result
        await interaction.response.send_message(
            f"Your one-time overlay code is **`{code}`**. It expires in {ttl // 60} minutes "
            "and can be used only once. Do not share it.",
            ephemeral=True,
        )


async def _serve(host: str, port: int) -> None:
    global _SERVICE
    key = _signing_key()
    origins = _origins()
    if web is None:
        print("[OVERLAY] aiohttp is unavailable; overlay API disabled")
        return
    if key is None:
        print("[OVERLAY] missing signing secret; add overlay_signing_secret.txt")
        return
    if not origins:
        print("[OVERLAY] missing allowed origins; check PRIMEVAL_OVERLAY_ORIGINS")
        return
    service = OverlayService(key, origins, _fetch_snapshot)
    runner = web.AppRunner(service.app, access_log=None)
    try:
        await runner.setup()
        cert_path = Path(_setting("PRIMEVAL_OVERLAY_TLS_CERT"))
        key_path = Path(_setting("PRIMEVAL_OVERLAY_TLS_KEY"))
        if not cert_path.is_absolute():
            cert_path = Path(__file__).resolve().parent / cert_path
        if not key_path.is_absolute():
            key_path = Path(__file__).resolve().parent / key_path
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cert_path, key_path)
        site = web.TCPSite(runner, host, port, ssl_context=context)
        await site.start()
        _SERVICE = service
        print(f"[OVERLAY] listening on {host}:{port}")
        await asyncio.Event().wait()
    finally:
        _SERVICE = None
        await runner.cleanup()


def start(bot: Any) -> asyncio.Task | None:
    """Start once on the bot loop; absent/unsafe configuration is a no-op."""
    global _START_TASK
    if not configured():
        if web is None:
            print("[OVERLAY] aiohttp is unavailable; overlay API disabled")
        elif not _overlay_secret() and _signing_key() is None:
            print("[OVERLAY] missing signing secret; add overlay_signing_secret.txt")
        elif not _origins():
            print("[OVERLAY] missing allowed origins; check PRIMEVAL_OVERLAY_ORIGINS")
        else:
            cert = Path(_setting("PRIMEVAL_OVERLAY_TLS_CERT"))
            key = Path(_setting("PRIMEVAL_OVERLAY_TLS_KEY"))
            if not cert.is_absolute():
                cert = Path(__file__).resolve().parent / cert
            if not key.is_absolute():
                key = Path(__file__).resolve().parent / key
            if not cert.is_file() or not key.is_file():
                print("[OVERLAY] TLS cert/key missing beside the bot")
        return None
    if _START_TASK is not None and not _START_TASK.done():
        return _START_TASK
    try:
        port = int(_setting("PRIMEVAL_OVERLAY_PORT"))
    except ValueError:
        return None
    if not 1 <= port <= 65535:
        return None
    host = _setting("PRIMEVAL_OVERLAY_HOST", "127.0.0.1")
    loop = getattr(bot, "loop", None)
    if loop is None:
        return None
    _START_TASK = loop.create_task(_serve(host, port), name="primeval-overlay-http")
    return _START_TASK


async def stop() -> None:
    global _START_TASK
    if _START_TASK is not None and not _START_TASK.done():
        _START_TASK.cancel()
        try:
            await _START_TASK
        except asyncio.CancelledError:
            pass
    _START_TASK = None
