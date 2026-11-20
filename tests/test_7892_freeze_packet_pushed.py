# SPDX-License-Identifier: AGPL-3.0-only
"""#7892 : au gel d'un membre d'un porteur déjà poussé, avertir l'auteur et le pilote.

Constat de l'auditeur (#228274 §6 b) : #7694 et #7865 ont été regelés alors
que leur porteur était déjà poussé ; la CI du porteur ne portait plus la tête
du lot et la recomposition a dû attendre la fin de la tentative.

Le gel reste accepté (un regel est souvent le correctif d'un rouge) ; la note
du jalon porte la ligne machine (imprimée par la CLI pour l'auteur) et le
pilote reçoit un courrier dans le même geste. Aucun avis quand le porteur
n'est pas poussé, n'est plus ouvert, ou quand la nouvelle tête est encore la
source composée.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from unittest import mock

from ameesh import freeze_packet, registry, storage, work

from .support import PgTestCase
from .test_2962_freeze_unresolved import author_with_forge, git


class _Base(PgTestCase):
    def setUp(self):
        super().setUp()
        self.root = tempfile.mkdtemp(prefix="ameesh-7892-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.forge, self.repo = author_with_forge(self.root)
        for name in ("author", "pilote"):
            registry.upsert(self.db, name, harness="deepseek", host=self.cfg.host)
        self.member = work.add(self.db, title="membre du paquet", app="ameesh",
                               assignee="author", actor="pilote")
        self.member_id = int(self.member["id"])
        work.move(self.db, self.member_id, "build", actor="author")
        work.set_branch(self.db, self.member_id, "lot/7892-member", "main",
                        actor="author")
        self.carrier = work.add(
            self.db, title="porteur", app="ameesh",
            branch="integration/paquet-7892", branch_target="main", actor="pilote")
        self.carrier_id = int(self.carrier["id"])
        self.base = git(self.repo, "rev-parse", "HEAD")
        # La source composée S, poussée sur la branche du membre.
        git(self.repo, "checkout", "-qb", "lot/7892-member")
        self.source = self.commit("membre.txt", "membre\n", "membre")
        git(self.repo, "push", "-q", "origin", "lot/7892-member")

    def commit(self, path: str, text: str, message: str) -> str:
        full = os.path.join(self.repo, path)
        os.makedirs(os.path.dirname(full) or self.repo, exist_ok=True)
        with open(full, "a", encoding="utf-8") as stream:
            stream.write(text)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", message)
        return git(self.repo, "rev-parse", "HEAD")

    def compose(self, *, pushes: bool = True) -> str:
        """Une composition du membre sur la branche du porteur, poussée ou non."""
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "checkout", "-qb", "integration/paquet-7892")
        git(self.repo, "merge", "-q", "--no-ff", "-m",
            "composition #%d %s\n\nameesh-work: %d"
            % (self.member_id, self.source[:12], self.member_id), self.source)
        head = git(self.repo, "rev-parse", "HEAD")
        if pushes:
            git(self.repo, "push", "-q", "origin", "integration/paquet-7892")
            self.observe(head)
        return head

    def record(self, head: str, *, source: str | None = None) -> None:
        storage.of(self.db).work.record_packet(
            self.carrier_id, branch="integration/paquet-7892",
            base_sha=self.base, head_sha=head,
            units=[{"work_item_id": self.member_id, "unit_sha": head,
                    "source_sha": source or self.source, "frozen_sha": source or self.source}],
            note="packet composition recorded", actor="pilote")

    def observe(self, head: str) -> None:
        """Le balayage de la forge a vu cette tête poussée sur le porteur."""
        self.db.execute(
            "UPDATE work_items SET branch_head = %s, branch_head_at = now()"
            " WHERE id = %s", (head, self.carrier_id))

    def refreeze(self, sha: str, *, note: str = "") -> dict:
        with mock.patch.object(freeze_packet, "pilot_names", return_value=["pilote"]):
            return work.milestone(self.db, self.member_id, "frozen", sha=sha,
                                  actor="author", repo=self.repo, note=note)

    def new_head(self) -> str:
        git(self.repo, "checkout", "-q", "lot/7892-member")
        head = self.commit("correctif.txt", "correctif\n", "correctif d'un rouge")
        git(self.repo, "push", "-q", "origin", "lot/7892-member")
        return head

    def pilot_mails(self) -> list[dict]:
        return self.db.query(
            "SELECT id, recipient, body, payload, work_item_id FROM agent_mailbox"
            " WHERE recipient = %s ORDER BY id", ("pilote",))

    def freezes(self) -> list[dict]:
        return [mark for mark in work.milestones(self.db, self.member_id)
                if mark["kind"] == "frozen"]


class PushedMemberTest(_Base):
    def test_le_regel_d_un_membre_d_un_porteur_pousse_est_accepte_et_avertit(self):
        composed = self.compose()
        self.record(composed)
        head = self.new_head()
        row = self.refreeze(head)
        # Pas de refus : le gel est écrit.
        self.assertEqual(row["kind"], "frozen")
        self.assertEqual(row["sha"], head)
        self.assertEqual([mark["sha"] for mark in self.freezes()], [head])
        self.assertEqual(work.get(self.db, self.member_id)["state"], "qa")
        # L'auteur lit porteur, tête poussée et effet dans la note du gel.
        self.assertIn(freeze_packet.MARK, row["note"])
        self.assertIn("#%d" % self.carrier_id, row["note"])
        self.assertIn(composed[:12], row["note"])
        self.assertIn("#4581", row["note"])
        self.assertIn(head[:12], row["note"])
        # Le pilote est prévenu dans le même geste.
        mails = self.pilot_mails()
        self.assertEqual(len(mails), 1)
        self.assertIn("#%d" % self.carrier_id, mails[0]["body"])
        self.assertIn(composed[:12], mails[0]["body"])
        self.assertIn("#4581", mails[0]["body"])
        self.assertEqual(str(mails[0]["work_item_id"]), str(self.member_id))
        payload = (mails[0].get("payload") or {}).get("freeze_packet_pushed") or {}
        self.assertEqual(payload.get("lot"), self.member_id)
        self.assertEqual(payload["carriers"][0]["pushed"], composed)

    def test_un_regel_de_la_meme_tete_ne_duplique_pas_le_courrier(self):
        composed = self.compose()
        self.record(composed)
        head = self.new_head()
        self.refreeze(head)
        self.refreeze(head)
        self.assertEqual(len(self.freezes()), 2)
        self.assertEqual(len(self.pilot_mails()), 1)

    def test_deux_gels_de_tetes_differentes_avertissent_deux_fois(self):
        # Relecture cx2 (#7892) : la clé d'idempotence portait (lot, porteurs,
        # têtes poussées) mais pas la tête gelée. H1 puis H2 avec un porteur
        # inchangé réutilisaient la clé avec un corps différent : le second
        # avis était refusé par le courrier puis masqué.
        composed = self.compose()
        self.record(composed)
        first = self.new_head()
        self.refreeze(first)
        second = self.new_head()
        self.refreeze(second)
        self.refreeze(second)  # rejeu de H2 : même corps, rien de plus
        mails = self.pilot_mails()
        self.assertEqual(len(mails), 2)
        self.assertIn(first[:12], mails[0]["body"])
        self.assertIn(second[:12], mails[1]["body"])

    def test_une_ref_de_suivi_distante_prouve_la_poussee(self):
        # La composition est poussée mais ni le gel du porteur ni le balayage
        # ne l'ont encore vue : la ref de suivi distante est la preuve.
        composed = self.compose()
        self.record(composed)
        self.db.execute("UPDATE work_items SET branch_head = NULL,"
                        " branch_head_at = NULL WHERE id = %s", (self.carrier_id,))
        git(self.repo, "fetch", "-q", "origin")
        head = self.new_head()
        row = self.refreeze(head)
        self.assertIn(freeze_packet.MARK, row["note"])
        self.assertIn(composed[:12], row["note"])
        self.assertEqual(len(self.pilot_mails()), 1)

    def test_l_ascendance_dans_la_tete_poussee_prouve_la_poussee(self):
        # Le porteur a grandi depuis la composition : la source composée est
        # toujours contenue dans la tête poussée, donc la CI a bien tourné
        # dessus.
        composed = self.compose()
        self.record(composed)
        git(self.repo, "checkout", "-q", "integration/paquet-7892")
        self.commit("gros.txt", "gros\n", "le porteur grandit")
        pushed = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "push", "-q", "origin", "integration/paquet-7892")
        self.observe(pushed)
        head = self.new_head()
        row = self.refreeze(head)
        self.assertIn(freeze_packet.MARK, row["note"])
        self.assertIn(pushed[:12], row["note"])
        self.assertEqual(len(self.pilot_mails()), 1)

    def test_la_note_d_appelant_ne_fabrique_pas_l_avertissement(self):
        composed = self.compose()
        self.record(composed)
        head = self.new_head()
        row = self.refreeze(
            head, note="ameesh-freeze-packet-pushed: forge par l'appelant")
        self.assertNotIn("forge par l'appelant", row["note"])
        self.assertEqual(row["note"].count(freeze_packet.MARK), 1)


class NoWarningTest(_Base):
    def test_un_porteur_jamais_pousse_n_avertit_pas(self):
        composed = self.compose(pushes=False)
        self.record(composed)
        head = self.new_head()
        row = self.refreeze(head)
        self.assertNotIn(freeze_packet.MARK, row["note"])
        self.assertEqual(self.pilot_mails(), [])

    def test_un_porteur_termine_n_avertit_pas(self):
        composed = self.compose()
        self.record(composed)
        work.close(self.db, self.carrier_id, abandoned=True, actor="pilote",
                   note="porteur terminé pour le témoin")
        head = self.new_head()
        row = self.refreeze(head)
        self.assertNotIn(freeze_packet.MARK, row["note"])
        self.assertEqual(self.pilot_mails(), [])

    def test_la_meme_source_composee_ne_sort_pas_du_paquet(self):
        composed = self.compose()
        self.record(composed)
        row = self.refreeze(self.source)
        self.assertNotIn(freeze_packet.MARK, row["note"])
        self.assertEqual(self.pilot_mails(), [])

    def test_un_lot_hors_paquet_n_avertit_pas(self):
        composed = self.compose()
        self.record(composed)
        other = work.add(self.db, title="autre", app="ameesh", assignee="author",
                         actor="pilote")
        work.move(self.db, other["id"], "build", actor="author")
        work.set_branch(self.db, other["id"], "lot/7892-other", "main", actor="author")
        git(self.repo, "checkout", "-qb", "lot/7892-other", self.base)
        head = self.commit("autre.txt", "autre\n", "autre")
        with mock.patch.object(freeze_packet, "pilot_names", return_value=["pilote"]):
            row = work.milestone(self.db, other["id"], "frozen", sha=head,
                                 actor="author", repo=self.repo)
        self.assertNotIn(freeze_packet.MARK, row["note"])
        self.assertEqual(self.pilot_mails(), [])


class CliWarningTest(_Base):
    def test_la_sortie_du_gel_dit_le_porteur_et_la_tete_poussee(self):
        composed = self.compose()
        self.record(composed)
        head = self.new_head()
        proc = self.mesh("work", "milestone", str(self.member_id), "frozen",
                         "--sha", head, "--repo", self.repo,
                         "--actor", "human:t")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("warning: " + freeze_packet.MARK, proc.stderr)
        self.assertIn("#%d" % self.carrier_id, proc.stderr)
        self.assertIn(composed[:12], proc.stderr)


class PilotNamesTest(_Base):
    def test_le_pilote_vient_de_la_regle_des_briefs(self):
        with mock.patch("ameesh.alert_briefs.project_pilots",
                        return_value=["pilote"]) as lookup:
            self.assertEqual(freeze_packet.pilot_names(self.cfg, self.db, "ameesh"),
                             ["pilote"])
        self.assertEqual(lookup.call_args.args[0], "ameesh")

    def test_un_role_illisible_ne_leve_pas(self):
        with mock.patch("ameesh.alert_briefs.project_pilots",
                        side_effect=RuntimeError("canon")):
            self.assertEqual(freeze_packet.pilot_names(self.cfg, self.db, "ameesh"), [])
