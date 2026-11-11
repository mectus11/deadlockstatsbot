"""Tiny SQLite storage (stdlib only)."""
import json
import os
import sqlite3
from dataclasses import dataclass

from steam_news import PatchNote


@dataclass
class Link:
    discord_id: int
    guild_id: int
    account_id: int
    last_match_id: int
    display_name: str | None = None
    last_pp_score: int | None = None
    last_rank_number: int | None = None


@dataclass
class PatchChannel:
    guild_id: int
    last_guid: str | None = None
    last_pub_ts: int = 0


class DB:
    def __init__(self, path: str):
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)  # in case this directory doesn't exist yet (e.g. a fresh volume)
        try:
            self.conn = sqlite3.connect(path)
        except sqlite3.OperationalError:
            # print (not log: logging may not be configured yet this early) exactly why, so a
            # deploy that still can't open the database says more than "unable to open database file"
            print(f"[diag] could not open sqlite db at {path!r} (directory {directory!r})", flush=True)
            print(f"[diag] running as uid={os.getuid()} gid={os.getgid()}", flush=True)
            try:
                print(f"[diag] directory contents: {os.listdir(directory)}", flush=True)
            except OSError as e:
                print(f"[diag] could not list {directory!r}: {e}", flush=True)
            test_file = os.path.join(directory, ".write_test")
            try:
                open(test_file, "w").close()
                os.remove(test_file)
                print(f"[diag] {directory!r} IS writable by this process", flush=True)
            except OSError as e:
                print(f"[diag] {directory!r} is NOT writable by this process: {e}", flush=True)
            raise
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
            CREATE TABLE IF NOT EXISTS patch_channels (
                guild_id INTEGER PRIMARY KEY,
                last_guid TEXT,
                last_pub_ts INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS patch_notes (
                guid TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                link TEXT NOT NULL,
                pub_ts INTEGER NOT NULL,
                lines TEXT NOT NULL,
                heroes TEXT NOT NULL,
                posted_at INTEGER NOT NULL
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

    # --- patch notes -----------------------------------------------------
    def get_patch_channel(self, guild_id: int) -> PatchChannel | None:
        r = self.conn.execute(
            "SELECT * FROM patch_channels WHERE guild_id=?", (guild_id,)
        ).fetchone()
        return PatchChannel(**dict(r)) if r else None

    def set_patch_baseline(self, guild_id: int, guid: str | None, pub_ts: int = 0) -> None:
        self.conn.execute(
            "INSERT INTO patch_channels (guild_id, last_guid, last_pub_ts) VALUES (?,?,?) "
            "ON CONFLICT(guild_id) DO UPDATE SET last_guid=excluded.last_guid, "
            "last_pub_ts=excluded.last_pub_ts",
            (guild_id, guid, pub_ts),
        )
        self.conn.commit()

    def all_patch_channels(self) -> list[PatchChannel]:
        return [PatchChannel(**dict(r)) for r in self.conn.execute("SELECT * FROM patch_channels")]

    def save_patch_note(self, note: PatchNote, posted_at: int) -> None:
        """Store a posted note so its Hero Changes button keeps working after a restart.

        The Steam feed only holds 10 items, so old posts cannot be rebuilt from it.
        """
        self.conn.execute(
            "INSERT INTO patch_notes (guid, title, link, pub_ts, lines, heroes, posted_at) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(guid) DO UPDATE SET title=excluded.title, "
            "link=excluded.link, pub_ts=excluded.pub_ts, lines=excluded.lines, "
            "heroes=excluded.heroes, posted_at=excluded.posted_at",
            (note.guid, note.title, note.link, note.pub_ts, json.dumps(note.lines),
             json.dumps(note.heroes), posted_at),
        )
        self.conn.execute(
            "DELETE FROM patch_notes WHERE guid NOT IN "
            "(SELECT guid FROM patch_notes ORDER BY pub_ts DESC, rowid DESC LIMIT 100)"
        )
        self.conn.commit()

    def get_patch_note(self, guid: str) -> PatchNote | None:
        r = self.conn.execute("SELECT * FROM patch_notes WHERE guid=?", (guid,)).fetchone()
        if not r:
            return None
        return PatchNote(guid=r["guid"], title=r["title"], link=r["link"], pub_ts=r["pub_ts"],
                         lines=json.loads(r["lines"]), heroes=json.loads(r["heroes"]))

    def all_patch_notes(self) -> list[PatchNote]:
        return [self.get_patch_note(r["guid"]) for r in
                self.conn.execute("SELECT guid FROM patch_notes ORDER BY pub_ts DESC")]
