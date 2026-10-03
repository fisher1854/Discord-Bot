"""Wire every Primeval module onto the live Discord bot.

From Primeval_Island_Bot.py after helpers exist (RCON, wallets, Steam links)
and load_database() has run:

    import primeval_boot
    primeval_boot.attach_all(bot, globals())
"""

from __future__ import annotations

_ATTACHED = set()
# Server nickname (guild only) + global identity (DMs / friend list).
BOT_NICK = "Fallen Earth Elder"
BOT_DISPLAY = "Fallen Earth Elder"
# Unique Discord username handle (no spaces). Tried in order if still Primeval*.
BOT_USERNAME_CANDIDATES = ("FallenEarthElder", "FallenEarth")


async def apply_bot_name(bot):
    """Set Fallen Earth branding in-guild and in DMs.

    Guild nicknames do not appear in DMs — Discord shows the bot's global
    display name / username there. Update both.
    """
    await _apply_global_identity(bot)
    for guild in getattr(bot, "guilds", []) or []:
        me = getattr(guild, "me", None)
        if me is None:
            continue
        current = str(getattr(me, "nick", None) or getattr(me, "display_name", "") or "")
        if current == BOT_NICK:
            continue
        try:
            await me.edit(nick=BOT_NICK, reason="Fallen Earth bot name")
            print(f"[BOOT] nick set to {BOT_NICK} in {guild.id}")
        except Exception as exc:
            print(f"[BOOT] nick failed in {guild.id}: {exc}")


async def _apply_global_identity(bot):
    user = getattr(bot, "user", None)
    if user is None:
        return

    # Global display name — this is what shows in DMs (not guild nick).
    try:
        global_name = str(getattr(user, "global_name", None) or "")
        if global_name != BOT_DISPLAY:
            try:
                await user.edit(global_name=BOT_DISPLAY)
                print(f"[BOOT] global_name set to {BOT_DISPLAY}")
            except TypeError:
                # Older discord.py: no global_name kwarg.
                print("[BOOT] discord.py build has no global_name; will try username if needed")
            except Exception as exc:
                print(f"[BOOT] global_name failed: {exc}")
        else:
            print(f"[BOOT] global display already {BOT_DISPLAY}")
    except Exception as exc:
        print(f"[BOOT] global identity check failed: {exc}")

    # Username handle if the account is still named after Primeval.
    try:
        name = str(getattr(user, "name", "") or "")
        if "primeval" not in name.lower():
            return
        for candidate in BOT_USERNAME_CANDIDATES:
            try:
                await user.edit(username=candidate)
                print(f"[BOOT] username set to {candidate}")
                return
            except Exception as exc:
                print(f"[BOOT] username {candidate} failed: {exc}")
    except Exception as exc:
        print(f"[BOOT] username change failed: {exc}")


def attach_all(bot, g):
    g = g or {}
    if id(bot) in _ATTACHED:
        print("[BOOT] Primeval modules already attached")
        return g
    _ATTACHED.add(id(bot))

    import primeval_isle
    import primeval_overlay
    import primeval_species

    primeval_isle.bind(g)
    primeval_overlay.bind(g)
    g["queue_isle_cmd_flag"] = primeval_isle.queue_verb
    g["queue_isle_inbox"] = primeval_isle.queue_inbox
    g["read_isle_prime_json"] = primeval_isle.read_prime
    g["read_isle_vault_json"] = primeval_isle.read_vault
    live = g.get("LIVE_HEADCOUNT")
    if not isinstance(live, dict):
        live = {}
    for name in primeval_species.playable():
        live.setdefault(name, 0)
    g["LIVE_HEADCOUNT"] = live

    import primeval_panels
    import primeval_tickets
    import primeval_gate
    import primeval_tally
    import primeval_health_board
    import primeval_qol
    import primeval_patreon
    import primeval_opening
    import primeval_admin_audit
    import primeval_vault_audit
    import primeval_voice
    import primeval_posts
    import primeval_disboard
    import primeval_events
    import primeval_giveaway
    import primeval_ai
    import primeval_skin
    import primeval_trade

    primeval_panels.bind(g)
    primeval_tickets.bind(g)
    primeval_gate.bind(g)
    primeval_tally.bind(g)
    primeval_health_board.bind(g)
    primeval_qol.bind(g)
    primeval_patreon.bind(g)
    primeval_opening.bind(g)
    primeval_admin_audit.bind(g)
    primeval_vault_audit.bind(g)
    primeval_voice.bind(g)
    primeval_posts.bind(g)
    primeval_disboard.bind(g)
    primeval_events.bind(g)
    primeval_giveaway.bind(g)
    primeval_ai.bind(g)
    primeval_skin.bind(g)
    primeval_trade.bind(g)
    # Ecology is optional until deployed to the host; must not block panel loops.
    try:
        import primeval_ecology

        primeval_ecology.load_state()
        print("[BOOT] ecology state ready")
    except Exception as exc:
        print(f"[BOOT] ecology optional (skipped): {exc}")

    primeval_panels.register_views(bot)
    primeval_panels.register_slash(bot)
    primeval_tickets.register_views(bot)
    primeval_tickets.register_slash(bot)
    primeval_gate.register_views(bot)
    primeval_gate.register_slash(bot)
    primeval_gate.attach(bot)
    primeval_tally.register_slash(bot)
    primeval_health_board.register_slash(bot)
    primeval_patreon.register_views(bot)
    primeval_patreon.register_slash(bot)
    primeval_patreon.attach(bot)
    primeval_opening.register_slash(bot)
    primeval_voice.register_slash(bot)
    primeval_posts.register_views(bot)
    primeval_posts.register_slash(bot)
    primeval_disboard.register_slash(bot)
    primeval_events.register_views(bot)
    primeval_events.register_slash(bot)
    primeval_giveaway.register_slash(bot)
    primeval_ai.register_slash(bot)
    primeval_skin.register_views(bot)
    primeval_skin.register_slash(bot)
    primeval_trade.register_views(bot)
    primeval_trade.register_slash(bot)
    primeval_overlay.register_slash(bot)

    primeval_tally.start(bot)
    primeval_health_board.start(bot)
    primeval_admin_audit.start(bot)
    primeval_vault_audit.start(bot)
    primeval_opening.start(bot)
    primeval_voice.attach(bot)
    primeval_voice.start(bot)
    primeval_disboard.start(bot)
    primeval_events.start(bot)
    primeval_giveaway.start(bot)
    primeval_overlay.start(bot)
    try:
        import primeval_ecology as _eco

        _eco.start(bot)
    except Exception as exc:
        print(f"[BOOT] ecology scheduler optional (skipped): {exc}")

    @bot.listen("on_ready")
    async def _fallen_earth_name():
        try:
            await apply_bot_name(bot)
        except Exception as exc:
            print(f"[BOOT] bot name failed: {exc}")

    if getattr(bot, "is_ready", lambda: False)():
        try:
            bot.loop.create_task(apply_bot_name(bot))
        except Exception as exc:
            print(f"[BOOT] bot name schedule failed: {exc}")

    print("[BOOT] Primeval modules attached")
    return g
