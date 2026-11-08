"""Tiny SQLite storage (stdlib only)."""
import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Link:
    discord_id: int
    guild_id: int
    account_id: int
    last_match_id: int
    display_name: str | None = None
    last_pp_score: int | None = None
    last_rank_number: int | None = None


class DB:
    def __init__(self, path: str):
        # Ensure the parent directory exists before connecting to avoid 'unable to open database file' errors
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS links (
                discord_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                account_id INTEGER NOT NULL,
                last_match_id INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (discord_id, guild_id)
            );
            CREATE TABLE IF NOT EXISTS channels (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL
            );
            """
        )
        for col in ("display_name TEXT", "last_pp_score INTEGER", "last_rank_number INTEGER"):
            try:  # migration for databases created before these columns existed
                self.conn.execute(f"ALTER TABLE links ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass
        self.conn.commit()

    # --- links -----------------------------------------------------------
    def set_link(self, discord_id: int, guild_id: int, account_id: int, last_match_id: int,
                 display_name: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO links (discord_id, guild_id, account_id, last_match_id, display_name) VALUES (?,?,?,?,?) "
            "ON CONFLICT(discord_id, guild_id) DO UPDATE SET "
            "account_id=excluded.account_id, last_match_id=excluded.last_match_id, "
            "display_name=COALESCE(excluded.display_name, links.display_name)",
            (discord_id, guild_id, account_id, last_match_id, display_name),
        )
        self.conn.commit()

    def get_link(self, discord_id: int, guild_id: int) -> Link | None:
        r = self.conn.execute(
            "SELECT * FROM links WHERE discord_id=? AND guild_id=?", (discord_id, guild_id)
        ).fetchone()
        return Link(**dict(r)) if r else None

    def set_rank_baseline(self, discord_id: int, guild_id: int, pp_score: int | None, rank_number: int | None) -> None:
        """Remembers the PP/rank last seen for this account, so the next match card can show the change."""
        self.conn.execute(
            "UPDATE links SET last_pp_score=?, last_rank_number=? WHERE discord_id=? AND guild_id=?",
            (pp_score, rank_number, discord_id, guild_id),
        )
        self.conn.commit()

    def delete_link(self, discord_id: int, guild_id: int) -> bool:
        cur = self.conn.execute(
            "DELETE FROM links WHERE discord_id=? AND guild_id=?", (discord_id, guild_id)
        )
        self.conn.commit()
        return cur.rowcount > 0

    def all_links(self) -> list[Link]:
        return [Link(**dict(r)) for r in self.conn.execute("SELECT * FROM links")]

    def update_last_match(self, discord_id: int, guild_id: int, match_id: int) -> None:
        self.conn.execute(
            "UPDATE links SET last_match_id=? WHERE discord_id=? AND guild_id=?",
            (match_id, discord_id, guild_id),
        )
        self.conn.commit()

    # --- channels --------------------------------------------------------
    def set_channel(self, guild_id: int, channel_id: int) -> None:
        self.conn.execute(
            "INSERT INTO channels (guild_id, channel_id) VALUES (?,?) "
            "ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id",
            (guild_id, channel_id),
        )
        self.conn.commit()

    def get_channel(self, guild_id: int) -> int | None:
        r = self.conn.execute(
            "SELECT channel_id FROM channels WHERE guild_id=?", (guild_id,)
        ).fetchone()
        return r["channel_id"] if r else None