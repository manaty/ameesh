# SPDX-License-Identifier: AGPL-3.0-only
"""L234a : une erreur de lecture ne bloque que le canon propriétaire."""
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock

from . import support  # isole la configuration et l'identité du poste
from . import test_l234_auditor as l234
from ameesh import auditor, canon as canon_mod, work
from ameesh.config import CanonEntry


class CanonGuardTest(unittest.TestCase):
    take = l234.AuditorTest.take

    def setUp(self):
        support.patch_mechanics(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        # #2451 : le plafond §4 passe par le canon compilé ; ces témoins
        # n'ouvrent pas de dépôt et gardent la fixture de `AuditorTest.take`.
        cap = mock.patch.object(work, "build_limit", return_value=support.FIXTURE_BUILD_CAP)
        cap.start()
        self.addCleanup(cap.stop)
        self.a = Path(temporary.name) / "A"
        self.b = Path(temporary.name) / "B"
        self.a.mkdir()
        self.b.mkdir()
        self.cfg = types.SimpleNamespace(host=None, canon_entries=[
            CanonEntry(str(self.a), "", True), CanonEntry(str(self.b), "", True)])
        self.lot = {"id": 1, "type": "evolution", "title": "Evolution",
                    "state": "intake", "created_ts": 100, "lot_team": "ameesh"}
        self.queue = {"id": 2, "type": "improvement", "title": "Optimisation",
                      "team": "ameesh"}

    def malformed(self, root, path, type_line=""):
        card = root / path
        card.parent.mkdir(parents=True, exist_ok=True)
        card.write_text("---\n" + type_line + "title: controle\nroles: [auditor\n---\n",
                        encoding="utf-8")

    def assert_takes(self, owner, count, name="dev"):
        for item, queue in ((self.lot, False), (self.queue, True)):
            with self.subTest(owner=owner, queue=queue, name=name):
                result = self.take(item, name, queue, owner=owner, cfg=self.cfg)
                self.assertEqual(len(result), count)

    def test_non_agent_frontmatter_error_only_blocks_its_own_canon(self):
        self.malformed(self.a, "docs/notes.md")
        loaded = auditor.load_all(self.cfg, strict=False)
        self.assertEqual(loaded[0].load_findings[0].severity, canon_mod.WARNING)
        self.assert_takes(None, 0)
        self.assert_takes("A", 0)
        self.assert_takes("B", 1)

    def test_malformed_auditor_cards_still_refuse_in_their_own_canon(self):
        for type_line in ("type: Agent\n", ""):
            for path in ("agents/controle.md", "personas/controle.md"):
                with self.subTest(type_line=type_line, path=path):
                    self.malformed(self.b, path, type_line)
                    self.assert_takes("B", 0, "controle")
                    self.assert_takes(None, 1)
                    (self.b / path).unlink()

    def test_bad_default_canon_does_not_mask_auditor_role_in_other_canon(self):
        self.malformed(self.a, "docs/notes.md")
        cards = self.b / "agents"
        cards.mkdir()
        (cards / "controle.md").write_text(
            "---\ntype: Agent\ntitle: controle\nroles: [auditor]\n---\n", encoding="utf-8")
        self.assert_takes("B", 0, "controle")
        self.assert_takes("B", 0, "auditeur")
        self.assert_takes("B", 1)

    def test_missing_owner_never_borrows_another_canon(self):
        self.assert_takes("absent", 0)
        self.assert_takes(None, 1)
        self.assert_takes("B", 1)

    def test_load_errors_and_unread_scopes_are_scoped_to_the_owner(self):
        for fault in ("unreadable", "scope", "error"):
            with self.subTest(fault=fault):
                a = l234.canon([], ident="A")
                b = l234.canon([], ident="B", default=False)
                if fault == "unreadable":
                    a.readable = False
                elif fault == "scope":
                    a.scope_unread = {"owner"}
                else:
                    a.load_findings = [canon_mod.Finding(
                        "canon-git-error", canon_mod.ERROR, "objet git illisible")]
                with mock.patch("ameesh.canon.from_config",
                                side_effect=lambda cfg, entry: a if entry.path == str(self.a) else b):
                    self.assert_takes(None, 0)
                    self.assert_takes("B", 1)

    def test_duplicate_identities_still_refuse_all_takes(self):
        with mock.patch("ameesh.canon.from_config", return_value=l234.canon([], ident="duplicate")):
            self.assert_takes(None, 0)
            self.assert_takes("duplicate", 0)
