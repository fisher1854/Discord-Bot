"""Auto-assign Member on join and require Steam linking before chatting.

Hook from Primeval_Island_Bot.py the same way as the other modules:

    import primeval_gate
    primeval_gate.bind(globals())
    primeval_gate.attach(bot)
    primeval_gate.register_views(bot)
    primeval_gate.register_slash(bot)
"""

from __future__ import annotations

import time

import discord
from discord import ui

from primeval_panels import staff_rank

G = {}

MEMBER_ROLE_NAME = "Member"
LINKED_ROLE_NAME = "Linked"

_WARN_COOLDOWN = {}
_ATTACHED = set()

OPEN_CHANNEL_HINTS = (
    "welcome",
    "verify",
    "verification",
    "link",
    "steam",
    "rules",
    "arrivals",
    "gate",
    "intro",
)


def bind(g):
    G.clear()
    G.update(g)


def _fn(name, default=None):
    return G.get(name, default)


def _role_named(guild, name):
    if guild is None:
        return None
    want = str(name).strip().lower()
    for role in guild.roles:
        if str(role.name).strip().lower() == want:
            return role
    return None


def is_steam_linked(user_id):
    getter = _fn("get_linked_steam_id")
    if not getter:
        links = G.get("STEAM_LINKS") or {}
        steam = links.get(int(user_id)) if str(user_id).isdigit() else None
        if steam is None:
            steam = links.get(str(user_id))
        return bool(str(steam or "").strip())
    try:
        steam = getter(int(user_id))
    except Exception:
        steam = None
    return bool(str(steam or "").strip())


def _is_staff(member):
    if member is None:
        return False
    perms = getattr(member, "guild_permissions", None)
    if perms and (perms.administrator or perms.manage_guild):
        return True
    return staff_rank(member) >= 1


def _is_open_channel(channel):
    name = str(getattr(channel, "name", "") or "").lower()
    return any(hint in name for hint in OPEN_CHANNEL_HINTS)


async def _ensure_role(guild, name, **kwargs):
    role = _role_named(guild, name)
    if role:
        return role
    try:
        return await guild.create_role(
            name=name,
            reason="Fallen Earth join / Steam link gate",
            **kwargs,
        )
    except Exception as exc:
        print(f"[GATE] could not create role {name}: {exc}")
        return None


async def _add_role(member, role):
    if member is None or role is None:
        return
    if role in getattr(member, "roles", []):
        return
    try:
        await member.add_roles(role, reason="Fallen Earth join / Steam link gate")
    except Exception as exc:
        print(f"[GATE] add {role.name} to {member} failed: {exc}")


async def _strip_talk_perms(role, talk):
    if role is None:
        return
    perms = role.permissions
    try:
        perms.update(
            send_messages=talk,
            send_messages_in_threads=talk,
            create_public_threads=talk,
            create_private_threads=False,
            add_reactions=talk,
            speak=talk,
            connect=talk,
            stream=talk,
            use_voice_activation=talk,
        )
        await role.edit(permissions=perms, reason="Fallen Earth Steam link gate")
    except Exception as exc:
        print(f"[GATE] edit {role.name} permissions failed: {exc}")


async def ensure_gate_roles(guild, *, lock_talk=False):
    if guild is None:
        return None, None
    member_role = await _ensure_role(
        guild,
        MEMBER_ROLE_NAME,
        mentionable=False,
        hoist=False,
    )
    linked_role = await _ensure_role(
        guild,
        LINKED_ROLE_NAME,
        mentionable=False,
        hoist=False,
        colour=discord.Colour.blurple(),
    )
    if lock_talk:
        await _strip_talk_perms(guild.default_role, False)
        await _strip_talk_perms(member_role, False)
        await _strip_talk_perms(linked_role, True)
    return member_role, linked_role


async def apply_member_roles(member, *, linked=None):
    if member is None or getattr(member, "bot", False):
        return
    guild = member.guild
    member_role, linked_role = await ensure_gate_roles(guild)
    await _add_role(member, member_role)
    if linked is None:
        linked = is_steam_linked(member.id) or _is_staff(member)
    if linked:
        await _add_role(member, linked_role)


async def on_steam_linked(interaction):
    member = getattr(interaction, "user", None)
    guild = getattr(interaction, "guild", None)
    if guild is None or member is None:
        return
    if not isinstance(member, discord.Member):
        member = guild.get_member(member.id)
    await apply_member_roles(member, linked=True)
    try:
        import primeval_patreon

        await primeval_patreon.grant_free_stipend_for(interaction.client, member)
    except Exception as exc:
        print(f"[GATE] free stipend after link failed: {exc}")
    try:
        import primeval_opening

        await primeval_opening.on_steam_linked(interaction)
    except Exception as exc:
        print(f"[GATE] opening weekend after link failed: {exc}")


def _should_gate_message(message):
    if message.guild is None:
        return False
    author = message.author
    if getattr(author, "bot", False):
        return False
    if _is_staff(author):
        return False
    if _is_open_channel(message.channel):
        return False
    if is_steam_linked(author.id):
        return False
    linked_role = _role_named(message.guild, LINKED_ROLE_NAME)
    if linked_role and linked_role in getattr(author, "roles", []):
        return False
    return True


async def _warn_unlinked(channel, user):
    now = time.time()
    key = f"{getattr(channel, 'id', 0)}:{user.id}"
    if now - _WARN_COOLDOWN.get(key, 0) < 45:
        return
    _WARN_COOLDOWN[key] = now
    try:
            send = getattr(channel, "send", None)
            if send is None:
                return
            await send(
                f"{user.mention} you need to **link Steam** before you can chat or join voice. "
                "Use the **Link Steam + Discord** button in the welcome channel.",
                delete_after=12,
            )
    except Exception:
        pass


class WelcomeLinkView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(
        label="Link Steam + Discord",
        emoji="🔗",
        style=discord.ButtonStyle.success,
        custom_id="pi_gate_link",
    )
    async def link(self, interaction: discord.Interaction, button: ui.Button):
        from primeval_panels import LinkSteamModal

        await interaction.response.send_modal(LinkSteamModal())


def welcome_embed():
    return discord.Embed(
        title="🦕 Fallen Earth | Link required",
        description=(
            "Welcome. You have the **Member** role, but you cannot chat or use voice "
            "until you link your **Steam ID** to this Discord account.\n\n"
            "1. Click **Link Steam + Discord**\n"
            "2. Enter your 17-digit SteamID64\n"
            "3. The **Linked** role unlocks the rest of the server\n\n"
            "Find SteamID64 on your Steam profile page (the long number starting with `7656119`)."
        ),
        color=discord.Color.green(),
    )


def attach(bot):
    marker = id(bot)
    if marker in _ATTACHED:
        return
    _ATTACHED.add(marker)

    @bot.listen("on_member_join")
    async def _gate_join(member):
        try:
            await apply_member_roles(member)
            try:
                import primeval_opening

                await primeval_opening.on_discord_join(member)
            except Exception as exc:
                print(f"[GATE] opening join note failed: {exc}")
            if is_steam_linked(member.id) or _is_staff(member):
                return
            try:
                await member.send(
                    "Welcome to Fallen Earth. You must **link Steam** before you can "
                    "chat or join voice. Use **Link Steam + Discord** in the welcome channel."
                )
            except Exception:
                pass
        except Exception as exc:
            print(f"[GATE] join failed: {exc}")

    @bot.listen("on_message")
    async def _gate_message(message):
        try:
            if not _should_gate_message(message):
                if (
                    message.guild
                    and not getattr(message.author, "bot", False)
                    and is_steam_linked(message.author.id)
                ):
                    linked_role = _role_named(message.guild, LINKED_ROLE_NAME)
                    if linked_role and linked_role not in getattr(message.author, "roles", []):
                        await _add_role(message.author, linked_role)
                return
            try:
                await message.delete()
            except Exception:
                pass
            await _warn_unlinked(message.channel, message.author)
        except Exception as exc:
            print(f"[GATE] message failed: {exc}")

    @bot.listen("on_voice_state_update")
    async def _gate_voice(member, before, after):
        try:
            if after.channel is None:
                return
            if getattr(member, "bot", False) or _is_staff(member):
                return
            if is_steam_linked(member.id):
                await apply_member_roles(member, linked=True)
                return
            try:
                await member.move_to(None, reason="Steam not linked")
            except Exception:
                pass
            await _warn_unlinked(after.channel, member)
        except Exception as exc:
            print(f"[GATE] voice failed: {exc}")

    @bot.listen("on_ready")
    async def _gate_ready():
        try:
            for guild in bot.guilds:
                await ensure_gate_roles(guild)
            print("[GATE] Member / Linked roles ready")
        except Exception as exc:
            print(f"[GATE] ready failed: {exc}")


def register_views(bot):
    bot.add_view(WelcomeLinkView())


def register_slash(bot):
    existing = {cmd.name for cmd in bot.tree.get_commands()}
    if "welcome_panel" not in existing:

        @bot.tree.command(
            name="welcome_panel",
            description="Post the Steam link panel new members must use",
        )
        async def welcome_panel(interaction: discord.Interaction):
            if interaction.guild and staff_rank(interaction.user) < 4:
                await interaction.response.send_message(
                    "Administrator or higher can post this panel.", ephemeral=True
                )
                return
            await ensure_gate_roles(interaction.guild)
            await interaction.response.send_message(embed=welcome_embed(), view=WelcomeLinkView())

    if "setup_link_gate" not in existing:

        @bot.tree.command(
            name="setup_link_gate",
            description="Create Member/Linked roles and strip talk perms until Steam is linked",
        )
        async def setup_link_gate(interaction: discord.Interaction):
            if staff_rank(interaction.user) < 4:
                await interaction.response.send_message(
                    "Administrator or higher can run this.", ephemeral=True
                )
                return
            await interaction.response.defer(ephemeral=True)
            member_role, linked_role = await ensure_gate_roles(interaction.guild, lock_talk=True)
            await interaction.followup.send(
                "Gate is set.\n"
                f"**{MEMBER_ROLE_NAME}** is given on join and cannot talk.\n"
                f"**{LINKED_ROLE_NAME}** is given after Steam is linked and unlocks chat/voice.\n"
                "Post `/welcome_panel` in your welcome channel, then `/sync_gate_roles` once "
                "to backfill people already in the server.\n"
                f"Roles: {member_role.mention if member_role else 'missing'} / "
                f"{linked_role.mention if linked_role else 'missing'}",
                ephemeral=True,
            )

    if "sync_gate_roles" not in existing:

        @bot.tree.command(
            name="sync_gate_roles",
            description="Give Member to everyone; give Linked to Steam-linked members and staff",
        )
        async def sync_gate_roles(interaction: discord.Interaction):
            if staff_rank(interaction.user) < 4:
                await interaction.response.send_message(
                    "Administrator or higher can run this.", ephemeral=True
                )
                return
            await interaction.response.defer(ephemeral=True)
            guild = interaction.guild
            await ensure_gate_roles(guild)
            members = 0
            linked = 0
            async for person in guild.fetch_members(limit=None):
                if getattr(person, "bot", False):
                    continue
                before = is_steam_linked(person.id) or _is_staff(person)
                await apply_member_roles(person)
                members += 1
                if before:
                    linked += 1
            await interaction.followup.send(
                f"Synced **{members}** members. **{linked}** already had Steam or staff and received **Linked**.",
                ephemeral=True,
            )
