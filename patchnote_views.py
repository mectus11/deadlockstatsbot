"""Discord components for patch-note posts.

`PatchNoteView` is what gets sent with a post. The Hero Changes button is persistent (its
custom_id carries the note guid), so a post made months ago still works after a restart:
`HeroChangesView` is registered at startup and reloads the note from SQLite.

The picker and hero-detail views are ephemeral and short-lived, so they keep their state in
memory and do not need a custom_id.
"""
import discord

from steam_news import FEED_URL, PatchNote, render_hero_content

PAGE_SIZE = 25  # Discord's limit for one string select


def page_slice(items: list, page: int, size: int = PAGE_SIZE) -> tuple[list, int, int]:
    """(items on this page, clamped page index, page count)."""
    pages = max(1, (len(items) + size - 1) // size)
    page = max(0, min(page, pages - 1))
    return items[page * size:(page + 1) * size], page, pages


def hero_picker_content(note: PatchNote, page: int) -> str:
    _, page, pages = page_slice(list(note.heroes), page)
    text = f"**{note.title}** — Hero Changes\nPick a hero to see what changed for them."
    if pages > 1:
        text += f"\nPage {page + 1}/{pages}"
    return text


class SteamLinkButton(discord.ui.Button):
    def __init__(self, url: str):
        super().__init__(label="Steam Patch Notes", style=discord.ButtonStyle.link, url=url)


class HeroChangesButton(discord.ui.Button):
    def __init__(self, bot, guid: str, count: int):
        super().__init__(label=f"Hero Changes ({count})", style=discord.ButtonStyle.primary,
                         custom_id=f"pn:heroes:{guid}")
        self.bot = bot
        self.guid = guid

    async def callback(self, interaction: discord.Interaction):
        note = self.bot.db.get_patch_note(self.guid)
        if note is None or not note.heroes:
            return await interaction.response.send_message(
                "That patch note is no longer stored by the bot.", ephemeral=True)
        await interaction.response.send_message(
            hero_picker_content(note, 0), view=HeroPickerView(self.bot, note), ephemeral=True)


class PatchNoteView(discord.ui.View):
    """The view attached to a posted patch note (Steam link + Hero Changes)."""

    def __init__(self, bot, note: PatchNote | None = None, guid: str | None = None):
        super().__init__(timeout=None)
        self.bot = bot
        if note is None and guid:
            note = bot.db.get_patch_note(guid)
        self.add_item(SteamLinkButton(note.link if note else FEED_URL))
        if note and note.heroes:
            self.add_item(HeroChangesButton(bot, note.guid, len(note.heroes)))


class HeroChangesView(discord.ui.View):
    """Persistent container registered at startup; link buttons need no handler."""

    def __init__(self, bot, guid: str, count: int):
        super().__init__(timeout=None)
        self.add_item(HeroChangesButton(bot, guid, count))


class HeroPickerView(discord.ui.View):
    def __init__(self, bot, note: PatchNote, page: int = 0):
        super().__init__(timeout=900)
        self.bot = bot
        self.note = note
        self.page = page
        self._build()

    def _build(self) -> None:
        self.clear_items()
        heroes = list(self.note.heroes)
        chunk, self.page, pages = page_slice(heroes, self.page)
        self.select = discord.ui.Select(
            placeholder=(f"Choose a hero… (page {self.page + 1}/{pages})" if pages > 1
                         else "Choose a hero…"),
            options=[discord.SelectOption(label=name[:100], value=name) for name in chunk],
        )
        self.select.callback = self._pick
        self.add_item(self.select)
        if pages > 1:
            previous = discord.ui.Button(label="Previous", style=discord.ButtonStyle.secondary,
                                         disabled=self.page == 0)
            previous.callback = self._previous
            following = discord.ui.Button(label="Next", style=discord.ButtonStyle.secondary,
                                          disabled=self.page >= pages - 1)
            following.callback = self._next
            self.add_item(previous)
            self.add_item(following)
        self.add_item(SteamLinkButton(self.note.link))

    async def _pick(self, interaction: discord.Interaction):
        hero = self.select.values[0]
        content, extra = render_hero_content(self.note, hero)
        await interaction.response.edit_message(content=content,
                                                view=HeroDetailView(self.bot, self.note, self.page))
        for chunk in extra[:4]:
            await interaction.followup.send(chunk, ephemeral=True)

    async def _previous(self, interaction: discord.Interaction):
        self.page -= 1
        self._build()
        await interaction.response.edit_message(content=hero_picker_content(self.note, self.page),
                                                view=self)

    async def _next(self, interaction: discord.Interaction):
        self.page += 1
        self._build()
        await interaction.response.edit_message(content=hero_picker_content(self.note, self.page),
                                                view=self)


class HeroDetailView(discord.ui.View):
    def __init__(self, bot, note: PatchNote, page: int):
        super().__init__(timeout=900)
        self.bot = bot
        self.note = note
        self.page = page
        back = discord.ui.Button(label="All heroes", style=discord.ButtonStyle.secondary)
        back.callback = self._back
        self.add_item(back)
        self.add_item(SteamLinkButton(note.link))

    async def _back(self, interaction: discord.Interaction):
        view = HeroPickerView(self.bot, self.note, self.page)
        await interaction.response.edit_message(content=hero_picker_content(self.note, view.page),
                                                view=view)
