"""Single species table for Discord and the Isle Lua mod."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_PATH = ROOT / "species.json"
CAPS_PATH = ROOT / "species_caps.json"
LUA_PATHS = [
    ROOT.parent / "Mods" / "PrimevalRedeem" / "Scripts" / "species.lua",
    ROOT.parent / "vendor" / "ue4ss-experimental" / "ue4ss" / "Mods" / "PrimevalRedeem" / "Scripts" / "species.lua",
]
LUA_HELPERS = r'''
-- ============================================================
-- Optimized species helper layer
-- Generated automatically by primeval_species.py.
-- ============================================================

PRIMEVAL_SPECIES_CACHE = PRIMEVAL_SPECIES_CACHE or {}
PRIMEVAL_CAP_CACHE = PRIMEVAL_CAP_CACHE or {}

local PRIMEVAL_CANONICAL_BY_KEY = {}
local PRIMEVAL_PLAYABLE_BY_KEY = {}

for _, displayName in ipairs(PRIMEVAL_PLAYABLE or {}) do
    local display = tostring(displayName or "")
    local key = string.lower(display)

    PRIMEVAL_CANONICAL_BY_KEY[key] = display
    PRIMEVAL_PLAYABLE_BY_KEY[key] = true
end

for alias, displayName in pairs(PRIMEVAL_ALIASES or {}) do
    local aliasKey = string.lower(tostring(alias or ""))
    local display = tostring(displayName or "")
    local canonicalKey = string.lower(display)

    PRIMEVAL_CANONICAL_BY_KEY[aliasKey] = display
    PRIMEVAL_PLAYABLE_BY_KEY[canonicalKey] = true
end

local function primevalCleanSpeciesText(value)
    local text = tostring(value or "")
    if text == "" then
        return ""
    end

    text = text:gsub("\\", "/")
    text = text:gsub("%s+", "")
    text = text:gsub("^.*/", "")
    text = text:gsub("^Class", "")
    text = text:gsub("^BPC?[_%-]", "")
    text = text:gsub("Character$", "")
    text = text:gsub("Dinosaur$", "")
    text = text:gsub("Pawn$", "")
    text = text:gsub("Player$", "")

    return string.lower(text)
end

local function primevalSpeciesFromClassPath(raw)
    local lower = string.lower(tostring(raw or ""))
    if lower == "" then
        return nil
    end

    -- Search aliases first so UE class names such as Allosaurus
    -- resolve to the configured playable name Allo.
    for alias, displayName in pairs(PRIMEVAL_ALIASES or {}) do
        local aliasKey = string.lower(tostring(alias or ""))
        if aliasKey ~= "" and lower:find(aliasKey, 1, true) ~= nil then
            return string.lower(tostring(displayName or ""))
        end
    end

    for _, displayName in ipairs(PRIMEVAL_PLAYABLE or {}) do
        local key = string.lower(tostring(displayName or ""))
        if key ~= "" and lower:find(key, 1, true) ~= nil then
            return key
        end
    end

    return nil
end

function speciesKey(value)
    local raw = tostring(value or "")
    if raw == "" then
        return ""
    end

    local cached = PRIMEVAL_SPECIES_CACHE[raw]
    if cached ~= nil then
        return cached
    end

    local fromPath = primevalSpeciesFromClassPath(raw)
    if fromPath ~= nil and fromPath ~= "" then
        PRIMEVAL_SPECIES_CACHE[raw] = fromPath
        return fromPath
    end

    local cleaned = primevalCleanSpeciesText(raw)
    local canonical = PRIMEVAL_CANONICAL_BY_KEY[cleaned]

    if canonical ~= nil then
        local result = string.lower(tostring(canonical))
        PRIMEVAL_SPECIES_CACHE[raw] = result
        return result
    end

    PRIMEVAL_SPECIES_CACHE[raw] = cleaned
    return cleaned
end

function speciesDisplayName(value)
    local key = speciesKey(value)
    if key == "" then
        return ""
    end

    local canonical = PRIMEVAL_CANONICAL_BY_KEY[key]
    if canonical ~= nil then
        return canonical
    end

    return tostring(value or "")
end

function speciesMatch(left, right)
    local a = speciesKey(left)
    local b = speciesKey(right)

    return a ~= "" and b ~= "" and a == b
end

function isPlayableSpecies(value)
    local key = speciesKey(value)
    return key ~= "" and PRIMEVAL_PLAYABLE_BY_KEY[key] == true
end

function isHerbivoreSpecies(value)
    return PRIMEVAL_HERB_KEYS[speciesKey(value)] == true
end

function isOmnivoreSpecies(value)
    return PRIMEVAL_OMNI_KEYS[speciesKey(value)] == true
end

function isCarnivoreSpecies(value)
    local key = speciesKey(value)
    return key ~= ""
        and PRIMEVAL_HERB_KEYS[key] ~= true
        and PRIMEVAL_OMNI_KEYS[key] ~= true
end

function capForSpecies(value, growth)
    local key = speciesKey(value)
    if key == "" then
        return nil
    end

    local cached = PRIMEVAL_CAP_CACHE[key]
    if cached ~= nil then
        if cached == false then
            return nil
        end
        return cached
    end

    local source = PRIMEVAL_GROWTH_CAPS[key]
    if source == nil then
        PRIMEVAL_CAP_CACHE[key] = false
        return nil
    end

    local result = {
        species = key,
        maxHunger = tonumber(source.maxHunger),
        maxFood = tonumber(source.maxFood),
    }

    PRIMEVAL_CAP_CACHE[key] = result
    return result
end

function clearSpeciesCaches()
    PRIMEVAL_SPECIES_CACHE = {}
    PRIMEVAL_CAP_CACHE = {}
end

function speciesCacheStats()
    local speciesCount = 0
    local capCount = 0

    for _ in pairs(PRIMEVAL_SPECIES_CACHE or {}) do
        speciesCount = speciesCount + 1
    end

    for _ in pairs(PRIMEVAL_CAP_CACHE or {}) do
        capCount = capCount + 1
    end

    return speciesCount, capCount
end

log(string.format(
    "species helpers ready: playable=%d aliases=%d caps=%d",
    #(PRIMEVAL_PLAYABLE or {}),
    (function()
        local n = 0
        for _ in pairs(PRIMEVAL_ALIASES or {}) do
            n = n + 1
        end
        return n
    end)(),
    (function()
        local n = 0
        for _ in pairs(PRIMEVAL_GROWTH_CAPS or {}) do
            n = n + 1
        end
        return n
    end)()
))
'''
_CACHE = None


def load():
    global _CACHE
    if _CACHE is None:
        with DATA_PATH.open("r", encoding="utf-8") as handle:
            _CACHE = json.load(handle)
    return _CACHE


def playable():
    return list(load().get("playable") or [])


def aliases():
    return dict(load().get("aliases") or {})


def herb():
    return {str(name).lower() for name in (load().get("herb") or [])}


def omni():
    return {str(name).lower() for name in (load().get("omni") or [])}


def small_prime():
    return set(load().get("small_prime") or [])


def emoji():
    return dict(load().get("emoji") or {})


_HERB_CACHE = None
_OMNI_CACHE = None


def _herb_set():
    global _HERB_CACHE
    if _HERB_CACHE is None:
        _HERB_CACHE = {
            str(name).strip().lower()
            for name in (load().get("herb") or [])
        }
    return _HERB_CACHE


def _omni_set():
    global _OMNI_CACHE
    if _OMNI_CACHE is None:
        _OMNI_CACHE = {
            str(name).strip().lower()
            for name in (load().get("omni") or [])
        }
    return _OMNI_CACHE


def diet(species):
    key = str(species or "").strip().lower()
    if not key:
        return "carni"

    herbs = _herb_set()
    omnis = _omni_set()

    if key in herbs or any(name in key for name in herbs):
        return "herb"

    if key in omnis or any(name in key for name in omnis):
        return "omni"

    return "carni"

def mutation_fname(label):
    mapping = (load().get("mutations") or {}).get("fname") or {}
    name = str(label or "").strip()
    return str(mapping.get(name) or name)


def mutation_choices(species, slot, female, exclude=None):
    """Juvenile (1) / sub-adult (2) mutation labels for a buy dropdown."""
    data = load().get("mutations") or {}
    slot = 1 if int(slot or 1) <= 1 else 2
    kind = diet(species)
    skip = set()
    for name in exclude or []:
        raw = str(name or "").strip()
        if not raw:
            continue
        skip.add(raw)
        skip.add(raw.lower())
        fname = mutation_fname(raw)
        if fname:
            skip.add(fname)
            skip.add(str(fname).lower())
    labels = []
    seen = set()

    def _add(names):
        for raw in names or []:
            label = str(raw).strip()
            fname = mutation_fname(label)
            if (
                not label
                or label in seen
                or label in skip
                or label.lower() in skip
                or fname in skip
                or str(fname).lower() in skip
            ):
                continue
            seen.add(label)
            labels.append(label)

    slot1 = data.get("slot1") or {}
    _add(slot1.get("all"))
    _add(slot1.get(kind))
    female_data = data.get("female") or {}
    if female:
        _add(female_data.get("slot1"))
    if slot >= 2:
        extra = data.get("slot2_extra") or {}
        _add(extra.get("all"))
        _add(extra.get(kind))
        if female:
            _add(female_data.get("slot2"))
    labels.sort(key=str.lower)
    return labels[:25]


def pack_mutations(slot1, slot2):
    parts = []
    one = mutation_fname(slot1)
    two = mutation_fname(slot2)
    if one:
        parts.append("MutationSlot1=" + one.replace("|", "").replace("=", ""))
    if two:
        parts.append("MutationSlot2=" + two.replace("|", "").replace("=", ""))
    return "|".join(parts)


def mutation_allowed(species, slot, female, label):
    want = str(label or "").strip()
    if not want:
        return False

    allowed_names = mutation_choices(species, slot, female)
    allowed_labels = {str(name).strip().lower() for name in allowed_names}
    allowed_files = {
        mutation_fname(name).strip().lower()
        for name in allowed_names
    }

    return (
        want.lower() in allowed_labels
        or mutation_fname(want).strip().lower() in allowed_files
    )

def _lua_set(names):
    parts = []
    for name in names:
        key = str(name).lower().replace('"', "")
        parts.append(f'    ["{key}"] = true')
    return ",\n".join(parts)


def _lua_map(mapping):
    parts = []
    for key, value in mapping.items():
        k = str(key).replace('"', "")
        v = str(value).replace('"', "")
        parts.append(f'    ["{k}"] = "{v}"')
    return ",\n".join(parts)


def growth_caps():
    if not CAPS_PATH.is_file():
        return {}
    try:
        data = json.loads(CAPS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    caps = data.get("caps") if isinstance(data, dict) else None
    return caps if isinstance(caps, dict) else {}


def _lua_caps(caps):
    parts = []
    for key in sorted(caps):
        row = caps.get(key) or {}
        hunger = row.get("maxHunger")
        food = row.get("maxFood")
        if hunger is None:
            continue
        name = str(key).lower().replace('"', "")
        food_txt = "nil" if food is None else f"{float(food):.10g}"
        parts.append(
            f'    ["{name}"] = {{ maxHunger = {float(hunger):.10g}, maxFood = {food_txt} }}'
        )
    return ",\n".join(parts)


def lua_source():
    data = load()

    playables = ", ".join(
        f'"{str(name).replace(chr(34), "")}"'
        for name in data.get("playable") or []
    )

    generated = (
        "-- Generated from bot/species.json + bot/species_caps.json.\n"
        "-- Do not edit the generated section by hand.\n"
        "PRIMEVAL_PLAYABLE = {" + playables + "}\n"
        "PRIMEVAL_ALIASES = {\n"
        + _lua_map(data.get("aliases") or {})
        + "\n}\n"
        "PRIMEVAL_HERB_KEYS = {\n"
        + _lua_set(data.get("herb") or [])
        + "\n}\n"
        "PRIMEVAL_OMNI_KEYS = {\n"
        + _lua_set(data.get("omni") or [])
        + "\n}\n"
        "PRIMEVAL_GROWTH_CAPS = {\n"
        + _lua_caps(growth_caps())
        + "\n}\n"
    )

    return generated + LUA_HELPERS

def write_lua():
    body = lua_source()
    written = []
    for path in LUA_PATHS:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        written.append(str(path))
    return written


if __name__ == "__main__":
    for path in write_lua():
        print("wrote", path)
