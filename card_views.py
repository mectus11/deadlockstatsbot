"""Persistent Discord components for cards.

`MatchRefreshView` carries a static custom_id, so it is registered once at startup
and every match card ever posted keeps a working Refresh button. The callback looks
the message up in `match_posts` to find the account and match to re-render.
"""
import discord


class MatchRefreshView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary, emoji="🔄",
                       custom_id="match:refresh")
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.bot.refresh_match_card(interaction)
