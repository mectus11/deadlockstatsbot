"""Tests for deploy/bot-update.py.

The script itself needs Docker and the panel's database, but the parts that decide *what* to
change are pure functions: rebuilding the container's run command and rewriting the container
id in the panel database. Those are what get tested here, so a mistake is caught on Windows
instead of during a live update.
"""

import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).parent / "deploy" / "bot-update.py"
_spec = importlib.util.spec_from_file_location("bot_update", SCRIPT)
bot_update = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bot_update)

OLD_ID = "3fa81bbe056b8be72e3fc81608a92a8927eb79f1401b5a1148ccd9cabed81d6e"
NEW_ID = "3703d1268f4c0c50f539b7d0083344d4cec50aa0770cfb4276c55baaa46c7381"
IMAGE = "ghcr.io/mectus11/deadlockstatsbot:latest"

INSPECT = {
    "Id": OLD_ID,
    "Name": "/discordbot_deadlockstats-3",
    "Config": {
        "Hostname": "discordbot_deadlockstats-3",
        "Env": [
            "GAMEPANEL_PROVIDER=external",
            "DISCORD_TOKEN=MTIz.abc.def",
            "STATLOCKER_API_KEY=",
            "DEADLOCK_API_KEY=",
            "POLL_SECONDS=120",
            "DB_PATH=/data/bot.db",
            "PATH=/usr/local/bin:/usr/local/sbin:/usr/bin",
            "BUILD_SHA=oldsha",
            "CACHE_DIR=/app/cache",
            "LOCAL_ITEM_ICONS=/app/assets/items",
            "PYTHONUNBUFFERED=1",
        ],
        "Cmd": ["python", "bot.py"],
        "Labels": {
            "gamepanel.managed": "true",
            "gamepanel.provider": "external",
            "gamepanel.serverId": "3",
        },
        "Entrypoint": None,
        "User": "",
    },
    "HostConfig": {
        "Binds": ["/opt/gamepanel/servers/3/data:/data"],
        "Memory": 536870912,
        "MemorySwap": 1073741824,
        "NanoCpus": 1000000000,
        "RestartPolicy": {"Name": "unless-stopped"},
    },
    "NetworkSettings": {
        "Networks": {
            "gamepanel-games": {
                "Aliases": ["discordbot_deadlockstats-3", "gamepanel-server-3", OLD_ID[:12]],
            },
        },
    },
}

IMAGE_DEFAULTS = {
    "PATH": "/usr/local/bin:/usr/local/sbin:/usr/bin",  # same as the container: drop it
    "BUILD_SHA": "newsha",                              # new image supplies the new one
    "DB_PATH": "/app/data/bot.db",                      # container overrides it: keep it
    "CACHE_DIR": "/app/cache",
    "LOCAL_ITEM_ICONS": "/app/assets/items",
    "PYTHONUNBUFFERED": "1",
}


class RunArgs(unittest.TestCase):
    def setUp(self):
        self.args = bot_update.run_args(INSPECT, IMAGE, "discordbot_deadlockstats-3", IMAGE_DEFAULTS)

    def pairs(self, flag):
        """Values that follow a flag, e.g. pairs('-e') -> ['K=V', ...]."""
        return [self.args[i + 1] for i, a in enumerate(self.args) if a == flag]

    def test_keeps_identity_and_limits(self):
        self.assertEqual(self.pairs("--name"), ["discordbot_deadlockstats-3"])
        self.assertEqual(self.pairs("--hostname"), ["discordbot_deadlockstats-3"])
        self.assertEqual(self.pairs("--restart"), ["unless-stopped"])
        self.assertEqual(self.pairs("--memory"), ["536870912"])
        self.assertEqual(self.pairs("--memory-swap"), ["1073741824"])
        self.assertEqual(self.pairs("--cpus"), ["1"])

    def test_keeps_bind_and_labels(self):
        self.assertEqual(self.pairs("-v"), ["/opt/gamepanel/servers/3/data:/data"])
        labels = self.pairs("--label")
        self.assertIn("gamepanel.serverId=3", labels)
        self.assertIn("gamepanel.managed=true", labels)

    def test_network_aliases_skip_the_container_own_names(self):
        self.assertEqual(self.pairs("--network"), ["gamepanel-games"])
        self.assertEqual(self.pairs("--network-alias"), ["gamepanel-server-3"])

    def test_env_keeps_overrides_and_drops_image_defaults(self):
        env = self.pairs("-e")
        self.assertIn("DISCORD_TOKEN=MTIz.abc.def", env)   # app secret survives
        self.assertIn("DB_PATH=/data/bot.db", env)          # override of an image default survives
        self.assertIn("GAMEPANEL_PROVIDER=external", env)
        self.assertNotIn("PATH=/usr/local/bin:/usr/local/sbin:/usr/bin", env)
        self.assertNotIn("BUILD_SHA=oldsha", env)           # the new image supplies it
        self.assertNotIn("PYTHONUNBUFFERED=1", env)

    def test_image_and_command_come_last(self):
        self.assertEqual(self.args[-3:], [IMAGE, "python", "bot.py"])

    def test_entrypoint_is_preserved(self):
        info = {**INSPECT, "Config": {**INSPECT["Config"], "Entrypoint": ["/entry.sh", "-x"]}}
        args = bot_update.run_args(info, IMAGE, "bot", {})
        self.assertEqual(args[args.index("--entrypoint") + 1], "/entry.sh")
        self.assertEqual(args[-4:], [IMAGE, "-x", "python", "bot.py"])

    def test_build_sha_is_never_copied_from_the_old_container(self):
        # even if the new image declares nothing, the old commit must not be pinned
        args = bot_update.run_args(INSPECT, IMAGE, "bot", {})
        env = [args[i + 1] for i, a in enumerate(args) if a == "-e"]
        self.assertNotIn("BUILD_SHA=oldsha", env)
        self.assertNotIn("PATH=/usr/local/bin:/usr/local/sbin:/usr/bin", env)

    def test_mask_hides_secrets(self):
        masked = bot_update.mask(self.args)
        joined = " ".join(masked)
        self.assertIn("DISCORD_TOKEN=***", joined)
        self.assertNotIn("MTIz.abc.def", joined)
        self.assertIn("GAMEPANEL_PROVIDER=external", joined)


class Helpers(unittest.TestCase):
    def test_repo_of(self):
        self.assertEqual(bot_update.repo_of("ghcr.io/a/b:latest"), "ghcr.io/a/b")
        self.assertEqual(bot_update.repo_of("ghcr.io/a/b"), "ghcr.io/a/b")
        self.assertEqual(bot_update.repo_of("ghcr.io/a/b@sha256:abc"), "ghcr.io/a/b")
        self.assertEqual(bot_update.repo_of("localhost:5000/a/b:1.2"), "localhost:5000/a/b")

    def test_parse_env(self):
        self.assertEqual(bot_update.parse_env(["A=1", "B=x=y", "C="]), {"A": "1", "B": "x=y", "C": ""})
        self.assertEqual(bot_update.parse_env(None), {})


class FindBot(unittest.TestCase):
    """A pull moves the :latest tag, which makes `docker ps` show a bare image id for the old
    container. Detection has to survive that, or every second update fails."""

    def find(self, names, infos, images, override=None):
        with mock.patch.object(bot_update, "docker", return_value="\n".join(names)), \
             mock.patch.object(bot_update, "inspect", side_effect=lambda n: infos[n]), \
             mock.patch.object(bot_update, "docker_json", side_effect=lambda *a: [images[a[-1]]]):
            return bot_update.find_bot_container(IMAGE, override)

    def test_finds_container_whose_image_now_displays_an_id(self):
        # exactly what the VPS did: tag moved, container shows "a493537c9976"
        infos = {
            "discordbot_deadlockstats-3": {"Config": {"Image": IMAGE}, "Image": "sha256:old"},
            "si-cherni-1": {"Config": {"Image": "ovhcom/gamepanel-valheim:1.6.0"},
                            "Image": "sha256:valheim"},
        }
        images = {
            "sha256:old": {"RepoTags": [], "RepoDigests": []},
            "sha256:valheim": {"RepoTags": ["ovhcom/gamepanel-valheim:1.6.0"]},
        }
        self.assertEqual(
            self.find(list(infos), infos, images), "discordbot_deadlockstats-3")

    def test_matches_by_tag_when_the_container_was_created_from_an_id(self):
        infos = {"bot": {"Config": {"Image": "a493537c9976"}, "Image": "sha256:new"}}
        images = {"sha256:new": {"RepoTags": [IMAGE]}}
        self.assertEqual(self.find(["bot"], infos, images), "bot")

    def test_matches_by_digest(self):
        infos = {"bot": {"Config": {"Image": "a493537c9976"}, "Image": "sha256:new"}}
        images = {"sha256:new": {"RepoDigests": ["ghcr.io/mectus11/deadlockstatsbot@sha256:abc"]}}
        self.assertEqual(self.find(["bot"], infos, images), "bot")

    def test_falls_back_to_build_sha_when_the_old_image_lost_its_refs(self):
        # a pull can untag the old image completely, leaving the container with no repo reference
        infos = {"bot": {"Config": {"Image": "a493537c9976"}, "Image": "sha256:old"}}
        images = {"sha256:old": {"RepoTags": [], "RepoDigests": [],
                                 "Config": {"Env": ["BUILD_SHA=de92a06"]}}}
        self.assertEqual(self.find(["bot"], infos, images), "bot")

    def test_ignores_prev_and_failed_containers(self):
        infos = {
            "bot": {"Config": {"Image": IMAGE}, "Image": "sha256:new"},
            "bot-prev": {"Config": {"Image": IMAGE}, "Image": "sha256:old"},
            "bot-failed": {"Config": {"Image": IMAGE}, "Image": "sha256:old"},
        }
        images = {"sha256:new": {}, "sha256:old": {}}
        self.assertEqual(self.find(list(infos), infos, images), "bot")

    def test_ambiguous_containers_are_reported(self):
        infos = {
            "bot-a": {"Config": {"Image": IMAGE}, "Image": "sha256:new"},
            "bot-b": {"Config": {"Image": IMAGE}, "Image": "sha256:new"},
        }
        with self.assertRaises(bot_update.Fail) as ctx:
            self.find(list(infos), infos, {"sha256:new": {}})
        self.assertIn("bot-a", str(ctx.exception))
        self.assertIn("bot-b", str(ctx.exception))

    def test_no_match_mentions_the_container_flag(self):
        infos = {"si-cherni-1": {"Config": {"Image": "ovhcom/gamepanel-valheim:1.6.0"},
                                 "Image": "sha256:valheim"}}
        with self.assertRaises(bot_update.Fail) as ctx:
            self.find(list(infos), infos, {"sha256:valheim": {}})
        self.assertIn("--container", str(ctx.exception))


class PanelDatabase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "game-panel.db")
        con = sqlite3.connect(self.db)
        con.executescript(
            """
            CREATE TABLE servers (id INTEGER PRIMARY KEY, name TEXT, container_id TEXT);
            CREATE TABLE events (id INTEGER PRIMARY KEY, message TEXT);
            INSERT INTO servers (id, name, container_id) VALUES (1, 'Si Cherni', 'aaaa1111bbbb');
            INSERT INTO servers (id, name, container_id) VALUES (3, 'discordbot', '%s');
            INSERT INTO events (message) VALUES ('started %s');
            INSERT INTO events (message) VALUES ('unrelated aaaa1111bbbb');
            """ % (OLD_ID, OLD_ID[:12]))
        con.commit()
        con.close()

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self, query):
        con = sqlite3.connect(self.db)
        try:
            return con.execute(query).fetchall()
        finally:
            con.close()

    def test_replaces_full_and_short_ids(self):
        hits = bot_update.patch_panel_db(self.db, OLD_ID, NEW_ID)
        self.assertEqual(sum(count for _, _, count in hits), 2)
        self.assertEqual(self.rows("SELECT container_id FROM servers WHERE id = 3")[0][0], NEW_ID)
        self.assertEqual(self.rows("SELECT message FROM events WHERE id = 1")[0][0], f"started {NEW_ID[:12]}")

    def test_leaves_other_containers_alone(self):
        bot_update.patch_panel_db(self.db, OLD_ID, NEW_ID)
        self.assertEqual(self.rows("SELECT container_id FROM servers WHERE id = 1")[0][0], "aaaa1111bbbb")
        self.assertEqual(self.rows("SELECT message FROM events WHERE id = 2")[0][0],
                         "unrelated aaaa1111bbbb")

    def test_stale_references_counts_what_is_left(self):
        self.assertEqual(bot_update.stale_references(self.db, OLD_ID), 2)
        bot_update.patch_panel_db(self.db, OLD_ID, NEW_ID)
        self.assertEqual(bot_update.stale_references(self.db, OLD_ID), 0)

    def test_snapshot_is_a_real_copy(self):
        target = bot_update.snapshot(self.db)
        con = sqlite3.connect(target)
        try:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM servers").fetchone()[0], 2)
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
