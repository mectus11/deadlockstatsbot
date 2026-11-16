import os
import sqlite3
import sys
import tempfile
import types
import unittest

try:  # the pure-logic tests below do not need aiohttp itself
    import aiohttp  # noqa: F401
except ImportError:
    sys.modules["aiohttp"] = types.ModuleType("aiohttp")

from db import DB
from local_icons import LocalIcons, norm
from models import (BrawlRound, ItemBuy, Match, MatchStats, SteamProfile, final_items,
                    ingest_verdict, match_type, mate_ids, merge_match, normalize_entry,
                    normalize_metadata, parse_assets, parse_match_info, parse_salts,
                    parse_steam_profile, parse_steam_profiles, pick_image, resolve_identity)
from statlocker import (RankSnapshot, format_top, parse_lobby, parse_match_players, parse_profile,
                        rank_badge_url, rank_name)
from stats import compute_overall
from steam import account_to_steam64, find_steam_input, parse_steam_input


class SteamIds(unittest.TestCase):
    def test_steam64_to_account(self):
        self.assertEqual(parse_steam_input("76561197960435530"), 169802)  # Gabe's well-known ID
        self.assertEqual(account_to_steam64(169802), 76561197960435530)

    def test_other_formats(self):
        self.assertEqual(parse_steam_input("169802"), 169802)
        self.assertEqual(parse_steam_input("[U:1:169802]"), 169802)
        self.assertEqual(parse_steam_input(" https://steamcommunity.com/profiles/76561197960435530/ "), 169802)

    def test_rejects(self):
        for bad in ["", "gabelogannewell", "https://steamcommunity.com/id/gabe", "0", "-5"]:
            self.assertIsNone(parse_steam_input(bad), bad)

    def test_find_in_message(self):
        self.assertEqual(find_steam_input("<https://steamcommunity.com/profiles/76561197960435530>"), 169802)
        self.assertEqual(find_steam_input("add me [U:1:169802] thanks"), 169802)
        self.assertEqual(find_steam_input("my id is 76561197960435530."), 169802)
        self.assertIsNone(find_steam_input("I got 169802 souls and 765 kills"))
        self.assertIsNone(find_steam_input(""))


class Normalize(unittest.TestCase):
    def test_win_from_team_and_result(self):
        m = normalize_entry({"match_id": 5, "hero_id": 2, "player_team": 1, "match_result": 1,
                             "player_kills": 3, "player_deaths": 2, "player_assists": 9,
                             "match_duration_s": 1800, "start_time": 1700000000, "net_worth": 36000})
        self.assertTrue(m.won)
        self.assertEqual((m.kills, m.deaths, m.assists, m.duration_s), (3, 2, 9, 1800))
        loss = normalize_entry({"match_id": 6, "player_team": 0, "match_result": 1})
        self.assertFalse(loss.won)

    def test_missing_fields_are_none_not_crash(self):
        m = normalize_entry({"match_id": "7"})
        self.assertEqual(m.match_id, 7)
        self.assertIsNone(m.won)
        self.assertIsNone(m.kills)
        self.assertIsNone(normalize_entry({"hero_id": 1}))


META = {"match_info": {"match_id": 77, "duration_s": 1900, "start_time": 1700000000, "winning_team": 1, "players": [
    {"account_id": 5, "hero_id": 3, "team": 1, "kills": 7, "deaths": 2, "assists": 11, "net_worth": 38000,
     "last_hits": 190, "denies": 8, "items": [
         {"item_id": 10, "game_time_s": 300, "sold_time_s": 0},
         {"item_id": 11, "game_time_s": 100, "sold_time_s": 0},
         {"item_id": 12, "game_time_s": 120, "sold_time_s": 900},   # component sold / merged into an upgrade
         {"upgrade_id": 13, "game_time_s": 900}]},
    {"account_id": 6, "hero_id": 4, "team": 0, "items": []}]}}


class Metadata(unittest.TestCase):
    def test_player_stats_and_items(self):
        m = normalize_metadata(META, 5)
        self.assertEqual((m.match_id, m.hero_id, m.kills, m.deaths, m.assists), (77, 3, 7, 2, 11))
        self.assertEqual((m.net_worth, m.last_hits, m.denies, m.duration_s), (38000, 190, 8, 1900))
        self.assertTrue(m.won)
        self.assertEqual([i.item_id for i in m.items], [11, 10, 13])  # purchase order, sold item dropped
        self.assertFalse(normalize_metadata(META, 6).won)

    def test_unwrapped_and_bad_shapes(self):
        self.assertEqual(normalize_metadata(META["match_info"], 5).match_id, 77)
        self.assertIsNone(normalize_metadata(META, 999))
        for bad in (None, [], {}, {"match_info": {"players": "x"}}, "nope"):
            self.assertIsNone(normalize_metadata(bad, 5))

    def test_final_items_and_merge(self):
        self.assertEqual([i.item_id for i in final_items([ItemBuy(1, 50), ItemBuy(2, 10), ItemBuy(3, 5, 99)])], [2, 1])
        base = Match(77, hero_id=3, kills=None, won=None)
        merged = merge_match(base, normalize_metadata(META, 5))
        self.assertEqual((merged.hero_id, merged.kills, merged.won, len(merged.items)), (3, 7, True, 3))
        keep = merge_match(Match(77, kills=1), normalize_metadata(META, 5))
        self.assertEqual(keep.kills, 1)  # history value wins when present

    def test_roster_is_parsed_from_all_players(self):
        m = normalize_metadata(META, 5)
        self.assertEqual([p.account_id for p in m.roster], [5, 6])
        me = m.roster[0]
        self.assertEqual((me.hero_id, me.team, me.kills, me.deaths, me.assists, me.net_worth),
                         (3, 1, 7, 2, 11, 38000))
        # an empty history roster must not shadow the metadata roster
        merged = merge_match(Match(77), m)
        self.assertEqual([p.account_id for p in merged.roster], [5, 6])

    def test_mate_ids_filters_by_match(self):
        rows = [{"mate_id": 10, "matches": [77, 78]},
                {"mate_id": 11, "matches": [78]},
                {"mate_id": 12},  # no match list: keep the API's own filtering
                {"account_id": 13, "matches": [77]},
                "junk", {"matches": [77]}]
        self.assertEqual(mate_ids(rows, 77), {10, 12, 13})
        self.assertEqual(mate_ids(None, 77), set())
        self.assertEqual(mate_ids(rows, 99), {12})


class Assets(unittest.TestCase):
    def test_pick_image_prefers_raster_over_svg(self):
        o = {"images": {"icon_hero_card": "a/b.svg", "icon_image_small": "https://x/y.png"}}
        self.assertEqual(pick_image(o, ("icon_hero_card", "icon_image_small")), "https://x/y.png")
        self.assertEqual(pick_image({"image": "/img/z.webp"}, ("image",)), "https://assets-bucket.deadlock-api.com/img/z.webp")
        self.assertIsNone(pick_image({"image": "q.svg"}, ("image",)))
        self.assertIsNone(pick_image({}, ("image",)))

    def test_parse_items_and_heroes(self):
        items = parse_assets([{"id": 10, "name": "Basic Mag", "image": "https://x/a.webp",
                               "item_slot_type": "EItemSlotType_Weapon", "item_tier": "EModTier_2"},
                              {"id": 11, "name": "Odd"}, {"name": "no id"}, "junk"], ("image",))
        self.assertEqual((items[10].name, items[10].slot, items[10].tier), ("Basic Mag", "weapon", 2))
        self.assertIsNone(items[11].image)
        self.assertEqual(len(items), 2)

    def test_ability_upgrades_have_no_shop_slot(self):
        # the catalogue also lists hero ability upgrades; they must not look like weapon/vitality/spirit items
        items = parse_assets([{"id": 20, "name": "Fireball", "item_slot_type": "EItemSlotType_Ability"}],
                             ("image",))
        self.assertIsNone(items[20].slot)
        wrapped = parse_assets({"heroes": [{"id": 1, "name": "Infernus", "images": {"icon_hero_card": "https://x/h.png"}}]},
                               ("icon_hero_card",))
        self.assertEqual(wrapped[1].image, "https://x/h.png")
        self.assertEqual(parse_assets(None, ("image",)), {})


class SteamIdentity(unittest.TestCase):
    def test_parse_profile_prefers_largest_avatar(self):
        p = parse_steam_profile([{
            "account_id": 1, "personaname": "Mectus", "profileurl": "https://steamcommunity.com/id/x/",
            "avatar": "a.jpg", "avatarmedium": "b.jpg", "avatarfull": "c.jpg", "friends": [],
        }])
        self.assertEqual((p.name, p.avatar_url, p.profile_url),
                         ("Mectus", "c.jpg", "https://steamcommunity.com/id/x/"))
        self.assertEqual(parse_steam_profile({"personaname": "X", "avatarmedium": "b.jpg"}).avatar_url, "b.jpg")
        self.assertEqual(parse_steam_profile({"personaname": "X", "avatar": "a.jpg"}).avatar_url, "a.jpg")

    def test_parse_profile_bad_shapes(self):
        for bad in (None, [], {}, {"personaname": None, "avatarfull": None}, "junk", [None, 3]):
            self.assertIsNone(parse_steam_profile(bad), bad)
        self.assertEqual(parse_steam_profile({"players": [{"personaname": "Y"}]}).name, "Y")

    def test_parse_profiles_batch_is_keyed_by_account(self):
        got = parse_steam_profiles([
            {"account_id": 7, "personaname": "Mectus", "avatarfull": "a.jpg"},
            {"account_id": 8, "personaname": "Nyx", "avatar": "b.jpg"},
            {"account_id": 9},  # no name/avatar: skipped
            {"personaname": "no id"},
            "junk",
        ])
        self.assertEqual(sorted(got), [7, 8])
        self.assertEqual((got[7].name, got[7].avatar_url), ("Mectus", "a.jpg"))
        self.assertEqual(got[8].avatar_url, "b.jpg")
        self.assertEqual(parse_steam_profiles(None), {})

    def test_avatar_and_name_precedence(self):
        sl = types.SimpleNamespace(name="Statlocker", avatar_url="sl.jpg")
        steam = SteamProfile("Steam", "steam.jpg")
        cases = [
            ((sl, steam, "Saved"), ("Statlocker", "steam.jpg")),
            ((sl, None, "Saved"), ("Statlocker", "sl.jpg")),
            ((None, steam, "Saved"), ("Saved", "steam.jpg")),
            ((None, SteamProfile("Steam", None), None), ("Steam", None)),
            ((None, None, None), ("Account 7", None)),
        ]
        for args, want in cases:
            i = resolve_identity(*args, 7)
            self.assertEqual((i.name, i.avatar_url), want, args)


class IngestStatus(unittest.TestCase):
    def test_parse_salts(self):
        s = parse_salts({"match_id": 5, "metadata_salt": 1, "replay_salt": None,
                         "metadata_url": "m", "demo_url": None})
        self.assertEqual((s.match_id, s.metadata_salt, s.replay_salt, s.has_demo), (5, 1, None, False))
        self.assertTrue(parse_salts({"metadata_salt": 1, "demo_url": "d"}).has_demo)
        for bad in (None, [], {}, "x", {"match_id": 1}):
            self.assertIsNone(parse_salts(bad), bad)

    def test_parse_match_info(self):
        row = {"match_id": 135405866, "start_time": "2026-11-11 09:21:25", "winning_team": "Team1",
               "duration_s": 934, "match_mode": "Unranked", "game_mode": "StreetBrawl"}
        i = parse_match_info([row])
        self.assertEqual((i.match_id, i.duration_s, i.mode_label), (135405866, 934, "Street Brawl"))
        self.assertEqual(parse_match_info({"match_id": 1, "match_mode": "Ranked"}).mode_label, "Ranked")
        self.assertIsNone(parse_match_info({"match_id": None}))
        self.assertIsNone(parse_match_info([]))

    def test_verdicts(self):
        cases = [
            ((200, True, 200, None), "full"),
            ((200, False, 200, None), "partial"),
            ((200, None, 200, None), "full"),
            ((404, None, 200, None), "queued"),
            ((404, None, 404, True), "processing"),
            ((404, None, 404, False), "missing"),
            ((None, None, None, None), "unreachable"),
            ((404, None, None, None), "unreachable"),
        ]
        for args, kind in cases:
            self.assertEqual(ingest_verdict(*args).kind, kind, args)


class LocalIconFiles(unittest.TestCase):
    def test_name_matching(self):
        self.assertEqual(norm("Basic Magazine"), norm("basic_magazine_icon"))
        self.assertEqual(norm("Item Basic Magazine v2"), "basicmagazine")
        with tempfile.TemporaryDirectory() as d:
            for n in ("Basic Magazine.png", "extra_health icon.webp", "readme.txt", "Extra Health Regen.png"):
                open(os.path.join(d, n), "wb").write(b"x")
            os.makedirs(os.path.join(d, "WEAPON"))
            open(os.path.join(d, "WEAPON", "Titanic Magazine.png"), "wb").write(b"y")
            os.makedirs(os.path.join(d, "Old Icons"))
            open(os.path.join(d, "Old Icons", "Basic Magazine.png"), "wb").write(b"old")
            open(os.path.join(d, "WEAPON", "Toxic Bullets_Old.png"), "wb").write(b"o")
            open(os.path.join(d, "WEAPON", "Toxic_Bullets.png"), "wb").write(b"t")
            open(os.path.join(d, "aerial_mastery_psd.png"), "wb").write(b"a")
            open(os.path.join(d, "Hunter_sAura.png"), "wb").write(b"h")
            open(os.path.join(d, "TrophyCollector_T2.png"), "wb").write(b"t2")
            open(os.path.join(d, "TrophyCollector_T3.png"), "wb").write(b"t3")
            li = LocalIcons(d)
            self.assertEqual(li.get("Trophy Collector", 2), b"t2")  # tiered name tried first
            self.assertEqual(li.get("Trophy Collector", 3), b"t3")
            self.assertIsNone(li.get("Trophy Collector", 4))  # no T4 file, and no plain fallback either
            self.assertIsNone(li.get("Trophy Collector"))
            self.assertEqual(li.get("Aerial Mastery"), b"a")
            self.assertEqual(li.get("Hunter's Aura"), b"h")
            self.assertEqual(li.get("Toxic Bullets"), b"t")
            self.assertEqual(li.get("Titanic Magazine"), b"y")  # found in a subfolder
            self.assertEqual(li.match_key("Trophy Collector", 2), norm("Trophy Collector T2"))
            self.assertNotEqual(li.match_key("Trophy Collector", 2), norm("Trophy Collector"))  # regression:
            # match_key must report the key actually used (tiered), not the bare item name — an orphan-icon
            # report that only checked norm(name) would wrongly flag this legitimately-matched file
            self.assertEqual(li.get("Basic Magazine"), b"x")
            self.assertEqual(li.get("Extra Health"), b"x")
            self.assertEqual(li.get("Extra Health Regen"), b"x")
            self.assertIsNone(li.get("Extra"))          # no partial matches
            self.assertIsNone(li.get("Unknown Item"))
        self.assertIsNone(LocalIcons("/nonexistent/folder").get("Basic Magazine"))

    def test_folder_case_mismatch(self):
        # Regression: files tracked in git as assets/ITEMS are invisible to a lowercase
        # assets/items lookup on a case-sensitive filesystem (Linux/Docker), even though
        # Windows hides the mismatch entirely. LocalIcons must still find them.
        with tempfile.TemporaryDirectory() as d:
            real = os.path.join(d, "assets", "ITEMS")
            os.makedirs(real)
            open(os.path.join(real, "Trophy Collector.png"), "wb").write(b"t")
            li = LocalIcons(os.path.join(d, "assets", "items"))  # lowercase, doesn't exist as-is
            self.assertEqual(li.get("Trophy Collector"), b"t")


class Stats(unittest.TestCase):
    def test_aggregates(self):
        ms = [
            Match(3, hero_id=1, kills=10, deaths=2, assists=10, duration_s=1200, net_worth=24000, won=True),
            Match(2, hero_id=1, kills=0, deaths=4, assists=0, duration_s=1200, net_worth=12000, won=False),
            Match(1, hero_id=2, kills=5, deaths=0, assists=5, duration_s=600, net_worth=6000, won=True),
        ]
        o = compute_overall(ms)
        self.assertEqual((o.matches, o.wins, o.losses), (3, 2, 1))
        self.assertAlmostEqual(o.winrate, 66.666, places=2)
        self.assertAlmostEqual(o.kda, (15 + 15) / 6)
        self.assertAlmostEqual(o.souls_per_min, (1200 + 600 + 600) / 3)
        self.assertEqual(o.top_heroes[0].hero_id, 1)
        self.assertEqual(o.top_heroes[0].games, 2)
        self.assertEqual((o.streak, o.streak_won), (1, True))  # newest match won, then a loss breaks it

    def test_streaks(self):
        win_streak = compute_overall([Match(i, won=True) for i in range(3, 0, -1)])
        self.assertEqual((win_streak.streak, win_streak.streak_won), (3, True))
        loss_streak = compute_overall([Match(3, won=False), Match(2, won=False), Match(1, won=True)])
        self.assertEqual((loss_streak.streak, loss_streak.streak_won), (2, False))
        unknown = compute_overall([Match(2, won=None), Match(1, won=True)])
        self.assertEqual((unknown.streak, unknown.streak_won), (0, None))  # most recent result missing

    def test_empty_and_no_deaths(self):
        o = compute_overall([])
        self.assertEqual(o.matches, 0)
        self.assertIsNone(o.winrate)
        self.assertIsNone(o.kda)
        z = compute_overall([Match(1, kills=4, deaths=0, assists=2, won=True)])
        self.assertEqual(z.kda, 6.0)


class StatlockerParsing(unittest.TestCase):
    def test_rank_helpers(self):
        self.assertEqual(rank_name(84), "Oracle 4")
        self.assertEqual(rank_name(116), "Eternus 6")
        self.assertIsNone(rank_name(None))
        self.assertIsNone(rank_name(5))
        self.assertTrue(rank_badge_url(84).endswith("/rank8/badge_lg_subrank4.webp"))

    def test_profile_prefers_season1_ranked(self):
        p = parse_profile({"name": "X", "avatarUrl": "u", "ppScore": 4250, "estimatedRankNumber": 84,
                           "season1": {"ranked": {"rankNumber": 64, "ppScore": 3926}, "casual": None}})
        self.assertEqual((p.rank_number, p.pp_score, p.rank_label), (64, 3926, "Season 1 Ranked"))

    def test_profile_falls_back(self):
        p = parse_profile({"name": "X", "ppScore": 4250, "estimatedRankNumber": 84,
                           "season1": {"ranked": None, "casual": None}})
        self.assertEqual((p.rank_number, p.pp_score), (84, 4250))
        p2 = parse_profile({"name": "Y", "ppScore": None, "estimatedRankNumber": None})
        self.assertIsNone(p2.rank_number)

    def test_lobby_by_team(self):
        match = {"amberHandMeanPPScoreRankNumber": 86, "sapphireFlameMeanPPScoreRankNumber": 85,
                 "players": [{"account_id": 123, "team": "Amber Hand"},
                             {"account_id": 456, "team": "Sapphire Flame"}]}
        a = parse_lobby(match, 123)
        self.assertEqual((a.own_rank, a.enemy_rank), (86, 85))
        s = parse_lobby(match, 456)
        self.assertEqual((s.own_rank, s.enemy_rank), (85, 86))
        self.assertIsNone(parse_lobby(match, 999))

    def test_parse_match_players_tolerant(self):
        match = {"players": [
            {"accountId": 10, "team": "Amber Hand", "heroId": 2, "kills": 5, "deaths": 1, "assists": 9,
             "playerDamage": 12000, "netWorth": 30000, "mvpScore": 99.5, "teamMvpPlacement": 1,
             "globalRank": 42, "objectiveDamage": 3400, "killParticipation": 0.62,
             "damagePerMin": 1450.5, "soulsPerMinute": 980.0, "accuracy": 0.41,
             "playerDamageTaken": 4300, "playerHealing": 900, "level": 24,
             "steamProfile": {"name": "A", "avatarUrl": "u", "estimatedRankNumber": 84}},
            {"account_id": 11, "team": "Sapphire Flame", "hero_id": 3, "kills": 2, "deaths": 7, "assists": 4,
             "metrics": {"kill_participation": 55, "objective_damage": 900, "damage_taken": 5000, "healing": 100}},
            {"team": "Amber Hand"},  # no account id: skipped
            "junk",
        ]}
        players = {p.account_id: p for p in parse_match_players(match)}
        self.assertEqual(set(players), {10, 11})
        self.assertEqual((players[10].hero_id, players[10].kills, players[10].hero_damage,
                          players[10].net_worth, players[10].rank_number, players[10].team_mvp_place,
                          players[10].global_rank, players[10].name, players[10].obj_damage,
                          players[10].damage_taken, players[10].healing, players[10].level),
                         (2, 5, 12000, 30000, 84, 1, 42, "A", 3400, 4300, 900, 24))
        self.assertAlmostEqual(players[10].kill_part, 0.62)
        self.assertAlmostEqual(players[10].dmg_per_min, 1450.5)
        self.assertAlmostEqual(players[10].souls_per_min, 980.0)
        self.assertAlmostEqual(players[10].accuracy, 0.41)
        self.assertEqual(players[11].obj_damage, 900)
        self.assertEqual(players[11].kill_part, 55)
        self.assertEqual((players[11].damage_taken, players[11].healing), (5000, 100))
        self.assertIsNone(players[11].rank_number)
        self.assertEqual(parse_match_players({}), [])


class Database(unittest.TestCase):
    def test_creates_missing_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DB(os.path.join(tmp, "nested", "sub", "t.db"))  # directory doesn't exist yet
            db.set_channel(1, 2)
            self.assertEqual(db.get_channel(1), 2)
            db.conn.close()

    def test_links_and_channels(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DB(os.path.join(tmp, "t.db"))
            self.assertIsNone(db.get_link(1, 10))
            db.set_link(1, 10, 169802, 500)
            db.set_link(1, 10, 169802, 600)  # upsert
            self.assertEqual(db.get_link(1, 10).last_match_id, 600)
            db.set_link(1, 10, 169802, 600, "Fadi")
            self.assertEqual(db.get_link(1, 10).display_name, "Fadi")
            db.set_link(1, 10, 169802, 601)  # no name given -> keeps the stored one
            self.assertEqual(db.get_link(1, 10).display_name, "Fadi")
            db.update_last_match(1, 10, 700)
            self.assertEqual(db.all_links()[0].last_match_id, 700)
            db.set_channel(10, 99)
            db.set_channel(10, 100)
            self.assertEqual(db.get_channel(10), 100)
            self.assertIsNone(db.get_channel(11))
            db.set_rank_baseline(1, 10, 4250, 84)
            self.assertEqual((db.get_link(1, 10).last_pp_score, db.get_link(1, 10).last_rank_number), (4250, 84))
            self.assertTrue(db.delete_link(1, 10))
            self.assertFalse(db.delete_link(1, 10))
            db.conn.close()

    def test_guild_settings_and_legacy_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "old.db")
            old = sqlite3.connect(path)
            old.execute("CREATE TABLE channels (guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL)")
            old.execute("INSERT INTO channels VALUES (5, 77)")
            old.commit()
            old.close()

            db = DB(path)
            s = db.get_settings(5)
            self.assertEqual(s.match_channel_id, 77)          # migrated from `channels`
            self.assertIsNone(s.patch_channel_id)
            self.assertEqual(s.channel_for("matches"), 77)
            self.assertEqual(s.channel_for("patches"), 77)    # fallback
            db.set_feature_channel(5, "patches", 88)
            self.assertEqual(db.get_settings(5).channel_for("patches"), 88)
            self.assertEqual(db.all_channels(), [(5, 77)])    # legacy view still works
            db.set_feature_channel(5, "patches", None)
            self.assertEqual(db.get_settings(5).channel_for("patches"), 77)
            db.set_feature_channel(5, "matches", None)   # deliberately cleared
            db.conn.close()
            db = DB(path)
            self.assertIsNone(db.get_settings(5).match_channel_id)  # migration must not resurrect it
            db.conn.close()

    def test_pause_and_match_posts(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DB(os.path.join(tmp, "t.db"))
            db.set_link(1, 10, 169802, 500)
            self.assertFalse(db.set_paused(2, 10, True))      # no link -> nothing paused
            self.assertTrue(db.set_paused(1, 10, True))
            self.assertEqual(db.count_paused_links(), 1)
            db.set_link(1, 10, 169802, 600)                   # /save must not resume
            self.assertEqual(db.get_link(1, 10).paused, 1)
            db.set_paused(1, 10, False)
            self.assertEqual(db.get_link(1, 10).paused, 0)

            db.save_match_post(900, 169802, 600, "Fadi", 1234)
            self.assertEqual(db.get_match_post(900)["match_id"], 600)
            self.assertEqual(db.count_match_posts(), 1)
            db.save_match_post(900, 169802, 601, "Fadi", 1235)  # same message re-saved
            self.assertEqual(db.count_match_posts(), 1)
            self.assertIsNone(db.get_match_post(901))
            db.conn.close()

    def test_match_post_snapshot_and_backfill(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DB(os.path.join(tmp, "t.db"))
            db.save_match_post(900, 169802, 600, "Fadi", 1000, channel_id=55,
                               pp_before=1000, pp_after=1038, rank_before=74, rank_after=75)
            row = db.get_match_post(900)
            self.assertEqual((row["channel_id"], row["pp_before"], row["pp_after"],
                              row["rank_before"], row["rank_after"]), (55, 1000, 1038, 74, 75))
            self.assertEqual(db.count_cards_awaiting_rank(0), 0)  # snapshot complete

            db.save_match_post(901, 1, 700, "Nyx", 2000, channel_id=55)  # no rank data yet
            self.assertEqual(db.count_cards_awaiting_rank(0), 1)
            self.assertEqual(db.count_cards_awaiting_rank(2001), 0)  # outside the window
            self.assertEqual([r["message_id"] for r in db.cards_awaiting_rank(0)], [901])
            db.set_match_post_rank(901, 800, 81)
            self.assertIsNone(db.get_match_post(901)["pp_before"])  # baseline is never invented
            self.assertEqual(db.cards_awaiting_rank(0), [])

            db.save_match_post(902, 2, 701, "Abrams", 2000, channel_id=55)
            db.set_match_post_attempts(902, 3)  # gave up
            self.assertEqual(db.cards_awaiting_rank(0), [])
            self.assertEqual(db.count_cards_awaiting_rank(0), 0)

            # only the newest match per account is eligible, so its delta has a real baseline
            db.save_match_post(903, 3, 710, "Haze", 3000, channel_id=55)
            db.save_match_post(904, 3, 711, "Haze", 3001, channel_id=55)
            self.assertEqual([r["message_id"] for r in db.cards_awaiting_rank(0)], [904])
            db.conn.close()

    def test_migrates_old_match_posts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.db")
            conn = sqlite3.connect(path)  # the schema from before the snapshot columns
            conn.execute(
                "CREATE TABLE match_posts (message_id INTEGER PRIMARY KEY, account_id INTEGER "
                "NOT NULL, match_id INTEGER NOT NULL, display_name TEXT, created_at INTEGER NOT NULL)")
            conn.commit()
            conn.close()
            db = DB(path)
            db.save_match_post(1, 1, 1, None, 1, channel_id=2, pp_after=3)
            row = db.get_match_post(1)
            self.assertEqual((row["pp_after"], row["backfill_attempts"]), (3, 0))
            db.conn.close()

    def test_links_for_guild_and_baseline_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DB(os.path.join(tmp, "t.db"))
            db.set_link(1, 10, 169802, 500)
            db.set_link(2, 10, 1, 1)
            db.set_link(3, 20, 2, 1)
            self.assertEqual({l.discord_id for l in db.links_for_guild(10)}, {1, 2})
            db.set_rank_baseline(1, 10, 5000, 74)
            db.set_link(1, 10, 169802, 600)  # same account: baseline kept
            self.assertEqual(db.get_link(1, 10).last_pp_score, 5000)
            db.set_link(1, 10, 999, 1)       # a different account must not inherit it
            self.assertIsNone(db.get_link(1, 10).last_pp_score)
            self.assertIsNone(db.get_link(1, 10).last_rank_number)
            db.conn.close()


class MatchesCard(unittest.TestCase):
    def test_renders_without_error(self):
        import cards
        data = cards.MatchesCardData(
            name="Mectus", account_id=180817836,
            matches=[
                cards.MatchRow("Graves", True, 2, 6, 22, 133810241, 1794163216, 3067,
                               mode="Ranked", mode_kind="ranked"),
                cards.MatchRow("Seven", False, 10, 8, 5, 133800000, 1794150000, 1800,
                               mode="Brawl", mode_kind="brawl"),
                cards.MatchRow("Unknown", None, None, None, None, None, None, None),  # unfinished/missing data
            ],
        )
        png = cards.render_matches_card(data)
        self.assertGreater(len(png.getvalue()), 0)

    def test_renders_with_no_matches(self):
        import cards
        png = cards.render_matches_card(cards.MatchesCardData(name="Mectus", account_id=1))
        self.assertGreater(len(png.getvalue()), 0)


class MatchTypes(unittest.TestCase):
    def test_labels_and_kinds(self):
        cases = [
            (Match(1, game_mode=1, match_mode=4), ("ranked", "Ranked")),
            (Match(1, game_mode=1, match_mode=1), ("standard", "Standard")),
            (Match(1, game_mode=1, match_mode=3, bot_difficulty=1), ("bot", "Bot Match · Easy")),
            (Match(1, game_mode=4, match_mode=1), ("brawl", "Street Brawl")),
            (Match(1, game_mode=4, match_mode=3, bot_difficulty=3), ("brawl", "Street Brawl · Hard Bots")),
            (Match(1, game_mode=1, match_mode=2), ("other", "Private Lobby")),
            (Match(1, game_mode=1, match_mode=8), ("other", "New Player")),
            (Match(1), ("other", "Unknown")),  # old payloads without mode fields
        ]
        for match, want in cases:
            self.assertEqual(match_type(match), want)

    def test_history_entries_parse_mode_and_team(self):
        m = normalize_entry({"match_id": 1, "game_mode": 4, "match_mode": 1, "player_team": 0})
        self.assertEqual((m.game_mode, m.match_mode, m.team), (4, 1, 0))

    def test_brawl_score_and_average(self):
        m = Match(1, team=0, brawl_rounds=[BrawlRound(0, 100), BrawlRound(1, 120), BrawlRound(0, 90)])
        self.assertEqual(m.brawl_score, (2, 1))
        self.assertEqual(m.brawl_avg_round_s, 103)  # round(310/3)
        self.assertIsNone(Match(1).brawl_score)
        self.assertIsNone(Match(1, brawl_rounds=[BrawlRound(0, 100)]).brawl_score)  # no team to compare


class MetadataStats(unittest.TestCase):
    def test_picks_latest_snapshot_and_computes_rates(self):
        data = {"match_info": {
            "match_id": 1, "game_mode": 1, "match_mode": 4, "ranked_type": 0,
            "players": [{
                "account_id": 7, "hero_id": 2, "team": 1, "level": 24,
                "stats": [
                    {"time_stamp_s": 180, "player_damage": 100, "shots_hit": 1, "shots_missed": 1},
                    {"time_stamp_s": 858, "player_damage": 17883, "shots_hit": 1913, "shots_missed": 1321,
                     "hero_bullets_hit": 1759, "hero_bullets_hit_crit": 226, "bullet_kills": 9,
                     "creep_kills": 31, "possible_creeps": 38},
                    {"time_stamp_s": 400, "player_damage": 999},  # out of order on purpose
                ],
            }],
        }}
        m = normalize_metadata(data, 7, 1)
        self.assertEqual((m.game_mode, m.match_mode, m.ranked_type, m.team), (1, 4, 0, 1))
        self.assertEqual(m.level, 24)
        self.assertEqual(m.stats.hero_damage, 17883)  # highest timestamp, not the last element
        self.assertAlmostEqual(m.stats.accuracy, 1913 / 3234 * 100)
        self.assertAlmostEqual(m.stats.crit_rate, 226 / 1759 * 100)

    def test_missing_stats_is_none(self):
        m = normalize_metadata({"match_info": {"players": [{"account_id": 7, "stats": []}]}}, 7, 1)
        self.assertIsNone(m.stats)

    def test_stats_rates_with_no_shots(self):
        s = MatchStats(shots_hit=0, shots_missed=0)
        self.assertIsNone(s.accuracy)
        self.assertIsNone(s.crit_rate)
        self.assertIsNone(MatchStats().accuracy)

    def test_merge_keeps_history_mode_and_metadata_stats(self):
        base = Match(1, game_mode=4, match_mode=1)
        detail = Match(1, game_mode=1, match_mode=4, stats=MatchStats(hero_damage=5),
                       brawl_rounds=[BrawlRound(0, 100)])
        merged = merge_match(base, detail)
        self.assertEqual((merged.game_mode, merged.match_mode), (4, 1))  # history wins
        self.assertEqual(merged.stats.hero_damage, 5)                    # metadata fills the gap
        self.assertEqual(len(merged.brawl_rounds), 1)                    # empty base list doesn't shadow it


class RankSnapshotTests(unittest.TestCase):
    def test_delta_and_promotion(self):
        snap = RankSnapshot(pp_before=1000, pp_after=1038, rank_before=74, rank_after=75)
        self.assertEqual(snap.pp_delta, 38)
        self.assertTrue(snap.rank_changed)
        self.assertTrue(snap.promoted)
        demoted = RankSnapshot(1000, 960, 75, 74)
        self.assertEqual(demoted.pp_delta, -40)
        self.assertFalse(demoted.promoted)
        same = RankSnapshot(1000, 1010, 74, 74)
        self.assertFalse(same.rank_changed)
        self.assertFalse(same.promoted)

    def test_missing_side_means_no_claim(self):
        first = RankSnapshot(rank_after=74)  # no baseline yet
        self.assertIsNone(first.pp_delta)
        self.assertFalse(first.rank_changed)
        self.assertFalse(first.promoted)
        self.assertEqual(RankSnapshot(1000, 1000).pp_delta, 0)  # no change, but known

    def test_big_jump_still_one_promotion(self):
        snap = RankSnapshot(rank_before=84, rank_after=91)  # Oracle 4 -> Phantom 1
        self.assertTrue(snap.promoted)


class TopLadder(unittest.TestCase):
    def _p(self, pp, rank):
        from statlocker import Profile
        return Profile("x", None, rank, "Season 1 Ranked", pp)

    def test_sorts_by_pp_then_rank_then_name(self):
        entries = [("b", self._p(1000, 50)), ("a", self._p(1000, 60)),
                   ("c", self._p(1200, 40)), ("d", self._p(1000, 60))]
        body, shown, unranked = format_top(entries)
        self.assertEqual((shown, unranked), (4, 0))
        self.assertEqual([line.split("**")[1] for line in body.splitlines()], ["c", "a", "d", "b"])

    def test_truncates_and_counts_unranked(self):
        entries = [(f"p{i}", self._p(i, 50)) for i in range(12)]
        entries += [("nobody", None), ("fresh", self._p(None, None))]
        body, shown, unranked = format_top(entries, limit=10)
        self.assertEqual((shown, unranked), (10, 2))
        self.assertEqual(len(body.splitlines()), 10)
        self.assertIn("p11", body.splitlines()[0])  # highest PP first


class MatchCardStats(unittest.TestCase):
    def _card(self, **kw):
        import cards
        base = dict(name="Mectus", hero="Infernus", match_id=1, won=True, kills=10, deaths=2, assists=8,
                    duration_s=1800, start_time=1794163216, net_worth=30000, last_hits=150, denies=8)
        base.update(kw)
        return cards.render_match_card(cards.MatchCardData(**base))

    def test_renders_with_stats_and_mode(self):
        png = self._card(mode="Ranked", mode_kind="ranked",
                         stats=MatchStats(hero_damage=17883, shots_hit=1913, shots_missed=1321))
        self.assertGreater(len(png.getvalue()), 0)

    def test_renders_brawl_round_score(self):
        png = self._card(mode="Street Brawl", mode_kind="brawl", brawl_score=(3, 1), brawl_round_avg_s=154,
                         stats=MatchStats(hero_damage=1))
        self.assertGreater(len(png.getvalue()), 0)

    def test_renders_without_stats(self):
        png = self._card()  # old payload: no stats, no mode
        self.assertGreater(len(png.getvalue()), 0)

    def test_renders_rank_transition(self):
        png = self._card(rank_name="Phantom 1", rank_prev="Oracle 4", rank_up=True,
                         pp_score=1038, pp_delta=38)
        self.assertGreater(len(png.getvalue()), 0)
        demoted = self._card(rank_name="Oracle 4", rank_prev="Oracle 5", rank_up=False,
                             pp_score=900, pp_delta=-40)
        self.assertGreater(len(demoted.getvalue()), 0)

    def test_renders_party_panel(self):
        import cards
        party = [cards.PartyMemberView("Mectus", "Abrams", 7, 5, 19, 41230),
                 cards.PartyMemberView("Nyx", "Haze", 14, 2, 9, 52880),
                 cards.PartyMemberView("Unknown", "Hero #99", None, None, None, None)]
        png = self._card(party=party, stats=MatchStats(hero_damage=1))
        self.assertGreater(len(png.getvalue()), 0)

    def test_renders_scoreboard_and_lobby_badges(self):
        import cards
        own = cards.ScoreTeam("Amber Hand", True, [
            cards.ScoreRow("Infernus", "Me", 10, 2, 8, 30000, 15000, is_self=True, mvp=True),
            cards.ScoreRow("Haze", "Ally", 5, 4, 6, 22000, 9000),
        ])
        enemy = cards.ScoreTeam("Sapphire Flame", False, [
            cards.ScoreRow("Seven", "Foe", 3, 9, 4, 18000, 7000),
        ])
        board = cards.Scoreboard(own=own, enemy=enemy)
        with_board = self._card(scoreboard=board, own_lobby_rank="Oracle 5",
                                enemy_lobby_rank="Oracle 3")
        without = self._card()
        self.assertGreater(len(with_board.getvalue()), 0)
        self.assertGreater(len(without.getvalue()), 0)
        # scoreboard adds height
        from PIL import Image
        self.assertGreater(Image.open(with_board).size[1], Image.open(without).size[1])


if __name__ == "__main__":
    unittest.main()
