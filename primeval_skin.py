"""Natural-look skins: Discord per-channel swatches, one paint per dino.

Isle Lua (skin.lua) applies CustomizerData. No neon, no HDR, no free RGB.
Live recode requires a fresh juvenile (under 35%). Vault paint locks that dino.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time

import discord
from discord import app_commands, ui

G = {}

CHANNELS = (
    ("BodyColor", "Body", "body"),
    ("MarkingsColor", "Markings", "markings"),
    ("FlankColor", "Flank", "flank"),
    ("UnderbellyColor", "Underbelly", "underbelly"),
    ("Detail1Color", "Detail", "detail"),
    ("EyesColor", "Eyes", "eyes"),
    ("MaleDisplayColor", "Male display", "display"),
    ("TeethColor", "Teeth", "teeth"),
    ("MouthColor", "Mouth", "mouth"),
    ("ClawsColor", "Claws", "claws"),
)

SWATCHES = (
    ("sand", "Sand", (0.58, 0.48, 0.34)),
    ("tan", "Tan", (0.56, 0.46, 0.32)),
    ("khaki", "Khaki", (0.54, 0.48, 0.30)),
    ("clay", "Clay", (0.52, 0.32, 0.22)),
    ("rust", "Rust", (0.48, 0.28, 0.18)),
    ("terracotta", "Terracotta", (0.50, 0.30, 0.22)),
    ("mahogany", "Mahogany", (0.36, 0.20, 0.14)),
    ("bark", "Bark", (0.28, 0.20, 0.14)),
    ("mud", "Mud", (0.30, 0.22, 0.16)),
    ("umber", "Umber", (0.34, 0.24, 0.14)),
    ("olive", "Olive", (0.36, 0.36, 0.22)),
    ("forest", "Forest", (0.32, 0.34, 0.22)),
    ("pine", "Pine", (0.16, 0.24, 0.12)),
    ("moss", "Moss", (0.34, 0.36, 0.24)),
    ("sage", "Sage", (0.40, 0.42, 0.30)),
    ("grass", "Dry grass", (0.50, 0.44, 0.26)),
    ("stone", "Stone", (0.42, 0.40, 0.36)),
    ("ash", "Ash", (0.40, 0.40, 0.38)),
    ("slate", "Slate", (0.34, 0.36, 0.38)),
    ("dust", "Dust", (0.56, 0.50, 0.40)),
    ("charcoal", "Charcoal", (0.20, 0.18, 0.16)),
    ("dusk", "Dusk", (0.36, 0.24, 0.18)),
    ("wine", "Wine", (0.38, 0.20, 0.18)),
    ("ivory", "Ivory", (0.64, 0.60, 0.50)),
    ("bone", "Bone", (0.62, 0.56, 0.46)),
    ("coal", "Coal", (0.14, 0.12, 0.10)),
)

SWATCH_MAP = {key: tuple(rgb) for key, _label, rgb in SWATCHES}
SWATCH_LABELS = {key: label for key, label, _rgb in SWATCHES}
SWATCH_DESCRIPTORS = {
    "sand": "warm sandy brown",
    "tan": "muted golden tan",
    "khaki": "dusty olive tan",
    "clay": "earthy red clay",
    "rust": "deep weathered orange",
    "terracotta": "warm muted red-orange",
    "mahogany": "dark reddish brown",
    "bark": "deep tree-bark brown",
    "mud": "dark neutral earth",
    "umber": "warm raw umber",
    "olive": "muted olive green",
    "forest": "muted woodland green",
    "pine": "deep shadowed evergreen",
    "moss": "soft mossy green",
    "sage": "pale grey-green",
    "grass": "sun-dried grass",
    "stone": "warm stone grey",
    "ash": "neutral ash grey",
    "slate": "cool slate grey",
    "dust": "light dusty beige",
    "charcoal": "soft near-black",
    "dusk": "smoky reddish brown",
    "wine": "deep muted burgundy",
    "ivory": "warm creamy ivory",
    "bone": "aged natural bone",
    "coal": "deep natural black",
}

THEMES = {
    "forest": {
        "label": "Forest",
        "description": "Deep woodland greens, bark, and moss",
        "palette": ("pine", "forest", "moss", "olive", "sage", "bark", "umber", "grass"),
        "defaults": ("forest", "bark", "moss", "sage", "olive", "rust", "grass", "bone", "wine", "coal"),
    },
    "jungle": {
        "label": "Jungle",
        "description": "Lush greens with warm natural accents",
        "palette": ("pine", "moss", "forest", "olive", "sage", "bark", "rust", "terracotta"),
        "defaults": ("moss", "forest", "olive", "sage", "bark", "rust", "terracotta", "bone", "wine", "coal"),
    },
    "delta": {
        "label": "Delta",
        "description": "River mud, reeds, slate, and silt",
        "palette": ("mud", "umber", "olive", "slate", "stone", "dust", "sage"),
        "defaults": ("mud", "slate", "stone", "dust", "umber", "olive", "rust", "bone", "wine", "charcoal"),
    },
    "plains": {
        "label": "Plains",
        "description": "Dry grass, khaki, and sun-warmed earth",
        "palette": ("grass", "khaki", "tan", "umber", "sand", "dust", "clay"),
        "defaults": ("grass", "umber", "khaki", "dust", "tan", "rust", "clay", "bone", "mahogany", "bark"),
    },
    "desert": {
        "label": "Desert",
        "description": "Sandstone, clay, and muted red earth",
        "palette": ("sand", "tan", "khaki", "clay", "rust", "terracotta", "dust"),
        "defaults": ("sand", "rust", "tan", "dust", "terracotta", "umber", "clay", "bone", "wine", "bark"),
    },
    "swamp": {
        "label": "Swamp",
        "description": "Dark mud, algae greens, and peat",
        "palette": ("mud", "coal", "pine", "olive", "forest", "moss", "bark", "sage"),
        "defaults": ("mud", "coal", "olive", "sage", "forest", "wine", "rust", "bone", "mahogany", "coal"),
    },
    "mountain": {
        "label": "Mountain",
        "description": "Cold stone, ash, slate, and dark earth",
        "palette": ("stone", "ash", "slate", "charcoal", "dust", "dusk", "bark"),
        "defaults": ("stone", "charcoal", "slate", "ash", "dusk", "coal", "wine", "ivory", "mahogany", "charcoal"),
    },
    "dark": {
        "label": "Dark",
        "description": "Natural near-black, charcoal, and dusk tones",
        "palette": ("coal", "charcoal", "pine", "slate", "ash", "bark", "dusk", "wine"),
        "defaults": ("charcoal", "coal", "slate", "ash", "bark", "wine", "rust", "bone", "wine", "coal"),
    },
}

BIOLOGICAL_PALETTES = {
    "EyesColor": ("coal", "charcoal", "wine", "olive", "rust", "umber"),
    "TeethColor": ("ivory", "bone", "dust", "stone"),
    "MouthColor": ("wine", "mahogany", "rust", "mud", "dusk"),
    "ClawsColor": ("coal", "charcoal", "bark", "mud", "bone"),
}

AREA_PALETTE_EXTRAS = {
    "MarkingsColor": ("bark", "charcoal", "coal"),
    "UnderbellyColor": ("dust", "bone", "ivory", "tan"),
    "Detail1Color": ("bark", "umber", "charcoal"),
    "MaleDisplayColor": ("rust", "terracotta", "clay", "wine"),
}

_ACTIVE_WIZARDS = {}
WIZARD_TIMEOUT = 20 * 60


def bind(g):
    G.clear()
    G.update(g)


def _steam_for(user):
    getter = G.get("get_linked_steam_id")
    if not getter or user is None:
        return ""
    try:
        return str(getter(int(user.id)) or "").strip()
    except Exception:
        return ""


def _clamp(n, lo, hi):
    n = tonumber(n) or 0
    if n < lo:
        return lo
    if n > hi:
        return hi
    return n


def tonumber(v):
    try:
        return float(v)
    except Exception:
        return 0.0


def _hex_for_swatch(key):
    rgb = SWATCH_MAP.get(key)
    if not rgb:
        return "#??????"
    return "#" + "".join(f"{round(_clamp(value, 0, 1) * 255):02X}" for value in rgb)


def _swatch_name(rgb):
    if not rgb or len(rgb) < 3:
        return "?"
    best, best_d = "?", 9.0
    for key, label, color in SWATCHES:
        d = abs(tonumber(rgb[0]) - color[0]) + abs(tonumber(rgb[1]) - color[1]) + abs(tonumber(rgb[2]) - color[2])
        if d < best_d:
            best, best_d = label, d
    return best


def _theme_mix(theme_key):
    theme = THEMES.get(theme_key) or {}
    defaults = tuple(theme.get("defaults") or ())
    out = {}
    for index, (field, _label, _alias) in enumerate(CHANNELS):
        if index < len(defaults):
            key = defaults[index]
            if key in SWATCH_MAP:
                out[field] = SWATCH_MAP[key]
    return out


def _theme_default_key(theme_key, field):
    defaults = tuple((THEMES.get(theme_key) or {}).get("defaults") or ())
    index = next((i for i, row in enumerate(CHANNELS) if row[0] == field), -1)
    if 0 <= index < len(defaults):
        return defaults[index]
    return ""


def _area_swatch_keys(theme_key, field):
    theme = THEMES.get(theme_key) or {}
    default = _theme_default_key(theme_key, field)
    source = BIOLOGICAL_PALETTES.get(field)
    if source is None:
        source = tuple(theme.get("palette") or ()) + tuple(AREA_PALETTE_EXTRAS.get(field) or ())
    keys = []
    for key in (default,) + tuple(source):
        if key in SWATCH_MAP and key not in keys:
            keys.append(key)
    return keys[:12]


def _wizard_summary(session):
    theme = THEMES.get(session.theme) or {}
    lines = [
        f"**Theme:** {theme.get('label', session.theme.title())}",
        "**Final colors:**",
    ]
    for field, label, _alias in CHANNELS:
        key = session.choices.get(field) or _theme_default_key(session.theme, field)
        name = SWATCH_LABELS.get(key, _swatch_name(session.mix.get(field)))
        lines.append(f"• {label}: **{name} · `{_hex_for_swatch(key)}`**")
    return "\n".join(lines)


async def _wizard_reply(interaction, content):
    kwargs = {} if interaction.guild is None else {"ephemeral": True}
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, **kwargs)
        else:
            await interaction.response.send_message(content, **kwargs)
    except Exception:
        pass


def pack_mix(mix):
    if not mix:
        return ""
    parts = ["cv=2"]
    for field, _label, _key in CHANNELS:
        c = mix.get(field)
        if not c or len(c) < 3:
            continue
        r = _clamp(tonumber(c[0]), 0.10, 0.68)
        g = _clamp(tonumber(c[1]), 0.08, 0.68)
        b = _clamp(tonumber(c[2]), 0.08, 0.58)
        parts.append(f"{field}={r:.5f},{g:.5f},{b:.5f},1.00000")
    return "|".join(parts) if len(parts) > 1 else ""


def _looks_ours(packed):
    packed = str(packed or "")
    if not packed:
        return False
    known = {field for field, _label, _key in CHANNELS}
    n = 0
    hit = 0
    for piece in packed.split("|"):
        if "=" not in piece:
            continue
        key, val = piece.split("=", 1)
        if key not in known:
            continue
        parts = val.split(",")
        if len(parts) < 3:
            continue
        try:
            r, g, b = tonumber(parts[0]), tonumber(parts[1]), tonumber(parts[2])
        except Exception:
            continue
        n += 1
        if abs(tonumber(r)) + abs(tonumber(g)) + abs(tonumber(b)) > 0.01:
            hit += 1
    return n >= 6 and (packed.find("cv=2") >= 0 or hit == n)


def _locked(row):
    unlocked = (row or {}).get("skinUnlocked")
    if unlocked is True or str(unlocked).lower() == "true":
        return False
    v = (row or {}).get("skinLocked")
    if v is True or str(v).lower() == "true":
        return True
    name = str((row or {}).get("skin") or "").strip().lower()
    if name in THEMES or name == "custom":
        return True
    return _looks_ours((row or {}).get("skinData") or "")


def _source(row):
    return str((row or {}).get("source") or (row or {}).get("kind") or "store").lower()


async def _queue_skin(steam, preset, slot="", skin_data="", command_id=""):
    import primeval_isle

    payload = {
        "id": command_id or f"skin-{steam[-6:]}-{int(time.time() * 1000)}",
        "ts": int(time.time()),
        "verb": "skin",
        "steam": str(steam),
        "preset": preset or "custom",
    }
    if slot:
        payload["slot"] = str(slot)
    if skin_data:
        payload["skinData"] = str(skin_data)
    return await primeval_isle.queue_inbox(payload)


async def _queue_skin_unlock(steam, store_id, command_id=""):
    import primeval_isle

    payload = {
        "id": command_id or f"skinunlock-{steam[-6:]}-{int(time.time() * 1000)}",
        "ts": int(time.time()),
        "verb": "skinunlock",
        "steam": str(steam),
        "slot": str(store_id),
    }
    return await primeval_isle.queue_inbox(payload)


async def _wait_skin_result(command_id, timeout=18):
    import primeval_isle

    path = "/TheIsle/Binaries/Win64/ue4ss/Mods/PrimevalRedeem/Saved/results.ndjson"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status, raw = await primeval_isle.read_file(path)
        except Exception:
            status, raw = 0, ""
        if status == 200:
            for line in reversed(str(raw or "").splitlines()):
                if command_id not in line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if str(row.get("id") or "") == command_id:
                    return row.get("ok") is True, str(row.get("msg") or "")
        await asyncio.sleep(1.5)
    return None, "The Isle did not return a confirmation in time."


def _growth_pct(row):
    try:
        return float((row or {}).get("growth") or 0) * 100
    except Exception:
        return 0.0


def _vault_options(slots):
    options = []
    rows = {}
    seen = set()
    for row in list(slots or [])[-25:]:
        sid = str((row or {}).get("id") or "").strip()[:100]
        if not sid or sid in seen:
            continue
        seen.add(sid)
        rows[sid] = row
        species = str((row or {}).get("species") or "?")[:80]
        src = _source(row)
        if _locked(row):
            desc = "Locked: already painted"
        elif src in ("buy", "gamble"):
            desc = "Shop/gamble: store first"
        else:
            desc = "Stored: paint once, lock"
        options.append(
            discord.SelectOption(
                label=f"{species} · {_growth_pct(row):.0f}%"[:100],
                value=sid,
                description=desc[:100],
            )
        )
    if not options:
        options.append(discord.SelectOption(label="No vault slots", value="none"))
    return options, rows


async def _load_slot_row(steam, sid, row=None):
    from primeval_panels import _isle_file

    data = dict(row or {})
    sid = str(sid or data.get("id") or "").strip()
    if not sid:
        return data
    data["id"] = sid
    for folder in (
        "/TheIsle/Binaries/Win64/ue4ss/Mods/PrimevalRedeem/Saved/vault",
        "/TheIsle/Binaries/Win64/ue4ss/Mods/PrimevalRedeem/Saved/stored",
    ):
        status, raw = await _isle_file("GET", folder + "/" + str(steam) + "_" + sid + ".json")
        if status == 200 and raw and str(raw).strip().startswith("{"):
            try:
                loaded = json.loads(str(raw).splitlines()[0])
            except Exception:
                loaded = None
            if isinstance(loaded, dict):
                data.update(loaded)
                data["id"] = sid
                break
    return data


async def _read_vault_slots(steam):
    from primeval_panels import _isle_file

    status, raw = await _isle_file("GET", "/TheIsle/Binaries/Win64/ue4ss/Mods/PrimevalRedeem/Saved/vault/" + str(steam) + ".index.ndjson")
    rows = []
    if status == 200 and raw:
        for line in str(raw or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            sid = str((row or {}).get("id") or "").strip()
            if sid:
                rows.append(row)
    if rows:
        return rows
    status, raw = await _isle_file("GET", "/TheIsle/Binaries/Win64/ue4ss/Mods/PrimevalRedeem/Saved/vault/" + str(steam) + ".json")
    if status != 200 or not raw or not str(raw).strip().startswith("{"):
        return []
    try:
        data = json.loads(str(raw).splitlines()[0])
    except Exception:
        return []
    if isinstance(data, dict):
        data.setdefault("id", "vault")
        return [data]
    return []


class SkinWizardSession:
    def __init__(self, user_id, steam):
        self.user_id = int(user_id)
        self.steam = str(steam)
        self.token = secrets.token_hex(8)
        self.mode = "ephemeral"
        self.theme = ""
        self.mix = {}
        self.choices = {}
        self.area_index = 0
        self.slots = []
        self.applying = False

    def reset(self):
        self.token = secrets.token_hex(8)
        self.theme = ""
        self.mix = {}
        self.choices = {}
        self.area_index = 0
        self.slots = []
        self.applying = False


class SkinWizardView(ui.View):
    def __init__(self, session):
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.owner_id = session.user_id
        self.token = session.token

    def session(self):
        s = _ACTIVE_WIZARDS.get(self.owner_id)
        if s is None or s.token != self.token:
            return None
        return s

    async def interaction_check(self, interaction):
        if int(interaction.user.id) != self.owner_id:
            await _wizard_reply(interaction, "This skin wizard is not yours. Use `/skin`.")
            return False
        if self.session() is None:
            await _wizard_reply(interaction, "That skin step expired. Use `/skin` to restart.")
            return False
        return True

    async def on_timeout(self):
        s = self.session()
        if s:
            _ACTIVE_WIZARDS.pop(self.owner_id, None)

    async def on_error(self, interaction, error, item):
        print(f"[SKIN] wizard {type(item).__name__}: {error}")
        s = self.session()
        if s is not None:
            s.applying = False
        await _wizard_reply(interaction, "Skin step failed. Restart with `/skin`.")


def _render_theme_step():
    return (
        "**Skin painter — Step 1 of 12: Pick a theme**\n"
        "Each theme has coordinated realistic palettes. You'll choose every body area next.\n"
        "Natural colors only; one paint permanently locks that dino's look."
    )


def _render_area_step(session, index):
    field, label, _alias = CHANNELS[index]
    theme = THEMES.get(session.theme) or {}
    default_key = _theme_default_key(session.theme, field)
    return (
        f"**Skin painter — Step {index + 2} of 12: {label}**\n"
        f"Theme: **{theme.get('label', session.theme.title())}**\n"
        f"Choose a compatible swatch. Theme default: **{SWATCH_LABELS.get(default_key, default_key.title())} · `{_hex_for_swatch(default_key)}`**.\n"
        "HEX codes are approximate; species materials and lighting shift the final shade."
    )


async def _advance_wizard(interaction, current_view, session, completed, content, next_view):
    if session.mode == "dm":
        for child in current_view.children:
            child.disabled = True
        await interaction.response.edit_message(content=completed, view=current_view)
        current_view.stop()
        await interaction.channel.send(content, view=next_view)
        return
    await interaction.response.edit_message(content=completed + "\n\n" + content, view=next_view)
    current_view.stop()


async def _show_final_step(interaction, current_view, session, completed):
    if session.mode == "dm":
        for child in current_view.children:
            child.disabled = True
        await interaction.response.edit_message(content=completed + "\nLoading your vault…", view=current_view)
        current_view.stop()
    else:
        await interaction.response.edit_message(content="Loading your vault…", view=None)
        current_view.stop()

    await _refresh_slots(session)

    content = (
        "**Skin painter — Step 12 of 12: Apply**\n"
        + _wizard_summary(session)
        + "\n\nChoose the spawned juvenile or a stored vault dino. "
        "Locked and shop/gamble slots cannot be painted. **Applying locks the look.**"
    )
    final_view = SkinWizardFinalView(session)
    if session.mode == "dm":
        await interaction.channel.send(content, view=final_view)
    else:
        await interaction.edit_original_response(content=content, view=final_view)


async def _refresh_slots(session):
    try:
        session.slots = await _read_vault_slots(session.steam)
    except Exception:
        session.slots = []


async def _finish_apply(interaction, view, session, success, msg):
    active = _ACTIVE_WIZARDS.get(session.user_id)
    same = active is session and active.token == session.token
    if not same:
        view.stop()
        suffix = "" if success else "\nA newer wizard is active; continue there."
        await interaction.edit_original_response(content=msg + suffix, view=None)
        return
    if success:
        _ACTIVE_WIZARDS.pop(session.user_id, None)
        view.stop()
        await interaction.edit_original_response(content=msg, view=None)
    else:
        session.applying = False
        view.stop()
        retry_view = SkinWizardFinalView(session)
        await interaction.edit_original_response(content="**Not applied**\n" + msg + "\n\n" + _wizard_summary(session), view=retry_view)


class SkinWizardRestartButton(ui.Button):
    def __init__(self, row=1):
        super().__init__(label="Restart wizard", style=discord.ButtonStyle.secondary, row=row)

    async def callback(self, interaction):
        view = self.view
        s = view.session()
        if s is None:
            await _wizard_reply(interaction, "That step expired. Use `/skin`.")
            return
        s.reset()
        _ACTIVE_WIZARDS[s.user_id] = s
        nxt = SkinWizardThemeView(s)
        await _advance_wizard(interaction, view, s, "Wizard restarted.", _render_theme_step(), nxt)


class SkinWizardThemeSelect(ui.Select):
    def __init__(self):
        opts = [
            discord.SelectOption(label=v["label"], value=k, description=v["description"][:100])
            for k, v in THEMES.items()
        ]
        super().__init__(placeholder="Pick a habitat…", options=opts, row=0)

    async def callback(self, interaction):
        view = self.view
        s = view.session()
        if s is None:
            await _wizard_reply(interaction, "That step expired. Use `/skin`.")
            return
        s.theme = self.values[0]
        s.mix = _theme_mix(s.theme)
        s.choices = {}
        s.area_index = 0
        lbl = THEMES[s.theme]["label"]
        nxt = SkinWizardAreaView(s, 0)
        await _advance_wizard(interaction, view, s, f"Theme: **{lbl}**.", _render_area_step(s, 0), nxt)


class SkinWizardThemeView(SkinWizardView):
    def __init__(self, session):
        super().__init__(session)
        self.add_item(SkinWizardThemeSelect())
        self.add_item(SkinWizardRestartButton(row=1))


class SkinWizardAreaSelect(ui.Select):
    def __init__(self, session, index):
        self.area_index = index
        field, label, _alias = CHANNELS[index]
        default_key = _theme_default_key(session.theme, field)
        opts = []
        for key in _area_swatch_keys(session.theme, field):
            desc = f"Theme default" if key == default_key else f"Realistic option"
            lbl = SWATCH_LABELS[key]
            if key == default_key:
                lbl = f"Theme default — {lbl}"
            opts.append(
                discord.SelectOption(
                    label=f"{lbl} · {_hex_for_swatch(key)}"[:100],
                    value=key,
                    description=(f"{SWATCH_DESCRIPTORS.get(key, desc)} · in-game")[:100],
                )
            )
        super().__init__(placeholder=f"Choose {label}…", options=opts, row=0)

    async def callback(self, interaction):
        view = self.view
        s = view.session()
        if s is None:
            await _wizard_reply(interaction, "That step expired. Use `/skin`.")
            return
        if s.area_index != self.area_index:
            await _wizard_reply(interaction, "That step is complete.")
            return
        field, label, _alias = CHANNELS[self.area_index]
        key = self.values[0]
        if key not in _area_swatch_keys(s.theme, field):
            await _wizard_reply(interaction, "That color is not valid for this step.")
            return
        s.mix[field] = SWATCH_MAP[key]
        s.choices[field] = key
        s.area_index += 1

        completed = f"**{label}:** {SWATCH_LABELS[key]} `{_hex_for_swatch(key)}`"

        if s.area_index >= len(CHANNELS):
            await _show_final_step(interaction, view, s, completed)
            return

        nxt = SkinWizardAreaView(s, s.area_index)
        await _advance_wizard(interaction, view, s, completed, _render_area_step(s, s.area_index), nxt)


class SkinWizardAreaView(SkinWizardView):
    def __init__(self, session, index):
        super().__init__(session)
        self.add_item(SkinWizardAreaSelect(session, index))
        self.add_item(SkinWizardRestartButton(row=1))


class SkinWizardLiveButton(ui.Button):
    def __init__(self):
        super().__init__(label="Apply on spawned juvie", style=discord.ButtonStyle.success, row=0)

    async def callback(self, interaction):
        view = self.view
        s = view.session()
        if s is None:
            await _wizard_reply(interaction, "That step expired. Use `/skin`.")
            return
        if s.applying:
            await _wizard_reply(interaction, "Already submitting.")
            return
        if _steam_for(interaction.user) != s.steam:
            await _wizard_reply(interaction, "Your Steam link changed. Restart with `/skin`.")
            return

        s.applying = True
        await interaction.response.edit_message(content="Submitting to the Isle…", view=None)
        packed = pack_mix(s.mix)
        cmd_id = f"skin-{s.steam[-6:]}-{int(time.time() * 1000)}"
        try:
            ok, err = await _queue_skin(s.steam, s.theme, "", packed, command_id=cmd_id)
        except Exception as e:
            ok, err = False, str(e)

        if ok:
            confirmed, result = await _wait_skin_result(cmd_id)
            if confirmed is False:
                ok, err = False, result or "Isle rejected it."
            elif confirmed is None:
                await _finish_apply(interaction, view, s, True, "Submitted; Isle confirmation timed out. Check in-game.")
                return

        if ok:
            await _finish_apply(interaction, view, s, True, f"✅ Applied & locked. {result}")
        else:
            await _finish_apply(interaction, view, s, False, f"🛑 Isle rejected: {err}")


class SkinWizardVaultSelect(ui.Select):
    def __init__(self, opts, rows):
        self.slot_rows = rows
        super().__init__(placeholder="Paint a vault slot…", options=opts, row=1)

    async def callback(self, interaction):
        view = self.view
        s = view.session()
        if s is None:
            await _wizard_reply(interaction, "That step expired. Use `/skin`.")
            return
        sid = self.values[0]
        if sid == "none":
            await _wizard_reply(interaction, "No vault slots found.")
            return

        row = self.slot_rows.get(sid) or {}
        if _source(row) in ("buy", "gamble"):
            await _wizard_reply(interaction, "Shop slots can't be painted. Store first.")
            return
        if _locked(row):
            await _wizard_reply(interaction, "That dino's look is locked.")
            return
        if s.applying:
            await _wizard_reply(interaction, "Already submitting.")
            return
        if _steam_for(interaction.user) != s.steam:
            await _wizard_reply(interaction, "Steam link changed. Restart.")
            return

        s.applying = True
        await interaction.response.edit_message(content="Checking vault slot…", view=None)
        try:
            row = await _load_slot_row(s.steam, sid, row)
        except Exception:
            row = self.slot_rows.get(sid) or {}

        if _source(row) in ("buy", "gamble"):
            await _finish_apply(interaction, view, s, False, "Shop slots cannot be painted.")
            return
        if _locked(row):
            await _finish_apply(interaction, view, s, False, "That dino's look is locked.")
            return

        cmd_id = f"skin-{s.steam[-6:]}-{int(time.time() * 1000)}"
        try:
            ok, err = await _queue_skin(s.steam, s.theme, sid, pack_mix(s.mix), command_id=cmd_id)
        except Exception as e:
            ok, err = False, str(e)

        if ok:
            confirmed, result = await _wait_skin_result(cmd_id)
            if confirmed is False:
                ok, err = False, result or "Isle rejected it."
            elif confirmed is None:
                await _finish_apply(interaction, view, s, True, "Submitted; confirmation timed out.")
                return

        if ok:
            await _finish_apply(interaction, view, s, True, f"✅ Vault painted & locked. {result}")
        else:
            await _finish_apply(interaction, view, s, False, f"🛑 Isle rejected: {err}")


class SkinWizardFinalView(SkinWizardView):
    def __init__(self, session):
        super().__init__(session)
        self.add_item(SkinWizardLiveButton())
        opts, rows = _vault_options(session.slots)
        self.add_item(SkinWizardVaultSelect(opts, rows))
        self.add_item(SkinWizardRestartButton(row=2))


async def open_skin(interaction):
    steam = _steam_for(interaction.user)
    if not steam:
        await interaction.response.send_message("Link Steam first with 🔗.", ephemeral=True)
        return

    if interaction.response.is_done():
        await interaction.followup.send("Use a fresh `/skin` to start the painter.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    session = SkinWizardSession(interaction.user.id, steam)
    _ACTIVE_WIZARDS[session.user_id] = session

    content = _render_theme_step()
    dm_ok = False

    try:
        dm = await interaction.user.create_dm()
        session.mode = "dm"
        await dm.send(content, view=SkinWizardThemeView(session))
        dm_ok = True
    except Exception:
        pass

    if dm_ok:
        try:
            await interaction.edit_original_response(content="Sent the painter to your DMs.", view=None)
        except Exception:
            pass
        return

    session.mode = "ephemeral"
    try:
        await interaction.edit_original_response(content=content, view=SkinWizardThemeView(session))
    except Exception:
        _ACTIVE_WIZARDS.pop(session.user_id, None)


class SkinAdminUnlockSelect(ui.Select):
    def __init__(self, owner_id, member, steam, slots):
        self.owner_id = int(owner_id)
        self.member = member
        self.steam = str(steam)
        opts = []
        seen = set()
        for row in list(slots or [])[-25:]:
            sid = str((row or {}).get("id") or "").strip()
            if not sid or sid in seen:
                continue
            seen.add(sid)
            if _source(row) in ("buy", "gamble"):
                continue
            species = str((row or {}).get("species") or "?")
            opts.append(
                discord.SelectOption(
                    label=f"{species} · {_growth_pct(row):.0f}%"[:100],
                    value=sid,
                    description=f"{sid} · stored",
                )
            )
        if not opts:
            opts.append(discord.SelectOption(label="No stored dinos", value="none"))
        super().__init__(placeholder="Choose slot to unlock…", options=opts)

    async def callback(self, interaction):
        from primeval_panels import can_staff, deny_staff

        if interaction.user.id != self.owner_id or not can_staff(interaction.user, "skin_unlock"):
            await deny_staff(interaction, "skin_unlock")
            return

        slot = self.values[0]
        if slot == "none":
            await interaction.response.send_message("No vault slots.", ephemeral=True)
            return

        # The Isle confirmation wait can exceed Discord's 3s initial-response window.
        await interaction.response.defer(ephemeral=True)
        cmd_id = f"skinunlock-{self.steam[-6:]}-{int(time.time() * 1000)}"
        try:
            ok, msg = await _queue_skin_unlock(self.steam, slot, command_id=cmd_id)
        except Exception as e:
            ok, msg = False, str(e)
        if not ok:
            await interaction.followup.send(f"🛑 {msg}", ephemeral=True)
            return

        done, result = await _wait_skin_result(cmd_id, timeout=18.0)
        if done is False:
            await interaction.followup.send(f"🛑 {result or 'Isle rejected.'}", ephemeral=True)
            return
        if done is None:
            await interaction.followup.send("Submitted; confirmation timed out.", ephemeral=True)
            return

        await interaction.followup.send(
            f"✅ Unlocked `{slot}` for {self.member.mention}. Skin preserved; one repaint allowed.",
            ephemeral=True,
        )


class SkinAdminUnlockView(ui.View):
    def __init__(self, owner_id, member, steam, slots):
        super().__init__(timeout=180)
        self.add_item(SkinAdminUnlockSelect(owner_id, member, steam, slots))


@app_commands.command(name="skin", description="Open private guided skin painter")
async def skin_cmd(interaction: discord.Interaction):
    await open_skin(interaction)


@app_commands.command(name="skinunlock", description="Admin: unlock one stored skin for repainting")
@app_commands.describe(member="Player to unlock")
async def skinunlock_cmd(interaction: discord.Interaction, member: discord.Member):
    from primeval_panels import can_staff, deny_staff

    if not can_staff(interaction.user, "skin_unlock"):
        await deny_staff(interaction, "skin_unlock")
        return

    steam = _steam_for(member)
    if not steam:
        await interaction.response.send_message("Member has no linked Steam.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    slots = await _read_vault_slots(steam)
    view = SkinAdminUnlockView(interaction.user.id, member, steam, slots)
    await interaction.edit_original_response(
        content=f"Unlock a vault slot for {member.mention} (`{steam}`).",
        view=view,
    )


def register_views(bot):
    pass


def register_slash(bot):
    existing = {cmd.name for cmd in bot.tree.get_commands()}
    if "skin" not in existing:
        bot.tree.add_command(skin_cmd)
    if "skinunlock" not in existing:
        bot.tree.add_command(skinunlock_cmd)