"""Onboarding, lock waitlist, and vault/death recap for Primeval Island.

Wired from primeval_panels (buttons) and primeval_tally (census tick).
"""

from __future__ import annotations

import json
import os
import threading
import time

import discord
from discord import ui

G = {}

WAITLIST_PATH = "primeval_waitlist.json"
PROGRESS_PATH = "primeval_progress.json"
_WAIT_LOCK = threading.Lock()
_PROG_LOCK = threading.Lock()
CLAIM_SECONDS = 10 * 60
CHEAPEST_BUY = 2
RECAP_PATH = "/TheIsle/Binaries/Win64/ue4ss/Mods/PrimevalRedeem/Saved/recap"
RECAP_LOG = "/TheIsle/Binaries/Win64/ue4ss/Mods/PrimevalRedeem/Saved/recap.ndjson"
LOCKS_CHANNEL_ID = 1541014100971360326

HOW_TO_PLAY = (
    "**How Fallen Earth works — do these in order**\n\n"
    "1. **Link Steam** on this panel (blue 🔗). The rest will not work until you do. Linked members get **2 free tokens every 2 weeks**.\n"
    "2. **Spawn in-game** as the species you want (a fresh juvenile if you are redeeming).\n"
    "3. **Buy** a vaulted dino *or* **Store dino** then start a **safelog** and stay in it.\n"
    "4. **Redeem** as a matching juvenile. Bought dinos inject **70% growth**, **75% hunger**, **gender**, and **both mutations** in one write where you stand. Redeem **uses up** that vault slot — if you die, store again first or it is gone.\n\n"
    "**Waitlist** — if a capped species is LOCKED, join the queue. We ping you when a slot opens.\n"
    "**Pack VC** — spawn in-game, then join **Create pack VC** (or the panel button). We put you in a voice channel named for that dino. It deletes when everyone leaves.\n"
    "**Skin** — Discord **Skin** / `/skin` opens a private guided painter in your DMs (or a private fallback if DMs are blocked). Pick a habitat theme, then choose a realistic color for each body area step by step. **One paint per dino** — it locks even after you store again, and a stored dino that was already painted as a juvie is locked too. Live recode is a **fresh juvenile under 35%**. Unpainted stored dinos: finish the wizard, choose that vault slot, then redeem. In-game: `!skin forest` or `!skin body sand`.\n"
    "**Last recap** — receipt after store/safelog: saved or not, species, growth, Prime, what to do next.\n\n"
    "Stuck? Use **I'm stuck**. That opens a bot-issue ticket with these steps already filled in."
)


def bind(g):
    G.clear()
    G.update(g)


def _fn(name, default=None):
    return G.get(name, default)


def _load_wait():
    with _WAIT_LOCK:
        try:
            with open(WAITLIST_PATH, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                data.setdefault("queues", {})
                return data
        except Exception:
            pass
        return {"queues": {}}


def _load_progress():
    with _PROG_LOCK:
        try:
            with open(PROGRESS_PATH, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                data.setdefault("steams", {})
                if isinstance(data["steams"], dict):
                    return data
        except Exception:
            pass
        return {"steams": {}}


def _save_progress(state):
    with _PROG_LOCK:
        tmp = PROGRESS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2)
            handle.write("\n")
        os.replace(tmp, PROGRESS_PATH)


def mark_progress(steam, **flags):
    steam = str(steam or "").strip()
    if not steam:
        return
    now = int(time.time())
    state = _load_progress()
    row = state["steams"].setdefault(steam, {})
    if not isinstance(row, dict):
        row = {}
        state["steams"][steam] = row
    changed = False
    for key, value in flags.items():
        if not value or row.get(key):
            continue
        row[key] = now
        changed = True
    if changed:
        _save_progress(state)


def note_online(steams):
    now = int(time.time())
    state = _load_progress()
    changed = False
    for raw in steams or []:
        steam = str(raw or "").strip()
        if not steam:
            continue
        row = state["steams"].setdefault(steam, {})
        if not isinstance(row, dict):
            row = {}
            state["steams"][steam] = row
        if row.get("seen"):
            continue
        row["seen"] = now
        changed = True
    if changed:
        _save_progress(state)


def _progress_flags(steam):
    steam = str(steam or "").strip()
    if not steam:
        return {}
    row = (_load_progress().get("steams") or {}).get(steam) or {}
    return row if isinstance(row, dict) else {}


def _opening_seen(steam):
    try:
        import primeval_opening

        return bool(primeval_opening.was_seen(steam))
    except Exception:
        return False


def _token_balance(steam):
    get_bal = _fn("get_steam_token_balance")
    if not get_bal or not steam:
        return 0
    try:
        return int(get_bal(steam) or 0)
    except Exception:
        return 0


def _slot_source(slot):
    return str((slot or {}).get("source") or (slot or {}).get("kind") or "").lower()


def _slot_species(slot):
    return str((slot or {}).get("species") or (slot or {}).get("classPath") or "dino")


def _check_line(done, label, detail=""):
    mark = "✅" if done else "⬜"
    if detail:
        return f"{mark} **{label}** — {detail}"
    return f"{mark} **{label}**"


def _save_wait(state):
    with _WAIT_LOCK:
        tmp = WAITLIST_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2)
            handle.write("\n")
        os.replace(tmp, WAITLIST_PATH)


def capped_species():
    caps = _fn("SPECIES_CAPS") or G.get("SPECIES_CAPS") or {}
    names = []
    for name, cap in caps.items():
        if int(cap or 0) > 0:
            names.append(str(name))
    return names


def _wait_options():
    names = capped_species()
    if not names:
        return [discord.SelectOption(label="No capped species yet", value="none")]
    return [
        discord.SelectOption(
            label=name[:100],
            value=name[:100],
            description="Ping me when this lock opens",
        )
        for name in names[:25]
    ]


def _remove_user(state, user_id):
    uid = str(user_id)
    removed = []
    queues = state.setdefault("queues", {})
    for species, rows in list(queues.items()):
        keep = []
        for row in rows:
            if str(row.get("user_id")) == uid:
                removed.append(species)
            else:
                keep.append(row)
        queues[species] = keep
    return removed


def join_waitlist(user_id, species):
    species = str(species or "").strip()
    if not species or species == "none":
        return False, "Pick a capped species."
    caps = _fn("SPECIES_CAPS") or {}
    if species not in caps:
        return False, f"{species} is not a locked/capped species."
    state = _load_wait()
    _remove_user(state, user_id)
    rows = state.setdefault("queues", {}).setdefault(species, [])
    rows.append({
        "user_id": str(user_id),
        "joined_at": int(time.time()),
        "claim_until": 0,
        "notified_at": 0,
    })
    _save_wait(state)
    return True, f"You're **#{len(rows)}** for **{species}**. We'll ping you in the locks channel when a slot opens. One species at a time — joining another replaces this."


def leave_waitlist(user_id):
    state = _load_wait()
    removed = _remove_user(state, user_id)
    _save_wait(state)
    if not removed:
        return "You were not on a waitlist."
    return "Left the waitlist for **" + ", ".join(removed) + "**."


def waitlist_status(user_id):
    uid = str(user_id)
    state = _load_wait()
    bits = []
    for species, rows in (state.get("queues") or {}).items():
        for i, row in enumerate(rows, start=1):
            if str(row.get("user_id")) == uid:
                claim = int(row.get("claim_until") or 0)
                extra = f" — your open window ends <t:{claim}:R>" if claim > time.time() else ""
                bits.append(f"**{species}** — #{i} of {len(rows)}{extra}")
    return "\n".join(bits) if bits else "You are not on a waitlist."


async def process_waitlist(bot, counts):
    caps = _fn("SPECIES_CAPS") or {}
    if not caps:
        return
    now = int(time.time())
    state = _load_wait()
    queues = state.setdefault("queues", {})
    changed = False
    channel = bot.get_channel(LOCKS_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(LOCKS_CHANNEL_ID)
        except Exception:
            channel = None

    for species, cap in caps.items():
        cap = int(cap or 0)
        n = int((counts or {}).get(species, 0) or 0)
        rows = queues.get(species) or []
        if not rows:
            continue
        if n >= cap:
            for row in rows:
                if int(row.get("claim_until") or 0):
                    row["claim_until"] = 0
                    changed = True
            continue
        head = rows[0]
        claim_until = int(head.get("claim_until") or 0)
        if claim_until and now < claim_until:
            continue
        if claim_until and now >= claim_until and len(rows) > 1:
            rows.append(rows.pop(0))
            rows[-1]["claim_until"] = 0
            head = rows[0]
            changed = True
        head["claim_until"] = now + CLAIM_SECONDS
        head["notified_at"] = now
        changed = True
        if channel is None:
            continue
        mention = f"<@{head['user_id']}>"
        try:
            await channel.send(
                f"{mention} **{species}** is OPEN `{n}/{cap}`.\n"
                f"You are first on the waitlist — you have **10 minutes** to spawn one.\n"
                f"If the window ends, you rotate to the back. Leave anytime with **Waitlist** on the member panel.",
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        except Exception as exc:
            print(f"[QOL] waitlist ping failed: {exc}")
    if changed:
        _save_wait(state)


def locks_wait_view():
    return LocksWaitView()


def recap_embed(data, steam):
    status = str((data or {}).get("status") or "unknown")
    species = data.get("species") or "?"
    gender = data.get("gender") or ""
    growth = float(data.get("growth") or 0) * 100
    have = int(data.get("primeHave") or 0)
    nxt = data.get("next") or ""
    detail = data.get("detail") or ""
    captured = int(data.get("capturedAt") or 0)
    titles = {
        "saved": "Vault saved",
        "failed": "Vault not saved",
        "cancelled": "Store cancelled",
        "armed_lost": "Store armed — not saved",
    }
    colors = {
        "saved": discord.Color.green(),
        "failed": discord.Color.red(),
        "cancelled": discord.Color.orange(),
        "armed_lost": discord.Color.orange(),
    }
    bit = f"**{species}**"
    if gender:
        bit += f" · {gender}"
    bit += f" · **{growth:.0f}%** · Prime **{have}/10**"
    desc = f"{bit}\n"
    if detail:
        desc += f"{detail}\n"
    if nxt:
        desc += f"\n**Next:** {nxt}"
    if captured:
        desc += f"\n<t:{captured}:f> (<t:{captured}:R>)"
    embed = discord.Embed(
        title="🧾 " + titles.get(status, "Last recap"),
        description=desc,
        color=colors.get(status, discord.Color.greyple()),
    )
    embed.set_footer(text=f"Steam {steam}")
    return embed


async def _read_recap(steam):
    from primeval_panels import _isle_file

    status, text = await _isle_file("GET", RECAP_PATH + "/" + str(steam) + ".json")
    if status == 200 and text and str(text).strip().startswith("{"):
        try:
            return json.loads(text)
        except Exception:
            return None
    return None


def _discord_ids_for_steam(steam):
    steam = str(steam or "")
    ids = []
    links = G.get("STEAM_LINKS") or {}
    if isinstance(links, dict):
        for key, value in links.items():
            sid = value.get("steam") if isinstance(value, dict) else value
            if str(sid) == steam and str(key).isdigit():
                ids.append(int(key))
    return ids


async def process_recaps(bot, tally_state):
    from primeval_panels import _isle_file

    status, text = await _isle_file("GET", RECAP_LOG)
    if status != 200 or not text:
        return tally_state
    seen = int(tally_state.get("recap_log_bytes") or 0)
    raw = str(text)
    if len(raw) < seen:
        seen = 0
    chunk = raw[seen:]
    tally_state["recap_log_bytes"] = len(raw)
    for line in chunk.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        steam = str(row.get("steam") or "")
        if not steam:
            continue
        for uid in _discord_ids_for_steam(steam):
            try:
                user = bot.get_user(uid) or await bot.fetch_user(uid)
                await user.send(embed=recap_embed(row, steam))
            except Exception:
                pass
    return tally_state


async def on_census(bot, counts, tally_state):
    try:
        await process_waitlist(bot, counts)
    except Exception as exc:
        print(f"[QOL] waitlist failed: {exc}")
    try:
        return await process_recaps(bot, tally_state)
    except Exception as exc:
        print(f"[QOL] recap poll failed: {exc}")
        return tally_state


async def show_how_to(interaction: discord.Interaction):
    extra = ""
    try:
        import primeval_opening

        extra = primeval_opening.how_to_line()
    except Exception:
        extra = ""
    try:
        import primeval_events

        extra += primeval_events.how_to_line()
    except Exception:
        pass
    embed = discord.Embed(
        title="🦕 How to play Fallen Earth",
        description=HOW_TO_PLAY + extra,
        color=discord.Color.green(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


async def _build_progress_embed(user):
    from primeval_panels import _read_vault_slots

    get_steam = _fn("get_linked_steam_id")
    steam = ""
    try:
        steam = str(get_steam(user.id) or "").strip() if get_steam else ""
    except Exception:
        steam = ""
    flags = _progress_flags(steam)
    slots = []
    recap = None
    if steam:
        try:
            slots = await _read_vault_slots(steam) or []
        except Exception:
            slots = []
        try:
            recap = await _read_recap(steam)
        except Exception:
            recap = None
    recap_status = str((recap or {}).get("status") or "").lower()
    recap_saved = recap_status == "saved"
    buy_slots = [s for s in slots if _slot_source(s) == "buy"]
    store_slots = [s for s in slots if _slot_source(s) != "buy"]
    has_vault = bool(slots)
    seen = bool(
        flags.get("seen")
        or _opening_seen(steam)
        or has_vault
        or recap
    )
    stored = bool(flags.get("stored") or has_vault or recap_saved)
    redeemed = bool(flags.get("redeemed"))
    if recap_saved and not has_vault:
        redeemed = True
    if steam:
        mark_progress(
            steam,
            seen=seen,
            stored=stored,
            redeemed=redeemed,
        )
    tokens = _token_balance(steam) if steam else 0
    first_buy = bool(steam and tokens >= CHEAPEST_BUY and not has_vault and not stored and not redeemed)
    if not steam:
        nxt = "Use **Link Steam** on this panel. Nothing else works until you do."
        color = discord.Color.blurple()
    elif first_buy:
        nxt = (
            f"You have **{tokens}** tokens and an empty vault. "
            "Use **Buy** on this panel — T1–T2 dinos cost **2** tokens."
        )
        color = discord.Color.gold()
    elif not seen:
        nxt = "Join the Isle and spawn. This box flips once the server sees you online."
        color = discord.Color.blurple()
    elif not stored:
        if tokens >= CHEAPEST_BUY:
            nxt = (
                f"You have **{tokens}** tokens. **Buy** a vaulted dino on this panel, "
                "or grow in-game and **Store dino** then safelog."
            )
            color = discord.Color.gold()
        elif tokens > 0:
            nxt = (
                f"You have **{tokens}** token{'s' if tokens != 1 else ''} — cheapest buy is **{CHEAPEST_BUY}**. "
                "Earn more, then **Buy**, or grow in-game and **Store dino**."
            )
            color = discord.Color.blurple()
        else:
            nxt = "Earn tokens (weekly link drop or the token event), then **Buy**, or grow in-game and **Store dino**."
            color = discord.Color.blurple()
    elif has_vault and buy_slots:
        species = _slot_species(buy_slots[-1])
        nxt = f"Spawn as **{species}**, then **Redeem** and pick that bought slot. It applies where you stand."
        color = discord.Color.gold()
    elif has_vault:
        species = _slot_species((store_slots or slots)[-1])
        nxt = f"Spawn a fresh **{species}** juvenile, then **Redeem** and pick that stored slot."
        color = discord.Color.gold()
    elif not redeemed:
        nxt = "Spawn a matching juvenile, then **Redeem**. If the vault already applied, click this again."
        color = discord.Color.blurple()
    else:
        nxt = "First session is done. Grow, **Store dino** before you die, or **Buy** another."
        color = discord.Color.green()
    steam_detail = f"`{steam}`" if steam else "not linked"
    seen_detail = "server saw you online" if seen else "not seen yet"
    if has_vault:
        stored_detail = f"{len(slots)} vault slot{'s' if len(slots) != 1 else ''}"
    elif recap_saved:
        stored_detail = "recap says saved"
    elif stored:
        stored_detail = "saved before"
    else:
        stored_detail = "nothing vaulted yet"
    redeemed_detail = "at least once" if redeemed else "not yet"
    lines = [
        _check_line(bool(steam), "Link Steam", steam_detail),
        _check_line(seen, "Join the Isle", seen_detail),
        _check_line(stored, "Store or buy a dino", stored_detail),
        _check_line(redeemed, "Redeem once", redeemed_detail),
    ]
    embed = discord.Embed(
        title="📋 My progress",
        description="\n".join(lines) + f"\n\n**Next:** {nxt}",
        color=color,
    )
    if steam:
        embed.set_footer(text=f"{tokens} tokens")
    return embed


async def show_progress(interaction: discord.Interaction):
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)
    embed = await _build_progress_embed(interaction.user)
    await interaction.followup.send(embed=embed, ephemeral=True)


async def show_waitlist(interaction: discord.Interaction):
    status = waitlist_status(interaction.user.id)
    await interaction.response.send_message(
        status + "\nPick a capped species to join (replaces any current queue).",
        view=LocksWaitView(),
        ephemeral=True,
    )


async def show_recap(interaction: discord.Interaction):
    from primeval_panels import _need_steam, _read_vault_slots

    steam = await _need_steam(interaction)
    if not steam:
        return
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)
    data = await _read_recap(steam)
    if data:
        await interaction.followup.send(embed=recap_embed(data, steam), ephemeral=True)
        return
    slots = await _read_vault_slots(steam)
    if not slots:
        await interaction.followup.send(
            "No recap yet. Store a dino (then safelog) or buy one — the receipt shows up here.",
            ephemeral=True,
        )
        return
    row = slots[-1]
    fake = {
        "status": "saved",
        "species": row.get("species"),
        "gender": row.get("gender"),
        "growth": row.get("growth"),
        "primeHave": row.get("primeHave") or 0,
        "detail": "No live recap file — this is your last vault slot.",
        "next": "Spawn a matching juvenile, then Redeem.",
        "capturedAt": row.get("capturedAt") or 0,
    }
    await interaction.followup.send(embed=recap_embed(fake, steam), ephemeral=True)


class StuckModal(ui.Modal, title="I'm stuck"):
    note = ui.TextInput(
        label="Where are you stuck?",
        style=discord.TextStyle.paragraph,
        max_length=400,
        placeholder="Example: I stored, safelogged, but redeem says wrong species.",
    )

    async def on_submit(self, interaction: discord.Interaction):
        import primeval_tickets

        await interaction.response.defer(ephemeral=True)
        details = (
            "Player is stuck on the Fallen Earth onboarding flow.\n\n"
            "Expected order:\n"
            "1. Link Steam\n"
            "2. Spawn as the species they want\n"
            "3. Buy a vaulted dino, or Store then safelog\n"
            "4. Redeem as a matching juvenile\n\n"
            f"Player note:\n{self.note.value}"
        )
        await primeval_tickets.open_ticket(interaction, primeval_tickets.KIND_BOT, details)


async def start_stuck(interaction: discord.Interaction):
    await interaction.response.send_modal(StuckModal())


class WaitSpeciesSelect(ui.Select):
    def __init__(self, custom_id="pi_lock_wait_sel"):
        super().__init__(
            placeholder="Join lock waitlist…",
            min_values=1,
            max_values=1,
            options=_wait_options(),
            custom_id=custom_id,
        )

    async def callback(self, interaction: discord.Interaction):
        ok, msg = join_waitlist(interaction.user.id, self.values[0])
        await interaction.response.send_message(msg, ephemeral=True)


class LocksWaitView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(WaitSpeciesSelect())

    @ui.button(label="Leave waitlist", emoji="🚪", style=discord.ButtonStyle.secondary, custom_id="pi_lock_wait_leave", row=1)
    async def leave(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message(leave_waitlist(interaction.user.id), ephemeral=True)


def register_views(bot):
    bot.add_view(LocksWaitView())
