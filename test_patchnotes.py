"""Tests for Steam patch-note parsing, rendering and storage (no network, no Discord)."""
import html
import os
import sqlite3
import tempfile
import unittest

import steam_news
from db import DB
from patchnote_views import hero_picker_content, page_slice
from steam_news import (PatchNote, extract_hero_changes, find_new_notes, html_to_lines,
                        is_patch_note, parse_feed, preview, render_hero_content,
                        render_patch_content)

HEROES = ["Apollo", "Baba", "Mo & Krill", "The Doorman", "Nurse Harrow", "Celeste",
          "Deadman Danny", "Rat King", "Sinclair", "Seven"]


def _item(title, body, link="https://store.steampowered.com/news/app/1422450/view/1",
          pub="Thu, 05 Nov 2026 19:00:00 +0000", guid=None):
    return (
        "<item>"
        f"<title>{html.escape(title)}</title>"
        f"<link><![CDATA[{link}]]></link>"
        f'<guid isPermaLink="false">{html.escape(guid or link)}</guid>'
        f"<pubDate>{pub}</pubDate>"
        f"<description>{html.escape(body)}</description>"
        "</item>"
    )


def _feed(*items):
    return '<?xml version="1.0"?><rss version="2.0"><channel>' + "".join(items) + "</channel></rss>"


def _body(*lines, heading=None):
    parts = []
    if heading:
        parts.append(f'<div class="bb_h3">{html.escape(heading)}</div>')
    parts.extend(f'<p class="bb_paragraph">{html.escape(line)}</p>' for line in lines)
    return "".join(parts)


class FeedParsing(unittest.TestCase):
    def test_parses_fields_lines_and_headings(self):
        xml = _feed(_item("Minor Update - 11-04-2026",
                          _body("- Apollo: Riposte - Pommel Hit now also disarms the enemy",
                                "General changes here", heading="Gameplay Update"),
                          link="https://example/view/42", guid="g-42"))
        notes = parse_feed(xml, HEROES)
        self.assertEqual(len(notes), 1)
        note = notes[0]
        self.assertEqual((note.title, note.link, note.guid),
                         ("Minor Update - 11-04-2026", "https://example/view/42", "g-42"))
        self.assertGreater(note.pub_ts, 0)
        self.assertIn("General changes here", note.lines)
        self.assertIn("Gameplay Update", note.lines)
        self.assertEqual(note.heroes, {"Apollo": ["Riposte - Pommel Hit now also disarms the enemy"]})

    def test_html_entities_are_decoded(self):
        lines = html_to_lines('<div class="bb_h3">Hero Changes</div>'
                              '<p class="bb_paragraph">- Mo &amp; Krill: 50% &gt; 60%</p>')
        self.assertEqual(lines, ["Hero Changes", "- Mo & Krill: 50% > 60%"])

    def test_literal_less_than_is_not_eaten(self):
        lines = html_to_lines('<p class="bb_paragraph">- Apollo: damage &lt; 50 and &gt; 10</p>')
        self.assertEqual(lines, ["- Apollo: damage < 50 and > 10"])

    def test_empty_description_is_not_a_crash(self):
        xml = _feed(_item("Minor Update", ""))
        self.assertEqual(parse_feed(xml, HEROES)[0].lines, [])


class HeroExtraction(unittest.TestCase):
    def test_hero_prefixes_and_item_exclusion(self):
        changes = extract_hero_changes([
            "- Apollo: Riposte - Pommel Hit now also disarms the enemy",
            "- Mo & Krill: Bullet damage increased",
            "- Doorman: Doorway duration increased",
            "- Nurse: Healing reduced",
            "- Healbane: Stacks now diminish faster",
            "- Radiant Regeneration: Heal increased",
            "- Street Brawl: Round time changed",
            "- Unknown Hero: something",
        ], HEROES)
        self.assertEqual(set(changes), {"Apollo", "Mo & Krill", "The Doorman", "Nurse Harrow"})
        self.assertEqual(changes["The Doorman"], ["Doorway duration increased"])
        self.assertEqual(changes["Nurse Harrow"], ["Healing reduced"])

    def test_short_prefixes_need_an_exact_match(self):
        self.assertEqual(extract_hero_changes(["- Sev: x"], ["Seven"]), {})
        self.assertEqual(extract_hero_changes(["- Seven: x"], ["Seven"]), {"Seven": ["x"]})

    def test_ambiguous_prefix_is_ignored(self):
        self.assertEqual(extract_hero_changes(["- The: x"], ["The Doorman", "The Boss"]), {})


class Detection(unittest.TestCase):
    def note(self, title, heroes=0):
        return PatchNote("g", title, "l", 0, [], {f"Hero {i}": [] for i in range(heroes)})

    def test_update_titles(self):
        self.assertTrue(is_patch_note(self.note("Minor Update - 11-04-2026")))
        self.assertTrue(is_patch_note(self.note(" Update - 10-05-2026")))
        self.assertFalse(is_patch_note(self.note("Ranked - Beta Season 2")))
        self.assertFalse(is_patch_note(self.note("Paint the Town")))

    def test_themed_update_needs_hero_lines(self):
        self.assertTrue(is_patch_note(self.note("Halloween 2026 + Gameplay Update", 5)))
        self.assertFalse(is_patch_note(self.note("Halloween 2026 + Gameplay Update", 4)))


class Rendering(unittest.TestCase):
    def make_note(self, count=50):
        return PatchNote("g", "Minor Update - 11-04-2026", "https://x", 1762378800,
                         [f"- Apollo: change number {i}" for i in range(count)],
                         {"Apollo": ["change"]})

    def test_preview_and_length_cap(self):
        note = self.make_note()
        shown, remaining = preview(note, 8)
        self.assertEqual((len(shown), remaining), (8, 42))
        content = render_patch_content(note)
        self.assertLessEqual(len(content), steam_news.MAX_MESSAGE_CHARS)
        self.assertIn("42 more changes", content)
        self.assertTrue(content.startswith("**Minor Update - 11-04-2026**"))
        self.assertIn("1 hero changed", content)

    def test_long_lines_are_trimmed(self):
        note = self.make_note(8)
        note.lines = ["- Apollo: " + "x" * 400 for _ in range(8)]
        self.assertLessEqual(len(render_patch_content(note)), steam_news.MAX_MESSAGE_CHARS)

    def test_hero_content_chunks(self):
        note = PatchNote("g", "T", "l", 0, [],
                         {"Apollo": [f"change {i} " + "x" * 100 for i in range(40)]})
        first, extra = render_hero_content(note, "Apollo")
        self.assertTrue(extra)
        for chunk in [first, *extra]:
            self.assertLessEqual(len(chunk), steam_news.MAX_MESSAGE_CHARS)
        self.assertIn("change 39", extra[-1])

    def test_hero_content_without_changes(self):
        note = PatchNote("g", "T", "l", 0, [], {})
        first, extra = render_hero_content(note, "Apollo")
        self.assertIn("Apollo", first)
        self.assertEqual(extra, [])


class NewNotes(unittest.TestCase):
    def notes(self, count=6):
        # newest first, like Steam
        return [PatchNote(f"g{i}", f"t{i}", "l", i, [], {}) for i in range(count)]

    def test_new_notes_are_oldest_first(self):
        new, baseline = find_new_notes(self.notes(4), "g2")
        self.assertEqual([n.guid for n in new], ["g1", "g0"])
        self.assertEqual(baseline, "g0")

    def test_no_baseline_is_silent(self):
        new, baseline = find_new_notes(self.notes(4), None)
        self.assertEqual((new, baseline), ([], "g0"))

    def test_nothing_new(self):
        self.assertEqual(find_new_notes(self.notes(4), "g0"), ([], "g0"))

    def test_missing_guid_baselines_without_posting(self):
        self.assertEqual(find_new_notes(self.notes(4), "gone"), ([], "g0"))

    def test_cap_keeps_the_newest_notes(self):
        new, baseline = find_new_notes(self.notes(6), "g4", cap=2)
        self.assertEqual([n.guid for n in new], ["g1", "g0"])
        self.assertEqual(baseline, "g0")


class PatchStorage(unittest.TestCase):
    def test_channels_and_notes_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DB(os.path.join(tmp, "t.db"))
            self.assertIsNone(db.get_patch_channel(1))
            db.set_channel(1, 10)
            db.set_channel(2, 20)
            self.assertEqual(sorted(db.all_channels()), [(1, 10), (2, 20)])
            db.set_patch_baseline(1, "g1", 100)
            self.assertEqual(db.get_patch_channel(1).last_guid, "g1")
            self.assertEqual(db.get_patch_channel(1).announced, 0)
            db.set_patch_baseline(1, "g2", 200, announced=True)  # upsert
            self.assertEqual(db.all_patch_channels()[0].last_pub_ts, 200)
            self.assertEqual(db.get_patch_channel(1).announced, 1)

            note = PatchNote("g2", "Minor Update", "https://x", 200,
                             ["- Apollo: x"], {"Apollo": ["x"]})
            db.save_patch_note(note, 300)
            loaded = db.get_patch_note("g2")
            self.assertEqual((loaded.title, loaded.link, loaded.lines, loaded.heroes),
                             ("Minor Update", "https://x", ["- Apollo: x"], {"Apollo": ["x"]}))
            self.assertEqual([n.guid for n in db.all_patch_notes()], ["g2"])
            self.assertIsNone(db.get_patch_note("nope"))
            db.conn.close()

    def test_announced_column_is_added_to_an_old_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.db")
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE patch_channels (guild_id INTEGER PRIMARY KEY, "
                         "last_guid TEXT, last_pub_ts INTEGER NOT NULL DEFAULT 0)")
            conn.execute("INSERT INTO patch_channels VALUES (1, 'g1', 100)")
            conn.commit()
            conn.close()
            db = DB(path)
            state = db.get_patch_channel(1)
            self.assertEqual((state.last_guid, state.announced), ("g1", 0))
            db.set_patch_baseline(1, "g1", 100, announced=True)
            self.assertEqual(db.get_patch_channel(1).announced, 1)
            db.conn.close()

    def test_notes_are_pruned_to_100(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DB(os.path.join(tmp, "t.db"))
            for i in range(105):
                db.save_patch_note(PatchNote(f"g{i}", "t", "l", i, [], {}), i)
            self.assertEqual(len(db.all_patch_notes()), 100)
            self.assertIsNone(db.get_patch_note("g0"))
            self.assertIsNotNone(db.get_patch_note("g104"))
            db.conn.close()


class Paging(unittest.TestCase):
    def test_page_slice(self):
        items = [f"h{i}" for i in range(33)]
        chunk, page, pages = page_slice(items, 0)
        self.assertEqual((len(chunk), page, pages), (25, 0, 2))
        chunk, page, pages = page_slice(items, 1)
        self.assertEqual((len(chunk), page, pages), (8, 1, 2))
        self.assertEqual(page_slice(items, 99)[1], 1)   # clamps
        self.assertEqual(page_slice(items, -5)[1], 0)   # clamps

    def test_picker_content_shows_the_page(self):
        note = PatchNote("g", "Minor Update", "l", 0, [], {f"Hero{i}": [] for i in range(33)})
        self.assertIn("Page 1/2", hero_picker_content(note, 0))
        self.assertIn("Page 2/2", hero_picker_content(note, 1))


if __name__ == "__main__":
    unittest.main()
