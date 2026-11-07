# SPDX-License-Identifier: AGPL-3.0-only
"""#6092 (manaty/ameesh#4642, écart 5) : l'empreinte se rafraîchit depuis le
diff de branche lu à la forge, sans clone local ; l'occupation d'un lot se
libère après un run échoué sans clore le lot.

Constat : `work footprint --refresh` exigeait un clone (`--repo`) ; et rien ne
rendait l'empreinte d'un lot `build` sans le fermer — un lot réservé à un
exécutant externe n'a pas d'assigné, `work return` le refusait, et `build →
intake` n'est pas une transition."""
from __future__ import annotations

import argparse
import json
import subprocess
from unittest import mock

from ameesh import storage, work, work_footprints as fp
from ameesh.storage.postgres.work import WorkItems

from .test_4173_empreinte import _Empreintes


def _gh(answers):
    """Un faux `gh` : `answers` associe un préfixe d'URL d'API à une réponse
    (dict, ou `(code, texte)`), et garde les appels."""
    calls = []

    def run(args):
        calls.append(list(args))
        url = args[1] if len(args) > 1 else ""
        for prefix, answer in answers.items():
            if url.startswith(prefix):
                if isinstance(answer, tuple):
                    return subprocess.CompletedProcess(args, answer[0], "", answer[1])
                return subprocess.CompletedProcess(args, 0, json.dumps(answer), "")
        return subprocess.CompletedProcess(args, 1, "", "HTTP 404: Not Found")

    run.calls = calls
    return run


COMPARE = {"status": "ahead", "base_commit": {"sha": "b" * 40},
           "files": [{"filename": "src/a.py", "status": "modified"},
                     {"filename": "src/new.py", "status": "renamed",
                      "previous_filename": "src/old.py"},
                     {"filename": "docs/x.md", "status": "added"}]}


class ForgeRefreshTest(_Empreintes):
    def lot_with_branch(self, **kw):
        kw.setdefault("branch", "lot/6092-x")
        kw.setdefault("issue_ref", "manaty/nexlink#12")
        return self.lot("lot externe", **kw)

    def test_sans_clone_le_diff_se_lit_a_la_forge(self):
        item = self.lot_with_branch(branch_target="develop")
        gh = _gh({"repos/manaty/nexlink/compare/": COMPARE})
        with mock.patch.object(fp, "_clone_and_target", return_value=(None, "")):
            result = fp.refresh(self.db, self.cfg, item["id"], actor="ima", gh=gh)
        self.assertEqual(gh.calls, [["api", "repos/manaty/nexlink/compare/develop...lot/6092-x"]])
        # un renommage compte ses deux chemins, comme `git diff --no-renames`
        self.assertEqual(fp.paths_of(self.db, item["id"]),
                         ["docs/x.md", "src/a.py", "src/new.py", "src/old.py"])
        self.assertEqual((result["source"], result["fresh"], result["target"], result["base"]),
                         ("forge", True, "develop", "b" * 40))
        rows = storage.of(self.db).work_footprints.for_items([item["id"]])
        self.assertEqual({r["source"] for r in rows}, {"diff"})

    def test_forge_explicite_meme_avec_un_clone(self):
        item = self.lot_with_branch(branch_target="origin/develop")
        gh = _gh({"repos/manaty/nexlink/compare/": COMPARE})
        with mock.patch.object(fp, "_clone_and_target") as clone:
            fp.refresh(self.db, self.cfg, item["id"], actor="ima", forge=True,
                       head="c" * 40, gh=gh)
        clone.assert_not_called()
        self.assertEqual(gh.calls[0][1],
                         "repos/manaty/nexlink/compare/develop...%s" % ("c" * 40))
        heads = {r["head"] for r in storage.of(self.db).work_footprints.for_items([item["id"]])}
        self.assertEqual(heads, {"c" * 40})

    def test_sans_cible_la_branche_par_defaut_de_la_forge(self):
        item = self.lot_with_branch()
        gh = _gh({"repos/manaty/nexlink/compare/": COMPARE,
                  "repos/manaty/nexlink": {"default_branch": "trunk"}})
        fp.refresh(self.db, self.cfg, item["id"], actor="ima", forge=True, gh=gh)
        self.assertEqual(gh.calls[-1][1], "repos/manaty/nexlink/compare/trunk...lot/6092-x")

    def test_liste_tronquee_ou_forge_muette_n_ecrit_rien(self):
        item = self.lot_with_branch(branch_target="develop")
        self.footprint(item, "src/a.py", source="diff")
        full = dict(COMPARE, files=[{"filename": "f%d.py" % i}
                                    for i in range(fp.FORGE_FILES_CAP)])
        for gh in (_gh({"repos/manaty/nexlink/compare/": full}),
                   _gh({"repos/manaty/nexlink/compare/": (1, "HTTP 404: Not Found")})):
            with self.assertRaises(work.WorkError):
                fp.refresh(self.db, self.cfg, item["id"], actor="ima", forge=True, gh=gh)
            self.assertEqual(fp.paths_of(self.db, item["id"]), ["src/a.py"])

    def test_un_repo_invalide_reste_un_refus(self):
        item = self.lot_with_branch(branch_target="develop")
        gh = _gh({"repos/manaty/nexlink/compare/": COMPARE})
        with self.assertRaises(work.WorkError):
            fp.refresh(self.db, self.cfg, item["id"], actor="ima",
                       repo=self.tmp + "/pas-un-depot", gh=gh)
        self.assertEqual(gh.calls, [])


class ReleaseTest(_Empreintes):
    def occupied(self, assignee="claude1", state="build"):
        premier = self.lot("run échoué", branch="lot/1-x")
        second = self.lot("attend")
        self.footprint(premier, "src/a.py")
        self.footprint(second, "src")
        self.db.execute("UPDATE work_items SET state = %s, assignee = %s WHERE id = %s",
                        (state, assignee, premier["id"]))
        self.assertTrue(storage.of(self.db).work_footprints.overlapping(second["id"]))
        return premier, second

    def test_liberation_par_l_assigne_rend_le_voisin_prenable(self):
        self.agent("claude1", team="nexlink")
        premier, second = self.occupied()
        out = fp.release(self.db, self.cfg, premier["id"], actor="claude1",
                         reason="run échoué")
        self.assertEqual((out["released"], out["before"], out["previous_assignee"]),
                         (True, "build", "claude1"))
        after = work.get(self.db, premier["id"])
        self.assertEqual((after["state"], after["assignee"], after["branch"]),
                         ("intake", None, "lot/1-x"))
        # l'empreinte reste : seule l'occupation tombe
        self.assertEqual(fp.paths_of(self.db, premier["id"]), ["src/a.py"])
        self.assertEqual(storage.of(self.db).work_footprints.overlapping(second["id"]), [])
        self.assertIsNotNone(WorkItems(self.db).assign(
            second["id"], "claude1", current=None, note="t", actor="t", ready_only=True))
        notes = [r["note"] for r in self.db.query(
            "SELECT note FROM work_item_events WHERE work_item_id = %s", (premier["id"],))]
        self.assertTrue(any("occupation released by claude1" in n and "run échoué" in n
                            for n in notes), notes)

    def test_autorite_et_etats(self):
        self.agent("claude1", team="nexlink")
        self.agent("claude2", team="nexlink")
        premier, _second = self.occupied()
        with self.assertRaises(work.WorkError):
            fp.release(self.db, self.cfg, premier["id"], actor="claude2")
        self.assertEqual(work.get(self.db, premier["id"])["assignee"], "claude1")
        self.assertTrue(fp.release(self.db, self.cfg, premier["id"],
                                   actor="human:proprio")["released"])
        # idempotent : un lot qui n'occupe rien ne s'écrit pas
        self.assertFalse(fp.release(self.db, self.cfg, premier["id"],
                                    actor="human:proprio")["released"])
        self.db.execute("UPDATE work_items SET state = 'qa' WHERE id = %s", (premier["id"],))
        with self.assertRaises(work.WorkError):
            fp.release(self.db, self.cfg, premier["id"], actor="human:proprio")

    def test_lot_reserve_libere_par_son_executant_garde_la_reservation(self):
        self.agent("ima", harness="codex")
        self.db.execute("UPDATE agent_registry SET mode = 'externe' WHERE name = 'ima'")
        self.agent("claude1", team="nexlink")
        premier = work.add(self.db, title="réservé", app="nexlink", external_executor="ima",
                           actor="human:proprio", cfg=self.cfg)
        second = self.lot("attend")
        self.footprint(premier, "src/a.py")
        self.footprint(second, "src")
        self.db.execute("UPDATE work_items SET state = 'build' WHERE id = %s", (premier["id"],))
        self.assertTrue(storage.of(self.db).work_footprints.overlapping(second["id"]))
        with self.assertRaises(work.WorkError):
            fp.release(self.db, self.cfg, premier["id"], actor="claude1")
        self.assertTrue(fp.release(self.db, self.cfg, premier["id"], actor="ima")["released"])
        after = work.get(self.db, premier["id"])
        self.assertEqual((after["state"], after["external_executor"]), ("intake", "ima"))
        self.assertEqual(storage.of(self.db).work_footprints.overlapping(second["id"]), [])
        # la réservation tient : L165 ne le prend pas
        self.assertNotIn(premier["id"],
                         [i["id"] for i in storage.of(self.db).work.unassigned_lots(10)])

    def test_cas_un_lot_change_entre_lecture_et_ecriture(self):
        self.agent("claude1", team="nexlink")
        premier, _second = self.occupied()
        store = storage.of(self.db).work_footprints
        self.assertIsNone(store.release(premier["id"], state="build", assignee="claude2",
                                        note="t", actor="t"))
        self.assertIsNone(store.release(premier["id"], state="blocked", assignee="claude1",
                                        note="t", actor="t"))
        self.assertEqual(work.get(self.db, premier["id"])["state"], "build")

    def test_cli_forge_sans_refresh_refuse(self):
        parser = argparse.ArgumentParser()
        fp.add_parser(parser.add_subparsers(), func=None)
        item = self.lot("x")
        for argv in (["footprint", str(item["id"]), "--forge"],
                     ["footprint", str(item["id"]), "--refresh", "--forge", "--repo", "."]):
            with self.assertRaises(work.WorkError):
                fp.run(self.cfg, self.db, parser.parse_args(argv))
