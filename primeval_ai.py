"""Staff control for the Teno / Dibble / Galli AI herd.

The herd itself lives in Isle Lua (ai.lua) and starts after each restart.
This only writes Saved/ai_herd.json and optional inbox verbs.
Demand targets (targetTeno / targetDibble / targetGalli) come from Adaptive Ecology.
"""

from __future__ import annotations

import json
import time

import discord
from discord import app_commands

from primeval_panels import deny_staff, staff_rank

G = {}
AI_PATH = "/TheIsle/Binaries/Win64/ue4ss/Mods/PrimevalRedeem/Saved/ai_herd.json"


def bind(g):
    G.clear()
    G.update(g)


async def _read_cfg():
    import primeval_isle

    status, text = await primeval_isle.read_file(AI_PATH)
    if status != 200 or not text:
        # FIXED: Updated fallback values to prevent zero-target drops during file read hitches
        return {
            "enabled": True,
            "perCarniTeno": 0,
            "perCarniDibble": 2,
            "perCarniGalli": 2,
            "stabilityCeiling": 40,
            "fillGap": 120,
            "deathGap": 300,
            "targetTeno": 0,
            "targetDibble": 12,
            "targetGalli": 12,
        }
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {
        "enabled": True,
        "perCarniTeno": 0,
        "perCarniDibble": 2,
        "perCarniGalli": 2,
        "stabilityCeiling": 40,
        "fillGap": 120,
        "deathGap": 300,
        "targetTeno": 0,
        "targetDibble": 12,
        "targetGalli": 12,
    }


async def _write_cfg(cfg):
    import primeval_isle

    body = json.dumps(cfg, indent=2) + "\n"
    return await primeval_isle.write_file(AI_PATH, body)


async def _save_or_report(interaction, cfg):
    status, _text = await _write_cfg(cfg)
    if status in (200, 204):
        return True
    await interaction.followup.send(
        f"Could not save the AI herd config (write status {status}). Nothing changed.",
        ephemeral=True,
    )
    return False


def _demand_line(cfg: dict) -> str:
    """Human status using ecology demand targets (not a hard 10/10 cap)."""
    target_t = 0
    target_d = cfg.get("targetDibble")
    target_g = cfg.get("targetGalli")
    
    if target_d is None and "capDibble" in cfg:
        target_d = cfg.get("capDibble")
    if target_g is None and "capGalli" in cfg:
        target_g = cfg.get("capGalli")
        
    try:
        target_d = int(target_d if target_d is not None else 12)
    except (TypeError, ValueError):
        target_d = 12
    try:
        target_g = int(target_g if target_g is not None else 12)
    except (TypeError, ValueError):
        target_g = 12
        
    def _int(key, default):
        try:
            return int(cfg.get(key) or default)
        except (TypeError, ValueError):
            return default

    per_d = _int("perCarniDibble", 2)
    ceiling = _int("stabilityCeiling", 40)
    fill_m = max(1, _int("fillGap", 120) // 60)
    death_m = max(1, _int("deathGap", 300) // 60)
    
    return (
        f"Demand targets: **{target_t}** Teno / **{target_d}** Dibble(Maia) / **{target_g}** Galli "
        f"({per_d} unlocked multiplier, ceiling **{ceiling}**). "
        f"Fill every **{fill_m} min**; after target: **{death_m} min** death refill."
    )


def register_slash(bot):
    existing = {cmd.name for cmd in bot.tree.get_commands()}
    if "ai_herd" in existing:
        return

    @bot.tree.command(
        name="ai_herd",
        description="Turn Teno/Dibble/Galli AI food herds on or off (Administrator+)",
    )
    @app_commands.describe(mode="on, off, or status")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="status", value="status"),
            app_commands.Choice(name="on", value="on"),
            app_commands.Choice(name="off", value="off"),
        ]
    )
    async def ai_herd(interaction: discord.Interaction, mode: app_commands.Choice[str]):
        if staff_rank(interaction.user) < 4:
            await deny_staff(interaction, "growth_window")
            return
        await interaction.response.defer(ephemeral=True)
        cfg = await _read_cfg()
        choice = str(mode.value if mode else "status")
        if choice == "on":
            cfg["enabled"] = True
            cfg["targetTeno"] = 0
            cfg["targetDibble"] = 12
            cfg["targetGalli"] = 12
            cfg["perCarniTeno"] = 0
            cfg["perCarniDibble"] = 2
            cfg["perCarniGalli"] = 2
            cfg["stabilityCeiling"] = 40
            if not await _save_or_report(interaction, cfg):
                return
            try:
                import primeval_isle

                await primeval_isle.queue_inbox(
                    {"id": f"aiherd-{int(time.time())}", "verb": "aiherd", "enabled": True}
                )
            except Exception:
                pass
            await interaction.followup.send(
                "Teno / Dibble / Galli AI herd **ON**. "
                + _demand_line(cfg)
                + " Proximity tracking engaged globally.",
                ephemeral=True,
            )
            return
        if choice == "off":
            cfg["enabled"] = False
            if not await _save_or_report(interaction, cfg):
                return
            try:
                import primeval_isle

                await primeval_isle.queue_inbox(
                    {"id": f"aiherd-{int(time.time())}", "verb": "aiherd", "enabled": False}
                )
            except Exception:
                pass
            await interaction.followup.send(
                "Teno / Dibble / Galli AI herd **OFF**. Existing AI stay until the next Isle restart "
                "(Lua will not destroy them).",
                ephemeral=True,
            )
            return
        on = cfg.get("enabled") is not False
        await interaction.followup.send(
            f"AI herd is **{'ON' if on else 'OFF'}**. {_demand_line(cfg)} "
            "Loads on the next Isle restart (no bounce from this command).",
            ephemeral=True,
        )
