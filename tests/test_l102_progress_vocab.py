# SPDX-License-Identifier: AGPL-3.0-only
"""L102 T3 (#4393): the progress page in the active module's vocabulary.

progress.py and progress_page.html take the work item's titles, labels and
columns from ``vocab.active()``, carried by the snapshot (``vocabulary``):
DEV ("lot") through the dev module, CORE ("tâche") when no active module
declares a vocabulary. JSON keys (``lots``, ``lots_merged``…) and states
stay unchanged.
"""
from __future__ import annotations

import json
import re
import unittest
from unittest import mock

from ameesh import progress, vocab
from ameesh.modules import dev

from .test_progress import NOW, _snap

EPICS = [{"id": "E1", "title": "Épopée", "lots_total": 3, "lots_merged": 1,
          "lots_abandoned": 1}]
MILESTONES = [{"date": "2026-11-01", "title": "Jalon", "lots_total": 2,
               "lots_merged": 1}]
AGENT = {"name": "a1", "harness": "codex", "model": None, "effort": None,
         "state": "idle", "since_ts": NOW - 60, "status_text": "", "turn": None,
         "unread": 0, "builds": 3}


def _text(**over):
    snap = _snap(epics=EPICS, milestones=MILESTONES, agents=[AGENT], build_cap=2,
                 truncated={"lots": {"shown": 1, "total": 9, "limit": 1}}, **over)
    return progress.format_text(snap, width=100)


class _VocabularyCase(unittest.TestCase):
    def setUp(self):
        vocab.reset()
        self.addCleanup(vocab.reset)

    def core(self):
        return mock.patch.object(vocab, "active", return_value=vocab.CORE)


class SnapshotFieldTest(_VocabularyCase):
    def test_dev_is_the_default(self):
        self.assertEqual(progress.vocabulary_view(), {
            "singular": "lot", "plural": "lots", "gender": "m", "title": "Lot",
            "title_plural": "Lots", "abbreviation": "lot(s)"})
        self.assertEqual(progress.vocabulary_view(), progress.vocabulary_view(dev.DEV))

    def test_core(self):
        with self.core():
            self.assertEqual(progress.vocabulary_view(), {
                "singular": "tâche", "plural": "tâches", "gender": "f",
                "title": "Tâche", "title_plural": "Tâches",
                "abbreviation": "tâche(s)"})


class TextRenderingTest(_VocabularyCase):
    def test_dev_text_is_unchanged(self):
        text = _text()
        self.assertIn("LOTS (1)", text)
        self.assertIn("TRUNCATED: 1 of 9 lots shown (limit 1)", text)
        self.assertIn("E1 — Épopée: 1/2 lots merged (1 abandoned)", text)
        self.assertIn("2026-11-01 Jalon — 1/2 lots merged", text)
        self.assertIn("3 lots in build (above the §4 cap of 2)", text)
        self.assertIn("no lot in the window", _text(lots=[]))
        self.assertIn("1 lot(s) in build", progress._wip_label(1, None))

    def test_core_text(self):
        with self.core():
            text = _text(lots=[dict(_snap()["lots"][0], title="Titre")])
            self.assertIn("TÂCHES (1)", text)
            self.assertIn("TRUNCATED: 1 of 9 tâches shown (limit 1)", text)
            self.assertIn("E1 — Épopée: 1/2 tâches merged (1 abandoned)", text)
            self.assertIn("2026-11-01 Jalon — 1/2 tâches merged", text)
            self.assertIn("3 tâches in build (above the §4 cap of 2)", text)
            self.assertIn("no tâche in the window", _text(lots=[]))
            self.assertIn("1 tâche(s) in build", progress._wip_label(1, None))
            self.assertNotIn("LOTS", text)
            self.assertNotRegex(text, r"\blots?\b")

    def test_a_dependency_wait_uses_the_vocabulary(self):
        # #4729 : le texte CORE suit planned_wait.what=dependency, comme la page
        lots = [dict(_snap()["lots"][0], title="Titre",
                     planned_wait={"what": "dependency", "lots": [12, 34]})]
        self.assertIn("waiting on lots #12, #34", _text(lots=lots))
        with self.core():
            text = _text(lots=lots)
        self.assertIn("waiting on tâches #12, #34", text)
        self.assertNotRegex(text, r"\blots?\b")

    def test_the_snapshot_vocabulary_wins(self):
        # a snapshot rendered elsewhere keeps the vocabulary it was built with
        text = _text(vocabulary=progress.vocabulary_view(vocab.CORE))
        self.assertIn("TÂCHES (1)", text)
        self.assertIn("3 tâches in build", text)


class PageTest(_VocabularyCase):
    def setUp(self):
        super().setUp()
        with open(progress.PAGE, encoding="utf-8") as fh:
            self.page = fh.read()
        # the script without its comments: what the page shows
        self.code = re.sub(r"^\s*//.*$", "", self.page, flags=re.M)

    def test_the_vocabulary_is_embedded(self):
        for words in (dev.DEV, vocab.CORE):
            html = progress.render_html(_snap(vocabulary=progress.vocabulary_view(words)))
            data = re.search(r'<script type="application/json" id="data">(.*?)</script>',
                             html, re.S).group(1)
            self.assertEqual(json.loads(data)["vocabulary"]["plural"], words.plural)

    def test_no_hard_coded_work_noun(self):
        for literal in re.findall(r'"([^"\n]*)"', self.code):
            if re.fullmatch(r"[a-z_-]+", literal):
                continue  # an identifier (key, id, tab name), not a label
            self.assertNotRegex(literal, r"(?i)\bwork items?\b|\blots\b|\blot #|\btasks?\b"
                                r"|\blots? merged|\bwaiting on lots",
                                "hard-coded work noun in the page: %r" % literal)

    def test_the_fallback_is_core(self):
        # without `vocabulary` (an older snapshot), the page says CORE's words
        fallback = re.search(r"var CORE_W = (\{.*?\});", self.page).group(1)
        fallback = json.loads(re.sub(r"(\w+):", r'"\1":', fallback))
        view = progress.vocabulary_view(vocab.CORE)
        view.pop("gender")
        self.assertEqual(fallback, view)

    def test_the_tab_is_named_by_the_vocabulary(self):
        self.assertIn('["lots", null]', self.page)
        self.assertIn('t[0] === "lots" ? W().title_plural : t[1]', self.page)
        self.assertIn('el("button", "tab", tabLabel(t))', self.page)
        self.assertIn("b.textContent = tabLabel(t);", self.page)


if __name__ == "__main__":
    unittest.main()
