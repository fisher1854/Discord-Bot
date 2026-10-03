"""Vault trading cards: list, buy, trade, and gamble.

Cards are real vault slots. Listings post in #dino-trade-and-gamble.
Gamble: stake 2+ cards, pick target species, juvie + sub mutations,
and 4 inherited mutations. Win is a 60% Entombed of that species.
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
import uuid

import discord
from discord import ui

G = {}
STATE_PATH = "primeval_trade.json"
_LOCK = threading.RLock()

TRADE_CHANNEL_ID = 1543461110085066913
TRADE_CHANNEL_NAMES = (
    "dino-trade-and-gamble",
    "dino-trade",
    "trade-and-gamble",
)
MIN_PRICE = 1
MAX_PRICE = 50
MAX_LISTING_OFFERS = 8
MODE_TOKENS = "tokens"
MODE_OFFERS = "offers"
GAMBLE_MIN_CARDS = 2
GAMBLE_MAX_CARDS = 8
GAMBLE_MAX_TOKENS = 40
GAMBLE_MAX_CHANCE = 20.0
GAMBLE_FLOOR_CHANCE = 1.5
ENTOMB_GROWTH = 0.60

HOW_IT_WORKS = (
    "**Cards are your vault dinos.** Stats come from the live slot "
    "(species, growth, gender, prime, mutations, skin, unlocks).\n\n"
    "**Post a card** — it goes in this channel so everyone can see it.\n"
    "• **Buy with tokens only** — you set a price. Anyone linked can **Buy**.\n"
    "• **Looking for offers** — people can offer tokens or offer one of their cards. "
    "You accept or decline each offer.\n"
    "**Send a card** — private trade to one linked player. They must accept. No tokens.\n"
    "**Gamble** — stake **at least two** cards and pick the rare you want "
    "(species, gender, juvenile + sub-adult mutations, then 4 inherited, "
    "then optional tokens). Win = a **60% Entombed** of that species: current-life "
    "juvie/sub picks plus 4 inherited locked in; grow it to elder to earn 4 more. "
    "Lose = cards and tokens are gone.\n"
    "The house wins most of the time. More cards, higher growth, higher tier, "
    "prime, and extra tokens **nudge** luck. Aiming at a higher-tier rare is harder. "
    "Cap is **20%**.\n\n"
    "Listed cards can still be redeemed if you do it on purpose — a missing "
    "slot cancels the sale. Unlist first if you want it back."
)


def bind(g):
    G.clear()
    G.update(g)


def _fn(name, default=None):
    return G.get(name, default)


def _blank():
    return {"listings": {}, "offers": {}, "listing_offers": {}}


def _load():
    with _LOCK:
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                data.setdefault("listings", {})
                data.setdefault("offers", {})
                data.setdefault("listing_offers", {})
                return data
        except Exception:
            pass
        return _blank()


def _save(state):
    with _LOCK:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2)
            handle.write("\n")
        os.replace(tmp, STATE_PATH)


def _new_id(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _steam_for(user_id):
    getter = _fn("get_linked_steam_id")
    if not getter:
        return ""
    try:
        return str(getter(int(user_id)) or "").strip()
    except Exception:
        return ""


def _balance(steam):
    getter = _fn("get_steam_token_balance")
    if not getter or not steam:
        return 0
    try:
        return int(getter(steam) or 0)
    except Exception:
        return 0


def _shop_price(species):
    try:
        from primeval_panels import buy_cost

        return max(MIN_PRICE, int(buy_cost(species) or 2))
    except Exception:
        return 2


def _species_tier(species):
    tiers = _fn("DINO_TIERS") or {}
    name = str(species or "").strip()
    try:
        return max(1, min(6, int(tiers.get(name, 1) or 1)))
    except Exception:
        return 1


def _prime_have(slot):
    try:
        have = int((slot or {}).get("primeHave") or 0)
    except Exception:
        have = 0
    flags = str((slot or {}).get("primeFlags") or "")
    if have <= 0 and flags:
        have = flags.count("1")
    return have


def _is_prime(slot):
    return bool((slot or {}).get("primeElder")) or _prime_have(slot) >= 5


def _growth_frac(slot):
    try:
        growth = float((slot or {}).get("growth") or 0)
    except Exception:
        return 0.0
    if growth > 1.5:
        growth = growth / 100.0
    return max(0.0, min(1.0, growth))


def card_weight(slot):
    growth_pts = _growth_frac(slot) * 10.0
    have = _prime_have(slot)
    prime_pts = min(6.0, have * 0.6)
    if _is_prime(slot):
        prime_pts += 4.0
    tier_pts = float(_species_tier(slot.get("species") or slot.get("classPath"))) * 2.0
    return growth_pts + prime_pts + tier_pts


def win_chance_percent(slots, tokens=0, target=""):
    slots = [row for row in (slots or []) if row]
    if len(slots) < GAMBLE_MIN_CARDS:
        return 0.0
    weights = sum(card_weight(row) for row in slots)
    extra = max(0, len(slots) - GAMBLE_MIN_CARDS)
    quality = min(10.0, max(0.0, (weights - 14.0) * 0.22))
    count = min(5.0, extra * 1.6)
    try:
        paid = max(0, int(tokens or 0))
    except Exception:
        paid = 0
    tok = min(6.0, (paid ** 0.62) * 0.72) if paid else 0.0
    tax = max(0.0, (_species_tier(target) - 1) * 0.85) if target else 0.0
    chance = 3.5 + quality + count + tok - tax
    return round(min(GAMBLE_MAX_CHANCE, max(GAMBLE_FLOOR_CHANCE, chance)), 1)


def _card_summary(slot):
    species = str((slot or {}).get("species") or "?")
    growth = _pct(slot.get("growth") if slot else 0)
    tier = _species_tier(species)
    prime = "prime" if _is_prime(slot) else "not prime"
    return f"**{species}** {growth} · T{tier} · {prime}"


def _card_audit_line(slot):
    slot = slot or {}
    return (
        f"{slot.get('id') or '?'}:{slot.get('species') or '?'} "
        f"{_pct(slot.get('growth'))} {'prime' if _is_prime(slot) else 'np'}"
    )


async def _audit(bot, verb, user=None, steam="", ok=True, **fields):
    try:
        import primeval_vault_audit

        await primeval_vault_audit.note(bot, verb, user, steam=steam, ok=ok, **fields)
    except Exception as exc:
        print(f"[TRADE] audit failed: {exc}")


async def trade_channel(guild, bot=None):
    import primeval_posts

    if guild is not None:
        found = guild.get_channel(TRADE_CHANNEL_ID)
        if found is not None:
            return found
    if bot is not None:
        found = bot.get_channel(TRADE_CHANNEL_ID)
        if found is not None:
            return found
        try:
            found = await bot.fetch_channel(TRADE_CHANNEL_ID)
            if found is not None:
                return found
        except Exception:
            pass
    return primeval_posts.find_named_channel(guild, TRADE_CHANNEL_NAMES)


def _pct(value):
    try:
        return f"{float(value or 0) * 100:.0f}%"
    except Exception:
        return "?"


def _mutation_map(raw):
    mapped = {}
    if isinstance(raw, dict):
        items = raw.items()
    elif isinstance(raw, (list, tuple)):
        items = []
        for i, val in enumerate(raw, start=1):
            items.append((f"MutationSlot{i}", val))
    else:
        items = []
        for piece in str(raw or "").split("|"):
            piece = piece.strip()
            if not piece:
                continue
            if "=" in piece:
                key, val = piece.split("=", 1)
                items.append((key, val))
            else:
                items.append((f"MutationSlot{len(items) + 1}", piece))
    for key, val in items:
        val = str(val or "").strip()
        if not val or val.lower() == "none":
            continue
        key = str(key or "").strip()
        if key.isdigit():
            num = int(key)
            key = f"MutationSlot{num + 1 if num == 0 else num}"
        mapped[key] = val
    return mapped


def _slot_mutation_groups(slot):
    mapped = _mutation_map((slot or {}).get("mutations"))
    life_labels = ("Juvenile", "Sub-adult", "Adult", "Prime")
    life = []
    for i, label in enumerate(life_labels, start=1):
        name = mapped.get(f"MutationSlot{i}") or mapped.get(str(i)) or ""
        if name:
            life.append((label, name))
    inherited = []
    for i in range(1, 5):
        name = (
            mapped.get(f"ParentMutationSlot{i}")
            or mapped.get(f"ElderMutationSlot{i}A")
            or mapped.get(f"ElderMutationSlot{i}B")
            or ""
        )
        if name:
            inherited.append((str(i), name))
    extras = []
    claimed = {name for _, name in life} | {name for _, name in inherited}
    for key, name in mapped.items():
        if name in claimed:
            continue
        if key.startswith("MutationSlot") or key.startswith("ParentMutation") or key.startswith("ElderMutation"):
            continue
        extras.append(name)
        claimed.add(name)
    return life, inherited, extras


def _format_card_mutations(slot):
    life, inherited, extras = _slot_mutation_groups(slot)
    if not life and not inherited and not extras:
        return "**Mutations** none"
    lines = []
    if life:
        lines.append("**Mutations (this life)** " + " · ".join(f"{label} {name}" for label, name in life))
    if inherited:
        lines.append("**Inherited** " + " · ".join(name for _, name in inherited))
    if extras:
        lines.append("**Other mutations** " + " · ".join(extras[:8]))
    return "\n".join(lines)


def _mut_names_from_slot(slot):
    mapped = _mutation_map(slot.get("mutations"))
    names = []
    seen = set()
    for i in range(1, 5):
        for key in (
            f"MutationSlot{i}",
            f"ParentMutationSlot{i}",
            f"ElderMutationSlot{i}A",
            f"ElderMutationSlot{i}B",
        ):
            val = mapped.get(key)
            if val and val not in seen:
                names.append(val)
                seen.add(val)
                break
    for val in mapped.values():
        if val not in seen:
            names.append(val)
            seen.add(val)
    return names


def _fill_four_muts(species, female, have):
    from primeval_species import mutation_choices, mutation_fname

    names = []
    seen = set()
    for raw in have or []:
        label = mutation_fname(raw) or str(raw).strip()
        if label and label not in seen:
            names.append(label)
            seen.add(label)
    pool = []
    for slot in (1, 2):
        for label in mutation_choices(species, slot, female) or []:
            fname = mutation_fname(label) or label
            if fname and fname not in seen and fname not in pool:
                pool.append(fname)
    random.shuffle(pool)
    for fname in pool:
        if len(names) >= 4:
            break
        names.append(fname)
        seen.add(fname)
    while len(names) < 4 and names:
        names.append(names[-1])
    return names[:4]


def build_entomb_prize(steam, species, female, life, inherited, test=False):
    """Same vault payload a gamble win writes — 60% + 75% hunger + life + inherit."""
    prize = {
        "version": 7,
        "kind": "vault",
        "id": _new_id("entest" if test else "en"),
        "source": "gamble",
        "stayPut": True,
        "steam": str(steam),
        "classPath": species,
        "species": species,
        "growth": ENTOMB_GROWTH,
        "hunger": 0.75,
        "gender": "Female" if female else "Male",
        "female": bool(female),
        "genderNum": 1 if female else 0,
        "skin": "",
        "skinData": "",
        "primeElder": False,
        "primeFlags": "",
        "primeHave": 0,
        "mutations": _entomb_packed(life, inherited),
        "unlocks": "",
        "elderStacks": 1,
        "capturedAt": int(time.time()),
    }
    if test:
        prize["testGrant"] = True
    return prize


def _clean_mut_name(name):
    from primeval_species import mutation_fname

    return str(mutation_fname(name) or name or "").replace("|", "").replace("=", "").strip()


def _entomb_packed(life, inherited):
    from primeval_species import pack_mutations

    parts = []
    life = list(life or [])
    if len(life) >= 2:
        packed = pack_mutations(life[0], life[1])
        if packed:
            parts.append(packed)
    for i, name in enumerate(list(inherited or [])[:4], start=1):
        clean = _clean_mut_name(name)
        if not clean:
            continue
        parts.append(f"ParentMutationSlot{i}={clean}")
        parts.append(f"ElderMutationSlot{i}A={clean}")
    return "|".join(parts)


def _valid_life_muts(species, female, life):
    from primeval_species import mutation_allowed, mutation_fname

    names = [str(name or "").strip() for name in (life or []) if str(name or "").strip()]
    if len(names) != 2:
        return False
    if mutation_fname(names[0]) == mutation_fname(names[1]):
        return False
    return mutation_allowed(species, 1, female, names[0]) and mutation_allowed(
        species, 2, female, names[1]
    )


def _valid_inherited_muts(species, female, inherited):
    from primeval_species import mutation_allowed, mutation_fname

    names = [str(name or "").strip() for name in (inherited or []) if str(name or "").strip()]
    if len(names) != 4:
        return False
    fnames = [mutation_fname(name) for name in names]
    if len(set(fnames)) < 4:
        return False
    for i, name in enumerate(names, start=1):
        pool = 1 if i == 1 else 2
        if not mutation_allowed(species, pool, female, name):
            return False
    return True


def _valid_gamble_muts(species, female, life, inherited):
    return _valid_life_muts(species, female, life) and _valid_inherited_muts(
        species, female, inherited
    )


def _mut_text(raw):
    text = str(raw or "").strip()
    if not text:
        return "none saved"
    parts = []
    for piece in text.split("|"):
        piece = piece.strip()
        if not piece:
            continue
        if "=" in piece:
            key, val = piece.split("=", 1)
            key = key.replace("MutationSlot", "slot ")
            if str(val).lower() in ("", "none"):
                continue
            parts.append(f"{key} {val}")
        else:
            parts.append(piece)
    return " · ".join(parts[:8]) if parts else text[:180]


def _skin_text(slot):
    if str(slot.get("skinData") or "").strip():
        name = str(slot.get("skin") or "").strip()
        return f"{name} (custom saved)" if name else "custom skin saved"
    name = str(slot.get("skin") or "").strip()
    if name:
        return name
    if slot.get("skinId") not in (None, ""):
        return f"id {slot.get('skinId')}"
    return "default / unknown"


def card_embed(slot, title=None, footer="", seller=None, price=None, note=""):
    from primeval_panels import _species_emoji

    slot = slot or {}
    species = str(slot.get("species") or slot.get("classPath") or "?")
    gender = slot.get("gender") or (
        "Female" if slot.get("female") is True else "Male" if slot.get("female") is False else "?"
    )
    source = str(slot.get("source") or "store")
    growth = _pct(slot.get("growth"))
    have = _prime_have(slot)
    flags = str(slot.get("primeFlags") or "")
    if _is_prime(slot):
        prime = f"eligible · {have}/10"
    elif have:
        prime = f"{have}/10"
    else:
        prime = "not prime"
    if flags:
        prime = f"{prime} `{flags}`"
    mark = _species_emoji(species)
    headline = title or f"{mark} {species}"
    desc = (
        f"**{gender} {species}** · **{growth}** · {source}\n"
        f"**Prime** {prime}\n"
        f"{_format_card_mutations(slot)}\n"
        f"**Skin** {_skin_text(slot)}\n"
        f"**Unlocks** {str(slot.get('unlocks') or 'none')[:120]}\n"
    )
    hunger = slot.get("hunger")
    if hunger not in (None, ""):
        desc += f"**Hunger** {_pct(hunger) if float(hunger or 0) <= 1.5 else str(hunger)}\n"
    if slot.get("elderStacks"):
        desc += f"**Elder stacks** {slot.get('elderStacks')} — grow to elder to pick 4 more\n"
    if seller:
        desc += f"\nSeller {seller}"
    if note:
        desc += f"\n{note}"
    if price is not None:
        desc += f"\n**{int(price)} tokens**"
    embed = discord.Embed(
        title=str(headline)[:256],
        description=desc[:4096],
        color=discord.Color.gold() if source == "buy" else discord.Color.green(),
    )
    sid = str(slot.get("id") or "")
    if sid:
        embed.set_footer(text=(footer + (" · " if footer else "") + f"slot {sid}")[:200])
    elif footer:
        embed.set_footer(text=footer[:200])
    return embed


async def _isle():
    from primeval_panels import ISLE_STORED_DIR, ISLE_VAULT_DIR, _isle_file, _parse_index_lines

    return _isle_file, ISLE_VAULT_DIR, ISLE_STORED_DIR, _parse_index_lines


async def read_full_slot(steam, slot_id):
    _isle_file, vault_dir, _stored, parse_index = await _isle()
    slot_id = str(slot_id or "").strip()
    path = vault_dir + "/" + str(steam) + "_" + slot_id + ".json"
    status, text = await _isle_file("GET", path)
    if status == 200 and str(text or "").strip().startswith("{"):
        try:
            data = json.loads(text)
        except Exception:
            data = None
        if isinstance(data, dict):
            data.setdefault("id", slot_id)
            return data
    from primeval_panels import _read_vault_json

    raw = await _read_vault_json(steam)
    if raw:
        try:
            data = json.loads(raw)
        except Exception:
            data = None
        if isinstance(data, dict) and str(data.get("id") or "vault") == slot_id:
            data.setdefault("id", slot_id)
            return data
    status, raw = await _isle_file("GET", vault_dir + "/" + str(steam) + ".index.ndjson")
    for row in parse_index(raw if status == 200 else ""):
        if str(row.get("id") or "") == slot_id:
            return row
    return None


async def _index_slots(steam):
    from primeval_panels import _read_vault_slots

    return list(await _read_vault_slots(steam) or [])


async def _write_index(steam, slots):
    _isle_file, vault_dir, _stored, _parse = await _isle()
    body = "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in slots)
    status, text = await _isle_file("POST", vault_dir + "/" + str(steam) + ".index.ndjson", body)
    return status in (200, 204), text


def _index_row(slot):
    life, inherited, extras = _slot_mutation_groups(slot)
    names = [name for _, name in life] + [name for _, name in inherited] + extras
    return {
        "id": slot.get("id"),
        "species": slot.get("species") or slot.get("classPath"),
        "gender": slot.get("gender"),
        "female": slot.get("female"),
        "growth": slot.get("growth"),
        "source": slot.get("source") or "store",
        "primeElder": bool(slot.get("primeElder")),
        "primeHave": slot.get("primeHave"),
        "primeFlags": slot.get("primeFlags") or "",
        "mutations": slot.get("mutations") or "",
        "hunger": slot.get("hunger"),
        "mutSummary": " · ".join(names[:6]),
        "capturedAt": slot.get("capturedAt") or int(time.time()),
    }


async def write_slot(steam, slot):
    _isle_file, vault_dir, stored_dir, _parse = await _isle()
    slot = dict(slot)
    slot["steam"] = str(steam)
    slot.setdefault("kind", "vault")
    slot.setdefault("version", 7)
    body = json.dumps(slot, separators=(",", ":")) + "\n"
    slot_id = str(slot.get("id") or "vault")
    status, text = await _isle_file("POST", vault_dir + "/" + str(steam) + "_" + slot_id + ".json", body)
    if status not in (200, 204):
        return False, text or f"write {status}"
    await _isle_file("POST", vault_dir + "/" + str(steam) + ".json", body)
    await _isle_file("POST", stored_dir + "/" + str(steam) + ".json", body)
    slots = [row for row in await _index_slots(steam) if str(row.get("id") or "") != slot_id]
    slots.append(_index_row(slot))
    ok, err = await _write_index(steam, slots)
    if not ok:
        return False, err or "index write failed"
    return True, slot_id


async def remove_slot(steam, slot_id):
    _isle_file, vault_dir, stored_dir, _parse = await _isle()
    slots = [row for row in await _index_slots(steam) if str(row.get("id") or "") != str(slot_id)]
    ok, err = await _write_index(steam, slots)
    if not ok:
        return False, err
    if slots:
        leftover = await read_full_slot(steam, slots[-1].get("id"))
        body = json.dumps(leftover or slots[-1], separators=(",", ":")) + "\n"
    else:
        body = "{}\n"
    await _isle_file("POST", vault_dir + "/" + str(steam) + ".json", body)
    await _isle_file("POST", stored_dir + "/" + str(steam) + ".json", body)
    return True, ""


def _listed_slot_ids(steam):
    steam = str(steam or "")
    found = set()
    for row in (_load().get("listings") or {}).values():
        if str(row.get("seller_steam") or "") == steam and row.get("open") is not False:
            found.add(str(row.get("slot_id") or ""))
    return found


def _held_slot_ids(steam):
    steam = str(steam or "")
    found = set(_listed_slot_ids(steam))
    for row in (_load().get("listing_offers") or {}).values():
        if not row or row.get("open") is False:
            continue
        if str(row.get("from_steam") or "") == steam and str(row.get("slot_id") or ""):
            found.add(str(row.get("slot_id")))
    return found


def listing_posted_view(listing_id, mode=MODE_TOKENS):
    if str(mode or MODE_TOKENS) == MODE_OFFERS:
        return PostedOffersView(listing_id)
    return PostedListingView(listing_id)


async def _need_linked(interaction):
    steam = _steam_for(interaction.user.id)
    if steam:
        return steam
    msg = "Link Steam on the member panel first."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)
    return ""


class SlotPickSelect(ui.Select):
    def __init__(self, slots, action, listed=None, extra=""):
        listed = listed or set()
        options = []
        for row in slots[-25:]:
            sid = str(row.get("id") or "").strip()
            if not sid:
                continue
            species = str(row.get("species") or "?")
            growth = _pct(row.get("growth"))
            mark = "LISTED · " if sid in listed else ""
            muts = str(row.get("mutSummary") or "").strip()
            if not muts:
                life, inherited, extras = _slot_mutation_groups(row)
                muts = " · ".join(
                    [name for _, name in life] + [name for _, name in inherited] + extras
                )
            desc = f"{growth} · {row.get('source') or 'store'}"
            if muts:
                desc = f"{desc} · {muts}"
            else:
                desc = f"{desc} · no mutations"
            options.append(
                discord.SelectOption(
                    label=f"{mark}{species}"[:100],
                    description=desc[:100],
                    value=sid[:100],
                )
            )
        if not options:
            options.append(discord.SelectOption(label="No vault cards", value="none"))
        if action == "gamble":
            take = min(GAMBLE_MAX_CARDS, max(GAMBLE_MIN_CARDS, len(options)))
            super().__init__(
                placeholder="Stake at least 2 cards…",
                options=options,
                min_values=min(GAMBLE_MIN_CARDS, len(options)),
                max_values=take,
            )
        else:
            super().__init__(placeholder="Pick a vault card…", options=options)
        self.action = action
        self.extra = str(extra or "")

    async def callback(self, interaction: discord.Interaction):
        if self.action == "gamble":
            picks = [str(v) for v in self.values if v and v != "none"]
            if len(picks) < GAMBLE_MIN_CARDS:
                await interaction.response.send_message(
                    f"Stake at least **{GAMBLE_MIN_CARDS}** cards.",
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(
                "What species are you gambling **for**? Win is a **60% Entombed** of that dino.",
                view=GambleSpeciesView(picks),
                ephemeral=True,
            )
            return
        choice = self.values[0]
        if choice == "none":
            await interaction.response.send_message("No cards in your vault.", ephemeral=True)
            return
        if self.action == "list":
            await interaction.response.send_message(
                "How should this card post?",
                view=ListModeView(choice),
                ephemeral=True,
            )
            return
        if self.action == "offer_card":
            await submit_card_offer(interaction, self.extra, choice)
            return
        if self.action == "trade":
            await interaction.response.send_message(
                "Who should receive this card?",
                view=TradeTargetView(choice),
                ephemeral=True,
            )
            return
        steam = _steam_for(interaction.user.id)
        slot = await read_full_slot(steam, choice)
        if not slot:
            await interaction.response.send_message("That card is gone.", ephemeral=True)
            return
        await interaction.response.send_message(embed=card_embed(slot), ephemeral=True)


class SlotPickView(ui.View):
    def __init__(self, slots, action, listed=None, extra=""):
        super().__init__(timeout=180)
        self.add_item(SlotPickSelect(slots, action, listed, extra=extra))


class ListModeView(ui.View):
    def __init__(self, slot_id):
        super().__init__(timeout=180)
        self.slot_id = slot_id

    @ui.button(label="Looking for offers", emoji="👀", style=discord.ButtonStyle.primary)
    async def offers(self, interaction: discord.Interaction, button: ui.Button):
        await start_listing(interaction, self.slot_id, mode=MODE_OFFERS)

    @ui.button(label="Buy with tokens only", emoji="🪙", style=discord.ButtonStyle.success)
    async def tokens(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(ListPriceModal(self.slot_id))


class ListPriceModal(ui.Modal, title="Buy with tokens only"):
    def __init__(self, slot_id):
        super().__init__()
        self.slot_id = slot_id
        self.price = ui.TextInput(
            label=f"Price in tokens ({MIN_PRICE}–{MAX_PRICE})",
            placeholder="4",
            max_length=3,
            required=True,
        )
        self.add_item(self.price)

    async def on_submit(self, interaction: discord.Interaction):
        await start_listing(interaction, self.slot_id, str(self.price.value), mode=MODE_TOKENS)


class TradeTargetView(ui.View):
    def __init__(self, slot_id):
        super().__init__(timeout=180)
        self.slot_id = slot_id
        self.add_item(TradeUserSelect(slot_id))


class TradeUserSelect(ui.UserSelect):
    def __init__(self, slot_id):
        super().__init__(placeholder="Send this card to…", min_values=1, max_values=1)
        self.slot_id = slot_id

    async def callback(self, interaction: discord.Interaction):
        await start_offer(interaction, self.slot_id, self.values[0])


def _gamble_species_options():
    from primeval_panels import _shop_species, buy_cost

    options = []
    for dino, tier in _shop_species():
        options.append(
            discord.SelectOption(
                label=f"{dino} (Tier {tier})"[:100],
                description=f"60% · juvie+sub + 4 inherit · shop {buy_cost(dino)}"[:100],
                value=dino,
            )
        )
    if not options:
        options.append(discord.SelectOption(label="No species", value="none"))
    return options[:25]


def _gamble_mut_options(species, female, kind, slot_n, exclude=None):
    from primeval_panels import _mutation_options

    pool = 1 if slot_n == 1 else 2
    options = _mutation_options(species, pool, female, exclude=exclude)
    if kind == "life":
        labels = {
            1: "This life · juvenile slot 1",
            2: "This life · sub-adult slot 2",
        }
    else:
        labels = {
            1: "Inherited 1 · juvenile",
            2: "Inherited 2 · sub-adult",
            3: "Inherited 3 · adult",
            4: "Inherited 4 · prime",
        }
    out = []
    for opt in options:
        out.append(
            discord.SelectOption(
                label=opt.label,
                value=opt.value,
                description=labels.get(slot_n, kind)[:100],
            )
        )
    if not out:
        out.append(discord.SelectOption(label="No unused mutations left", value="none"))
    return out[:25]


class GambleSpeciesSelect(ui.Select):
    def __init__(self, slot_ids, grant_steam=""):
        super().__init__(
            placeholder="Gamble toward this species…",
            options=_gamble_species_options(),
        )
        self.slot_ids = list(slot_ids)
        self.grant_steam = str(grant_steam or "")

    async def callback(self, interaction: discord.Interaction):
        species = self.values[0]
        if species == "none":
            await interaction.response.send_message("No species list loaded.", ephemeral=True)
            return
        lead = "Test grant" if self.grant_steam else "Gambling for"
        await interaction.response.send_message(
            f"{lead} **{species}**. Pick gender for the Entombed prize.",
            view=GambleGenderView(self.slot_ids, species, grant_steam=self.grant_steam),
            ephemeral=True,
        )


class GambleSpeciesView(ui.View):
    def __init__(self, slot_ids, grant_steam=""):
        super().__init__(timeout=180)
        self.add_item(GambleSpeciesSelect(slot_ids, grant_steam=grant_steam))


class GambleGenderView(ui.View):
    def __init__(self, slot_ids, species, grant_steam=""):
        super().__init__(timeout=180)
        self.slot_ids = list(slot_ids)
        self.species = species
        self.grant_steam = str(grant_steam or "")

    async def _pick(self, interaction, female):
        gender = "Female" if female else "Male"
        await interaction.response.send_message(
            f"**{gender} {self.species}** — pick **juvenile mutation** (this life, slot 1). "
            "You spawn at 60%, so this and sub-adult must be chosen now.",
            view=GambleMutView(
                self.slot_ids,
                self.species,
                female,
                "life",
                1,
                [],
                [],
                grant_steam=self.grant_steam,
            ),
            ephemeral=True,
        )

    @ui.button(label="Male", style=discord.ButtonStyle.primary)
    async def male(self, interaction: discord.Interaction, button: ui.Button):
        await self._pick(interaction, False)

    @ui.button(label="Female", style=discord.ButtonStyle.primary)
    async def female(self, interaction: discord.Interaction, button: ui.Button):
        await self._pick(interaction, True)


class GambleMutSelect(ui.Select):
    def __init__(self, slot_ids, species, female, kind, slot_n, life, inherited, grant_steam=""):
        self.slot_ids = list(slot_ids)
        self.species = species
        self.female = female
        self.kind = kind
        self.slot_n = slot_n
        self.life = list(life)
        self.inherited = list(inherited)
        self.grant_steam = str(grant_steam or "")
        exclude = list(life) + list(inherited)
        placeholder = (
            "Juvenile mutation (this life)…"
            if kind == "life" and slot_n == 1
            else "Sub-adult mutation (this life)…"
            if kind == "life"
            else f"Inherited mutation {slot_n} of 4…"
        )
        super().__init__(
            placeholder=placeholder,
            options=_gamble_mut_options(species, female, kind, slot_n, exclude=exclude),
        )

    async def callback(self, interaction: discord.Interaction):
        choice = self.values[0]
        if choice in ("none", "None"):
            await interaction.response.send_message(
                "No unused mutations left for that slot. Start the pick over.",
                ephemeral=True,
            )
            return
        life = list(self.life)
        inherited = list(self.inherited)
        if self.kind == "life":
            life.append(choice)
            if self.slot_n < 2:
                await interaction.response.send_message(
                    f"Juvenile **{choice}**. Now pick **sub-adult mutation** (this life, slot 2).",
                    view=GambleMutView(
                        self.slot_ids,
                        self.species,
                        self.female,
                        "life",
                        2,
                        life,
                        inherited,
                        grant_steam=self.grant_steam,
                    ),
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(
                f"Sub-adult **{choice}**. Now pick **inherited mutation 1** of 4.",
                view=GambleMutView(
                    self.slot_ids,
                    self.species,
                    self.female,
                    "inherit",
                    1,
                    life,
                    inherited,
                    grant_steam=self.grant_steam,
                ),
                ephemeral=True,
            )
            return
        inherited.append(choice)
        if self.slot_n < 4:
            await interaction.response.send_message(
                f"Locked inherited **{choice}**. Now inherited mutation **{self.slot_n + 1}**.",
                view=GambleMutView(
                    self.slot_ids,
                    self.species,
                    self.female,
                    "inherit",
                    self.slot_n + 1,
                    life,
                    inherited,
                    grant_steam=self.grant_steam,
                ),
                ephemeral=True,
            )
            return
        if self.grant_steam:
            await grant_test_entomb(
                interaction, self.grant_steam, self.species, self.female, life, inherited
            )
            return
        await interaction.response.send_modal(
            GambleTokenModal(self.slot_ids, self.species, self.female, life, inherited)
        )


class GambleMutView(ui.View):
    def __init__(self, slot_ids, species, female, kind, slot_n, life, inherited, grant_steam=""):
        super().__init__(timeout=180)
        self.add_item(
            GambleMutSelect(
                slot_ids,
                species,
                female,
                kind,
                slot_n,
                life,
                inherited,
                grant_steam=grant_steam,
            )
        )


class GambleTokenModal(ui.Modal, title="Tokens to add (optional)"):
    def __init__(self, slot_ids, species, female, life, inherited):
        super().__init__()
        self.slot_ids = list(slot_ids)
        self.species = species
        self.female = female
        self.life = list(life)
        self.inherited = list(inherited)
        self.amount = ui.TextInput(
            label=f"Tokens to add (0–{GAMBLE_MAX_TOKENS})",
            placeholder="0",
            required=False,
            max_length=3,
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.amount.value or "0").strip() or "0"
        try:
            tokens = int(raw)
        except Exception:
            await interaction.response.send_message("Tokens must be a whole number.", ephemeral=True)
            return
        if tokens < 0 or tokens > GAMBLE_MAX_TOKENS:
            await interaction.response.send_message(
                f"Tokens must be 0–{GAMBLE_MAX_TOKENS}.",
                ephemeral=True,
            )
            return
        await show_gamble_confirm(
            interaction,
            self.slot_ids,
            self.species,
            self.female,
            self.life,
            self.inherited,
            tokens,
        )


class GambleGoView(ui.View):
    def __init__(self, slot_ids, species, female, life, inherited, tokens):
        super().__init__(timeout=90)
        self.slot_ids = list(slot_ids)
        self.species = species
        self.female = female
        self.life = list(life)
        self.inherited = list(inherited)
        self.tokens = int(tokens)
        self._rolling = False

    @ui.button(label="Roll — lose stakes if you miss", emoji="🎲", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        if self._rolling:
            await interaction.response.send_message("Already rolling.", ephemeral=True)
            return
        self._rolling = True
        await run_gamble(
            interaction,
            self.slot_ids,
            self.species,
            self.female,
            self.life,
            self.inherited,
            self.tokens,
        )

    @ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.edit_message(content="Gamble cancelled.", view=None)


class OfferView(ui.View):
    def __init__(self, offer_id):
        super().__init__(timeout=600)
        self.offer_id = offer_id

    @ui.button(label="Accept card", emoji="✅", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: ui.Button):
        await accept_offer(interaction, self.offer_id)

    @ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: ui.Button):
        await decline_offer(interaction, self.offer_id)


class TradeBuyButton(ui.Button):
    def __init__(self, listing_id):
        super().__init__(
            label="Buy",
            emoji="🪙",
            style=discord.ButtonStyle.success,
            custom_id=f"pi_tr_b:{listing_id}",
        )
        self.listing_id = listing_id

    async def callback(self, interaction: discord.Interaction):
        await buy_listing(interaction, self.listing_id)


class TradeOfferTokensButton(ui.Button):
    def __init__(self, listing_id):
        super().__init__(
            label="Offer tokens",
            emoji="🪙",
            style=discord.ButtonStyle.success,
            custom_id=f"pi_tr_ot:{listing_id}",
        )
        self.listing_id = listing_id

    async def callback(self, interaction: discord.Interaction):
        await begin_token_offer(interaction, self.listing_id)


class TradeOfferCardButton(ui.Button):
    def __init__(self, listing_id):
        super().__init__(
            label="Offer a card",
            emoji="🃏",
            style=discord.ButtonStyle.primary,
            custom_id=f"pi_tr_oc:{listing_id}",
        )
        self.listing_id = listing_id

    async def callback(self, interaction: discord.Interaction):
        await begin_card_offer(interaction, self.listing_id)


class PostedOffersView(ui.View):
    def __init__(self, listing_id):
        super().__init__(timeout=None)
        self.add_item(TradeOfferTokensButton(listing_id))
        self.add_item(TradeOfferCardButton(listing_id))
        self.add_item(TradeUnlistButton(listing_id))


class ListingOfferView(ui.View):
    def __init__(self, offer_id):
        super().__init__(timeout=600)
        self.offer_id = offer_id

    @ui.button(label="Accept offer", emoji="✅", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: ui.Button):
        await accept_listing_offer(interaction, self.offer_id)

    @ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: ui.Button):
        await decline_listing_offer(interaction, self.offer_id)


class OfferTokenModal(ui.Modal, title="Offer tokens"):
    def __init__(self, listing_id):
        super().__init__()
        self.listing_id = listing_id
        self.amount = ui.TextInput(
            label=f"Tokens to offer ({MIN_PRICE}–{MAX_PRICE})",
            placeholder="8",
            max_length=3,
            required=True,
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        await submit_token_offer(interaction, self.listing_id, str(self.amount.value))


class TradeUnlistButton(ui.Button):
    def __init__(self, listing_id):
        super().__init__(
            label="Unlist",
            emoji="🛑",
            style=discord.ButtonStyle.secondary,
            custom_id=f"pi_tr_u:{listing_id}",
        )
        self.listing_id = listing_id

    async def callback(self, interaction: discord.Interaction):
        await unlist_listing(interaction, self.listing_id)


class PostedListingView(ui.View):
    def __init__(self, listing_id):
        super().__init__(timeout=None)
        self.add_item(TradeBuyButton(listing_id))
        self.add_item(TradeUnlistButton(listing_id))


class TradePanelView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="My cards", emoji="🃏", style=discord.ButtonStyle.primary, custom_id="pi_tr_cards", row=0)
    async def cards(self, interaction: discord.Interaction, button: ui.Button):
        await show_my_cards(interaction)

    @ui.button(label="Post card", emoji="🏷️", style=discord.ButtonStyle.success, custom_id="pi_tr_list", row=0)
    async def list_card(self, interaction: discord.Interaction, button: ui.Button):
        await pick_slot(interaction, "list")

    @ui.button(label="Send card", emoji="🤝", style=discord.ButtonStyle.primary, custom_id="pi_tr_trade", row=0)
    async def trade_card(self, interaction: discord.Interaction, button: ui.Button):
        await pick_slot(interaction, "trade")

    @ui.button(label="Gamble", emoji="🎲", style=discord.ButtonStyle.danger, custom_id="pi_tr_gamble", row=0)
    async def gamble(self, interaction: discord.Interaction, button: ui.Button):
        await pick_slot(interaction, "gamble")

    @ui.button(label="How it works", emoji="📖", style=discord.ButtonStyle.secondary, custom_id="pi_tr_how", row=0)
    async def how(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message(HOW_IT_WORKS, ephemeral=True)


def trade_embed():
    return discord.Embed(
        title="🃏 Fallen Earth | Dino trade & gamble",
        description=(
            "Your vault dinos are **cards**. Post them here so people can see them, "
            "send one privately, or gamble two or more.\n\n"
            "🃏 **My cards** — every stored slot as a stat card\n"
            "🏷️ **Post card** — **looking for offers** or **buy with tokens only**\n"
            "🤝 **Send card** — private trade to one linked player\n"
            "🎲 **Gamble** — stake **2+ cards**, pick the species, **juvenile + sub-adult** "
            "mutations, then **4 inherited**. Win = **60% Entombed** of that species. "
            "Miss = those cards and tokens are gone.\n"
            "House wins most rolls. Better cards (tier, growth, prime), more cards, "
            "and tokens only improve the chance — cap is **20%**."
        ),
        color=discord.Color.gold(),
    )


async def pick_slot(interaction, action, extra=""):
    steam = await _need_linked(interaction)
    if not steam:
        return
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)
    slots = await _index_slots(steam)
    if not slots:
        await interaction.followup.send(
            "Your vault is empty. Buy or store a dino on the member panel first.",
            ephemeral=True,
        )
        return
    listed = _held_slot_ids(steam)
    if action != "view":
        open_slots = [row for row in slots if str(row.get("id") or "") not in listed]
        if action in ("list", "trade", "gamble", "offer_card") and not open_slots:
            await interaction.followup.send(
                "Every card you have is already listed or offered. Unlist one first.",
                ephemeral=True,
            )
            return
        slots = open_slots if action in ("list", "trade", "gamble", "offer_card") else slots
    if action == "gamble" and len(slots) < GAMBLE_MIN_CARDS:
        await interaction.followup.send(
            f"Gamble needs at least **{GAMBLE_MIN_CARDS}** unlisted cards.",
            ephemeral=True,
        )
        return
    prompt = "Pick a card."
    if action == "gamble":
        prompt = f"Stake **{GAMBLE_MIN_CARDS}–{GAMBLE_MAX_CARDS}** cards. Higher tier, growth, and prime help your odds."
    elif action == "list":
        prompt = "Pick the card to post in the trade channel."
    elif action == "offer_card":
        prompt = "Pick a card to offer on that listing."
    await interaction.followup.send(
        prompt,
        view=SlotPickView(slots, action, listed, extra=extra),
        ephemeral=True,
    )


async def show_my_cards(interaction):
    steam = await _need_linked(interaction)
    if not steam:
        return
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)
    slots = await _index_slots(steam)
    if not slots:
        await interaction.followup.send("No cards yet. Buy or store a dino first.", ephemeral=True)
        return
    listed = _listed_slot_ids(steam)
    sent = 0
    for row in slots[-8:]:
        full = await read_full_slot(steam, row.get("id")) or row
        extra = "LISTED" if str(full.get("id") or "") in listed else f"{_balance(steam)} tokens"
        await interaction.followup.send(
            embed=card_embed(full, footer=extra),
            ephemeral=True,
        )
        sent += 1
    if len(slots) > 8:
        await interaction.followup.send(
            f"Showing the last **{sent}** of **{len(slots)}** cards.",
            ephemeral=True,
        )


async def start_listing(interaction, slot_id, price_raw=None, mode=MODE_TOKENS):
    steam = await _need_linked(interaction)
    if not steam:
        return
    mode = MODE_OFFERS if str(mode or "") == MODE_OFFERS else MODE_TOKENS
    price = 0
    if mode == MODE_TOKENS:
        try:
            price = int(str(price_raw).strip())
        except Exception:
            await interaction.response.send_message("Price must be a whole number.", ephemeral=True)
            return
        if price < MIN_PRICE or price > MAX_PRICE:
            await interaction.response.send_message(
                f"Price must be {MIN_PRICE}–{MAX_PRICE} tokens.",
                ephemeral=True,
            )
            return
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)
    if slot_id in _held_slot_ids(steam):
        await _audit(
            interaction.client, "list", interaction.user, steam=steam, ok=False,
            slot=slot_id, msg="Already listed or offered",
        )
        await interaction.followup.send("That card is already listed or offered.", ephemeral=True)
        return
    slot = await read_full_slot(steam, slot_id)
    if not slot:
        await _audit(
            interaction.client, "list", interaction.user, steam=steam, ok=False,
            slot=slot_id, msg="Vault slot missing on list",
        )
        await interaction.followup.send("Could not read that vault slot.", ephemeral=True)
        return
    channel = await trade_channel(interaction.guild, interaction.client)
    if channel is None:
        await _audit(
            interaction.client, "list", interaction.user, steam=steam, ok=False,
            slot=slot_id, species=slot.get("species"),
            msg="Trade channel missing",
        )
        await interaction.followup.send(
            "Could not find the trade channel. Ask staff to check that channel id.",
            ephemeral=True,
        )
        return
    listing_id = _new_id("ls")
    seller = interaction.user.mention
    if mode == MODE_OFFERS:
        title = "👀 Looking for offers"
        note = "**Looking for offers** — offer tokens or a card"
        view = listing_posted_view(listing_id, MODE_OFFERS)
        embed = card_embed(slot, title=title, seller=seller, note=note)
    else:
        title = "🪙 Buy with tokens only"
        view = listing_posted_view(listing_id, MODE_TOKENS)
        embed = card_embed(slot, title=title, seller=seller, price=price)
    try:
        message = await channel.send(embed=embed, view=view)
    except Exception as exc:
        await _audit(
            interaction.client, "list", interaction.user, steam=steam, ok=False,
            slot=slot_id, species=slot.get("species"), tokens=price,
            extra=str(exc), msg="Listing post failed",
        )
        await interaction.followup.send(f"Could not post the listing: {exc}", ephemeral=True)
        return
    state = _load()
    state["listings"][listing_id] = {
        "open": True,
        "mode": mode,
        "seller_discord": int(interaction.user.id),
        "seller_steam": steam,
        "slot_id": str(slot_id),
        "price": price,
        "species": slot.get("species"),
        "channel_id": int(channel.id),
        "message_id": int(message.id),
        "created_at": int(time.time()),
    }
    _save(state)
    await _audit(
        interaction.client, "list", interaction.user, steam=steam, ok=True,
        extra=listing_id, species=slot.get("species"), tokens=price or None,
        slot=slot_id, cards=_card_audit_line(slot),
        msg=f"Posted {slot.get('species')} mode={mode}"
        + (f" price={price}" if mode == MODE_TOKENS else " looking for offers"),
    )
    await interaction.followup.send(
        f"Posted in {channel.mention}: {message.jump_url}",
        ephemeral=True,
    )


def _close_listing_offers(state, listing_id, reason="listing_closed"):
    for row in (state.get("listing_offers") or {}).values():
        if row.get("listing_id") == listing_id and row.get("open") is not False:
            row["open"] = False
            row["closed"] = reason


async def _close_listing(listing_id, sold=False):
    state = _load()
    row = (state.get("listings") or {}).get(listing_id)
    if not row:
        return None
    row["open"] = False
    row["sold"] = bool(sold)
    _close_listing_offers(state, listing_id, "sold" if sold else "unlisted")
    _save(state)
    return row


async def unlist_listing(interaction, listing_id):
    state = _load()
    row = (state.get("listings") or {}).get(str(listing_id) or "")
    if not row or not row.get("open"):
        await interaction.response.send_message("That listing is already gone.", ephemeral=True)
        return
    if int(row.get("seller_discord") or 0) != int(interaction.user.id):
        from primeval_panels import can_staff

        if not can_staff(interaction.user, "bot_post"):
            await interaction.response.send_message("Only the seller can unlist this.", ephemeral=True)
            return
    await _close_listing(listing_id, sold=False)
    try:
        await interaction.response.edit_message(
            content="Unlisted.",
            embed=interaction.message.embeds[0] if interaction.message.embeds else None,
            view=None,
        )
    except Exception:
        if not interaction.response.is_done():
            await interaction.response.send_message("Unlisted.", ephemeral=True)
    await _audit(
        interaction.client, "unlist", interaction.user,
        steam=row.get("seller_steam"), ok=True, extra=listing_id,
        msg="Listing removed",
    )


async def buy_listing(interaction, listing_id):
    steam = await _need_linked(interaction)
    if not steam:
        return
    state = _load()
    row = (state.get("listings") or {}).get(str(listing_id) or "")
    if not row or not row.get("open"):
        await interaction.response.send_message("That listing is gone.", ephemeral=True)
        return
    if str(row.get("mode") or MODE_TOKENS) == MODE_OFFERS:
        await interaction.response.send_message(
            "This post is looking for offers. Use **Offer tokens** or **Offer a card**.",
            ephemeral=True,
        )
        return
    if int(row.get("seller_discord") or 0) == int(interaction.user.id):
        await interaction.response.send_message("You cannot buy your own card.", ephemeral=True)
        return
    seller_steam = str(row.get("seller_steam") or "")
    slot_id = str(row.get("slot_id") or "")
    price = int(row.get("price") or 0)
    await interaction.response.defer(ephemeral=True)
    if _balance(steam) < price:
        await _audit(
            interaction.client, "trade", interaction.user, steam=steam, ok=False,
            extra=listing_id, tokens=price, balance=_balance(steam),
            species=row.get("species"), msg="Not enough tokens to buy listing",
        )
        await interaction.followup.send(
            f"That card is **{price}** tokens. You have **{_balance(steam)}**.",
            ephemeral=True,
        )
        return
    slot = await read_full_slot(seller_steam, slot_id)
    if not slot:
        await _close_listing(listing_id, sold=False)
        await _audit(
            interaction.client, "trade", interaction.user, steam=steam, ok=False,
            extra=listing_id, slot=slot_id, species=row.get("species"),
            msg=f"Listing closed — seller {seller_steam} no longer has slot",
        )
        await interaction.followup.send(
            "The seller already redeemed or moved that card. Listing closed.",
            ephemeral=True,
        )
        return
    charge = _fn("charge_steam_tokens")
    credit = _fn("credit_steam_tokens") or _fn("add_steam_tokens")
    if not charge or not credit:
        await _audit(
            interaction.client, "trade", interaction.user, steam=steam, ok=False,
            extra=listing_id, tokens=price, msg="Wallet helpers down on listing buy",
        )
        await interaction.followup.send("Wallet helpers are down. Try again later.", ephemeral=True)
        return
    if not charge(steam, price):
        await _audit(
            interaction.client, "trade", interaction.user, steam=steam, ok=False,
            extra=listing_id, tokens=price, msg="Charge failed — nothing moved",
        )
        await interaction.followup.send("Could not take tokens. Nothing moved.", ephemeral=True)
        return
    new_id = _new_id("tr")
    moved = dict(slot)
    moved["id"] = new_id
    moved["steam"] = steam
    moved["source"] = "trade"
    moved["capturedAt"] = int(time.time())
    ok, err = await write_slot(steam, moved)
    if not ok:
        credit(steam, price)
        await _audit(
            interaction.client, "trade", interaction.user, steam=steam, ok=False,
            extra=listing_id, slot=slot_id, prize=new_id, tokens=price,
            refunded=price, species=slot.get("species"),
            msg=f"Buyer vault write failed, tokens refunded: {err}",
        )
        await interaction.followup.send(f"Could not write your vault. Tokens refunded.\n{err}", ephemeral=True)
        return
    await remove_slot(seller_steam, slot_id)
    credit(seller_steam, price)
    await _close_listing(listing_id, sold=True)
    try:
        await interaction.message.edit(content=f"Sold to {interaction.user.mention}.", view=None)
    except Exception:
        pass
    await _audit(
        interaction.client, "trade", interaction.user, steam=steam, ok=True,
        extra=listing_id, species=slot.get("species"), tokens=price,
        slot=slot_id, prize=new_id, cards=_card_audit_line(slot),
        msg=f"Bought {slot.get('species')} from {seller_steam} for {price} → {new_id}",
    )
    await interaction.followup.send(
        f"Bought **{slot.get('species')}** for **{price}** tokens. "
        f"Redeem it from the member panel as a matching juvenile. Balance **{_balance(steam)}**.",
        ephemeral=True,
    )


def _open_listing(listing_id):
    row = (_load().get("listings") or {}).get(str(listing_id) or "")
    if row and row.get("open") is not False:
        return row
    return None


def _pending_offer_count(listing_id):
    listing_id = str(listing_id or "")
    count = 0
    for row in (_load().get("listing_offers") or {}).values():
        if row.get("listing_id") == listing_id and row.get("open") is not False:
            count += 1
    return count


async def _send_listing_offer(interaction, listing, offer_id, embed):
    seller_id = int(listing.get("seller_discord") or 0)
    seller = None
    if interaction.guild:
        seller = interaction.guild.get_member(seller_id)
    view = ListingOfferView(offer_id)
    sent_dm = False
    if seller is not None:
        try:
            await seller.send(
                f"{interaction.user.mention} offered on your posted **{listing.get('species')}**.",
                embed=embed,
                view=view,
            )
            sent_dm = True
        except Exception:
            sent_dm = False
    if sent_dm:
        if interaction.response.is_done():
            await interaction.followup.send("Offer sent to the seller. They accept or decline.", ephemeral=True)
        else:
            await interaction.response.send_message(
                "Offer sent to the seller. They accept or decline.",
                ephemeral=True,
            )
        return
    ping = f"<@{seller_id}> — {interaction.user.mention} offered on your posted card."
    if interaction.response.is_done():
        await interaction.followup.send(ping, embed=embed, view=view)
    else:
        await interaction.response.send_message(ping, embed=embed, view=view)


async def begin_token_offer(interaction, listing_id):
    steam = await _need_linked(interaction)
    if not steam:
        return
    listing = _open_listing(listing_id)
    if not listing or str(listing.get("mode") or "") != MODE_OFFERS:
        await interaction.response.send_message("That post is not looking for offers.", ephemeral=True)
        return
    if int(listing.get("seller_discord") or 0) == int(interaction.user.id):
        await interaction.response.send_message("You cannot offer on your own card.", ephemeral=True)
        return
    if _pending_offer_count(listing_id) >= MAX_LISTING_OFFERS:
        await interaction.response.send_message("This post already has too many open offers.", ephemeral=True)
        return
    await interaction.response.send_modal(OfferTokenModal(listing_id))


async def submit_token_offer(interaction, listing_id, raw):
    steam = await _need_linked(interaction)
    if not steam:
        return
    try:
        tokens = int(str(raw).strip())
    except Exception:
        await interaction.response.send_message("Tokens must be a whole number.", ephemeral=True)
        return
    if tokens < MIN_PRICE or tokens > MAX_PRICE:
        await interaction.response.send_message(
            f"Offer must be {MIN_PRICE}–{MAX_PRICE} tokens.",
            ephemeral=True,
        )
        return
    listing = _open_listing(listing_id)
    if not listing or str(listing.get("mode") or "") != MODE_OFFERS:
        await interaction.response.send_message("That post is gone.", ephemeral=True)
        return
    if _balance(steam) < tokens:
        await _audit(
            interaction.client, "listing_offer", interaction.user, steam=steam, ok=False,
            extra=listing_id, tokens=tokens, balance=_balance(steam),
            species=listing.get("species"), msg="Token offer — not enough tokens",
        )
        await interaction.response.send_message(
            f"You need **{tokens}** tokens. You have **{_balance(steam)}**.",
            ephemeral=True,
        )
        return
    offer_id = _new_id("lo")
    state = _load()
    state.setdefault("listing_offers", {})[offer_id] = {
        "open": True,
        "listing_id": str(listing_id),
        "from_discord": int(interaction.user.id),
        "from_steam": steam,
        "kind": "tokens",
        "tokens": tokens,
        "slot_id": "",
        "created_at": int(time.time()),
    }
    _save(state)
    slot = await read_full_slot(listing.get("seller_steam"), listing.get("slot_id"))
    embed = card_embed(
        slot or {"species": listing.get("species")},
        title="🪙 Token offer",
        seller=interaction.user.mention,
        price=tokens,
        note=f"Offer on posted **{listing.get('species')}**",
    )
    await _audit(
        interaction.client, "listing_offer", interaction.user, steam=steam, ok=True,
        extra=offer_id, tokens=tokens, species=listing.get("species"),
        slot=listing.get("slot_id"),
        msg=f"Offered {tokens} tokens on {listing.get('species')} listing {listing_id}",
    )
    await _send_listing_offer(interaction, listing, offer_id, embed)


async def begin_card_offer(interaction, listing_id):
    steam = await _need_linked(interaction)
    if not steam:
        return
    listing = _open_listing(listing_id)
    if not listing or str(listing.get("mode") or "") != MODE_OFFERS:
        await interaction.response.send_message("That post is not looking for offers.", ephemeral=True)
        return
    if int(listing.get("seller_discord") or 0) == int(interaction.user.id):
        await interaction.response.send_message("You cannot offer on your own card.", ephemeral=True)
        return
    if _pending_offer_count(listing_id) >= MAX_LISTING_OFFERS:
        await interaction.response.send_message("This post already has too many open offers.", ephemeral=True)
        return
    await pick_slot(interaction, "offer_card", extra=str(listing_id))


async def submit_card_offer(interaction, listing_id, slot_id):
    steam = await _need_linked(interaction)
    if not steam:
        return
    listing = _open_listing(listing_id)
    if not listing or str(listing.get("mode") or "") != MODE_OFFERS:
        await interaction.response.send_message("That post is gone.", ephemeral=True)
        return
    if slot_id in _held_slot_ids(steam):
        await interaction.response.send_message(
            "That card is already listed or offered. Pick another.",
            ephemeral=True,
        )
        return
    slot = await read_full_slot(steam, slot_id)
    if not slot:
        await interaction.response.send_message("That card is gone.", ephemeral=True)
        return
    offer_id = _new_id("lo")
    state = _load()
    state.setdefault("listing_offers", {})[offer_id] = {
        "open": True,
        "listing_id": str(listing_id),
        "from_discord": int(interaction.user.id),
        "from_steam": steam,
        "kind": "card",
        "tokens": 0,
        "slot_id": str(slot_id),
        "species": slot.get("species"),
        "created_at": int(time.time()),
    }
    _save(state)
    embed = card_embed(
        slot,
        title="🃏 Card offer",
        seller=interaction.user.mention,
        note=f"Offered for posted **{listing.get('species')}**",
    )
    await _audit(
        interaction.client, "listing_offer", interaction.user, steam=steam, ok=True,
        extra=offer_id, species=slot.get("species"), slot=slot_id,
        cards=_card_audit_line(slot),
        msg=f"Offered {slot.get('species')} on listing {listing_id} ({listing.get('species')})",
    )
    await _send_listing_offer(interaction, listing, offer_id, embed)


async def _take_listing_offer(interaction, offer_id):
    state = _load()
    row = (state.get("listing_offers") or {}).get(str(offer_id) or "")
    if not row or row.get("open") is False:
        await interaction.response.send_message("That offer is gone.", ephemeral=True)
        return None, None
    listing = _open_listing(row.get("listing_id"))
    if not listing:
        row["open"] = False
        _save(state)
        await interaction.response.send_message("That post was unlisted or sold.", ephemeral=True)
        return None, None
    if int(listing.get("seller_discord") or 0) != int(interaction.user.id):
        await interaction.response.send_message("Only the poster can accept this offer.", ephemeral=True)
        return None, None
    return row, listing


async def decline_listing_offer(interaction, offer_id):
    row, listing = await _take_listing_offer(interaction, offer_id)
    if not row:
        return
    state = _load()
    offer = (state.get("listing_offers") or {}).get(str(offer_id) or "")
    if offer:
        offer["open"] = False
        offer["closed"] = "declined"
        _save(state)
    try:
        await interaction.response.edit_message(content="Offer declined.", view=None)
    except Exception:
        if not interaction.response.is_done():
            await interaction.response.send_message("Offer declined.", ephemeral=True)
    await _audit(
        interaction.client, "listing_offer_decline", interaction.user,
        steam=listing.get("seller_steam") if listing else "",
        ok=True, extra=offer_id, species=(listing or {}).get("species"),
        msg=f"Declined offer from {row.get('from_steam')} kind={row.get('kind')}",
    )


async def accept_listing_offer(interaction, offer_id):
    row, listing = await _take_listing_offer(interaction, offer_id)
    if not row:
        return
    await interaction.response.defer(ephemeral=True)
    listing_id = str(row.get("listing_id") or "")
    seller_steam = str(listing.get("seller_steam") or "")
    buyer_steam = str(row.get("from_steam") or "")
    listed_id = str(listing.get("slot_id") or "")
    listed = await read_full_slot(seller_steam, listed_id)
    if not listed:
        await _close_listing(listing_id, sold=False)
        await _audit(
            interaction.client, "listing_offer", interaction.user, steam=seller_steam, ok=False,
            extra=offer_id, slot=listed_id, species=listing.get("species"),
            msg="Accept failed — posted card is gone",
        )
        await interaction.followup.send("Your posted card is gone. Post closed.", ephemeral=True)
        return
    kind = str(row.get("kind") or "")
    if kind == "tokens":
        tokens = int(row.get("tokens") or 0)
        charge = _fn("charge_steam_tokens")
        credit = _fn("credit_steam_tokens") or _fn("add_steam_tokens")
        if _balance(buyer_steam) < tokens:
            await _audit(
                interaction.client, "listing_offer", interaction.user, steam=buyer_steam, ok=False,
                extra=offer_id, tokens=tokens, species=listed.get("species"),
                msg="Accept failed — offerer no longer has tokens",
            )
            await interaction.followup.send("They no longer have enough tokens.", ephemeral=True)
            return
        if not charge or not credit or not charge(buyer_steam, tokens):
            await _audit(
                interaction.client, "listing_offer", interaction.user, steam=buyer_steam, ok=False,
                extra=offer_id, tokens=tokens, msg="Accept failed — token charge",
            )
            await interaction.followup.send("Could not take their tokens. Offer still open.", ephemeral=True)
            state = _load()
            offer = (state.get("listing_offers") or {}).get(str(offer_id) or "")
            if offer:
                offer["open"] = True
                _save(state)
            return
        new_id = _new_id("tr")
        moved = dict(listed)
        moved["id"] = new_id
        moved["steam"] = buyer_steam
        moved["source"] = "trade"
        moved["capturedAt"] = int(time.time())
        ok, err = await write_slot(buyer_steam, moved)
        if not ok:
            if credit:
                credit(buyer_steam, tokens)
            await _audit(
                interaction.client, "listing_offer", interaction.user, steam=buyer_steam, ok=False,
                extra=offer_id, tokens=tokens, refunded=tokens, prize=new_id,
                species=listed.get("species"), msg=f"Accept write failed, tokens refunded: {err}",
            )
            await interaction.followup.send(f"Could not move the card. Tokens refunded.\n{err}", ephemeral=True)
            return
        await remove_slot(seller_steam, listed_id)
        credit(seller_steam, tokens)
        state = _load()
        offer = (state.get("listing_offers") or {}).get(str(offer_id) or "")
        if offer:
            offer["open"] = False
            offer["closed"] = "accepted"
            _save(state)
        await _close_listing(listing_id, sold=True)
        await _mark_listing_sold(listing, interaction.user)
        await _audit(
            interaction.client, "trade", interaction.user, steam=buyer_steam, ok=True,
            extra=offer_id, tokens=tokens, slot=listed_id, prize=new_id,
            species=listed.get("species"), cards=_card_audit_line(listed),
            msg=f"Accepted token offer {tokens} for {listed.get('species')} → {new_id}",
        )
        await interaction.followup.send(
            f"Accepted **{tokens}** tokens. **{listed.get('species')}** is now in their vault.",
            ephemeral=True,
        )
        return
    offer_slot_id = str(row.get("slot_id") or "")
    offered = await read_full_slot(buyer_steam, offer_slot_id)
    if not offered:
        await _audit(
            interaction.client, "listing_offer", interaction.user, steam=buyer_steam, ok=False,
            extra=offer_id, slot=offer_slot_id, species=listing.get("species"),
            msg="Accept failed — offered card is gone",
        )
        await interaction.followup.send("Their offered card is gone. Decline or wait for a new offer.", ephemeral=True)
        return
    to_buyer_id = _new_id("tr")
    to_seller_id = _new_id("tr")
    listed_copy = dict(listed)
    listed_copy["id"] = to_buyer_id
    listed_copy["steam"] = buyer_steam
    listed_copy["source"] = "trade"
    listed_copy["capturedAt"] = int(time.time())
    offered_copy = dict(offered)
    offered_copy["id"] = to_seller_id
    offered_copy["steam"] = seller_steam
    offered_copy["source"] = "trade"
    offered_copy["capturedAt"] = int(time.time())
    ok, err = await write_slot(buyer_steam, listed_copy)
    if not ok:
        await _audit(
            interaction.client, "listing_offer", interaction.user, steam=buyer_steam, ok=False,
            extra=f"{offer_id} {err}", species=listed.get("species"),
            msg=f"Card swap failed writing posted card to offerer: {err}",
        )
        await interaction.followup.send(f"Could not move your posted card.\n{err}", ephemeral=True)
        return
    ok, err = await write_slot(seller_steam, offered_copy)
    if not ok:
        await remove_slot(buyer_steam, to_buyer_id)
        await _audit(
            interaction.client, "listing_offer", interaction.user, steam=seller_steam, ok=False,
            extra=offer_id, species=offered.get("species"),
            msg=f"Card swap failed writing offer card to poster; posted card copy removed: {err}",
        )
        await interaction.followup.send(f"Could not move their card. Nothing finished.\n{err}", ephemeral=True)
        return
    await remove_slot(seller_steam, listed_id)
    await remove_slot(buyer_steam, offer_slot_id)
    state = _load()
    offer = (state.get("listing_offers") or {}).get(str(offer_id) or "")
    if offer:
        offer["open"] = False
        offer["closed"] = "accepted"
        _save(state)
    await _close_listing(listing_id, sold=True)
    await _mark_listing_sold(listing, interaction.user)
    await _audit(
        interaction.client, "trade", interaction.user, steam=seller_steam, ok=True,
        extra=offer_id, slot=listed_id, prize=to_seller_id,
        species=listed.get("species"),
        cards=f"{_card_audit_line(listed)} <-> {_card_audit_line(offered)}",
        msg=(
            f"Accepted card swap {listed.get('species')} → {buyer_steam} ({to_buyer_id}) "
            f"for {offered.get('species')} → {seller_steam} ({to_seller_id})"
        ),
    )
    await interaction.followup.send(
        f"Swap done. You received **{offered.get('species')}**. "
        f"They received **{listed.get('species')}**.",
        ephemeral=True,
    )


async def _mark_listing_sold(listing, buyer):
    channel_id = int((listing or {}).get("channel_id") or 0)
    message_id = int((listing or {}).get("message_id") or 0)
    if not channel_id or not message_id or buyer is None:
        return
    try:
        channel = buyer.guild.get_channel(channel_id) if getattr(buyer, "guild", None) else None
        if channel is None:
            return
        message = await channel.fetch_message(message_id)
        await message.edit(content=f"Deal closed with {buyer.mention}.", view=None)
    except Exception:
        pass


async def start_offer(interaction, slot_id, member):
    steam = await _need_linked(interaction)
    if not steam:
        return
    if member.bot or int(member.id) == int(interaction.user.id):
        await interaction.response.send_message("Pick another player.", ephemeral=True)
        return
    their = _steam_for(member.id)
    if not their:
        await interaction.response.send_message(
            f"{member.mention} has not linked Steam.",
            ephemeral=True,
        )
        return
    slot = await read_full_slot(steam, slot_id)
    if not slot:
        await interaction.response.send_message("That card is gone.", ephemeral=True)
        return
    if slot_id in _held_slot_ids(steam):
        await interaction.response.send_message("Unlist that card or wait for the offer to settle first.", ephemeral=True)
        return
    offer_id = _new_id("of")
    state = _load()
    state["offers"][offer_id] = {
        "from_discord": int(interaction.user.id),
        "from_steam": steam,
        "to_discord": int(member.id),
        "to_steam": their,
        "slot_id": str(slot_id),
        "created_at": int(time.time()),
        "open": True,
    }
    _save(state)
    embed = card_embed(slot, title="🤝 Trade offer", seller=interaction.user.mention)
    await _audit(
        interaction.client, "trade_offer", interaction.user, steam=steam, ok=True,
        extra=offer_id, slot=slot_id, species=slot.get("species"),
        cards=_card_audit_line(slot),
        msg=f"Offered {slot.get('species')} to {their} ({member.id})",
    )
    await interaction.response.send_message(
        f"Offer sent to {member.mention}. They have 10 minutes.",
        ephemeral=True,
    )
    try:
        await member.send(
            f"{interaction.user.mention} wants to send you this vault card. Accept in Discord.",
            embed=embed,
            view=OfferView(offer_id),
        )
    except Exception:
        await interaction.followup.send(
            f"{member.mention} — {interaction.user.mention} offered you a card.",
            embed=embed,
            view=OfferView(offer_id),
        )


async def _take_offer(interaction, offer_id, accept):
    state = _load()
    row = (state.get("offers") or {}).get(str(offer_id) or "")
    if not row or not row.get("open"):
        await interaction.response.send_message("That offer expired.", ephemeral=True)
        return None
    if int(row.get("to_discord") or 0) != int(interaction.user.id):
        await interaction.response.send_message("This offer is not for you.", ephemeral=True)
        return None
    row["open"] = False
    _save(state)
    return row


async def decline_offer(interaction, offer_id):
    row = await _take_offer(interaction, offer_id, False)
    if not row:
        return
    await interaction.response.edit_message(content="Offer declined.", view=None)
    await _audit(
        interaction.client, "trade_decline", interaction.user,
        steam=row.get("to_steam"), ok=True, extra=offer_id, slot=row.get("slot_id"),
        msg=f"Declined offer from {row.get('from_steam')}",
    )


async def accept_offer(interaction, offer_id):
    row = await _take_offer(interaction, offer_id, True)
    if not row:
        return
    await interaction.response.defer(ephemeral=True)
    slot = await read_full_slot(row["from_steam"], row["slot_id"])
    if not slot:
        await _audit(
            interaction.client, "trade", interaction.user, steam=row.get("to_steam"), ok=False,
            extra=offer_id, slot=row.get("slot_id"),
            msg=f"Accept failed — sender {row.get('from_steam')} no longer has card",
        )
        await interaction.followup.send("The sender no longer has that card.", ephemeral=True)
        return
    new_id = _new_id("tr")
    moved = dict(slot)
    moved["id"] = new_id
    moved["steam"] = row["to_steam"]
    moved["source"] = "trade"
    moved["capturedAt"] = int(time.time())
    ok, err = await write_slot(row["to_steam"], moved)
    if not ok:
        state = _load()
        state["offers"][str(offer_id)]["open"] = True
        _save(state)
        await _audit(
            interaction.client, "trade", interaction.user, steam=row.get("to_steam"), ok=False,
            extra=offer_id, slot=row.get("slot_id"), prize=new_id,
            species=slot.get("species"), msg=f"Accept write failed, offer reopened: {err}",
        )
        await interaction.followup.send(f"Could not move the card. Offer is open again.\n{err}", ephemeral=True)
        return
    await remove_slot(row["from_steam"], row["slot_id"])
    try:
        await interaction.message.edit(content="Accepted — card is in your vault.", view=None)
    except Exception:
        pass
    await _audit(
        interaction.client, "trade", interaction.user, steam=row["to_steam"], ok=True,
        extra=offer_id, species=slot.get("species"),
        slot=row.get("slot_id"), prize=new_id, cards=_card_audit_line(slot),
        msg=f"Accepted trade of {slot.get('species')} from {row.get('from_steam')} → {new_id}",
    )
    await interaction.followup.send(
        f"Received **{slot.get('species')}**. Redeem it from the member panel.",
        ephemeral=True,
    )


async def show_gamble_confirm(interaction, slot_ids, species, female, life, inherited, tokens):
    steam = await _need_linked(interaction)
    if not steam:
        return
    await interaction.response.defer(ephemeral=True)
    cards = []
    for sid in slot_ids:
        slot = await read_full_slot(steam, sid)
        if not slot:
            await interaction.followup.send(f"Card `{sid}` is gone. Start over.", ephemeral=True)
            return
        cards.append(slot)
    chance = win_chance_percent(cards, tokens, species)
    gender = "Female" if female else "Male"
    lines = [_card_summary(row) for row in cards]
    if tokens:
        lines.append(f"**+{tokens} tokens**")
    desc = (
        "**Staking**\n" + "\n".join(f"• {line}" for line in lines)
        + f"\n\n**For** {gender} **{species}** · **60% Entombed**\n"
        + f"**This life** {' · '.join(life)}\n"
        + f"**Inherited** {' · '.join(inherited)}\n\n"
        + f"**Win chance: {chance}%** (house keeps the rest; cap {GAMBLE_MAX_CHANCE:.0f}%)\n"
        + "Miss = those cards and tokens are gone. No refund."
    )
    embed = discord.Embed(title="🎲 Confirm gamble", description=desc[:4096], color=discord.Color.dark_red())
    await interaction.followup.send(
        embed=embed,
        view=GambleGoView(slot_ids, species, female, life, inherited, tokens),
        ephemeral=True,
    )


async def run_gamble(interaction, slot_ids, species, female, life, inherited, tokens):
    steam = await _need_linked(interaction)
    if not steam:
        return
    await interaction.response.defer(ephemeral=True)
    slot_ids = list(dict.fromkeys(str(sid) for sid in slot_ids if sid))
    if len(slot_ids) < GAMBLE_MIN_CARDS:
        await _audit(
            interaction.client, "gamble", interaction.user, steam=steam, ok=False,
            species=species, extra=",".join(slot_ids), msg="Fewer than min cards",
        )
        await interaction.followup.send(f"Need at least {GAMBLE_MIN_CARDS} cards.", ephemeral=True)
        return
    if not _valid_gamble_muts(species, female, life, inherited):
        await _audit(
            interaction.client, "gamble", interaction.user, steam=steam, ok=False,
            species=species, life=" · ".join(life or []),
            inherited=" · ".join(inherited or []),
            msg="Invalid mutations — nothing staked",
        )
        await interaction.followup.send(
            "Those mutations are not valid for that species and gender. Start over.",
            ephemeral=True,
        )
        return
    held = _held_slot_ids(steam)
    if any(sid in held for sid in slot_ids):
        await _audit(
            interaction.client, "gamble", interaction.user, steam=steam, ok=False,
            species=species, extra=",".join(slot_ids), msg="Staked a listed or offered card",
        )
        await interaction.followup.send("Unlist every staked card first (and wait out open offers).", ephemeral=True)
        return
    cards = []
    for sid in slot_ids:
        slot = await read_full_slot(steam, sid)
        if not slot:
            await _audit(
                interaction.client, "gamble", interaction.user, steam=steam, ok=False,
                species=species, slot=sid, extra=",".join(slot_ids),
                msg="Staked card missing — cancelled",
            )
            await interaction.followup.send(f"Card `{sid}` is gone. Gamble cancelled.", ephemeral=True)
            return
        cards.append(slot)
    tokens = max(0, int(tokens or 0))
    if tokens and _balance(steam) < tokens:
        await _audit(
            interaction.client, "gamble", interaction.user, steam=steam, ok=False,
            species=species, tokens=tokens, balance=_balance(steam),
            cards=" | ".join(_card_audit_line(row) for row in cards),
            msg="Not enough tokens — nothing staked",
        )
        await interaction.followup.send(
            f"You need **{tokens}** tokens. You have **{_balance(steam)}**.",
            ephemeral=True,
        )
        return
    chance = win_chance_percent(cards, tokens, species)
    charge = _fn("charge_steam_tokens")
    credit = _fn("credit_steam_tokens") or _fn("add_steam_tokens")
    if tokens:
        if not charge or not charge(steam, tokens, tokens):
            await _audit(
                interaction.client, "gamble", interaction.user, steam=steam, ok=False,
                species=species, tokens=tokens, chance=f"{chance}%",
                cards=" | ".join(_card_audit_line(row) for row in cards),
                msg="Token charge failed — nothing staked",
            )
            await interaction.followup.send("Could not take tokens. Nothing was staked.", ephemeral=True)
            return
    removed = []
    for sid, slot in zip(slot_ids, cards):
        ok, err = await remove_slot(steam, sid)
        if not ok:
            for back in removed:
                await write_slot(steam, back)
            if tokens and credit:
                credit(steam, tokens)
            await _audit(
                interaction.client, "gamble", interaction.user, steam=steam, ok=False,
                species=species, tokens=tokens, refunded=tokens, slot=sid,
                chance=f"{chance}%", extra=str(err),
                cards=" | ".join(_card_audit_line(row) for row in cards),
                msg="Stake remove failed — cards and tokens returned",
            )
            await interaction.followup.send(
                f"Could not take a staked card. Stakes returned.\n{err}",
                ephemeral=True,
            )
            return
        removed.append(slot)
    roll = random.uniform(0, 100)
    won = roll < chance
    stake_txt = " | ".join(_card_audit_line(row) for row in removed)
    gender = "Female" if female else "Male"
    if not won:
        await _audit(
            interaction.client, "gamble", interaction.user, steam=steam, ok=True,
            species=species, gender=gender, tokens=tokens, chance=f"{chance}%",
            roll=f"{roll:.2f}", cards=stake_txt,
            life=" · ".join(life or []), inherited=" · ".join(inherited or []),
            extra=",".join(slot_ids),
            msg=f"MISS {chance}% roll={roll:.2f} burned {len(removed)} cards"
            + (f" +{tokens} tokens" if tokens else ""),
        )
        await interaction.followup.send(
            f"🎲 **Miss** ({chance}% chance). Your {len(cards)} cards"
            + (f" and **{tokens}** tokens" if tokens else "")
            + " are gone.",
            ephemeral=True,
        )
        return
    prize = build_entomb_prize(steam, species, female, life, inherited)
    ok, err = await write_slot(steam, prize)
    if not ok:
        for back in removed:
            await write_slot(steam, back)
        if tokens and credit:
            credit(steam, tokens)
        await _audit(
            interaction.client, "gamble", interaction.user, steam=steam, ok=False,
            species=species, gender=gender, tokens=tokens, refunded=tokens,
            chance=f"{chance}%", roll=f"{roll:.2f}", cards=stake_txt,
            prize=prize.get("id"), extra=str(err),
            life=" · ".join(life or []), inherited=" · ".join(inherited or []),
            msg="WIN write failed — cards and tokens put back",
        )
        await interaction.followup.send(
            f"Win write failed. Cards and tokens were put back.\n{err}",
            ephemeral=True,
        )
        return
    await _audit(
        interaction.client, "gamble", interaction.user, steam=steam, ok=True,
        species=species, gender=gender, tokens=tokens, chance=f"{chance}%",
        roll=f"{roll:.2f}", cards=stake_txt, prize=prize.get("id"),
        growth="60%", slot=prize.get("id"),
        life=" · ".join(life or []), inherited=" · ".join(inherited or []),
        mutations=prize.get("mutations"),
        extra=",".join(slot_ids),
        msg=f"WIN {chance}% roll={roll:.2f} 60% entombed {gender} {species} prize={prize.get('id')}",
    )
    await interaction.followup.send(
        embed=card_embed(
            prize,
            title="🎲 Entombed win",
            footer="60% · juvie + sub + 4 inherited · grow to elder to earn 4 more",
        ),
        ephemeral=True,
    )


async def start_gamble_test(interaction, member=None):
    from primeval_panels import can_staff, deny_staff

    if interaction.guild and not can_staff(interaction.user, "gamble_test"):
        await deny_staff(interaction, "gamble_test")
        return
    target = member or interaction.user
    steam = _steam_for(target.id)
    if not steam:
        msg = (
            f"{target.mention} has not linked Steam."
            if member and member.id != interaction.user.id
            else "Link Steam on the member panel first."
        )
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        return
    await interaction.response.send_message(
        f"Grant a **test Entombed** to {target.mention} (`{steam}`).\n"
        "Pick species, gender, **juvenile + sub-adult**, then **4 inherited** — "
        "same card a gamble win writes.\n"
        "Then spawn that species as a juvenile and **Redeem**. Expect **60%** growth, "
        "**75% hunger**, **ABY 50%**.",
        view=GambleSpeciesView([], grant_steam=steam),
        ephemeral=True,
    )


async def grant_test_entomb(interaction, steam, species, female, life, inherited):
    from primeval_panels import can_staff, deny_staff

    if interaction.guild and not can_staff(interaction.user, "gamble_test"):
        await deny_staff(interaction, "gamble_test")
        return
    if not _valid_gamble_muts(species, female, life, inherited):
        await _audit(
            interaction.client, "gamble_test", interaction.user, steam=steam, ok=False,
            species=species, life=" · ".join(life or []),
            inherited=" · ".join(inherited or []),
            msg="Test grant rejected — invalid mutations",
        )
        await interaction.response.send_message(
            "Those mutations are not valid for that species and gender.",
            ephemeral=True,
        )
        return
    await interaction.response.defer(ephemeral=True)
    prize = build_entomb_prize(steam, species, female, life, inherited, test=True)
    ok, err = await write_slot(steam, prize)
    if not ok:
        await _audit(
            interaction.client, "gamble_test", interaction.user, steam=steam, ok=False,
            species=species, extra=str(err),
            life=" · ".join(life or []), inherited=" · ".join(inherited or []),
            msg="Test grant write failed",
        )
        await interaction.followup.send(f"Could not write the test card.\n{err}", ephemeral=True)
        return
    gender = "Female" if female else "Male"
    await _audit(
        interaction.client, "gamble_test", interaction.user, steam=steam, ok=True,
        species=species, gender=gender, growth="60%", slot=prize.get("id"),
        prize=prize.get("id"), mutations=prize.get("mutations"),
        life=" · ".join(life or []), inherited=" · ".join(inherited or []),
        msg=f"admin test grant 60% entombed {gender} {species} prize={prize.get('id')}",
    )
    await interaction.followup.send(
        content=(
            f"Test Entombed is in the vault for Steam `{steam}`.\n"
            f"Spawn a **{gender} {species}** juvenile, then **Redeem** that slot.\n"
            "Expect **60%** growth, **75% hunger**, **ABY 50%**, juvie + sub, and 4 inherited."
        ),
        embed=card_embed(
            prize,
            title="🧪 Test Entombed",
            footer="60% · 75% hunger · ABY 50% · juvie + sub + 4 inherited",
        ),
        ephemeral=True,
    )


async def show_trade_home(interaction):
    steam = await _need_linked(interaction)
    if not steam:
        return
    await interaction.response.send_message(
        embed=trade_embed(),
        view=TradePanelView(),
        ephemeral=True,
    )


async def post_trade_panel(interaction):
    from primeval_panels import can_staff, deny_staff

    if interaction.guild and not can_staff(interaction.user, "post_trade_panel"):
        await deny_staff(interaction, "post_trade_panel")
        return
    dest = await trade_channel(interaction.guild, interaction.client) or interaction.channel
    await dest.send(embed=trade_embed(), view=TradePanelView())
    if dest.id != getattr(interaction.channel, "id", 0):
        await interaction.response.send_message(f"Posted in {dest.mention}.", ephemeral=True)
    elif not interaction.response.is_done():
        await interaction.response.send_message("Trade panel posted.", ephemeral=True)


def register_views(bot):
    bot.add_view(TradePanelView())
    state = _load()
    for listing_id, row in (state.get("listings") or {}).items():
        if row.get("open"):
            bot.add_view(listing_posted_view(listing_id, row.get("mode") or MODE_TOKENS))


def register_slash(bot):
    from discord import app_commands

    names = {cmd.name for cmd in bot.tree.get_commands()}
    if "trade_panel" not in names:

        @bot.tree.command(name="trade_panel", description="Post the dino trade & gamble panel")
        async def trade_panel(interaction: discord.Interaction):
            await post_trade_panel(interaction)

    if "gamble_test" not in names:

        @bot.tree.command(
            name="gamble_test",
            description="Admin: grant a 60% Entombed test card to redeem",
        )
        @app_commands.describe(member="Who receives the card (defaults to you)")
        async def gamble_test(interaction: discord.Interaction, member: discord.Member = None):
            await start_gamble_test(interaction, member)
