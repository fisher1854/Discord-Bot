"""Adaptive Ecology state + diet-gated overlay payload fragments.

Runs only on the Discord/overlay HTTP host — never on the Isle game tick.
Hot path is cached, bounded, and fail-soft so overlay polls stay cheap.

Kill switch: set ``enabled: false`` in primeval_ecology_state.json (or env
``PRIMEVAL_ECOLOGY_ENABLED=0``) to return empty ecology fragments instantly.
"""

from __future__ import annotations

import json
import os
import random
import time
from pathlib import Path
from typing import Any

STATE_PATH = Path(__file__).with_name("primeval_ecology_state.json")
POIS_PATH = Path(__file__).with_name("primeval_overlay_pois.json")

# Soft magnets — invitation copy only (never punishment language).
NEST_SAFE_BLURB = (
    "Richer patches move slowly. Nesting is fine — you are not penalized for staying."
)

# Zone centers must sit on walkable ground (POI-backed), not lakes / ocean.
LAND_POI_CATEGORIES = frozenset({"salt", "gastroliths", "landmarks", "patrol", "migrations"})
WATER_CLEAR_UV = 0.12  # reject centers this close to marked water POIs

# Hard bounds so a bad state file cannot inflate overlay JSON / SVG work.
MAX_REGIONS_PER_LAYER = 6
MAX_ELLIPSE_POINTS_TOTAL = 12

# Isle files written by the Stage 2 ecology tick (Lua reads these).
AI_HERD_PATH = "/TheIsle/Binaries/Win64/ue4ss/Mods/PrimevalRedeem/Saved/ai_herd.json"
AI_FORAGE_PATH = "/TheIsle/Binaries/Win64/ue4ss/Mods/PrimevalRedeem/Saved/ai_forage.json"

# Gateway calibration (axisX=H): same contract as overlay POIs.
GATEWAY = {
    "minX": -607000.0,
    "maxX": 509000.0,
    "minY": -505000.0,
    "maxY": 607000.0,
    "axisX": "H",
}
GATEWAY_SPAN_X = GATEWAY["maxX"] - GATEWAY["minX"]  # 1_116_000
GATEWAY_SPAN_Y = GATEWAY["maxY"] - GATEWAY["minY"]  # 1_112_000

# Locked Stage 2 UV half-extents (±10% jitter max on centers only).
RADIUS_FORAGE = 0.10
RADIUS_PREY = 0.10
RADIUS_BLOOM = 0.12
RADIUS_HOT = 0.08
RADIUS_ECOLOGY = 0.11

ROTATE_MIN_SEC = 45 * 60
ROTATE_MAX_SEC = 90 * 60
PREVIEW_LEAD_SEC = 25 * 60
HOT_CONTACT_CHANCE = 0.30
HERB_CLEAR_UU = 20000  # ~200 m
# Sparse forage — soft ecology (few plants, no remediation storms).
FORAGE_LIVE_CAP = 6
FORAGE_FILL_GAP = 150
TICK_SECONDS = 60

# Quiet-biased UV anchors (fallback only if POI land list fails to load).
_QUIET_ANCHORS: tuple[tuple[float, float], ...] = (
    (0.22, 0.28),
    (0.30, 0.34),
    (0.26, 0.62),
    (0.68, 0.30),
    (0.72, 0.68),
    (0.58, 0.55),
    (0.40, 0.72),
    (0.78, 0.42),
)

_STATE: dict[str, Any] | None = None
_STATE_MTIME: float | None = None
_BUNDLE_BY_DIET: dict[str, dict[str, Any]] = {}
_BUNDLE_TOKEN: float | None = None
_LAND_UVS: list[tuple[float, float]] | None = None
_WATER_UVS: list[tuple[float, float]] | None = None
_task = None
_LAST_WRITTEN: dict[str, str] = {}


def ecology_enabled(state: dict[str, Any] | None = None) -> bool:
    """Global kill switch — env wins, then state.enabled (default True)."""
    env = os.environ.get("PRIMEVAL_ECOLOGY_ENABLED", "").strip().lower()
    if env in {"0", "false", "off", "no"}:
        return False
    if env in {"1", "true", "on", "yes"}:
        return True
    if state is not None:
        return bool(state.get("enabled", True))
    peek_state()
    return bool((_STATE or {}).get("enabled", True))


def overlay_diet(species: str | None) -> str:
    """Map live species to overlay diet. Empty/unknown → unknown (safe)."""
    key = str(species or "").strip()
    if not key:
        return "unknown"
    try:
        import primeval_species

        return str(primeval_species.diet(key) or "carni")
    except Exception:
        return "unknown"


def default_herd() -> dict[str, Any]:
    """Ship herd: spawn-mode soft calls, proximity gate, calibrated caps, unlocked Maia AI."""
    return {
        "enabled": True,
        "perCarniTeno": 0,       # Teno disabled entirely to prevent ecology bugs
        "perCarniDibble": 2,     # Unlocked to allow live Maiasaura spawning
        "perCarniGalli": 1,      
        "stabilityCeiling": 40,  # Dropped from 120 down to 40 to protect server FPS
        "fillGap": 120,          
        "deathGap": 300,
        "callsMode": "spawn",
        "callGap": 240,
        "callHearUU": 25000,
        "callGapPop": 15,
        "callsEnabled": True,
        "silentUntilLuaReload": False,
        "spawnMinGap": 45,       
        "cullWhenDisabled": True,
        "growthMin": 0.40,       
        "growthMax": 0.55,
        "requirePreyProximity": True, # Keep spawns locked directly to active players
        "preyArmPaddingUU": 20000,
        "proxCacheSec": 20,
        "proxDisarmCullSec": 300,
        "targetTeno": 0,
        "targetDibble": 12,      # Set baseline buffer to 12
        "targetGalli": 12,       # Set baseline buffer to 12
    }


def default_state() -> dict[str, Any]:
    """State profile with ALL zones permanently stripped out and blanked."""
    return {
        "schema": "primeval-ecology.state",
        "schemaVersion": 1,
        "enabled": True,
        "mode": "adaptive",
        "vanilla": False,
        "theme": "stable",
        "label": "STABLE",
        "blurb": "Ecology regions disabled. AI anchors strictly to live player coordinates.",
        "updatedAt": 0,
        "ecologyRegions": [],    # FIXED: Wiped permanently
        "forage": {"regions": []}, # FIXED: Wiped permanently
        "prey": {"regions": []},   # FIXED: Wiped permanently
        "bloom": {
            "active": False,
            "until": 0,
            "label": "",
            "forageRegions": [], # FIXED: Wiped permanently
            "preyRegions": [],   # FIXED: Wiped permanently
        },
        "hotContact": {
            "active": False,
            "intensity": "med",
            "regions": [],       # FIXED: Wiped permanently
        },
        "herd": default_herd(),
        "scheduler": {
            "nextRotateAt": 0,
            "lastTickAt": 0,
            "carnivores": 0,
            "previewForage": [], # FIXED: Wiped permanently
            "previewPrey": [],   # FIXED: Wiped permanently
        },
    }



def fixture_state(now: int | None = None) -> dict[str, Any]:
    """Dev/UI sample: Permanently stripped of all sample regions and zone coordinates."""
    now = int(now if now is not None else time.time())
    state = default_state()
    state.update(
        {
            "enabled": True,
            "mode": "adaptive",
            "vanilla": False,
            "theme": "quiet",
            "label": "QUIET",
            "blurb": "Ecology regions disabled. AI anchors strictly to live player coordinates.",
            "updatedAt": now,
            "ecologyRegions": [],
            "forage": {"regions": []},
            "prey": {"regions": []},
            "bloom": {
                "active": False,
                "until": now + 28 * 60,
                "label": "DISABLED",
                "forageRegions": [],
                "preyRegions": [],
            },
            "hotContact": {
                "active": False,
                "intensity": "med",
                "regions": [],
            },
            "scheduler": {
                "nextRotateAt": now + 20 * 60,
                "lastTickAt": now,
                "carnivores": 0,
                "previewForage": [],
                "previewPrey": [],
            },
        }
    )
    return state



def normalize_state(raw: dict[str, Any] | None) -> dict[str, Any]:
    base = default_state()
    if not isinstance(raw, dict):
        return base
    mode = str(raw.get("mode") or base["mode"]).strip().lower()
    if mode not in {"adaptive", "forced_calm", "paused", "forced_bias"}:
        mode = "adaptive"
    vanilla = bool(raw.get("vanilla")) or mode == "forced_calm"
    if mode == "paused" and raw.get("pauseAsCalm", True):
        vanilla = True
    theme = str(raw.get("theme") or "stable").strip().lower()[:32]
    label = str(raw.get("label") or theme.replace("_", " ").upper())[:48]
    blurb = "Ecology regions disabled. AI anchors strictly to live player coordinates."
    
    herd_in = raw.get("herd") if isinstance(raw.get("herd"), dict) else {}
    herd = default_herd()
    
    for key in (
        "perCarniTeno",
        "perCarniDibble",
        "perCarniGalli",
        "stabilityCeiling",
        "fillGap",
        "deathGap",
    ):
        if key in herd_in:
            try:
                herd[key] = int(herd_in[key])
            except (TypeError, ValueError):
                pass
                
    if "callsEnabled" in herd_in:
        herd["callsEnabled"] = bool(herd_in["callsEnabled"])
    if "silentUntilLuaReload" in herd_in:
        herd["silentUntilLuaReload"] = bool(herd_in["silentUntilLuaReload"])
        
    mode_raw = str(herd_in.get("callsMode") or herd.get("callsMode") or "spawn").strip().lower()
    if mode_raw not in {"off", "spawn"}:
        mode_raw = "spawn"
    herd["callsMode"] = mode_raw
    if "callsEnabled" in herd_in:
        herd["callsEnabled"] = bool(herd_in["callsEnabled"])
    else:
        herd["callsEnabled"] = mode_raw != "off"
        
    for key, lo, hi, default in (
        ("callGap", 30, 900, 240),
        ("callHearUU", 5000, 80000, 25000),
        ("callGapPop", 0, 60, 15),
        ("spawnMinGap", 30, 600, 120),
        ("preyArmPaddingUU", 0, 80000, 20000),
        ("proxCacheSec", 5, 120, 20),
        ("proxDisarmCullSec", 120, 3600, 600),
    ):
        try:
            herd[key] = int(herd_in[key]) if key in herd_in else int(herd.get(key, default))
        except (TypeError, ValueError):
            herd[key] = default
        herd[key] = max(lo, min(hi, int(herd[key])))
        
    if "cullWhenDisabled" in herd_in:
        herd["cullWhenDisabled"] = bool(herd_in["cullWhenDisabled"])
    else:
        herd["cullWhenDisabled"] = bool(herd.get("cullWhenDisabled", True))
        
    herd["requirePreyProximity"] = True
    
    try:
        g_min = float(herd_in["growthMin"]) if "growthMin" in herd_in else float(herd.get("growthMin", 0.40))
    except (TypeError, ValueError):
        g_min = 0.40
    try:
        g_max = float(herd_in["growthMax"]) if "growthMax" in herd_in else float(herd.get("growthMax", 0.55))
    except (TypeError, ValueError):
        g_max = 0.55
        
    herd["growthMin"] = max(0.2, min(1.0, g_min))
    herd["growthMax"] = max(herd["growthMin"], min(1.0, g_max))
    
    # HARD-LOCKED CAP BALANCES:
    herd["perCarniTeno"] = 0
    herd["perCarniDibble"] = 2  # FIXED: Granted spawning allocation
    herd["perCarniGalli"] = 1
    herd["stabilityCeiling"] = 40
    herd["fillGap"] = 120
    herd["deathGap"] = 300
    herd["targetTeno"] = 0
    herd["targetDibble"] = 12   # FIXED: Hold 12 baseline buffer floor
    herd["targetGalli"] = 12    # FIXED: Hold 12 baseline buffer floor
    herd["silentUntilLuaReload"] = bool(herd.get("silentUntilLuaReload", False))
    
    enabled = bool(raw.get("enabled", True))
    sched_in = raw.get("scheduler") if isinstance(raw.get("scheduler"), dict) else {}
    try:
        next_rotate = int(sched_in.get("nextRotateAt") or base["scheduler"]["nextRotateAt"])
    except (TypeError, ValueError):
        next_rotate = base["scheduler"]["nextRotateAt"]
    try:
        last_tick = int(sched_in.get("lastTickAt") or 0)
    except (TypeError, ValueError):
        last_tick = 0
    try:
        carni_n = int(sched_in.get("carnivores") or 0)
    except (TypeError, ValueError):
        carni_n = 0
        
    return {
        "schema": "primeval-ecology.state",
        "schemaVersion": 1,
        "enabled": enabled,
        "mode": mode,
        "vanilla": vanilla,
        "theme": theme,
        "label": label,
        "blurb": blurb,
        "updatedAt": int(time.time()),
        "ecologyRegions": [],    # FIXED: Hard-wiped zone coordinates
        "forage": {"regions": []}, # FIXED: Hard-wiped zone coordinates
        "prey": {"regions": []},   # FIXED: Hard-wiped zone coordinates
        "bloom": {
            "active": False,
            "until": 0,
            "label": "DISABLED",
            "forageRegions": [], # FIXED: Hard-wiped zone coordinates
            "preyRegions": [],   # FIXED: Hard-wiped zone coordinates
        },
        "hotContact": {
            "active": False,
            "intensity": "med",
            "regions": [],       # FIXED: Hard-wiped zone coordinates
        },
        "herd": herd,
        "scheduler": {
            "nextRotateAt": max(0, next_rotate),
            "lastTickAt": max(0, last_tick),
            "carnivores": max(0, carni_n),
            "previewForage": [], # FIXED: Hard-wiped zone coordinates
            "previewPrey": [],   # FIXED: Hard-wiped zone coordinates
        },
    }


def allow_ai_layer(diet: str) -> bool:
    return diet in {"carni", "omni"}


# Static map layers always allowed. Ecology-family layers are diet-gated.
_STATIC_OVERLAY_LAYERS = (
    "migrations",
    "patrol",
    "gastroliths",
    "salt",
    "landmarks",
    "water",
    "breadcrumbs",
)


def allowed_layers(diet: str) -> list[str]:
    """Layers the overlay may show for this diet (server contract for weekend tests)."""
    diet = diet if diet in {"herb", "carni", "omni", "unknown"} else "unknown"
    if diet == "herb":
        eco = ["ecology", "forage", "bloom", "hotContact", "preview"]
    elif diet == "carni":
        eco = ["ecology", "prey", "bloom", "hotContact", "preview", "ai"]
    elif diet == "omni":
        eco = ["ecology", "forage", "prey", "bloom", "hotContact", "preview", "ai"]
    else:
        eco = []
    return eco + list(_STATIC_OVERLAY_LAYERS)


def preview_lead_active(state: dict[str, Any], *, now: int | None = None) -> tuple[bool, int]:
    """True in the last PREVIEW_LEAD_SEC before nextRotateAt."""
    if ecology_is_calm(state):
        return False, 0
    now = int(now if now is not None else time.time())
    next_at = int((state.get("scheduler") or {}).get("nextRotateAt") or 0)
    if next_at <= 0:
        return False, 0
    if now < next_at - PREVIEW_LEAD_SEC or now >= next_at:
        return False, next_at
    return True, next_at


def preview_regions_for_diet(diet: str, state: dict[str, Any]) -> list[dict[str, Any]]:
    """Upcoming forage/prey ellipses for the overlay preview wash."""
    diet = diet if diet in {"herb", "carni", "omni", "unknown"} else "unknown"
    sched = state.get("scheduler") or {}
    if diet == "herb":
        rows = list(sched.get("previewForage") or [])
    elif diet == "carni":
        rows = list(sched.get("previewPrey") or [])
    elif diet == "omni":
        rows = list(sched.get("previewForage") or []) + list(sched.get("previewPrey") or [])
    else:
        rows = []
    return rows[:MAX_REGIONS_PER_LAYER]


def _empty_bundle(diet: str, state: dict[str, Any] | None = None) -> dict[str, Any]:
    herd = dict((state or {}).get("herd") or default_herd())
    disabled = state is not None and not bool(state.get("enabled", True))
    vanilla = bool((state or {}).get("vanilla")) if state is not None else True
    if disabled:
        vanilla = False
    chip = {"show": False, "text": "", "kind": "off"}
    if vanilla and not disabled:
        chip = {"show": True, "text": "CALM · VANILLA", "kind": "calm"}
    return {
        "diet": diet,
        "worldState": {
            "ecology": {
                "mode": (state or {}).get("mode") or "adaptive",
                "vanilla": True, # FIXED: Enforced True globally to keep tracking decoupled from zone checks
                "theme": "calm", # FIXED: Keep theme localized to calm states to prevent zone processing
                "label": "CALM",
                "blurb": "Ecology regions disabled. AI anchors strictly to live player coordinates.",
                "updatedAt": int((state or {}).get("updatedAt") or 0),
                "nestSafe": True,
                "regions": [],   # FIXED: Wiped permanently
            },
            "chip": chip,
            "bloom": {"active": False, "until": 0, "label": "", "kind": "none", "regions": []}, # FIXED: Wiped permanently
            "hotContact": {"active": False, "intensity": "med", "regions": []}, # FIXED: Wiped permanently
            "preview": {"active": False, "until": 0, "kind": "none", "regions": []}, # FIXED: Wiped permanently
        },
        "layers": {},
        "includeAi": allow_ai_layer(diet),
        "herd": herd,
        "allowedLayers": allowed_layers(diet),
    }


def _cap_layers(layers: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    """Keep total ellipse markers tiny for minimap SVG cost."""
    remaining = MAX_ELLIPSE_POINTS_TOTAL
    out: dict[str, list[dict[str, Any]]] = {}
    order = ("hotContact", "bloom", "preview", "forage", "prey", "ecology")
    for name in order:
        points = layers.get(name) or []
        if not points or remaining <= 0:
            continue
        take = points[: min(MAX_REGIONS_PER_LAYER, remaining)]
        out[name] = take
        remaining -= len(take)
    return out


def _ellipse_layer_point(region: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": region.get("id") or "",
        "u": region["u"],
        "v": region["v"],
        "geometries": [
            {
                "type": "ellipse",
                "u": region["u"],
                "v": region["v"],
                "radiusU": region["radiusU"],
                "radiusV": region["radiusV"],
                "rotation": 0,
            }
        ],
    }


def _chip_for_diet(
    diet: str,
    state: dict[str, Any],
    *,
    now: int | None = None,
    preview_on: bool = False,
    preview_until: int = 0,
) -> dict[str, Any]:
    if not state.get("enabled", True):
        return {"show": False, "text": "", "kind": "off"}
    if state.get("vanilla"):
        return {"show": True, "text": "CALM · VANILLA", "kind": "calm"}
    hot = state.get("hotContact") or {}
    bloom = state.get("bloom") or {}
    if hot.get("active"):
        return {"show": True, "text": "HOT CONTACT", "kind": "hotContact"}
    now = int(now if now is not None else time.time())
    if preview_on and preview_until > now:
        mins = max(1, int((preview_until - now + 59) // 60))
        if diet == "carni":
            text = f"PREY NEXT · {mins}m"
        elif diet == "herb":
            text = f"FORAGE NEXT · {mins}m"
        else:
            text = f"NEXT · {mins}m"
        return {
            "show": True,
            "text": text[:28],
            "kind": "preview",
            "until": int(preview_until),
        }
    if bloom.get("active"):
        if diet == "carni":
            return {"show": True, "text": "PREY · SHIFTING", "kind": "bloom"}
        if diet == "herb":
            return {"show": True, "text": "FORAGE · SHIFTING", "kind": "bloom"}
        return {"show": True, "text": "PRESSURE · SHIFTING", "kind": "bloom"}
    theme = str(state.get("theme") or "stable")
    if theme in {"stable", "calm"}:
        return {"show": False, "text": "", "kind": "stable"}
    label = str(state.get("label") or theme.replace("_", " ").upper())[:24]
    return {"show": True, "text": f"ECOLOGY · {label}", "kind": "theme"}


def client_world_state(diet: str, state: dict[str, Any]) -> dict[str, Any]:
    """Diet-specific worldState block with ALL zones permanently stripped out."""
    diet = diet if diet in {"herb", "carni", "omni", "unknown"} else "unknown"
    now = int(time.time())
    
    # FORCE VANILLA FAIL-SAFE STATE MODE GLOBALLY:
    # This prevents the system from ever processing or drawing ellipse layers
    vanilla = True 

    chip = {"show": False, "text": "", "kind": "off"}
    if not state.get("enabled", True):
        chip = {"show": False, "text": "", "kind": "off"}
    else:
        chip = {"show": True, "text": "CALM · VANILLA", "kind": "calm"}

    out: dict[str, Any] = {
        "ecology": {
            "mode": "adaptive",
            "vanilla": True,
            "theme": "calm",
            "label": "CALM",
            "blurb": "Vanilla fail-safe active. Directed forage and ecology bias are off.",
            "updatedAt": int(state.get("updatedAt") or 0),
            "nestSafe": True,
            "regions": [], # Wiped permanently
        },
        "chip": chip,
        "bloom": {
            "active": False,
            "until": 0,
            "label": "",
            "kind": "none",
            "regions": [], # Wiped permanently
        },
        "hotContact": {
            "active": False,
            "intensity": "med",
            "regions": [], # Wiped permanently
        },
        "preview": {
            "active": False,
            "until": 0,
            "kind": "none",
            "regions": [], # Wiped permanently
        },
    }
    return out



def client_layers(diet: str, state: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Ellipse layer arrays permanently disabled and returning empty tables."""
    return {} # FIXED: Short-circuit directly to skip any layer rendering loops entirely


def _rebuild_bundle_cache(state: dict[str, Any], token: float | None) -> None:
    global _BUNDLE_BY_DIET, _BUNDLE_TOKEN
    _BUNDLE_TOKEN = token
    
    # FIXED: Re-route all cache bundles exclusively through our stripped empty templates
    _BUNDLE_BY_DIET = {
        diet: _empty_bundle(diet, state) for diet in ("herb", "carni", "omni", "unknown")
    }
    
    for bundle in _BUNDLE_BY_DIET.values():
        bundle["worldState"]["chip"] = {"show": True, "text": "CALM · VANILLA", "kind": "calm"}
        bundle["worldState"]["ecology"]["vanilla"] = True
        bundle["worldState"]["ecology"]["label"] = "CALM"


def peek_state() -> dict[str, Any]:
    """Cached normalized state for hot path — treat as read-only."""
    load_state()
    return _STATE or default_state()


def load_state(*, use_fixture_if_empty: bool = False) -> dict[str, Any]:
    """Load ecology state from disk (bot-local JSON). Cached by mtime."""
    global _STATE, _STATE_MTIME
    try:
        mtime = STATE_PATH.stat().st_mtime
        if _STATE is not None and _STATE_MTIME == mtime:
            return _STATE
        parsed = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        state = normalize_state(parsed if isinstance(parsed, dict) else None)
        _STATE = state
        _STATE_MTIME = mtime
        _rebuild_bundle_cache(state, mtime)
        return state
    except (OSError, ValueError, json.JSONDecodeError):
        state = fixture_state() if use_fixture_if_empty else default_state()
        _STATE = state
        _STATE_MTIME = None
        _rebuild_bundle_cache(state, None)
        return state


def save_state(state: dict[str, Any]) -> None:
    global _STATE, _STATE_MTIME
    normalized = normalize_state(state)
    tmp = STATE_PATH.with_name(STATE_PATH.name + ".tmp")
    tmp.write_text(json.dumps(normalized, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, STATE_PATH)
    _STATE = normalized
    try:
        _STATE_MTIME = STATE_PATH.stat().st_mtime
    except OSError:
        _STATE_MTIME = None
    _rebuild_bundle_cache(normalized, _STATE_MTIME)


def set_state_for_tests(state: dict[str, Any] | None) -> None:
    """Test helper: inject memory state without touching disk."""
    global _STATE, _STATE_MTIME, _BUNDLE_BY_DIET, _BUNDLE_TOKEN
    if state is None:
        _STATE = None
        _STATE_MTIME = None
        _BUNDLE_BY_DIET = {}
        _BUNDLE_TOKEN = None
        return
    _STATE = normalize_state(state)
    _STATE_MTIME = -1.0
    _rebuild_bundle_cache(_STATE, _STATE_MTIME)



def force_calm(state: dict[str, Any] | None = None) -> dict[str, Any]:
    """Vanilla fail-safe: clear magnets, empty forage/prey, locked safe caps."""
    current = normalize_state(state if state is not None else peek_state())
    current["mode"] = "forced_calm"
    current["vanilla"] = True
    current["theme"] = "calm"
    current["label"] = "CALM"
    current["blurb"] = "Vanilla fail-safe active. Directed forage and ecology bias are off."
    current["ecologyRegions"] = []
    current["forage"] = {"regions": []}
    current["prey"] = {"regions": []}
    current["bloom"] = {
        "active": False,
        "until": 0,
        "label": "",
        "forageRegions": [],
        "preyRegions": [],
    }
    current["hotContact"] = {"active": False, "intensity": "med", "regions": []}
    
    # HARD-LOCKED CAP BALANCES:
    herd = default_herd()
    herd["perCarniTeno"] = 0
    herd["perCarniDibble"] = 2
    herd["perCarniGalli"] = 1
    herd["stabilityCeiling"] = 40
    herd["callsMode"] = "spawn"
    herd["callsEnabled"] = True
    herd["targetTeno"] = 0
    herd["targetDibble"] = 12
    herd["targetGalli"] = 12
    current["herd"] = herd
    
    sched = dict(current.get("scheduler") or {})
    sched["previewForage"] = []
    sched["previewPrey"] = []
    current["scheduler"] = sched
    current["updatedAt"] = int(time.time())
    return current


def client_bundle(
    species: str | None,
    state: dict[str, Any] | None = None,
    *,
    use_fixture_if_empty: bool = False,
) -> dict[str, Any]:
    """Full ecology fragment for ``overlay_snapshot_payload`` (cached hot path)."""
    diet = overlay_diet(species)
    env = os.environ.get("PRIMEVAL_ECOLOGY_ENABLED", "").strip().lower()
    if env in {"0", "false", "off", "no"}:
        return _empty_bundle(diet)

    if state is not None:
        resolved = normalize_state(state)
        return {
            "diet": diet,
            "worldState": client_world_state(diet, resolved),
            "layers": {},
            "includeAi": allow_ai_layer(diet),
            "herd": dict(resolved.get("herd") or default_herd()),
            "allowedLayers": allowed_layers(diet),
        }

    peek_state()
    resolved = peek_state()
    _rebuild_bundle_cache(resolved, _STATE_MTIME)
    return _BUNDLE_BY_DIET.get(diet) or _empty_bundle(diet, resolved)


def demand_targets(carnivore_count: int, state: dict[str, Any] | None = None) -> dict[str, int]:
    """FIXED: Immunized targets loop to guarantee a baseline of 12 for your prey species."""
    return {
        "carnivores": max(0, int(carnivore_count or 0)),
        "targetTeno": 0,
        "targetDibble": 12,      # FIXED: Hold 12 baseline buffer floor
        "targetGalli": 12,       # FIXED: Hold 12 baseline buffer floor
        "stabilityCeiling": 40,  # FIXED: Lock ceiling at 40
        "fillGap": 120,
        "deathGap": 300,
    }


def ecology_is_calm(state: dict[str, Any] | None = None) -> bool:
    """True when Force Calm / vanilla / disabled — no directed magnets."""
    return True


def world_to_uv(
    x: float,
    y: float,
    *,
    calibration: dict[str, float] | None = None,
) -> tuple[float, float]:
    """Unreal XY → UV (Gateway axisX=H)."""
    cal = calibration or GATEWAY
    min_x = float(cal["minX"])
    max_x = float(cal["maxX"])
    min_y = float(cal["minY"])
    max_y = float(cal["maxY"])
    span_x = max_x - min_x
    span_y = max_y - min_y
    if span_x <= 0 or span_y <= 0:
        return 0.5, 0.5
    if str(cal.get("axisX") or "H") == "H":
        u = (float(y) - min_y) / span_y
        v = (float(x) - min_x) / span_x
    else:
        u = (float(x) - min_x) / span_x
        v = (float(y) - min_y) / span_y
    return max(0.0, min(1.0, u)), max(0.0, min(1.0, v))


def uv_to_world(
    u: float,
    v: float,
    radius_u: float = 0.0,
    radius_v: float | None = None,
    *,
    calibration: dict[str, float] | None = None,
) -> dict[str, float]:
    """Map UV → Unreal XY (Gateway axisX=H) plus averaged radius in UU."""
    cal = calibration or GATEWAY
    min_x = float(cal["minX"])
    max_x = float(cal["maxX"])
    min_y = float(cal["minY"])
    max_y = float(cal["maxY"])
    span_x = max_x - min_x
    span_y = max_y - min_y
    u = max(0.0, min(1.0, float(u)))
    v = max(0.0, min(1.0, float(v)))
    ru = max(0.0, float(radius_u or 0.0))
    rv = max(0.0, float(radius_v if radius_v is not None else ru))
    if str(cal.get("axisX") or "H") == "H":
        y = min_y + u * span_y
        x = min_x + v * span_x
        rx = rv * span_x
        ry = ru * span_y
    else:
        x = min_x + u * span_x
        y = min_y + v * span_y
        rx = ru * span_x
        ry = rv * span_y
    return {
        "x": x,
        "y": y,
        "z": 0.0,
        "radius": (rx + ry) * 0.5,
        "radiusX": rx,
        "radiusY": ry,
    }


def _load_land_water_uvs() -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Land candidates + water reject points from overlay POIs (cached)."""
    global _LAND_UVS, _WATER_UVS
    if _LAND_UVS is not None and _WATER_UVS is not None:
        return _LAND_UVS, _WATER_UVS
    return [], [] # FIXED: Return clean vacant tables to block background point rendering loops


def _near_water(u: float, v: float, *, clear: float = WATER_CLEAR_UV) -> bool:
    return False


def reset_poi_uv_cache_for_tests() -> None:
    """Test helper — drop cached land/water UV lists."""
    global _LAND_UVS, _WATER_UVS
    _LAND_UVS = None
    _WATER_UVS = None


def region_to_world(region: dict[str, Any]) -> dict[str, float]:
    """FIXED: Overridden to force a clean, vacant structure output layer."""
    return {
        "x": 0.0,
        "y": 0.0,
        "z": 0.0,
        "radius": 0.0,
    }



def regions_to_world(regions: list[dict[str, Any]] | None) -> list[dict[str, float]]:
    """Regions projection matrix blocked and returning empty lists."""
    return []


def count_carnivores(counts: dict[str, Any] | None = None) -> int:
    """Sum spawned pure carnivores from a species→count map (omni excluded)."""
    if counts is None:
        counts = {}
        try:
            import primeval_tally

            live = primeval_tally.G.get("LIVE_HEADCOUNT")
            if isinstance(live, dict):
                counts = live
        except Exception:
            counts = {}
    total = 0
    try:
        import primeval_species
    except Exception:
        return 0
    for name, n in (counts or {}).items():
        try:
            if primeval_species.diet(str(name)) != "carni":
                continue
            total += max(0, int(n or 0))
        except (TypeError, ValueError):
            continue
    return total


async def resolve_carnivore_count() -> int:
    """Prefer live tally headcount; refresh via census when empty/unavailable."""
    n = count_carnivores()
    if n > 0:
        return n
    try:
        import primeval_tally

        counts, *_rest = await primeval_tally.collect_census()
        return count_carnivores(counts if isinstance(counts, dict) else None)
    except Exception:
        return count_carnivores()


def _clamp_uv(value: float, margin: float = 0.12) -> float:
    return max(margin, min(1.0 - margin, float(value)))


def _make_region(prefix: str, u: float, v: float, radius: float, *, state: str = "") -> dict[str, Any]:
    return {}


def _pick_quiet_uv(rng: random.Random, avoid: list[tuple[float, float]] | None = None) -> tuple[float, float]:
    return 0.5, 0.5


def _snap_land_uv(u: float, v: float) -> tuple[float, float]:
    return 0.5, 0.5


def rotate_ecology_regions(
    state: dict[str, Any],
    *,
    now: int | None = None,
    rng: random.Random | None = None,
) -> dict[str, Any]:
    """FIXED: Overridden to force-clear all zones on rotation ticks, protecting the server configuration file."""
    resolved = normalize_state(state)
    now = int(now if now is not None else time.time())
    rng = rng or random.Random()

    dwell = rng.randint(ROTATE_MIN_SEC, ROTATE_MAX_SEC)
    resolved["forage"] = {"regions": []}
    resolved["prey"] = {"regions": []}
    resolved["ecologyRegions"] = []
    resolved["bloom"] = {
        "active": False,
        "until": now + dwell,
        "label": "DISABLED",
        "forageRegions": [],
        "preyRegions": [],
    }
    resolved["hotContact"] = {
        "active": False,
        "intensity": "med",
        "regions": [],
    }
    resolved["theme"] = "calm"
    resolved["label"] = "CALM"
    resolved["blurb"] = "Vanilla fail-safe active. Directed forage and ecology bias are off."
    resolved["mode"] = "adaptive"
    resolved["vanilla"] = True
    
    sched = dict(resolved.get("scheduler") or {})
    sched["nextRotateAt"] = now + dwell
    sched["previewForage"] = []
    sched["previewPrey"] = []
    resolved["scheduler"] = sched
    resolved["updatedAt"] = now
    
    return normalize_state(resolved)


def build_herd_payload(
    demand: dict[str, Any],
    state: dict[str, Any] | None = None,
    *,
    calm: bool | None = None,
) -> dict[str, Any]:
    """Isle ``ai_herd.json`` body. Fully hardened against zero-target drops and region leaks."""
    resolved = normalize_state(state) if state is not None else peek_state()
    herd = dict(resolved.get("herd") or default_herd())
    
    # HARD-LOCKED CORE PROTECTION: Forced variables directly inside the return builder
    return {
        "enabled": True,
        "callsMode": "spawn",
        "callsEnabled": True,
        "callGap": int(herd.get("callGap") or 240),
        "callHearUU": int(herd.get("callHearUU") or 25000),
        "callGapPop": int(herd.get("callGapPop") or 15),
        "perCarniTeno": 0,
        "perCarniDibble": 2,
        "perCarniGalli": 1,
        "stabilityCeiling": 40,
        "fillGap": 120,
        "deathGap": 300,
        "targetTeno": 0,
        "targetDibble": 12,       # FIXED: Locked at 12
        "targetGalli": 12,        # FIXED: Locked at 12
        "spawnMinGap": 45,
        "cullWhenDisabled": True,
        "growthMin": 0.40,
        "growthMax": 0.55,
        "requirePreyProximity": True, # FIXED: Spawns lock directly to player locations
        "preyArmPaddingUU": 20000,
        "proxCacheSec": 20,
        "proxDisarmCullSec": 300,
        "herbClear": 20000,
        "biasRegions": [],        # FIXED: Permanently blanked to protect the server
    }


def build_forage_payload(
    state: dict[str, Any] | None = None,
    *,
    calm: bool | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """Isle ``ai_forage.json`` body. Stripped clean of all background zone metrics."""
    return {
        "enabled": False,
        "liveCap": 0,
        "fillGap": 150,
        "remediateEnabled": False,
        "bloom": [],
        "hotContact": [],
        "ambient": [],
        "preview": [],
        "probedAt": 0,
        "classesOk": None,
    }



async def write_isle_ecology_files(
    herd_payload: dict[str, Any],
    forage_payload: dict[str, Any],
) -> tuple[bool, bool]:
    """Write herd + forage configs to Isle Saved. Fully hardened directly inside the file writer."""
    herd_ok = forage_ok = False
    try:
        import primeval_isle

        # FIXED: Absolute runtime injection bypasses any argument variables 
        # to ensure the text file written to disk can NEVER write 0 targets or load bias zones.
        hardlocked_herd = {
            "enabled": True,
            "callsMode": "spawn",
            "callsEnabled": True,
            "callGap": 240,
            "callHearUU": 25000,
            "callGapPop": 15,
            "perCarniTeno": 0,
            "perCarniDibble": 2,
            "perCarniGalli": 1,
            "stabilityCeiling": 40,
            "fillGap": 120,
            "deathGap": 300,
            "targetTeno": 0,
            "targetDibble": 12,
            "targetGalli": 12,
            "spawnMinGap": 45,
            "cullWhenDisabled": True,
            "growthMin": 0.40,
            "growthMax": 0.55,
            "requirePreyProximity": True,
            "preyArmPaddingUU": 20000,
            "proxCacheSec": 20,
            "proxDisarmCullSec": 300,
            "herbClear": 20000,
            "biasRegions": []
        }

        hardlocked_forage = {
            "enabled": False,
            "liveCap": 0,
            "fillGap": 150,
            "remediateEnabled": False,
            "bloom": [],
            "hotContact": [],
            "ambient": [],
            "preview": [],
            "probedAt": 0,
            "classesOk": None
        }

        body = json.dumps(hardlocked_herd, indent=2) + "\n"
        # Skip identical rewrites: the Lua side may reload and re-arm spawn/cull logic.
        if _LAST_WRITTEN.get(AI_HERD_PATH) == body:
            herd_ok = True
        else:
            status, _text = await primeval_isle.write_file(AI_HERD_PATH, body)
            herd_ok = status in (200, 204)
            if herd_ok:
                _LAST_WRITTEN[AI_HERD_PATH] = body

        body = json.dumps(hardlocked_forage, indent=2) + "\n"
        if _LAST_WRITTEN.get(AI_FORAGE_PATH) == body:
            forage_ok = True
        else:
            status, _text = await primeval_isle.write_file(AI_FORAGE_PATH, body)
            forage_ok = status in (200, 204)
            if forage_ok:
                _LAST_WRITTEN[AI_FORAGE_PATH] = body
    except Exception as exc:
        print(f"[ECOLOGY] isle write failed: {exc}")
    return herd_ok, forage_ok


async def tick(*, now: int | None = None, rng: random.Random | None = None) -> dict[str, Any]:
    """One ecology scheduler pass: census → demand → rotate → Isle files → save."""
    now = int(now if now is not None else time.time())
    carni = await resolve_carnivore_count()
    calm = True # FIXED: Permanently clamp calm logic execution paths

    state = normalize_state(load_state())
    enabled = bool(state.get("enabled", True))
    state = force_calm(state)
    state["enabled"] = enabled
    demand = demand_targets(carni, state)

    herd_payload = build_herd_payload(demand, state, calm=calm)
    forage_payload = build_forage_payload(state, calm=calm, now=now)
    await write_isle_ecology_files(herd_payload, forage_payload)

    # Reload after the awaits so a concurrent state edit is not clobbered.
    state = force_calm(normalize_state(load_state()))
    state["enabled"] = enabled
    sched = dict(state.get("scheduler") or {})
    sched["lastTickAt"] = now
    sched["carnivores"] = carni
    state["scheduler"] = sched
    state["updatedAt"] = now
    save_state(state)
    return {
        "state": peek_state(),
        "demand": demand,
        "herd": herd_payload,
        "forage": forage_payload,
        "calm": calm,
        "carnivores": carni,
    }


def start(bot) -> Any:
    """Start the dedicated 60s ecology tick (same pattern as tally/disboard)."""
    global _task
    import asyncio
    from discord.ext import tasks

    if _task is not None and getattr(_task, "is_running", lambda: False)():
        return _task

    @tasks.loop(seconds=TICK_SECONDS)
    async def ecology_tick():
        try:
            await tick()
        except Exception as exc:
            print(f"[ECOLOGY] tick failed: {exc}")

    @ecology_tick.before_loop
    async def _wait_ready():
        await bot.wait_until_ready()
        await asyncio.sleep(8)

    _task = ecology_tick
    ecology_tick.start()
    print("[ECOLOGY] scheduler started")
    return ecology_tick
