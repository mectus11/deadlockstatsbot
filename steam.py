"""Steam ID helpers.

Statlocker and the Deadlock API both use the 32-bit Steam account ID
(also called steam32 / "friend code"). Users usually have a SteamID64
(17 digits, starts with 7656119...), so we convert.
"""
import re

STEAM64_BASE = 76561197960265728


def parse_steam_input(text: str) -> int | None:
    """Return the 32-bit account ID for what the user typed, or None.

    Accepts: SteamID64, 32-bit account ID, [U:1:123], and
    steamcommunity.com/profiles/<SteamID64> URLs.
    Vanity URLs (steamcommunity.com/id/name) cannot be resolved without a
    Steam Web API key, so they return None.
    """
    text = (text or "").strip()
    m = re.search(r"steamcommunity\.com/profiles/(\d{17})", text)
    if m:
        text = m.group(1)
    m = re.fullmatch(r"\[U:1:(\d+)\]", text)
    if m:
        return int(m.group(1))
    if re.fullmatch(r"\d+", text):
        n = int(text)
        if n >= STEAM64_BASE:
            return n - STEAM64_BASE
        if 0 < n < 2**32:
            return n
    return None


def account_to_steam64(account_id: int) -> int:
    return account_id + STEAM64_BASE


STEAM_INPUT_HELP = (
    "I couldn't read that Steam ID. Use your SteamID64 (17 digits, starts with 7656119), "
    "your 32-bit account ID, or a steamcommunity.com/profiles/... link. "
    "Custom /id/name links don't work; open your profile on steamid.io to get the number."
)
