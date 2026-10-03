"""Persistent member + staff Discord panels for Primeval Island.

Call bind(globals()) from Primeval_Island_Bot.py after helpers exist, then
register views in on_ready and the /panel + /staff_panel slash commands.
"""

from __future__ import annotations

import asyncio
import json
import time

import discord
from discord import ui

from primeval_species import (
    aliases as species_aliases,
    emoji as species_emoji,
    mutation_allowed,
    mutation_choices,
    mutation_fname,
    pack_mutations,
    playable as playable_species,
    small_prime as small_prime_species,
)

G = {}


def bind(g):
    G.clear()
    G.update(g)
    try:
        import primeval_isle

        primeval_isle.bind(g)
    except Exception:
        pass
    try:
        import primeval_qol

        primeval_qol.bind(g)
    except Exception:
        pass


def _fn(name, default=None):
    return G.get(name, default)


ISLE_SAVED = "/TheIsle/Binaries/Win64/ue4ss/Mods/PrimevalRedeem/Saved"
ISLE_VAULT_DIR = ISLE_SAVED + "/vault"
ISLE_STORED_DIR = ISLE_SAVED + "/stored"


async def _isle_file(method, path, body=None):
    import primeval_isle

    if str(method or "GET").upper() == "GET":
        return await primeval_isle.read_file(path)
    return await primeval_isle.write_file(path, body or "")


async def _queue_cmd_flag(verb, steam, extra=""):
    import primeval_isle

    if not primeval_isle.G:
        primeval_isle.bind(G)
    return await primeval_isle.queue_verb(verb, steam, extra)


async def _queue_inbox(payload):
    import primeval_isle

    if not primeval_isle.G:
        primeval_isle.bind(G)
    return await primeval_isle.queue_inbox(payload)


async def _audit_game_cmd(interaction, command, target="", extra="", result=""):
    try:
        import primeval_admin_audit

        await primeval_admin_audit.note_discord(
            interaction.client,
            interaction.user,
            command,
            target,
            extra,
            result,
        )
    except Exception as exc:
        print(f"[AUDIT] staff note failed: {exc}")


async def _vault_audit(interaction, verb, steam="", ok=True, **fields):
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
        print(f"[VAULT AUDIT] note failed: {exc}")


async def _read_vault_json(steam):
    reader = _fn("read_isle_vault_json")
    if reader:
        return await reader(steam)
    for folder in (ISLE_VAULT_DIR, ISLE_STORED_DIR):
        status, text = await _isle_file("GET", folder + "/" + str(steam) + ".json")
        if status == 200 and text and text.strip().startswith("{"):
            return text
    return ""


def _parse_index_lines(raw):
    by_id = {}
    order = []
    for line in str(raw or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        sid = str(row.get("id") or "").strip()
        if not sid:
            continue
        if sid not in by_id:
            order.append(sid)
        by_id[sid] = row
    return [by_id[sid] for sid in order]


async def _read_vault_slots(steam):
    status, raw = await _isle_file("GET", ISLE_VAULT_DIR + "/" + str(steam) + ".index.ndjson")
    slots = _parse_index_lines(raw if status == 200 else "")
    if slots:
        return slots
    single = await _read_vault_json(steam)
    if not single:
        return []
    try:
        data = json.loads(single)
    except Exception:
        return []
    data.setdefault("id", "vault")
    data.setdefault("source", data.get("kind") or "store")
    return [data]


async def _write_vault_purchase(steam, species, female, mutations=""):
    slot_id = f"buy-{str(steam)[-6:]}-{int(time.time())}"
    gender = "Female" if female else "Male"
    payload = {
        "version": 6,
        "kind": "vault",
        "id": slot_id,
        "source": "buy",
        "stayPut": True,
        "steam": str(steam),
        "classPath": species,
        "species": species,
        "growth": 0.70,
        "health": None,
        "maxHealth": None,
        "hunger": 0.75,
        "thirst": None,
        "stamina": None,
        "oxygen": None,
        "gender": gender,
        "female": bool(female),
        "genderNum": 1 if female else 0,
        "skin": "",
        "skinId": None,
        "primeElder": True,
        "primeFlags": "1111111111",
        "primeHave": 10,
        "mutations": str(mutations or ""),
        "unlocks": "",
        "elderStacks": 0,
        "x": None,
        "y": None,
        "z": None,
        "pitch": None,
        "yaw": None,
        "roll": None,
        "capturedAt": int(time.time()),
    }
    body = json.dumps(payload, separators=(",", ":")) + "\n"
    slot_path = ISLE_VAULT_DIR + "/" + str(steam) + "_" + slot_id + ".json"
    status, text = await _isle_file("POST", slot_path, body)
    if status not in (200, 204):
        return False, text or f"write {status}", slot_id
    await _isle_file("POST", ISLE_VAULT_DIR + "/" + str(steam) + ".json", body)
    await _isle_file("POST", ISLE_STORED_DIR + "/" + str(steam) + ".json", body)
    idx_path = ISLE_VAULT_DIR + "/" + str(steam) + ".index.ndjson"
    idx_status, existing = await _isle_file("GET", idx_path)
    if idx_status not in (200, 204):
        existing = ""
    index_line = json.dumps({
        "id": slot_id,
        "species": species,
        "gender": gender,
        "female": bool(female),
        "growth": 0.70,
        "source": "buy",
        "capturedAt": int(time.time()),
    }, separators=(",", ":")) + "\n"
    status, text = await _isle_file("POST", idx_path, (existing or "") + index_line)
    if status not in (200, 204):
        return False, text or f"index {status}", slot_id
    return True, "queued", slot_id


def _resolve_species_name(raw):
    name = str(raw or "").strip()
    if not name:
        return ""
    key = name.lower()
    als = species_aliases()
    if key in als:
        return str(als[key])
    for playable in playable_species():
        if str(playable).lower() == key:
            return str(playable)
    return ""


def _parse_growth_pct(raw):
    text = str(raw or "").strip().lower().replace("%", "")
    if not text:
        return None
    try:
        value = float(text)
    except Exception:
        return None
    if value > 1.0:
        value = value / 100.0
    if value < 0.01 or value > 1.0:
        return None
    return value


def _parse_gender_field(raw):
    text = str(raw or "").strip().lower()
    if not text or text in ("any", "keep", "none", "-"):
        return None, ""
    if text in ("f", "female", "girl"):
        return True, "Female"
    if text in ("m", "male", "boy"):
        return False, "Male"
    return False, ""


async def _write_vault_return_store(
    steam,
    species,
    growth,
    female=None,
    mutations="",
    prime_flags="1111111111",
    prime_have=10,
):
    """Staff recovery: put a stay-put store slot back in the player's vault."""
    slot_id = f"recover-{str(steam)[-6:]}-{int(time.time())}"
    gender = ""
    female_val = None
    gender_num = None
    if female is True:
        gender, female_val, gender_num = "Female", True, 1
    elif female is False:
        gender, female_val, gender_num = "Male", False, 0
    growth = float(growth)
    mut = str(mutations or "").strip()
    flags = str(prime_flags or "1111111111").strip() or "1111111111"
    try:
        have = int(prime_have)
    except Exception:
        have = 10
    payload = {
        "version": 6,
        "kind": "vault",
        "id": slot_id,
        "source": "store",
        "stayPut": True,
        "steam": str(steam),
        "classPath": species,
        "species": species,
        "growth": growth,
        "health": None,
        "maxHealth": None,
        "hunger": 0.75,
        "thirst": None,
        "stamina": None,
        "oxygen": None,
        "gender": gender,
        "female": female_val,
        "genderNum": gender_num,
        "skin": "",
        "skinId": None,
        "primeElder": have >= 10,
        "primeFlags": flags,
        "primeHave": have,
        "mutations": mut,
        "unlocks": "",
        "elderStacks": 0,
        "x": None,
        "y": None,
        "z": None,
        "pitch": None,
        "yaw": None,
        "roll": None,
        "capturedAt": int(time.time()),
    }
    body = json.dumps(payload, separators=(",", ":")) + "\n"
    slot_path = ISLE_VAULT_DIR + "/" + str(steam) + "_" + slot_id + ".json"
    status, text = await _isle_file("POST", slot_path, body)
    if status not in (200, 204):
        return False, text or f"write {status}", slot_id
    await _isle_file("POST", ISLE_VAULT_DIR + "/" + str(steam) + ".json", body)
    await _isle_file("POST", ISLE_STORED_DIR + "/" + str(steam) + ".json", body)
    idx_path = ISLE_VAULT_DIR + "/" + str(steam) + ".index.ndjson"
    idx_status, existing = await _isle_file("GET", idx_path)
    if idx_status not in (200, 204):
        existing = ""
    index_line = json.dumps({
        "id": slot_id,
        "species": species,
        "gender": gender,
        "female": female_val,
        "growth": growth,
        "source": "store",
        "mutations": mut,
        "capturedAt": int(time.time()),
    }, separators=(",", ":")) + "\n"
    status, text = await _isle_file("POST", idx_path, (existing or "") + index_line)
    if status not in (200, 204):
        return False, text or f"index {status}", slot_id
    return True, "queued", slot_id


def _normalize_return_mutations(raw):
    text = str(raw or "").strip()
    if not text:
        return ""
    if "MutationSlot" in text or "=" in text:
        return text.replace("\n", "|")
    parts = [p.strip() for p in text.replace("|", "/").split("/") if p.strip()]
    if not parts:
        return ""
    if len(parts) == 1:
        return pack_mutations(parts[0], "")
    return pack_mutations(parts[0], parts[1])


# Exact Discord role names (lowercase) -> staff rank. Highest number wins.
STAFF_ROLE_RANKS = {
    "helper": 1,
    "moderator": 2,
    "administrator": 4,
    "senior administrator": 4,
    "director": 5,
}

STAFF_ACTION_RANKS = {
    "players": 1,
    "headcount": 2,
    "broadcast": 2,
    "bot_post": 2,
    "kick": 2,
    "slay": 3,
    "revive": 3,
    "return_store": 3,
    "setgroup": 3,
    "ban": 3,
    "post_member_panel": 1,
    "post_staff_panel": 3,
    "post_events_panel": 2,
    "post_trade_panel": 2,
    "gamble_test": 4,
    "tokens": 5,
    "token_event": 5,
    "growth_window": 4,
    "hunt_night": 2,
    "rcon": 5,
    "disboard": 2,
    "free_giveaway": 4,
    "free_giveaway_uncapped": 5,
    "free_giveaway_run": 5,
    "relaunch_isle": 3,
    "restart_isle": 5,
    "skin_unlock": 4,
}

SPECIES_EMOJI = species_emoji()


def _species_emoji(name):
    mark = SPECIES_EMOJI.get(name)
    if mark and len(mark) <= 2:
        return mark
    return "🦕"


def staff_rank(member):
    if member is None:
        return 0
    rank = 0
    perms = getattr(member, "guild_permissions", None)
    if perms:
        if perms.administrator:
            rank = max(rank, 5)
        if perms.manage_guild:
            rank = max(rank, 4)
        if perms.ban_members:
            rank = max(rank, 3)
        if perms.kick_members or getattr(perms, "moderate_members", False):
            rank = max(rank, 2)
        if perms.manage_messages:
            rank = max(rank, 1)
    roles = getattr(member, "roles", None) or []
    for role in roles:
        name = str(getattr(role, "name", "")).strip().lower()
        if name in STAFF_ROLE_RANKS:
            rank = max(rank, STAFF_ROLE_RANKS[name])
    return rank


def can_staff(member, action):
    need = STAFF_ACTION_RANKS.get(action, 5)
    return staff_rank(member) >= need


_RANK_NAMES = {
    1: "Helper",
    2: "Moderator",
    3: "Administrator",
    4: "Administrator / Senior Administrator",
    5: "Director",
}


async def deny_staff(interaction, action):
    need = STAFF_ACTION_RANKS.get(action, 5)
    have = staff_rank(interaction.user)
    need_name = _RANK_NAMES.get(need, f"rank {need}")
    have_name = _RANK_NAMES.get(have, "no staff role")
    await interaction.response.send_message(
        f"**{have_name}** cannot use **{action}**. Needs **{need_name}** or higher.",
        ephemeral=True,
    )


async def member_balance_text(user):
    get_steam = _fn("get_linked_steam_id")
    get_bal = _fn("get_steam_token_balance")
    steam = get_steam(user.id) if get_steam else None
    if not steam:
        return "Link Steam first with the 🔗 button."
    bal = get_bal(steam) if get_bal else 0
    return f"Steam `{steam}` — **{bal}** tokens"


BUY_TIER_PRICES = {
    1: 2,
    2: 2,
    3: 3,
    4: 5,
    5: 8,
    6: 12,
}


def buy_cost(species):
    tiers = _fn("DINO_TIERS") or {}
    try:
        tier = int(tiers.get(species, 1) or 1)
    except Exception:
        tier = 1
    return int(BUY_TIER_PRICES.get(tier, 2))


def _species_names():
    return [dino for dino, _tier in _shop_species()]


def _shop_species():
    """Buy-menu rows: lowest tier first, then name. Discord select max is 25."""
    tiers = _fn("DINO_TIERS") or {}
    ranked = []
    for dino, raw in tiers.items():
        try:
            tier = int(raw or 1)
        except Exception:
            tier = 1
        ranked.append((tier, str(dino).lower(), dino))
    ranked.sort()
    return [(dino, tier) for tier, _key, dino in ranked[:25]]


def _sold_out(dino):
    caps = _fn("SPECIES_CAPS") or {}
    live = _fn("LIVE_HEADCOUNT") or {}
    locked = _fn("PLAYABLE_LOCKED") or {}
    if locked.get(dino):
        return True
    if dino in caps and live.get(dino, 0) >= caps[dino]:
        return True
    return False


def buy_options():
    options = []
    for dino, tier in _shop_species():
        if _sold_out(dino):
            options.append(
                discord.SelectOption(
                    label=f"[SOLD OUT] {dino}",
                    description=f"Tier {tier} · map cap reached",
                    value=f"LOCKED_{dino}",
                )
            )
            continue
        options.append(
            discord.SelectOption(
                label=f"{dino} (Tier {tier})",
                description=f"{buy_cost(dino)} tokens · 70% inject · gender + 2 mutations · 75% hunger",
                value=dino,
            )
        )
    if not options:
        options.append(discord.SelectOption(label="Shop unavailable", value="none"))
    return options


def redeem_options():
    return [
        discord.SelectOption(
            label="Redeem a stored dino…",
            description="Spawn as that species first, then pick a vault slot",
            value="pick",
        ),
        discord.SelectOption(
            label="Redeem last vault",
            description="Uses the most recently stored or bought dino",
            value="vault",
        ),
    ]


class LinkSteamModal(ui.Modal, title="Link SteamID64"):
    steam_id = ui.TextInput(label="17-digit SteamID64", min_length=17, max_length=17, placeholder="7656119...")

    async def on_submit(self, interaction: discord.Interaction):
        clean = str(self.steam_id.value or "").strip()
        save = _fn("save_steam_links")
        steam_links = G.get("STEAM_LINKS")
        if len(clean) != 17 or not clean.isdigit():
            await interaction.response.send_message("That is not a 17-digit SteamID64.", ephemeral=True)
            return
        if steam_links is not None:
            steam_links[interaction.user.id] = clean
            steam_links[str(interaction.user.id)] = clean
        if save:
            try:
                save()
            except Exception as exc:
                print(f"[PANELS] save_steam_links failed: {exc}")
                await interaction.response.send_message(
                    f"Could not save Steam `{clean}` — try again or ask staff.",
                    ephemeral=True,
                )
                return
        await interaction.response.send_message(f"Linked Steam `{clean}`.", ephemeral=True)
        role_msg = ""
        try:
            import primeval_gate

            await primeval_gate.on_steam_linked(interaction)
            guild = interaction.guild
            member = interaction.user
            if guild is not None:
                try:
                    member = await guild.fetch_member(interaction.user.id)
                except Exception:
                    member = guild.get_member(interaction.user.id) or member
                linked_role = primeval_gate._role_named(guild, primeval_gate.LINKED_ROLE_NAME)
                if linked_role is None:
                    role_msg = (
                        f" Steam saved, but the **{primeval_gate.LINKED_ROLE_NAME}** role is missing — "
                        "staff should run `/setup_link_gate`."
                    )
                elif linked_role in getattr(member, "roles", []):
                    role_msg = f" **{primeval_gate.LINKED_ROLE_NAME}** role is on — you can chat and join voice."
                else:
                    role_msg = (
                        f" Steam saved, but **{primeval_gate.LINKED_ROLE_NAME}** was not granted. "
                        "Staff: put the bot role above Linked and re-check `/sync_gate_roles`."
                    )
        except Exception as exc:
            print(f"[PANELS] on_steam_linked failed: {exc}")
            role_msg = f" Steam saved, but Linked role step failed: `{exc}`"
        if role_msg:
            try:
                await interaction.followup.send(role_msg.strip(), ephemeral=True)
            except Exception as exc:
                print(f"[PANELS] link followup failed: {exc}")


class BroadcastModal(ui.Modal, title="Server broadcast"):
    message = ui.TextInput(label="Message", style=discord.TextStyle.paragraph, max_length=200)

    async def on_submit(self, interaction: discord.Interaction):
        if not can_staff(interaction.user, "broadcast"):
            await deny_staff(interaction, "broadcast")
            return
        await interaction.response.defer(ephemeral=True)
        send = _fn("send_game_command")
        result = await send("announce " + str(self.message.value)) if send else "no rcon"
        await _audit_game_cmd(interaction, "announce", extra=str(self.message.value), result=result)
        await interaction.followup.send(f"Broadcast sent. `{result}`", ephemeral=True)


class KickModal(ui.Modal, title="Kick player"):
    steam_id = ui.TextInput(label="SteamID64 or in-game name", max_length=32)
    reason = ui.TextInput(label="Reason", max_length=100, default="Kicked by staff")

    async def on_submit(self, interaction: discord.Interaction):
        if not can_staff(interaction.user, "kick"):
            await deny_staff(interaction, "kick")
            return
        await interaction.response.defer(ephemeral=True)
        send = _fn("send_game_command")
        target = str(self.steam_id.value).strip()
        reason = str(self.reason.value).strip()
        result = await send(f"kick {target} {reason}") if send else "no rcon"
        await _audit_game_cmd(interaction, "kick", target, reason, result)
        await interaction.followup.send(f"Kick `{target}`: `{result}`", ephemeral=True)


class BanModal(ui.Modal, title="Ban player"):
    steam_id = ui.TextInput(label="SteamID64", max_length=32)
    duration = ui.TextInput(label="Duration (0 / 1h / 1d / perm)", default="perm", max_length=12)
    reason = ui.TextInput(label="Reason", max_length=100, default="Banned by staff")

    async def on_submit(self, interaction: discord.Interaction):
        if not can_staff(interaction.user, "ban"):
            await deny_staff(interaction, "ban")
            return
        await interaction.response.defer(ephemeral=True)
        send = _fn("send_game_command")
        target = str(self.steam_id.value).strip()
        result = await send(
            f"ban {target} {str(self.duration.value).strip()} {str(self.reason.value).strip()}"
        ) if send else "no rcon"
        await _audit_game_cmd(
            interaction,
            "ban",
            target,
            f"{str(self.duration.value).strip()} {str(self.reason.value).strip()}",
            result,
        )
        await interaction.followup.send(f"Ban `{target}`: `{result}`", ephemeral=True)


class TargetModal(ui.Modal):
    steam_id = ui.TextInput(label="SteamID64", max_length=32)

    def __init__(self, action, title, command):
        super().__init__(title=title)
        self._action = action
        self._command = command

    async def on_submit(self, interaction: discord.Interaction):
        if not can_staff(interaction.user, self._action):
            await deny_staff(interaction, self._action)
            return
        await interaction.response.defer(ephemeral=True)
        send = _fn("send_game_command")
        target = str(self.steam_id.value).strip()
        result = await send(f"{self._command} {target}") if send else "no rcon"
        await _audit_game_cmd(interaction, self._command, target, result=result)
        await interaction.followup.send(f"{self._command} `{target}`: `{result}`", ephemeral=True)


class TokenModal(ui.Modal, title="Adjust tokens"):
    discord_id = ui.TextInput(label="Player Discord ID", max_length=20)
    amount = ui.TextInput(label="Amount (number)", max_length=8)
    mode = ui.TextInput(label="Mode: add / remove / set", default="add", max_length=8)

    async def on_submit(self, interaction: discord.Interaction):
        if not can_staff(interaction.user, "tokens"):
            await deny_staff(interaction, "tokens")
            return
        get_steam = _fn("get_linked_steam_id")
        get_bal = _fn("get_steam_token_balance")
        set_bal = _fn("set_steam_token_balance")
        try:
            uid = int(str(self.discord_id.value).strip())
            amt = int(str(self.amount.value).strip())
        except Exception:
            await interaction.response.send_message("Discord ID and amount must be numbers.", ephemeral=True)
            return
        mode = str(self.mode.value or "add").strip().lower()
        steam = get_steam(uid) if get_steam else None
        if not steam:
            await interaction.response.send_message("That user has not linked Steam.", ephemeral=True)
            return
        cur = get_bal(steam) if get_bal else 0
        if mode == "set":
            new = amt
        elif mode == "remove":
            new = cur - amt
        else:
            new = cur + amt
        if set_bal:
            set_bal(steam, uid, new)
        await interaction.response.send_message(
            f"Tokens for `{steam}`: {cur} → **{new}** ({mode}).", ephemeral=True
        )


class SetGroupModal(ui.Modal, title="Set in-game group"):
    steam_id = ui.TextInput(label="SteamID64", max_length=32)
    group = ui.TextInput(label="Group name", max_length=32, default="admin")

    async def on_submit(self, interaction: discord.Interaction):
        if not can_staff(interaction.user, "setgroup"):
            await deny_staff(interaction, "setgroup")
            return
        await interaction.response.defer(ephemeral=True)
        send = _fn("send_game_command")
        target = str(self.steam_id.value).strip()
        group = str(self.group.value).strip()
        result = await send(f"addadmin {target} {group}") if send else "no rcon"
        await _audit_game_cmd(interaction, "addadmin", target, group, result)
        await interaction.followup.send(f"Setgroup `{target}` `{group}`: `{result}`", ephemeral=True)


class HeadcountModal(ui.Modal, title="Override species headcount"):
    species = ui.TextInput(label="Species name (exact)", max_length=40)
    count = ui.TextInput(label="Count", max_length=4)

    async def on_submit(self, interaction: discord.Interaction):
        if not can_staff(interaction.user, "headcount"):
            await deny_staff(interaction, "headcount")
            return
        live = _fn("LIVE_HEADCOUNT") or {}
        name = str(self.species.value).strip()
        try:
            n = int(str(self.count.value).strip())
        except Exception:
            await interaction.response.send_message("Count must be a number.", ephemeral=True)
            return
        if name not in live:
            await interaction.response.send_message(f"`{name}` is not in the live headcount table.", ephemeral=True)
            return
        live[name] = max(0, n)
        await interaction.response.send_message(f"Headcount **{name}** set to `{n}`.", ephemeral=True)


class LookupModal(ui.Modal, title="Look up wallet"):
    discord_id = ui.TextInput(label="Player Discord ID", max_length=20)

    async def on_submit(self, interaction: discord.Interaction):
        if not can_staff(interaction.user, "tokens"):
            await deny_staff(interaction, "tokens")
            return
        try:
            uid = int(str(self.discord_id.value).strip())
        except Exception:
            await interaction.response.send_message("Discord ID must be a number.", ephemeral=True)
            return
        get_steam = _fn("get_linked_steam_id")
        get_bal = _fn("get_steam_token_balance")
        steam = get_steam(uid) if get_steam else None
        if not steam:
            await interaction.response.send_message("That user has not linked Steam.", ephemeral=True)
            return
        bal = get_bal(steam) if get_bal else 0
        await interaction.response.send_message(f"<@{uid}> Steam `{steam}` — **{bal}** tokens", ephemeral=True)


class ReturnStoreModal(ui.Modal, title="Return store to vault"):
    player = ui.TextInput(
        label="Discord ID or SteamID64",
        max_length=20,
        placeholder="4680… or 7656119…",
    )
    species = ui.TextInput(
        label="Species",
        max_length=40,
        placeholder="Tyrannosaurus",
    )
    growth = ui.TextInput(
        label="Growth %",
        max_length=6,
        default="67",
        placeholder="67",
    )
    gender = ui.TextInput(
        label="Gender (Male / Female / blank)",
        max_length=8,
        required=False,
        placeholder="blank = keep live gender",
    )
    mutations = ui.TextInput(
        label="Mutations (juvie / sub-adult)",
        max_length=120,
        required=False,
        placeholder="Epidermal Fibrosis / Gastronomic Regeneration",
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not can_staff(interaction.user, "return_store"):
            await deny_staff(interaction, "return_store")
            return
        await interaction.response.defer(ephemeral=True)
        raw_player = str(self.player.value or "").strip()
        steam = ""
        discord_uid = None
        get_steam = _fn("get_linked_steam_id")
        if len(raw_player) == 17 and raw_player.isdigit() and raw_player.startswith("7656"):
            steam = raw_player
        else:
            try:
                discord_uid = int(raw_player)
            except Exception:
                await interaction.followup.send(
                    "Enter a Discord user ID or a 17-digit SteamID64.",
                    ephemeral=True,
                )
                return
            steam = str(get_steam(discord_uid) or "").strip() if get_steam else ""
            if not steam:
                await interaction.followup.send(
                    "That Discord user has not linked Steam.",
                    ephemeral=True,
                )
                return
        species = _resolve_species_name(self.species.value)
        if not species:
            await interaction.followup.send(
                f"`{str(self.species.value or '').strip()}` is not a playable species.",
                ephemeral=True,
            )
            return
        growth = _parse_growth_pct(self.growth.value)
        if growth is None:
            await interaction.followup.send(
                "Growth must be a percent between 1 and 100 (e.g. `67`).",
                ephemeral=True,
            )
            return
        female, gender_label = _parse_gender_field(self.gender.value)
        if self.gender.value and str(self.gender.value).strip() and female is False and gender_label == "":
            await interaction.followup.send(
                "Gender must be **Male**, **Female**, or blank.",
                ephemeral=True,
            )
            return
        mut = _normalize_return_mutations(self.mutations.value)
        ok, err, slot_id = await _write_vault_return_store(
            steam, species, growth, female, mutations=mut,
        )
        if not ok:
            await _vault_audit(
                interaction, "return_store", steam=steam, ok=False,
                species=species, growth=f"{growth * 100:.0f}%", gender=gender_label or None,
                slot=slot_id, msg=f"Vault write failed: {err}",
            )
            await interaction.followup.send(
                f"Could not write the vault slot: {err}",
                ephemeral=True,
            )
            return
        who = f"<@{discord_uid}> " if discord_uid else ""
        mut_bit = f" · mutations `{mut}`" if mut else ""
        msg = (
            f"Returned **{species}** at **{growth * 100:.0f}%**"
            + (f" ({gender_label})" if gender_label else "")
            + mut_bit
            + f" to {who}`{steam}` as slot `{slot_id}`.\n"
            "They spawn a matching juvenile, then **Redeem** — applies where they stand."
        )
        await _vault_audit(
            interaction, "return_store", steam=steam, ok=True,
            species=species, growth=f"{growth * 100:.0f}%", gender=gender_label or None,
            slot=slot_id, msg="Staff returned store to vault" + (f" mut={mut}" if mut else ""),
        )
        await interaction.followup.send(msg, ephemeral=True)


async def _need_steam(interaction):
    get_steam = _fn("get_linked_steam_id")
    steam = get_steam(interaction.user.id) if get_steam else None
    if steam:
        return steam
    if interaction.response.is_done():
        await interaction.followup.send("Link Steam first with the 🔗 button.", ephemeral=True)
    else:
        await interaction.response.send_message("Link Steam first with the 🔗 button.", ephemeral=True)
    return None


async def _private_panel_send(interaction, content, **kwargs):
    kwargs["ephemeral"] = True
    if interaction.response.is_done():
        return await interaction.followup.send(content, **kwargs)
    return await interaction.response.send_message(content, **kwargs)


def _vault_embed(raw, steam):
    try:
        data = json.loads(raw)
    except Exception:
        data = {}
    species = data.get("species") or "unknown"
    growth = float(data.get("growth") or 0) * 100
    gender = data.get("gender") or ""
    if not gender:
        if data.get("female") is True:
            gender = "Female"
        elif data.get("female") is False:
            gender = "Male"
    flags = str(data.get("primeFlags") or "")
    have = int(data.get("primeHave") or flags.count("1") or 0)
    mut = data.get("mutations") or ""
    loc = ""
    if data.get("x") is not None:
        loc = f"{float(data.get('x') or 0):.0f}, {float(data.get('y') or 0):.0f}, {float(data.get('z') or 0):.0f}"
    desc = (
        f"Steam `{steam}`\n"
        f"**{species}** at **{growth:.0f}%**"
        + (f" · {gender}" if gender else "")
        + (f"\nPrime flags **{have}/10**" if flags or have else "")
        + (f"\nMutations: `{mut}`" if mut else "")
        + (f"\nStored location: `{loc}`" if loc else "")
    )
    embed = discord.Embed(title="ℹ️ Vault", description=desc, color=discord.Color.greyple())
    return embed


async def run_isle_mod_slash(interaction, verb, extra=None):
    steam = await _need_steam(interaction)
    if not steam:
        return
    verb = str(verb or "").lower().strip()
    extra = extra or {}
    if verb == "prime":
        verb = "primeinfo"

    if verb == "apply":
        species = extra.get("species") or ""
        growth = float(extra.get("growth") if extra.get("growth") is not None else 0.7)
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        ok, err = await _queue_inbox({
            "id": f"discord-{verb}-{int(time.time())}",
            "verb": "apply",
            "steam": steam,
            "species": species,
            "growth": growth,
        })
        if not ok:
            await interaction.followup.send(f"Could not reach the Isle: {err}", ephemeral=True)
            return False
        await _vault_audit(
            interaction, "apply", steam=steam, ok=True,
            species=species, growth=f"{growth * 100:.0f}%",
            msg="Queued retired grow/apply (Isle will refuse)",
        )
        await interaction.followup.send(
            f"Queued **{growth * 100:.0f}%** growth on **{species}** for Steam `{steam}`.\n"
            "Be spawned as that juvenile in-game — it applies in about 3 seconds.",
            ephemeral=True,
        )
        return True

    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)
    if verb == "storeinfo":
        await _queue_cmd_flag("storeinfo", steam)
        slots = await _read_vault_slots(steam)
        if not slots:
            await interaction.followup.send(
                "No vault on file yet. Buy a dino from the dropdown, or store one with **Store dino** / `!store`.",
                ephemeral=True,
            )
            return True
        lines = []
        for row in slots[-15:]:
            species = row.get("species") or "?"
            gender = row.get("gender") or (
                "Female" if row.get("female") is True else "Male" if row.get("female") is False else ""
            )
            growth = float(row.get("growth") or 0) * 100
            source = row.get("source") or "store"
            sid = row.get("id") or ""
            bits = [f"**{species}**"]
            if gender:
                bits.append(gender)
            bits.append(f"{growth:.0f}%")
            bits.append(str(source))
            if sid:
                bits.append(f"`{sid}`")
            lines.append("• " + " · ".join(bits))
        embed = discord.Embed(
            title="Vault info",
            description="\n".join(lines),
            color=discord.Color.greyple(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return True
    slot = str(extra.get("slot") or extra.get("species") or "").strip()
    if verb == "redeem":
        slots = await _read_vault_slots(steam)
        if not slots:
            await _vault_audit(
                interaction, "redeem", steam=steam, ok=False,
                slot=slot, msg="Redeem refused — vault empty",
            )
            await interaction.followup.send(
                "Your vault is empty. Buy a dino, or store one with **Store dino** / `!store` "
                "and finish the safelog first.",
                ephemeral=True,
            )
            return False
        if slot:
            ids = {str(row.get("id") or "").strip() for row in slots}
            legacy_ok = slot in ("vault", "last") and any(
                not str(row.get("id") or "").strip() or str(row.get("id") or "").strip() == "vault"
                for row in slots
            )
            if slot not in ids and not legacy_ok:
                await _vault_audit(
                    interaction, "redeem", steam=steam, ok=False,
                    slot=slot, msg="Redeem refused — slot not in vault",
                )
                await interaction.followup.send(
                    f"Vault slot `{slot}` is not available. Pick a stored dino from the list, "
                    "or store one first.",
                    ephemeral=True,
                )
                return False
    ok, err = await _queue_cmd_flag(verb, steam, slot if verb == "redeem" else "")
    if not ok:
        if verb in ("store", "redeem", "cancelstore"):
            await _vault_audit(
                interaction, verb, steam=steam, ok=False,
                slot=slot, msg=f"Could not reach the Isle: {err}",
            )
        await interaction.followup.send(f"Could not reach the Isle: {err}", ephemeral=True)
        return False

    if verb == "store":
        await _vault_audit(
            interaction, "store", steam=steam, ok=True,
            msg="Store armed — waiting for safelog",
        )
        await interaction.followup.send(
            "Store **armed** on the Isle. Start a **safelog** and stay in it until the slay.\n"
            "`!cancelstore` in-game aborts.",
            ephemeral=True,
        )
        return True
    if verb == "redeem":
        slot = str(extra.get("slot") or "").strip()
        await _vault_audit(
            interaction, "redeem", steam=steam, ok=True,
            slot=slot, msg="Redeem queued (Isle applies in ~3s)",
        )
        await interaction.followup.send(
            "Redeem queued"
            + (f" for slot `{slot}`" if slot else "")
            + ". Stay spawned as a **matching juvenile** — it applies in about 3 seconds "
            + ("**where you are standing**." if slot.startswith("buy-") or not slot else ".")
            + " That vault slot is **used up**. If you die, store again first or it is gone.",
            ephemeral=True,
        )
        return True
    if verb in ("primeinfo", "cancelstore"):
        if verb == "cancelstore":
            await _vault_audit(
                interaction, "cancelstore", steam=steam, ok=True,
                msg="Cancel store sent to Isle",
            )
        await interaction.followup.send(
            f"`{verb}` sent to the Isle for Steam `{steam}`. Watch in-game for the result.",
            ephemeral=True,
        )
        return True
    await interaction.followup.send(f"`{verb}` queued for Steam `{steam}`.", ephemeral=True)
    return True


SMALL_PRIME_SPECIES = small_prime_species()

# Engine slot order from live reads: 7–8 are true on a fresh menu spawn.
# Those are the avoid-conditions (never infertile / never spasms), not nest.
PRIME_SLOT_NAMES = {
    1: "Visit a Sanctuary",
    2: "Visit 2 Migration Zones",
    3: "Perfect Diet",
    4: "Visit 4 Patrol Zones",
    5: "Born from a player nest",
    6: "Raise a hatchling to 50%",
    7: "Never go infertile (keep a nutrient)",
    8: "Never get muscle spasms",
    9: "Per-life objective",
    10: "Breeding lifetime objective",
}


def _prime_embed(data, steam):
    species = data.get("species") or "your dino"
    growth = float(data.get("growth") or 0) * 100
    have = int(data.get("have") or 0)
    need = int(data.get("need") or 5)
    eligible = data.get("eligible") is True
    conditions = data.get("conditions") or []
    done = []
    todo = []
    for row in conditions:
        slot = int(row.get("slot") or 0)
        name = PRIME_SLOT_NAMES.get(slot) or row.get("name") or f"Objective {slot}"
        if row.get("ok"):
            done.append(f"✅ {name}")
        else:
            todo.append(f"⬜ {name}")
    if growth >= 75:
        if eligible:
            window = "Past 75% with eligibility locked in."
        else:
            window = "Past 75% without eligibility — this life is on the frail path."
    elif have >= need:
        window = "Threshold met. Stay under 75% until Prime locks at adult."
    else:
        window = f"Need **{max(0, need - have)}** more engine flag(s) before **75%** growth."
    if species in SMALL_PRIME_SPECIES:
        window += f"\nSmall species (**{species}**) often already count one breeding-style flag."
    desc = (
        f"Steam `{steam}` · **{species}** at **{growth:.0f}%**\n"
        f"Engine flags **{have}/10** (need **{need}** true) · Eligible: **{'yes' if eligible else 'no'}**\n"
        f"{window}\n\n"
        "Names: slots 7–8 (**never infertile / never spasms**) start complete on a menu spawn "
        "and only fail if you empty all nutrients or spasm. Nest and hatchling are slots 5–6 "
        "and stay empty until you actually nest."
    )
    embed = discord.Embed(title="🧬 Prime Elder check", description=desc, color=discord.Color.purple())
    embed.add_field(name="Completed", value="\n".join(done) if done else "None yet", inline=True)
    embed.add_field(name="Still needed", value="\n".join(todo) if todo else "None — flags are set", inline=True)
    return embed


async def run_prime_check(interaction):
    steam = await _need_steam(interaction)
    if not steam:
        return
    import primeval_isle

    if not primeval_isle.G:
        primeval_isle.bind(G)
    queue = primeval_isle.queue_verb
    read = primeval_isle.read_prime
    if not queue or not read:
        await interaction.response.send_message(
            "Prime check is not wired to the Isle host yet.",
            ephemeral=True,
        )
        return
    await interaction.response.defer(ephemeral=True)
    started = int(time.time())
    ok, err = await queue("primeinfo", steam)
    if not ok:
        await interaction.followup.send(f"Could not reach the Isle: {err}", ephemeral=True)
        return
    data = None
    for _ in range(12):
        await asyncio.sleep(1)
        raw = await read(steam)
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except Exception:
            continue
        captured = int(parsed.get("capturedAt") or 0)
        if captured >= started - 2:
            data = parsed
            break
    if data is None:
        await interaction.followup.send(
            "No live Prime report yet. Spawn your dino on the Isle, then press **Prime check** again.",
            ephemeral=True,
        )
        return
    if data.get("ok") is False:
        await interaction.followup.send(
            data.get("error") or "Could not read your dino. Spawn first, then try again.",
            ephemeral=True,
        )
        return
    await interaction.followup.send(embed=_prime_embed(data, steam), ephemeral=True)


async def finish_buy(interaction, species, female, mut1, mut2):
    steam = await _need_steam(interaction)
    if not steam:
        return
    try:
        from primeval_tickets import shop_block_reason
        blocked, why = shop_block_reason(interaction.user.id, steam)
    except Exception:
        blocked, why = False, ""
    if blocked:
        msg = why or "The dino shop is locked on your account."
        await _vault_audit(
            interaction, "buy", steam=steam, ok=False,
            species=species, msg=msg,
        )
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        return
    if mutation_fname(mut1) == mutation_fname(mut2):
        msg = "Pick two different mutations."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        return
    if not mutation_allowed(species, 1, female, mut1) or not mutation_allowed(species, 2, female, mut2):
        msg = "Those mutations are not valid for that species and gender. Start the buy again."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        return
    get_bal = _fn("get_steam_token_balance")
    charge = _fn("charge_steam_tokens")
    cost = buy_cost(species)
    tokens = get_bal(steam) if get_bal else 0
    if tokens < cost:
        msg = f"Buying **{species}** costs **{cost}** tokens. You have **{tokens}**."
        await _vault_audit(
            interaction, "buy", steam=steam, ok=False,
            species=species, tokens=cost, balance=tokens, msg=msg,
        )
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        return
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)
    new_balance = charge(steam, interaction.user.id, cost) if charge else None
    if new_balance is None:
        await _vault_audit(
            interaction, "buy", steam=steam, ok=False,
            species=species, tokens=cost, msg="Wallet charge failed — tokens not taken",
        )
        await interaction.followup.send("Could not update your wallet. Tokens were not charged.", ephemeral=True)
        return
    ok, err, slot_id = await _write_vault_purchase(steam, species, female, pack_mutations(mut1, mut2))
    if not ok:
        credit = _fn("credit_steam_tokens") or _fn("add_steam_tokens")
        if credit:
            credit(steam, interaction.user.id, cost)
        await _vault_audit(
            interaction, "buy", steam=steam, ok=False,
            species=species, tokens=cost, slot=slot_id,
            msg=f"Vault write failed, tokens refunded: {err}",
        )
        await interaction.followup.send(
            f"Tokens were refunded. Could not save the vaulted dino: {err}",
            ephemeral=True,
        )
        return
    gender = "Female" if female else "Male"
    await _vault_audit(
        interaction, "buy", steam=steam, ok=True,
        species=species, gender=gender, slot=slot_id,
        mutations=f"{mut1} / {mut2}", tokens=cost, balance=new_balance,
        growth="70%", msg="Vaulted for redeem",
    )
    await interaction.followup.send(
        f"Bought a **{gender} {species}** for {cost} tokens. Balance: **{new_balance}**.\n"
        f"Juvenile: **{mut1}**. Sub-adult: **{mut2}**.\n"
        f"Saved as `{slot_id}` — **70%** growth, **75%** hunger.\n"
        f"Spawn in as a **fresh {species} juvenile**, then **Redeem** this slot.\n"
        "Redeem copies those stats onto you in **one inject** (gender, both mutations, 70% growth, 75% hunger). That vault slot is then used up.",
        ephemeral=True,
    )


async def run_buy(interaction, species):
    if species == "none" or not species:
        await _private_panel_send(interaction, "Shop is empty.")
        return
    if species.startswith("LOCKED_"):
        await _private_panel_send(interaction, "That species is sold out.")
        return
    steam = await _need_steam(interaction)
    if not steam:
        return
    try:
        from primeval_tickets import shop_block_reason
        blocked, why = shop_block_reason(interaction.user.id, steam)
    except Exception:
        blocked, why = False, ""
    if blocked:
        await _private_panel_send(interaction, why or "The dino shop is locked on your account.")
        return
    cost = buy_cost(species)
    await _private_panel_send(
        interaction,
        f"Buy **{species}** — pick gender, then juvenile and sub-adult mutations. **{cost}** tokens. Redeem injects a **70%** dino with those picks and **75%** hunger.",
        view=BuyGenderView(species),
    )


class BuyGenderView(ui.View):
    def __init__(self, species):
        super().__init__(timeout=180)
        self.species = species

    @ui.button(label="Male", emoji="♂️", style=discord.ButtonStyle.primary)
    async def male(self, interaction: discord.Interaction, button: ui.Button):
        await _start_buy_mutations(interaction, self.species, False)

    @ui.button(label="Female", emoji="♀️", style=discord.ButtonStyle.primary)
    async def female(self, interaction: discord.Interaction, button: ui.Button):
        await _start_buy_mutations(interaction, self.species, True)


def _mutation_options(species, slot, female, exclude=None):
    options = []
    for label in mutation_choices(species, slot, female, exclude=exclude):
        fname = mutation_fname(label)
        options.append(
            discord.SelectOption(
                label=label[:100],
                value=fname[:100],
                description=("Juvenile slot 1" if slot == 1 else "Sub-adult slot 2")[:100],
            )
        )
    return options


async def _start_buy_mutations(interaction, species, female):
    options = _mutation_options(species, 1, female)
    if len(options) < 1:
        await interaction.response.send_message(
            "No juvenile mutations are configured for that species. Tell staff.",
            ephemeral=True,
        )
        return
    gender = "Female" if female else "Male"
    await interaction.response.send_message(
        f"**{gender} {species}** — pick **juvenile mutation 1**.",
        view=BuyMutationView(species, female, slot=1),
        ephemeral=True,
    )


class BuyMutationSelect(ui.Select):
    def __init__(self, species, female, slot, mut1=None):
        self.species = species
        self.female = female
        self.slot = slot
        self.mut1 = mut1
        exclude = [mut1] if mut1 else None
        options = _mutation_options(species, slot, female, exclude=exclude)
        if not options:
            options = [discord.SelectOption(label="No mutations", value="none")]
        placeholder = (
            "Juvenile mutation (slot 1)…"
            if slot == 1
            else "Sub-adult mutation (slot 2)…"
        )
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options[:25],
        )

    async def callback(self, interaction: discord.Interaction):
        choice = self.values[0]
        if choice == "none":
            await interaction.response.send_message("No mutations available.", ephemeral=True)
            return
        if self.slot == 1:
            options = _mutation_options(self.species, 2, self.female, exclude=[choice])
            if len(options) < 1:
                await interaction.response.send_message(
                    "No sub-adult mutations are configured for that species. Tell staff.",
                    ephemeral=True,
                )
                return
            gender = "Female" if self.female else "Male"
            await interaction.response.send_message(
                f"**{gender} {self.species}** — juvenile **{choice}**. Now pick **sub-adult mutation 2**.",
                view=BuyMutationView(self.species, self.female, slot=2, mut1=choice),
                ephemeral=True,
            )
            return
        await finish_buy(interaction, self.species, self.female, self.mut1, choice)


class BuyMutationView(ui.View):
    def __init__(self, species, female, slot, mut1=None):
        super().__init__(timeout=180)
        self.add_item(BuyMutationSelect(species, female, slot, mut1=mut1))


class RedeemSlotSelect(ui.Select):
    def __init__(self, slots):
        options = []
        for row in slots[-25:]:
            sid = str(row.get("id") or "").strip()
            if not sid:
                continue
            species = str(row.get("species") or "?")
            gender = row.get("gender") or ("Female" if row.get("female") is True else "Male" if row.get("female") is False else "")
            growth = float(row.get("growth") or 0) * 100
            source = str(row.get("source") or "store")
            options.append(
                discord.SelectOption(
                    label=f"{species} {gender}".strip()[:100],
                    description=f"{growth:.0f}% · {source} · {sid}"[:100],
                    value=sid[:100],
                )
            )
        if not options:
            options.append(discord.SelectOption(label="No vault slots", value="none"))
        super().__init__(
            placeholder="Pick which stored dino to redeem…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        choice = self.values[0]
        if choice == "none":
            await interaction.response.send_message("No stored dinos in your vault.", ephemeral=True)
            return
        await run_isle_mod_slash(interaction, "redeem", extra={"slot": choice})


class RedeemPickView(ui.View):
    def __init__(self, slots):
        super().__init__(timeout=120)
        self.add_item(RedeemSlotSelect(slots))


async def run_redeem_picker(interaction, use_last=False):
    steam = await _need_steam(interaction)
    if not steam:
        return
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)
    slots = await _read_vault_slots(steam)
    if not slots:
        await interaction.followup.send(
            "Your vault is empty. Buy a dino, or store one with **Store dino** / `!store` "
            "and finish the safelog first.",
            ephemeral=True,
        )
        return
    if use_last:
        await run_isle_mod_slash(interaction, "redeem")
        return
    await interaction.followup.send(
        "Spawn as a **fresh juvenile of that species**, then pick the vault slot.\n"
        "Bought dinos apply **where you are standing**.",
        view=RedeemPickView(slots),
        ephemeral=True,
    )


async def _reset_member_panel_dropdowns(interaction):
    # Persistent selects retain their last visible value in Discord. A user
    # choosing that same action again may produce no interaction at all.
    # Acknowledge privately, then replace the public view with fresh selects.
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        if interaction.message is not None:
            await interaction.message.edit(view=MemberPanelView())
    except Exception as exc:
        print(f"[PANELS] dropdown reset failed: {exc}")


class BuySpeciesSelect(ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Buy a dinosaur…",
            min_values=1,
            max_values=1,
            options=buy_options(),
            custom_id="pi_mem_buy_sel",
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        choice = self.values[0]
        await _reset_member_panel_dropdowns(interaction)
        await run_buy(interaction, choice)


class RedeemSpeciesSelect(ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Redeem stored dino…",
            min_values=1,
            max_values=1,
            options=redeem_options(),
            custom_id="pi_mem_red_sel",
            row=2,
        )

    async def callback(self, interaction: discord.Interaction):
        choice = self.values[0]
        await _reset_member_panel_dropdowns(interaction)
        if choice == "vault":
            await run_redeem_picker(interaction, use_last=True)
            return
        await run_redeem_picker(interaction, use_last=False)


class MemberPanelView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(BuySpeciesSelect())
        self.add_item(RedeemSpeciesSelect())

    @ui.button(label="How to play", emoji="📖", style=discord.ButtonStyle.success, custom_id="pi_mem_how", row=0)
    async def how(self, interaction: discord.Interaction, button: ui.Button):
        import primeval_qol

        await primeval_qol.show_how_to(interaction)

    @ui.button(label="Link Steam", emoji="🔗", style=discord.ButtonStyle.primary, custom_id="pi_mem_link", row=0)
    async def link(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(LinkSteamModal())

    @ui.button(label="I'm stuck", emoji="🆘", style=discord.ButtonStyle.danger, custom_id="pi_mem_stuck", row=0)
    async def stuck(self, interaction: discord.Interaction, button: ui.Button):
        import primeval_qol

        await primeval_qol.start_stuck(interaction)

    @ui.button(label="My progress", emoji="📋", style=discord.ButtonStyle.primary, custom_id="pi_mem_progress", row=0)
    async def progress(self, interaction: discord.Interaction, button: ui.Button):
        import primeval_qol

        await primeval_qol.show_progress(interaction)

    @ui.button(label="Self kill", emoji="💀", style=discord.ButtonStyle.danger, custom_id="pi_mem_selfkill", row=3)
    async def selfkill(self, interaction: discord.Interaction, button: ui.Button):
        steam = await _need_steam(interaction)
        if not steam:
            return
        await interaction.response.send_message(
            "This will kill your currently spawned dinosaur. "
            "It will not store or recover the dinosaur.\n\n"
            "Are you sure?",
            view=SelfKillConfirmView(interaction.user.id),
            ephemeral=True,
        )

    @ui.button(label="Balance", emoji="💰", style=discord.ButtonStyle.primary, custom_id="pi_mem_bal", row=0)
    async def bal(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message(await member_balance_text(interaction.user), ephemeral=True)   

    @ui.button(label="Store dino", emoji="📦", style=discord.ButtonStyle.secondary, custom_id="pi_mem_store", row=3)
    async def store(self, interaction: discord.Interaction, button: ui.Button):
        await run_isle_mod_slash(interaction, "store")

    @ui.button(label="Vault info", emoji="ℹ️", style=discord.ButtonStyle.secondary, custom_id="pi_mem_info", row=3)
    async def info(self, interaction: discord.Interaction, button: ui.Button):
        await run_isle_mod_slash(interaction, "storeinfo")

    @ui.button(label="Prime check", emoji="🧬", style=discord.ButtonStyle.success, custom_id="pi_mem_prime", row=3)
    async def prime(self, interaction: discord.Interaction, button: ui.Button):
        await run_prime_check(interaction)

    @ui.button(label="Last recap", emoji="🧾", style=discord.ButtonStyle.success, custom_id="pi_mem_recap", row=3)
    async def recap(self, interaction: discord.Interaction, button: ui.Button):
        import primeval_qol

        await primeval_qol.show_recap(interaction)

    @ui.button(label="Waitlist", emoji="🔓", style=discord.ButtonStyle.success, custom_id="pi_mem_wait", row=4)
    async def waitlist(self, interaction: discord.Interaction, button: ui.Button):
        import primeval_qol

        await primeval_qol.show_waitlist(interaction)

    @ui.button(label="TP to friend", emoji="📍", style=discord.ButtonStyle.primary, custom_id="pi_mem_tp", row=4)
    async def tp(self, interaction: discord.Interaction, button: ui.Button):
        import primeval_tp

        await primeval_tp.open_tp_menu(interaction)

    @ui.button(label="Pack VC", emoji="🎙️", style=discord.ButtonStyle.primary, custom_id="pi_mem_packvc", row=4)
    async def pack_vc(self, interaction: discord.Interaction, button: ui.Button):
        import primeval_voice

        await primeval_voice.from_panel(interaction)

    @ui.button(label="Skin", emoji="🎨", style=discord.ButtonStyle.secondary, custom_id="pi_mem_skin", row=4)
    async def skin(self, interaction: discord.Interaction, button: ui.Button):
        import primeval_skin

        await primeval_skin.open_skin(interaction)

    @ui.button(label="My cards", emoji="🃏", style=discord.ButtonStyle.primary, custom_id="pi_mem_cards", row=4)
    async def cards(self, interaction: discord.Interaction, button: ui.Button):
        import primeval_trade

        await primeval_trade.show_trade_home(interaction)


async def _rcon_blob(interaction, command, title, action):
    if not can_staff(interaction.user, action):
        await deny_staff(interaction, action)
        return
    await interaction.response.defer(ephemeral=True)
    send = _fn("send_game_command")
    result = await send(command) if send else "no rcon"
    await _audit_game_cmd(interaction, command.split()[0] if command else title, extra=command, result=result)
    if len(str(result)) > 1800:
        result = str(result)[:1800] + "..."
    await interaction.followup.send(f"{title}:\n```\n{result}\n```", ephemeral=True)


class CorpseWipeConfirmView(ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=45)
        self.user_id = int(user_id)

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Only the staff member who opened this confirmation can use it.",
                ephemeral=True,
            )
            return False
        return True

    @ui.button(label="Wipe all corpses", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        if not can_staff(interaction.user, "rcon"):
            await deny_staff(interaction, "rcon")
            return
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content="Running the global corpse cleanup…",
            view=self,
        )
        send = _fn("send_game_command")
        result = await send("wipecorpses") if send else "no rcon"
        await _audit_game_cmd(
            interaction,
            "wipecorpses",
            extra="director staff panel; global corpse cleanup",
            result=result,
        )
        await interaction.followup.send(
            f"Corpse cleanup result:\n```\n{str(result)[:1800]}\n```",
            ephemeral=True,
        )
        self.stop()

    @ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Corpse cleanup cancelled.", view=self)
        self.stop()


class StaffToolsSelect(ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="🛠️ More staff tools…",
            min_values=1,
            max_values=1,
            custom_id="pi_adm_more_sel",
            row=4,
            options=[
                discord.SelectOption(label="Set in-game group", value="setgroup", emoji="👑", description="Administrator+"),
                discord.SelectOption(label="Override headcount", value="headcount", emoji="🧮", description="Moderator+"),
                discord.SelectOption(label="Look up wallet", value="lookup", emoji="📋", description="Director"),
                discord.SelectOption(label="Return store", value="return_store", emoji="📦", description="Admin — put a store back in their vault"),
                discord.SelectOption(label="Events panel", value="events_panel", emoji="🌙", description="Post token / growth / hunt / migration"),
                discord.SelectOption(label="Test Entombed grant", value="gamble_test", emoji="🧪", description="Admin — 60% gamble prize to redeem"),
                discord.SelectOption(label="Bot post", value="bot_post", emoji="📝", description="Moderator — formatted #announcements / #events"),
                discord.SelectOption(label="RCON serverdetails", value="rcon", emoji="🛠️", description="Director"),
                discord.SelectOption(label="Wipe all corpses", value="wipe_corpses", emoji="🧹", description="Director — global cleanup, confirmation required"),
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        choice = self.values[0]
        if choice == "setgroup":
            if not can_staff(interaction.user, "setgroup"):
                await deny_staff(interaction, "setgroup")
                return
            await interaction.response.send_modal(SetGroupModal())
            return
        if choice == "headcount":
            if not can_staff(interaction.user, "headcount"):
                await deny_staff(interaction, "headcount")
                return
            await interaction.response.send_modal(HeadcountModal())
            return
        if choice == "lookup":
            if not can_staff(interaction.user, "tokens"):
                await deny_staff(interaction, "tokens")
                return
            await interaction.response.send_modal(LookupModal())
            return
        if choice == "return_store":
            if not can_staff(interaction.user, "return_store"):
                await deny_staff(interaction, "return_store")
                return
            await interaction.response.send_modal(ReturnStoreModal())
            return
        if choice == "events_panel":
            import primeval_events

            await primeval_events.post_events_panel(interaction)
            return
        if choice == "gamble_test":
            import primeval_trade

            await primeval_trade.start_gamble_test(interaction)
            return
        if choice == "bot_post":
            import primeval_posts

            await primeval_posts.show_post_picker(interaction)
            return
        if choice == "wipe_corpses":
            if not can_staff(interaction.user, "rcon"):
                await deny_staff(interaction, "rcon")
                return
            await interaction.response.send_message(
                "This runs Evrima’s native **global** `wipecorpses` command. "
                "It removes every corpse, not only the stored dinosaur. Continue?",
                view=CorpseWipeConfirmView(interaction.user.id),
                ephemeral=True,
            )
            return
        await _rcon_blob(interaction, "serverdetails", "RCON", "rcon")


class DirectorRestartConfirmView(ui.View):
    def __init__(self):
        super().__init__(timeout=45)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True

    @ui.button(label="Confirm 5 min restart", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        if not can_staff(interaction.user, "restart_isle"):
            await deny_staff(interaction, "restart_isle")
            return
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content="Scheduling restart — players get **5 minutes** to safelog.",
            view=self,
        )
        import primeval_health_board

        ok, detail = await primeval_health_board.director_restart()
        await _audit_game_cmd(
            interaction,
            "restart",
            extra="director staff panel",
            result=detail,
        )
        try:
            import primeval_posts

            await primeval_posts.post_director_restart(interaction.client, interaction.user, ok, detail)
        except Exception as exc:
            print(f"[PANELS] director restart log failed: {exc}")
        await interaction.followup.send(
            ("✅ " if ok else "🛑 ") + detail,
            ephemeral=True,
        )
        self.stop()

    @ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Restart cancelled.", view=self)
        self.stop()


class AdminPanelView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(StaffToolsSelect())

    @ui.button(label="Players", emoji="👥", style=discord.ButtonStyle.success, custom_id="pi_adm_players", row=0)
    async def players(self, interaction: discord.Interaction, button: ui.Button):
        await _rcon_blob(interaction, "playerlist", "Live players", "players")

    @ui.button(label="Broadcast", emoji="📢", style=discord.ButtonStyle.primary, custom_id="pi_adm_bc", row=0)
    async def broadcast(self, interaction: discord.Interaction, button: ui.Button):
        if not can_staff(interaction.user, "broadcast"):
            await deny_staff(interaction, "broadcast")
            return
        await interaction.response.send_modal(BroadcastModal())

    @ui.button(label="Bot post", emoji="📝", style=discord.ButtonStyle.primary, custom_id="pi_adm_bot_post", row=0)
    async def bot_post(self, interaction: discord.Interaction, button: ui.Button):
        import primeval_posts

        await primeval_posts.show_post_picker(interaction)

    @ui.button(label="Kick", emoji="👢", style=discord.ButtonStyle.secondary, custom_id="pi_adm_kick", row=1)
    async def kick(self, interaction: discord.Interaction, button: ui.Button):
        if not can_staff(interaction.user, "kick"):
            await deny_staff(interaction, "kick")
            return
        await interaction.response.send_modal(KickModal())

    @ui.button(label="Slay", emoji="⚔️", style=discord.ButtonStyle.secondary, custom_id="pi_adm_slay", row=1)
    async def slay(self, interaction: discord.Interaction, button: ui.Button):
        if not can_staff(interaction.user, "slay"):
            await deny_staff(interaction, "slay")
            return
        await interaction.response.send_modal(TargetModal("slay", "Slay player", "slay"))

    @ui.button(label="Revive", emoji="💖", style=discord.ButtonStyle.secondary, custom_id="pi_adm_revive", row=1)
    async def revive(self, interaction: discord.Interaction, button: ui.Button):
        if not can_staff(interaction.user, "revive"):
            await deny_staff(interaction, "revive")
            return
        await interaction.response.send_modal(TargetModal("revive", "Revive player", "revive"))

    @ui.button(label="Return store", emoji="📦", style=discord.ButtonStyle.secondary, custom_id="pi_adm_return_store", row=1)
    async def return_store(self, interaction: discord.Interaction, button: ui.Button):
        if not can_staff(interaction.user, "return_store"):
            await deny_staff(interaction, "return_store")
            return
        await interaction.response.send_modal(ReturnStoreModal())

    @ui.button(label="Ban", emoji="🔨", style=discord.ButtonStyle.danger, custom_id="pi_adm_ban", row=2)
    async def ban(self, interaction: discord.Interaction, button: ui.Button):
        if not can_staff(interaction.user, "ban"):
            await deny_staff(interaction, "ban")
            return
        await interaction.response.send_modal(BanModal())

    @ui.button(label="Server restart", emoji="🔄", style=discord.ButtonStyle.danger, custom_id="pi_adm_restart", row=2)
    async def server_restart(self, interaction: discord.Interaction, button: ui.Button):
        if not can_staff(interaction.user, "restart_isle"):
            await deny_staff(interaction, "restart_isle")
            return
        await interaction.response.send_message(
            "This schedules an Isle restart in **5 minutes** so people can safelog. "
            "In-game: **5-minute announce**, then a **2-minute last call**, then save and bounce.\n"
            "**Director only.** Confirm if the world is lagging or broken.",
            view=DirectorRestartConfirmView(),
            ephemeral=True,
        )

    @ui.button(label="Tokens", emoji="🪙", style=discord.ButtonStyle.primary, custom_id="pi_adm_tokens", row=3)
    async def tokens(self, interaction: discord.Interaction, button: ui.Button):
        if not can_staff(interaction.user, "tokens"):
            await deny_staff(interaction, "tokens")
            return
        await interaction.response.send_modal(TokenModal())

    @ui.button(label="Events panel", emoji="🌙", style=discord.ButtonStyle.success, custom_id="pi_adm_events_panel", row=3)
    async def events_panel(self, interaction: discord.Interaction, button: ui.Button):
        import primeval_events

        await primeval_events.post_events_panel(interaction)


def member_embed():
    return discord.Embed(
        title="🦕 Fallen Earth | Member Panel",
        description=(
            "Start with **How to play**, then **Link Steam**. **My progress** shows your next step.\n\n"
            "🟢 **Dropdowns** — buy a vaulted dino (gender + 2 mutations), redeem a stored slot\n"
            "🔵 **Blue** — wallet, Steam link, and your first-session checklist\n"
            "⚪ **Gray** — store current dino (then **safelog**), or show vault info here in Discord\n"
            "🟢 **Prime check** — live engine flags for Prime Elder\n"
            "📍 **TP to friend** — search linked players; they must accept. They need **100+ HP**.\n"
            "🎙️ **Pack VC** — voice channel for the dino you are spawned as (also join **Create pack VC**)\n"
            "🟢 **Waitlist / Last recap / I'm stuck** — lock queue, store receipt, ticket\n"
            "🃏 **My cards** — vault trading cards, list / trade / gamble\n"
            "🎨 **Skin** — opens a private guided painter: pick a habitat theme, then choose each body-area color step by step. Apply once to a **fresh juvie** or eligible stored vault slot; the look **locks**. Also `!skin` in-game.\n\n"
            "**Buy** — pick **gender**, **juvenile mutation**, and **sub-adult mutation**. Redeem injects a **70%** dino with those picks and **75% hunger** in one write.\n"
            "Prices: T1–T2 **2**, T3 **3**, T4 **5**, T5 **8**, T6 **12** tokens.\n"
            "Linked members get **2 free tokens every 2 weeks** (first drop when you link Steam).\n"
            "Spawn as that species, then **Redeem** and pick the slot. It applies **where you are**.\n"
        ),
        color=discord.Color.green(),
    )


def staff_embed():
    return discord.Embed(
        title="🛡️ Fallen Earth | Staff Panel",
        description=(
            "Every click checks your Discord role (highest role wins).\n"
            "**Helper** — player list\n"
            "**Moderator** — kick, broadcast, bot post, headcount, **Events panel** (hunt / migration)\n"
            "**Administrator / Senior Administrator** — same rank: slay, revive, **return store**, setgroup, ban, post this panel, 24h and 48h timeouts, **growth window**, `/gamble_test`\n"
            "**Director** — owners: backend RCON (More staff tools), paywall / tokens, **token event** on the Events panel, **server restart** (5-minute safelog, then bounce)"
        ),
        color=discord.Color.dark_red(),
    )


async def post_member_panel(interaction: discord.Interaction):
    if interaction.guild and not can_staff(interaction.user, "post_member_panel"):
        perms = getattr(interaction.user, "guild_permissions", None)
        if not (perms and (perms.manage_messages or perms.manage_guild)):
            await deny_staff(interaction, "post_member_panel")
            return
    await interaction.response.send_message(embed=member_embed(), view=MemberPanelView())


async def post_staff_panel(interaction: discord.Interaction):
    if not can_staff(interaction.user, "post_staff_panel"):
        await deny_staff(interaction, "post_staff_panel")
        return
    await interaction.response.send_message(embed=staff_embed(), view=AdminPanelView())


def register_views(bot):
    bot.add_view(MemberPanelView())
    bot.add_view(AdminPanelView())
    try:
        import primeval_tp

        primeval_tp.register_views(bot)
    except Exception:
        pass
    try:
        import primeval_qol

        primeval_qol.register_views(bot)
    except Exception:
        pass
    try:
        import primeval_events

        primeval_events.register_views(bot)
    except Exception:
        pass
    try:
        import primeval_trade

        primeval_trade.register_views(bot)
    except Exception:
        pass


def register_slash(bot):
    from discord import app_commands

    existing = {cmd.name for cmd in bot.tree.get_commands()}
    if "panel" not in existing:

        @bot.tree.command(name="panel", description="Post the member panel (buy, redeem, store)")
        async def panel(interaction: discord.Interaction):
            await post_member_panel(interaction)

    if "staff_panel" not in existing:

        @bot.tree.command(name="staff_panel", description="Post the staff tools panel")
        async def staff_panel(interaction: discord.Interaction):
            await post_staff_panel(interaction)

    staff_cmd = bot.tree.get_command("staff_panel")
    if staff_cmd is not None:
        staff_cmd.default_permissions = None
    try:
        import primeval_skin
        primeval_skin.register_slash(bot)
    except Exception as exc:
        print(f"[PANELS] skin slash commands failed: {exc}")

class SelfKillConfirmView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=60)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This confirmation is not yours!", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Yes, Slay My Character", style=discord.ButtonStyle.danger, emoji="💀")
    async def confirm_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        
        import primeval_isle
        import time
        
        payload = {
            "id": f"selfkill-{interaction.user.id}-{int(time.time())}",
            "ts": int(time.time()),
            "verb": "selfkill",
            "steam": "",
        }
        
        # ====================================================================
        # FIXED LOOKUP MATRIX: Uses the native global bot database variables
        # ====================================================================
        try:
            # 1. First attempt to pull directly from your global database function
            main_bot = interaction.client
            getter = getattr(main_bot, "get_linked_steam_id", None)
            if getter:
                steam_id = getter(interaction.user.id)
                if steam_id:
                    payload["steam"] = str(steam_id).strip()
            
            # 2. Fallback check: Read directly from the global memory cache dictionary
            if not payload["steam"]:
                from __main__ import STEAM_LINKS
                if interaction.user.id in STEAM_LINKS:
                    payload["steam"] = str(STEAM_LINKS[interaction.user.id]).strip()
        except Exception as e:
            print(f"[SELF-KILL LOOKUP WARN] Global variables fallback check: {e}")
            
        # 3. Final safety check: Pull directly from the persistent JSON file
        if not payload["steam"]:
            try:
                import json
                import os
                if os.path.exists("steam_links.json"):
                    with open("steam_links.json", "r", encoding="utf-8") as f:
                        db = json.load(f)
                        # Discord IDs are stored as strings or ints depending on save sequence
                        if str(interaction.user.id) in db:
                            payload["steam"] = str(db[str(interaction.user.id)]).strip()
                        elif interaction.user.id in db:
                            payload["steam"] = str(db[interaction.user.id]).strip()
            except Exception as json_err:
                print(f"[SELF-KILL JSON CRITICAL ERROR] Failed parsing storage file: {json_err}")

        # If all 3 layers failed, decline execution safely
        if not payload["steam"] or len(payload["steam"]) != 17:
            await interaction.followup.send("❌ Error: Could not resolve your linked 17-digit Steam ID. Please ensure your account is registered via `/link_steam`.", ephemeral=True)
            return

        # Push payload to main.lua mod inbox channel
        ok, err = await primeval_isle.queue_inbox(payload)
        if ok:
            for child in self.children:
                child.disabled = True
            await interaction.edit_original_response(content="✅ **Self-kill command queued.** Please stay logged out of the server for a few moments while your character is safely processed.", view=self)
        else:
            await interaction.followup.send(f"🛑 Failed to queue self-kill: {err}", ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="❌ Self-kill cancelled safely.", view=self)
        self.stop()
