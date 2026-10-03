"""Public suggestion box for Primeval Island.

Members pick Bot / Server / Discord, fill a short form, and the result
posts to SUGGEST_CHANNEL_ID with Discord and Steam IDs.

Hook from primeval_tickets (bind + register_views + register_slash).
Staff post the panel with /suggestion_panel.
"""

from __future__ import annotations

import discord
from discord import ui

from primeval_panels import deny_staff, staff_rank

G = {}

SUGGEST_CHANNEL_ID = 1541456474629349397

KIND_BOT = "bot"
KIND_SERVER = "server"
KIND_DISCORD = "discord"

KIND_META = {
    KIND_BOT: {
        "label": "Bot suggestion",
        "emoji": "🤖",
        "color": discord.Color.blurple(),
        "hint": "Shop, panels, tokens, commands, Discord bot features",
    },
    KIND_SERVER: {
        "label": "Server suggestion",
        "emoji": "🦕",
        "color": discord.Color.green(),
        "hint": "Isle gameplay, rules, events, map, growth, species",
    },
    KIND_DISCORD: {
        "label": "Discord suggestion",
        "emoji": "💬",
        "color": discord.Color.fuchsia(),
        "hint": "Channels, roles, Discord layout, community tools",
    },
}


def bind(g):
    G.clear()
    G.update(g)


def _fn(name, default=None):
    return G.get(name, default)


def _steam_for(user_id):
    getter = _fn("get_linked_steam_id")
    if not getter or not user_id:
        return None
    try:
        steam = getter(int(user_id))
    except Exception:
        steam = None
    return str(steam).strip() if steam else None


class SuggestionModal(ui.Modal):
    def __init__(self, kind):
        meta = KIND_META[kind]
        super().__init__(title=meta["label"][:45])
        self.kind = kind
        self.summary = ui.TextInput(
            label="Short title",
            placeholder="One-line summary",
            max_length=100,
            required=True,
        )
        self.details = ui.TextInput(
            label="Your suggestion",
            style=discord.TextStyle.paragraph,
            placeholder="What should we add or change?",
            max_length=1500,
            required=True,
        )
        self.why = ui.TextInput(
            label="Why it helps (optional)",
            style=discord.TextStyle.paragraph,
            placeholder="Who it helps, or how you'd use it",
            max_length=800,
            required=False,
        )
        self.add_item(self.summary)
        self.add_item(self.details)
        self.add_item(self.why)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        meta = KIND_META[self.kind]
        user = interaction.user
        steam = _steam_for(user.id)
        channel = interaction.client.get_channel(SUGGEST_CHANNEL_ID)
        if channel is None:
            try:
                channel = await interaction.client.fetch_channel(SUGGEST_CHANNEL_ID)
            except Exception:
                channel = None
        if channel is None:
            await interaction.followup.send(
                "Suggestion channel is missing. Staff need to check the bot can see it.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=f"{meta['emoji']} {meta['label']}",
            description=str(self.details.value or "").strip()[:4096],
            color=meta["color"],
        )
        embed.add_field(name="Title", value=str(self.summary.value or "").strip()[:256], inline=False)
        why = str(self.why.value or "").strip()
        if why:
            embed.add_field(name="Why it helps", value=why[:1024], inline=False)
        embed.add_field(
            name="Discord",
            value=f"{user.mention}\n`{user}`\nID `{user.id}`",
            inline=True,
        )
        embed.add_field(
            name="Steam ID",
            value=f"`{steam}`" if steam else "Not linked",
            inline=True,
        )
        embed.set_footer(text=f"User ID {user.id}" + (f" · Steam {steam}" if steam else " · Steam not linked"))
        if getattr(user, "display_avatar", None):
            embed.set_thumbnail(url=user.display_avatar.url)

        try:
            await channel.send(embed=embed)
        except Exception as exc:
            await interaction.followup.send(f"Could not post the suggestion: {exc}", ephemeral=True)
            return
        await interaction.followup.send(
            f"Sent your **{meta['label'].lower()}**. Thank you.",
            ephemeral=True,
        )


class SuggestionPanelView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(
        label="Bot suggestion",
        emoji="🤖",
        style=discord.ButtonStyle.primary,
        custom_id="pi_sug_bot",
        row=0,
    )
    async def bot_sug(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(SuggestionModal(KIND_BOT))

    @ui.button(
        label="Server suggestion",
        emoji="🦕",
        style=discord.ButtonStyle.success,
        custom_id="pi_sug_server",
        row=0,
    )
    async def server_sug(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(SuggestionModal(KIND_SERVER))

    @ui.button(
        label="Discord suggestion",
        emoji="💬",
        style=discord.ButtonStyle.secondary,
        custom_id="pi_sug_discord",
        row=0,
    )
    async def discord_sug(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(SuggestionModal(KIND_DISCORD))


def suggestion_embed():
    return discord.Embed(
        title="💡 Fallen Earth · Suggestion box",
        description=(
            "Pick a category and fill the short form. Staff get Discord + Steam IDs automatically.\n\n"
            "🤖 **Bot suggestion** — shop, panels, tokens, commands\n"
            "🦕 **Server suggestion** — Isle gameplay, rules, events, species\n"
            "💬 **Discord suggestion** — channels, roles, community layout\n\n"
            "Keep it specific. One idea per form works best."
        ),
        color=discord.Color.gold(),
    )


async def post_suggestion_panel(interaction: discord.Interaction):
    if interaction.guild and staff_rank(interaction.user) < 2:
        await deny_staff(interaction, "broadcast")
        return
    await interaction.response.send_message(embed=suggestion_embed(), view=SuggestionPanelView())


def register_views(bot):
    bot.add_view(SuggestionPanelView())


def register_slash(bot):
    existing = {cmd.name for cmd in bot.tree.get_commands()}
    if "suggestion_panel" in existing:
        return

    @bot.tree.command(
        name="suggestion_panel",
        description="Post the public suggestion box panel",
    )
    async def suggestion_panel(interaction: discord.Interaction):
        await post_suggestion_panel(interaction)
