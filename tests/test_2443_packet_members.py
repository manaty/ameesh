# SPDX-License-Identifier: AGPL-3.0-only
"""#2443 : composition de paquet — lier chaque unité au paquet (package_id).

La composition est lue dans les commits de fusion (`ameesh-work:`), jamais
dans le manifeste. Les appartenances écrites servent ensuite à `work show` et
à `sync-merges`, y compris pour une unité sans jalon `frozen` (cas #2366).
"""
from __future__ import annotations

import json
import os
import threading
import time
import unittest
from unittest import mock

from ameesh import (db as db_mod, packet_head, packet_link, plan_git, registry,
                    storage, work)

from .support import PgTestCase
from .test_canon import _TmpMixin, git


class PacketLinkTest(_TmpMixin, PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = os.path.join(self.make_tmp(), "depot")
        os.makedirs(self.repo)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "remote", "add", "origin", "https://github.com/acme/web")
        self.base = self.commit("README", "acme\n", "initial")

    # -- dépôt jetable ------------------------------------------------------
    def commit(self, path: str, text: str, message: str) -> str:
        full = os.path.join(self.repo, path)
        os.makedirs(os.path.dirname(full) or self.repo, exist_ok=True)
        with open(full, "a", encoding="utf-8") as fh:
            fh.write(text)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", message)
        return git(self.repo, "rev-parse", "HEAD")

    def unit_branch(self, branch: str, path: str, text: str, message: str) -> str:
        git(self.repo, "checkout", "-q", "-b", branch, self.base)
        sha = self.commit(path, text, message)
        git(self.repo, "checkout", "-q", "main")
        return sha

    def compose(self, branch: str, units: list[tuple[int, str]]) -> str:
        """Compose `units` sur une branche de paquet ; rend la tête."""
        git(self.repo, "checkout", "-q", "-b", branch, self.base)
        for lot, sha in units:
            git(self.repo, "merge", "-q", "--no-ff", "-m",
                "vehicule : #%d %s\n\nameesh-work: %d" % (lot, sha[:12], lot), sha)
        head = git(self.repo, "rev-parse", "HEAD")
        return head

    def lot(self, title: str) -> dict:
        return work.add(self.db, title=title, issue_ref="acme/web#1", actor="pilote")

    def freeze(self, item: dict, sha: str) -> None:
        for state in ("build", "qa"):
            work.move(self.db, item["id"], state)
        work.milestone(self.db, item["id"], "frozen", sha=sha, actor="relecteur")

    def verdict(self, item: dict, sha: str) -> None:
        for state in ("build", "qa"):
            work.move(self.db, item["id"], state)
        # Cas #2366 : aucun gel n'est requis pour ce témoin de composition ;
        # l'avis est posé au stockage, la porte d'écriture est testée ailleurs.
        storage.of(self.db).work.add_milestone(
            item["id"], "verdict", sha=sha, actor="relecteur", verdict="ok",
            note="")

    def carrier(self, branch: str = "integration/paquet-1", target: str = "main") -> dict:
        return work.add(self.db, title="paquet", issue_ref="acme/web#1",
                        branch=branch, branch_target=target, actor="pilote",
                        source="composition")

    def membership(self, lot: int) -> list[dict]:
        return packet_link.for_item(self.db, lot)

    # -- lecture de la composition -----------------------------------------
    def test_record_reads_trailers_and_writes_membership(self):
        first = self.unit_branch("lot/cinq", "src/un.py", "un\n", "un")
        second = self.unit_branch("lot/six", "src/deux.py", "deux\n", "deux")
        one = self.lot("un")
        two = self.lot("deux")
        self.freeze(one, first)
        self.freeze(two, second)
        head = self.compose("integration/paquet-1", [(one["id"], first), (two["id"], second)])
        porteur = self.carrier()
        report = packet_link.record(self.db, porteur["id"], repo=self.repo,
                                    base=self.base, head=head, actor="pilote",
                                    cfg=self.cfg)
        self.assertTrue(report["recorded"])
        self.assertEqual({u["work_item_id"] for u in report["units"]},
                         {one["id"], two["id"]})
        rows = self.membership(one["id"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["package_id"], porteur["id"])
        self.assertEqual(rows[0]["package_branch"], "integration/paquet-1")
        self.assertEqual(rows[0]["unit_sha"], report["units"][0]["unit_sha"])
        self.assertEqual(rows[0]["frozen_sha"], first)
        self.assertEqual(rows[0]["source_sha"], first)
        self.assertEqual(rows[0]["package_state"], "intake")
        # le journal du porteur dit ce qui a été lié
        notes = [e["note"] for e in work.events(self.db, porteur["id"])]
        self.assertTrue(any("packet composition recorded" in n for n in notes), notes)

    def test_work_show_and_packet_show_read_the_link(self):
        sha = self.unit_branch("lot/sept", "src/trois.py", "trois\n", "trois")
        item = self.lot("trois")
        self.freeze(item, sha)
        head = self.compose("integration/paquet-1", [(item["id"], sha)])
        porteur = self.carrier()
        packet_link.record(self.db, porteur["id"], repo=self.repo, base=self.base,
                           head=head, actor="pilote", cfg=self.cfg)
        out = self.mesh("work", "show", str(item["id"]))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("package  : #%d integration/paquet-1" % porteur["id"], out.stdout)
        out = self.mesh("work", "packet", "show", str(item["id"]), "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        payload = json.loads(out.stdout)
        self.assertEqual(payload["as_unit"][0]["package_id"], porteur["id"])
        out = self.mesh("work", "packet", "show", str(porteur["id"]))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("unit #%d" % item["id"], out.stdout)

    def test_record_cli_and_dry_run(self):
        sha = self.unit_branch("lot/huit", "src/quatre.py", "quatre\n", "quatre")
        item = self.lot("quatre")
        self.freeze(item, sha)
        head = self.compose("integration/paquet-1", [(item["id"], sha)])
        porteur = self.carrier()
        out = self.mesh("work", "packet", "record", str(porteur["id"]),
                        "--repo", self.repo, "--base", self.base, "--head", head,
                        "--dry-run")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("[dry run]", out.stdout)
        self.assertEqual(self.membership(item["id"]), [])
        out = self.mesh("work", "packet", "record", str(porteur["id"]),
                        "--repo", self.repo, "--base", self.base, "--head", head)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("packet composition recorded", out.stdout)
        self.assertEqual(self.membership(item["id"])[0]["package_id"], porteur["id"])

    def test_record_refuses_a_second_composer_on_a_foreign_pushed_head(self):
        """#4501 : le manifeste d'une tête poussée (gelée) n'est pas écrasé.

        Témoin h-12 du 29/10 : le tic a recomposé un porteur dont la ci-full
        tournait et ses 9 unités locales ont remplacé les 27 de la tête
        poussée. Un enregistrement d'une autre tête est refusé tant que le
        composeur qui la détient ne le dit pas (`--expect-pushed`).
        """
        sha = self.unit_branch("lot/vingt", "src/vingt.py", "vingt\n", "vingt")
        item = self.lot("vingt")
        self.freeze(item, sha)
        head = self.compose("integration/paquet-1", [(item["id"], sha)])
        porteur = self.carrier()
        packet_link.record(self.db, porteur["id"], repo=self.repo,
                           base=self.base, head=head, actor="pilote", cfg=self.cfg)
        work.milestone(self.db, porteur["id"], "frozen", sha=head, actor="pilote")
        # la même tête poussée se réenregistre sans cérémonie (pilote, rejeu)
        again = packet_link.record(self.db, porteur["id"], repo=self.repo,
                                   base=self.base, head=head, actor="pilote",
                                   cfg=self.cfg)
        self.assertTrue(again["recorded"])
        # un second composeur repart de la base et fabrique sa propre tête
        git(self.repo, "checkout", "-q", "-B", "integration/paquet-1", self.base)
        git(self.repo, "merge", "-q", "--no-ff", "-m",
            "vehicule : #%d %s\n\nameesh-work: %d"
            % (item["id"], sha[:12], item["id"]), sha)
        other = self.commit("manifeste.md", "# second\n", "manifeste du second")
        with self.assertRaises(packet_link.PacketLinkError) as caught:
            packet_link.record(self.db, porteur["id"], repo=self.repo,
                               base=self.base, head=other, actor="second",
                               cfg=self.cfg)
        self.assertIn("already has the pushed head", str(caught.exception))
        self.assertEqual(
            {m["head_sha"] for m in
             packet_link.memberships(self.db, package_ids=[porteur["id"]])},
            {head})
        # un second composeur qui croit encore la tête distante ancienne ne
        # passe pas non plus : `expect_pushed` ne vaut pas la tête gelée.
        with self.assertRaises(packet_link.PacketLinkError) as stale:
            packet_link.record(self.db, porteur["id"], repo=self.repo,
                               base=self.base, head=other,
                               expect_pushed=self.base, actor="second",
                               cfg=self.cfg)
        self.assertIn("already has the pushed head", str(stale.exception))
        # le composeur qui détient la tête poussée peut la remplacer
        report = packet_link.record(self.db, porteur["id"], repo=self.repo,
                                    base=self.base, head=other,
                                    expect_pushed=head, actor="premier",
                                    cfg=self.cfg)
        self.assertTrue(report["recorded"])
        self.assertEqual(
            {m["head_sha"] for m in
             packet_link.memberships(self.db, package_ids=[porteur["id"]])},
            {other})

    def test_record_refuses_a_newer_observed_push_before_its_freeze(self):
        """#4501 : une poussée vue après le gel protège aussi le manifeste.

        Entre la poussée et son gel (quelques secondes), le gel déclaré est
        encore l'ancien : `branch_head`, relevé de la forge, est la trace
        partagée de la nouvelle poussée. Un second composeur dont la lecture
        distante est antérieure ne l'écrase pas.
        """
        sha = self.unit_branch("lot/vingt-et-un", "src/vingt-et-un.py", "x\n", "x")
        item = self.lot("vingt-et-un")
        self.freeze(item, sha)
        head = self.compose("integration/paquet-1", [(item["id"], sha)])
        porteur = self.carrier()
        packet_link.record(self.db, porteur["id"], repo=self.repo,
                           base=self.base, head=head, actor="pilote", cfg=self.cfg)
        work.milestone(self.db, porteur["id"], "frozen", sha=head, actor="pilote")
        # une poussée plus récente est vue par le veilleur, pas encore gelée
        pushed = self.commit("pousse.md", "# poussée\n", "poussée du premier")
        self.assertTrue(storage.of(self.db).work.set_branch_head(
            porteur["id"], porteur["branch"], pushed))
        # un second composeur repart de la base et fabrique sa propre tête
        git(self.repo, "checkout", "-q", "-B", "integration/paquet-1", self.base)
        git(self.repo, "merge", "-q", "--no-ff", "-m",
            "vehicule : #%d %s\n\nameesh-work: %d"
            % (item["id"], sha[:12], item["id"]), sha)
        other = self.commit("manifeste.md", "# second\n", "manifeste du second")
        with self.assertRaises(packet_link.PacketLinkError) as caught:
            packet_link.record(self.db, porteur["id"], repo=self.repo,
                               base=self.base, head=other,
                               expect_pushed=head, actor="second", cfg=self.cfg)
        self.assertIn("already has the pushed head %s" % pushed[:12],
                      str(caught.exception))
        self.assertEqual(
            {m["head_sha"] for m in
             packet_link.memberships(self.db, package_ids=[porteur["id"]])},
            {head})

    def test_local_composition_head_is_not_recorded_as_pushed(self):
        """#4562 : une tête locale non poussée n'est pas un relevé de la forge.

        Témoin h-06 du 29/10 : `paquet-auto` compose dans le clone du pilote ;
        le balayage de l'assigné y voit la pointe locale (unité 7) et
        l'enregistre comme `branch_head`. Le record de la composition finale
        est ensuite refusé « already has the pushed head » (#4501), alors que
        la forge n'a jamais reçu cette pointe. Le relevé ne doit retenir une
        tête de porteur que si elle est publiée ; le record final, avec le bail
        de la tête réellement poussée, passe alors.
        """
        root = self.make_tmp()
        bare = os.path.join(root, "acme", "web.git")
        os.makedirs(os.path.dirname(bare))
        git(root, "init", "-q", "--bare", bare)
        # le dépôt de composition garde son origine GitHub (pour `packet
        # record`) et pousse vers la forge locale par un second remote ; le
        # clone de l'assigné, lui, lit cette forge locale.
        git(self.repo, "remote", "add", "forge", bare)
        git(self.repo, "push", "-q", "forge", "main")
        sweep = os.path.join(root, "sweep")
        git(root, "clone", "-q", bare, sweep)
        registry.upsert(self.db, "dev1", harness="claude", host=self.cfg.host,
                        cwd=sweep)
        sha = self.unit_branch("lot/trente-et-un", "src/trente-et-un.py",
                               "trente et un\n", "trente et un")
        item = self.lot("trente et un")
        self.freeze(item, sha)
        pushed = self.compose("integration/paquet-1", [(item["id"], sha)])
        porteur = self.carrier()
        work.assign(self.db, porteur["id"], "dev1", actor="pilote")
        packet_link.record(self.db, porteur["id"], repo=self.repo,
                           base=self.base, head=pushed, actor="pilote",
                           cfg=self.cfg)
        git(self.repo, "push", "-q", "forge", "integration/paquet-1")
        work.milestone(self.db, porteur["id"], "frozen", sha=pushed,
                       actor="pilote", repo=self.repo)
        git(sweep, "fetch", "-q", "origin")
        git(sweep, "checkout", "-q", "-B", "integration/paquet-1",
            "origin/integration/paquet-1")
        # recomposition locale : la branche repart de la base et avance jusqu'à
        # une tête intermédiaire (une unité) que la forge ne porte pas encore.
        git(self.repo, "checkout", "-q", "-B", "integration/paquet-1", self.base)
        git(self.repo, "merge", "-q", "--no-ff", "-m",
            "vehicule : #%d %s (recomposition)\n\nameesh-work: %d"
            % (item["id"], sha[:12], item["id"]), sha)
        intermediate = git(self.repo, "rev-parse", "HEAD")
        self.assertNotEqual(intermediate, pushed)
        git(sweep, "fetch", "-q", self.repo,
            "integration/paquet-1:refs/ameesh/test/intermediate")
        git(sweep, "reset", "-q", "--hard", "refs/ameesh/test/intermediate")
        report = plan_git.sync_branches(self.db, host=self.cfg.host)
        entry = next(row for row in report["results"]
                     if int(row["work_item"]) == int(porteur["id"]))
        self.assertEqual(entry["result"], "open", entry)
        self.assertEqual(entry["how"], "unpublished", entry)
        self.assertIn("not published", entry["detail"])
        self.assertIsNone(work.get(self.db, porteur["id"])["branch_head"])
        # la composition finale (le manifeste au-dessus de l'unité) s'enregistre
        # avec le bail de la tête réellement poussée : plus de refus #4501.
        final = self.commit("reviews/integration-paquet-1.md",
                            "# paquet\n", "paquet : manifeste final")
        recorded = packet_link.record(self.db, porteur["id"], repo=self.repo,
                                      base=self.base, head=final,
                                      expect_pushed=pushed, actor="pilote",
                                      cfg=self.cfg)
        self.assertTrue(recorded["recorded"])
        self.assertEqual(
            {member["head_sha"] for member in
             packet_link.memberships(self.db, package_ids=[porteur["id"]])},
            {final})
        # une fois la tête poussée, le balayage la retient de nouveau.
        git(self.repo, "push", "-q", "--force", "forge", "integration/paquet-1")
        git(sweep, "fetch", "-q", "origin")
        git(sweep, "reset", "-q", "--hard", "origin/integration/paquet-1")
        plan_git.sync_branches(self.db, host=self.cfg.host)
        self.assertEqual(work.get(self.db, porteur["id"])["branch_head"], final)

    def test_record_rechecks_the_pushed_head_under_the_carrier_lock(self):
        """#4501 (B cx3) : un gel qui avance entre la lecture et l'écriture.

        La lecture initiale de `packet_link.record` voit F1 et accepte
        `expect_pushed=F1` ; un premier composeur gèle F2 avant la transaction
        d'écriture. `record_packet` relit la tête sous le verrou du porteur et
        refuse : les membres de F1 restent intacts.
        """
        sha = self.unit_branch("lot/vingt-deux", "src/vingt-deux.py", "y\n", "y")
        item = self.lot("vingt-deux")
        self.freeze(item, sha)
        head = self.compose("integration/paquet-1", [(item["id"], sha)])
        porteur = self.carrier()
        packet_link.record(self.db, porteur["id"], repo=self.repo,
                           base=self.base, head=head, actor="pilote", cfg=self.cfg)
        work.milestone(self.db, porteur["id"], "frozen", sha=head, actor="pilote")
        # la tête poussée/gelée pendant la course
        raced = self.commit("course.md", "# course\n", "gel concurrent")
        # un second composeur repart de la base et fabrique sa propre tête
        git(self.repo, "checkout", "-q", "-B", "integration/paquet-1", self.base)
        git(self.repo, "merge", "-q", "--no-ff", "-m",
            "vehicule : #%d %s\n\nameesh-work: %d"
            % (item["id"], sha[:12], item["id"]), sha)
        other = self.commit("manifeste.md", "# second\n", "manifeste du second")
        real = packet_link._protected_pushed_head

        def racing(db_arg, package, repo):
            protected = real(db_arg, package, repo)
            # le premier composeur gèle F2 juste après la lecture initiale
            work.milestone(self.db, porteur["id"], "frozen", sha=raced,
                           actor="premier")
            return protected

        with mock.patch.object(packet_link, "_protected_pushed_head",
                               side_effect=racing):
            with self.assertRaises(packet_link.PacketLinkError) as caught:
                packet_link.record(self.db, porteur["id"], repo=self.repo,
                                   base=self.base, head=other,
                                   expect_pushed=head, actor="second", cfg=self.cfg)
        self.assertIn("already has the pushed head %s" % raced[:12],
                      str(caught.exception))
        self.assertEqual(
            {m["head_sha"] for m in
             packet_link.memberships(self.db, package_ids=[porteur["id"]])},
            {head})

    def test_concurrent_records_serialize_on_the_carrier_lock(self):
        """#4501 (B cx3/auditeur) : deux enregistrements concurrents ne se mélangent pas.

        Deux connexions appellent `record_packet` sur le même porteur avec le
        même `expect_pushed` (la tête gelée), des têtes et des unités
        différentes. Le verrou `FOR NO KEY UPDATE` du porteur sérialise : le
        second attend le premier avant de relire la tête, puis écrit son
        ensemble complet. Avec deux `FOR SHARE` compatibles, il passerait
        pendant que le premier dort et les membres pourraient se mélanger.
        """
        head = self.base
        carrier = self.carrier()
        work.milestone(self.db, carrier["id"], "frozen", sha=head, actor="pilote")
        unit_a = self.lot("unit A")
        unit_b = self.lot("unit B")
        conn_a, conn_b = db_mod.connect(self.cfg), db_mod.connect(self.cfg)
        self.addCleanup(conn_a.close)
        self.addCleanup(conn_b.close)
        locked = threading.Event()
        real = packet_head.protected_head

        def slow_a(*args):
            if threading.current_thread().name == "record-a":
                locked.set()
                time.sleep(0.6)
            return real(*args)

        def record(conn, head_sha, unit):
            storage.of(conn).work.record_packet(
                int(carrier["id"]), branch="integration/paquet-1",
                base_sha=self.base, head_sha=head_sha,
                units=[{"work_item_id": int(unit["id"]), "unit_sha": "d" * 40,
                        "source_sha": self.base, "frozen_sha": self.base}],
                note="concurrence", actor="test", expect_pushed=head)

        done: dict = {}

        def run_a():
            try:
                record(conn_a, "2" * 40, unit_a)
                done["a"] = True
            except Exception as exc:  # noqa: BLE001 - remonté par le témoin
                done["a_error"] = exc

        with mock.patch.object(packet_head, "protected_head", side_effect=slow_a):
            thread = threading.Thread(target=run_a, name="record-a", daemon=True)
            thread.start()
            self.assertTrue(locked.wait(5), "le premier n'a pas pris le verrou")
            started = time.monotonic()
            record(conn_b, "3" * 40, unit_b)
            elapsed = time.monotonic() - started
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertNotIn("a_error", done)
        self.assertGreaterEqual(elapsed, 0.4,
                                "le second n'a pas attendu le verrou du porteur")
        self.assertEqual(
            {m["work_item_id"] for m in
             packet_link.memberships(self.db, package_ids=[carrier["id"]])},
            {unit_b["id"]})

    def test_record_reuses_the_recorded_base_after_a_target_update(self):
        """Une mise à jour de la cible dans le paquet ne perd pas les unités."""
        first = self.unit_branch("lot/treize", "src/neuf.py", "neuf\n", "neuf")
        one = self.lot("un avant la mise à jour")
        self.freeze(one, first)
        head = self.compose("integration/paquet-1", [(one["id"], first)])
        porteur = self.carrier()
        packet_link.record(self.db, porteur["id"], repo=self.repo,
                           base=self.base, head=head, actor="pilote", cfg=self.cfg)
        # la cible avance, puis le paquet la fusionne (commit sans trailer)
        git(self.repo, "checkout", "-q", "main")
        target_head = self.commit("MAJ", "mise à jour\n", "mise à jour de la cible")
        git(self.repo, "checkout", "-q", "integration/paquet-1")
        git(self.repo, "merge", "-q", "--no-ff", "-m", "mise à jour cible", target_head)
        second = self.unit_branch("lot/quatorze", "src/dix.py", "dix\n", "dix")
        two = self.lot("après la mise à jour")
        self.freeze(two, second)
        git(self.repo, "checkout", "-q", "integration/paquet-1")
        git(self.repo, "merge", "-q", "--no-ff", "-m",
            "vehicule : #%d %s\n\nameesh-work: %d" % (two["id"], second[:12], two["id"]),
            second)
        new_head = git(self.repo, "rev-parse", "HEAD")
        report = packet_link.record(self.db, porteur["id"], repo=self.repo,
                                    head=new_head, actor="pilote", cfg=self.cfg)
        self.assertEqual(report["base"], self.base)
        self.assertEqual({u["work_item_id"] for u in report["units"]},
                         {one["id"], two["id"]})
        self.assertEqual({m["work_item_id"] for m in
                          packet_link.memberships(self.db, package_ids=[porteur["id"]])},
                         {one["id"], two["id"]})

    def test_record_recomposed_on_newer_target_ignores_target_merges(self):
        """Un paquet refait sur une cible plus récente ne reprend pas l'ancienne
        base : la première parente de la cible porte aussi des trailers."""
        first = self.unit_branch("lot/quinze", "src/onze.py", "onze\n", "onze")
        one = self.lot("composé dans ce paquet")
        self.freeze(one, first)
        head = self.compose("integration/paquet-1", [(one["id"], first)])
        porteur = self.carrier()
        packet_link.record(self.db, porteur["id"], repo=self.repo,
                           base=self.base, head=head, actor="pilote", cfg=self.cfg)
        # un AUTRE paquet entre dans la cible, avec son trailer
        other_sha = self.unit_branch("lot/autre", "src/autre.py", "autre\n", "autre")
        other = self.lot("livré par un autre paquet")
        self.freeze(other, other_sha)
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "merge", "-q", "--no-ff", "-m",
            "fusion paquet-0\n\nameesh-work: %d" % other["id"], other_sha)
        new_base = git(self.repo, "rev-parse", "HEAD")
        # ce paquet est refait depuis la nouvelle tête de la cible
        git(self.repo, "checkout", "-q", "-B", "integration/paquet-1", new_base)
        git(self.repo, "merge", "-q", "--no-ff", "-m",
            "vehicule : #%d\n\nameesh-work: %d" % (one["id"], one["id"]), first)
        new_head = git(self.repo, "rev-parse", "HEAD")
        report = packet_link.record(self.db, porteur["id"], repo=self.repo,
                                    head=new_head, actor="pilote", cfg=self.cfg)
        self.assertEqual(report["base"], new_base)
        self.assertEqual({u["work_item_id"] for u in report["units"]}, {one["id"]})
        self.assertEqual(packet_link.for_item(self.db, other["id"]), [])

    def test_record_refuses_a_carrier_without_declared_branch(self):
        """#3905 (h-35 #3879) : `--branch` seul ne fait pas un véhicule."""
        sha = self.unit_branch("lot/dix", "src/dix.py", "dix\n", "dix")
        item = self.lot("dix")
        self.freeze(item, sha)
        head = self.compose("integration/paquet-1", [(item["id"], sha)])
        porteur = work.add(self.db, title="paquet", issue_ref="acme/web#1",
                           actor="pilote", source="composition")
        with self.assertRaises(packet_link.PacketLinkError) as ctx:
            packet_link.record(self.db, porteur["id"], repo=self.repo,
                               base=self.base, head=head,
                               branch="integration/paquet-1", actor="pilote",
                               cfg=self.cfg)
        self.assertIn("ameesh work branch %d integration/paquet-1" % porteur["id"],
                      str(ctx.exception))
        self.assertEqual(packet_link.memberships(self.db, package_ids=[porteur["id"]]), [])
        # la branche posée, la même composition s'enregistre
        work.set_branch(self.db, porteur["id"], "integration/paquet-1", "main",
                        actor="pilote")
        report = packet_link.record(self.db, porteur["id"], repo=self.repo,
                                    base=self.base, head=head, actor="pilote",
                                    cfg=self.cfg)
        self.assertTrue(report["recorded"])

    def test_record_refuses_unknown_unit_and_foreign_repo(self):
        sha = self.unit_branch("lot/neuf", "src/cinq.py", "cinq\n", "cinq")
        item = self.lot("cinq")
        self.freeze(item, sha)
        git(self.repo, "checkout", "-q", "-b", "integration/paquet-1", self.base)
        git(self.repo, "merge", "-q", "--no-ff", "-m",
            "vehicule : #999999\n\nameesh-work: 999999", sha)
        head = git(self.repo, "rev-parse", "HEAD")
        porteur = self.carrier()
        with self.assertRaises(packet_link.PacketLinkError) as ctx:
            packet_link.record(self.db, porteur["id"], repo=self.repo,
                               base=self.base, head=head, actor="pilote", cfg=self.cfg)
        self.assertIn("does not exist", str(ctx.exception))
        self.assertEqual(packet_link.memberships(self.db, package_ids=[porteur["id"]]), [])
        # un dépôt qui n'est pas celui du porteur ne s'attache pas
        foreign = os.path.join(self.make_tmp(), "autre")
        os.makedirs(foreign)
        git(foreign, "init", "-q", "-b", "main")
        git(foreign, "remote", "add", "origin", "https://github.com/autre/depot")
        with self.assertRaises(packet_link.PacketLinkError) as ctx:
            packet_link.record(self.db, porteur["id"], repo=foreign,
                               base=self.base, head=head, actor="pilote", cfg=self.cfg)
        self.assertIn("not of acme/web", str(ctx.exception))

    # -- sync-merges --------------------------------------------------------
    def test_sync_merges_closes_by_composition_without_freeze(self):
        """Cas #2366 par la composition : aucun jalon `frozen` n'est requis."""
        verdict_sha = self.unit_branch("lot/dix", "src/six.py", "six\n", "six")
        plain_sha = self.unit_branch("lot/onze", "src/sept.py", "sept\n", "sept")
        with_verdict = self.lot("verdict seul")
        without_freeze = self.lot("sans gel")
        self.verdict(with_verdict, verdict_sha)
        head = self.compose("integration/paquet-1",
                            [(with_verdict["id"], verdict_sha),
                             (without_freeze["id"], plain_sha)])
        porteur = self.carrier()
        packet_link.record(self.db, porteur["id"], repo=self.repo, base=self.base,
                           head=head, actor="pilote", cfg=self.cfg)
        # le gel d'origine verdict est bien celui enregistré, sans jalon frozen
        rows = self.membership(with_verdict["id"])
        self.assertEqual(rows[0]["frozen_sha"], verdict_sha)
        self.assertEqual(self.membership(without_freeze["id"])[0]["frozen_sha"], "")
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "merge", "-q", "--no-ff", "-m", "fusion paquet",
            "integration/paquet-1")
        dry = plan_git.sync_merges(self.db, self.repo, target="main", dry_run=True)
        dry_by_lot = {r["work_item"]: r for r in dry["results"]}
        for item in (with_verdict, without_freeze):
            self.assertEqual(dry_by_lot[item["id"]]["result"], "would-merge")
        self.assertEqual(work.get(self.db, with_verdict["id"])["state"], "qa")
        report = plan_git.sync_merges(self.db, self.repo, target="main")
        by_lot = {r["work_item"]: r for r in report["results"]}
        for item in (with_verdict, without_freeze):
            result = by_lot[item["id"]]
            self.assertEqual(result["result"], "merged", result)
            self.assertEqual(result["how"], "composition", result)
            self.assertEqual(result["composition"]["package"], porteur["id"])
            self.assertEqual(work.get(self.db, item["id"])["state"], "delivered")

    def test_sync_merges_accepts_a_source_containing_the_freeze(self):
        frozen = self.unit_branch("lot/frozen", "code.py", "code\n", "code")
        git(self.repo, "checkout", "-q", "lot/frozen")
        source = self.commit("adaptation.py", "adaptation\n", "adaptation")
        item = self.lot("source contenant le gel")
        self.freeze(item, frozen)
        head = self.compose("integration/paquet-1", [(item["id"], source)])
        porteur = self.carrier()
        packet_link.record(self.db, porteur["id"], repo=self.repo, base=self.base,
                           head=head, actor="pilote", cfg=self.cfg)
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "merge", "-q", "--no-ff", "-m", "fusion paquet",
            "integration/paquet-1")
        report = plan_git.sync_merges(self.db, self.repo, target="main")
        row = next(r for r in report["results"] if r["work_item"] == item["id"])
        self.assertEqual(row["result"], "merged", row)
        self.assertEqual(row["how"], "composition", row)

    def test_sync_merges_refuses_a_freeze_not_in_the_composed_source(self):
        """Un gel avancé avant record ne prouve pas que la source le contient."""
        old = self.unit_branch("lot/old", "old.py", "old\n", "old")
        new = self.unit_branch("lot/new", "new.py", "new\n", "new")
        item = self.lot("gel plus récent que la composition")
        self.freeze(item, new)
        head = self.compose("integration/paquet-1", [(item["id"], old)])
        porteur = self.carrier()
        packet_link.record(self.db, porteur["id"], repo=self.repo, base=self.base,
                           head=head, actor="pilote", cfg=self.cfg)
        self.assertEqual(self.membership(item["id"])[0]["frozen_sha"], new)
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "merge", "-q", "--no-ff", "-m", "fusion paquet",
            "integration/paquet-1")
        report = plan_git.sync_merges(self.db, self.repo, target="main")
        row = next(r for r in report["results"] if r["work_item"] == item["id"])
        self.assertEqual(row["result"], "open", row)
        self.assertEqual(work.get(self.db, item["id"])["state"], "qa")

    def test_sync_merges_withholds_a_blocked_composition_unit(self):
        """#2744 s'applique aussi à la fermeture par composition."""
        frozen = self.unit_branch("lot/bloque", "bloque.py", "bloque\n", "bloque")
        item = self.lot("unité bloquée")
        self.freeze(item, frozen)
        head = self.compose("integration/paquet-1", [(item["id"], frozen)])
        porteur = self.carrier()
        packet_link.record(self.db, porteur["id"], repo=self.repo, base=self.base,
                           head=head, actor="pilote", cfg=self.cfg)
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "merge", "-q", "--no-ff", "-m", "fusion paquet",
            "integration/paquet-1")
        work.move(self.db, item["id"], "blocked", note="passe réelle due")
        report = plan_git.sync_merges(self.db, self.repo, target="main")
        row = next(r for r in report["results"] if r["work_item"] == item["id"])
        self.assertEqual(row["result"], "withheld", row)
        self.assertEqual(row["retention"]["what"], "blocked")
        self.assertEqual(work.get(self.db, item["id"])["state"], "blocked")
        work.move(self.db, item["id"], "qa", note="retenue levée")
        report = plan_git.sync_merges(self.db, self.repo, target="main")
        row = next(r for r in report["results"] if r["work_item"] == item["id"])
        self.assertEqual(row["result"], "merged", row)
        self.assertEqual(row["how"], "composition", row)

    def test_sync_merges_refuses_a_stale_composition(self):
        """Un re-gel postérieur à la composition rend la preuve périmée."""
        composed_sha = self.unit_branch("lot/douze", "src/huit.py", "huit\n", "huit")
        newer_sha = self.unit_branch("lot/douze-v2", "src/huit.py", "huit v2\n", "huit v2")
        item = self.lot("re-gelé")
        self.freeze(item, composed_sha)
        head = self.compose("integration/paquet-1", [(item["id"], composed_sha)])
        porteur = self.carrier()
        packet_link.record(self.db, porteur["id"], repo=self.repo, base=self.base,
                           head=head, actor="pilote", cfg=self.cfg)
        work.milestone(self.db, item["id"], "frozen", sha=newer_sha, actor="relecteur")
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "merge", "-q", "--no-ff", "-m", "fusion paquet",
            "integration/paquet-1")
        report = plan_git.sync_merges(self.db, self.repo, target="main")
        row = next(r for r in report["results"] if r["work_item"] == item["id"])
        self.assertEqual(row["result"], "open", row)
        self.assertNotEqual(row.get("how"), "composition")
        self.assertEqual(work.get(self.db, item["id"])["state"], "qa")


if __name__ == "__main__":
    unittest.main()
