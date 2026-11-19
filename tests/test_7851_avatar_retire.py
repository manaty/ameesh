# SPDX-License-Identifier: AGPL-3.0-only
"""#7851 : une route humaine ignore un avatar retiré ou non admis.

Le canon garde la fiche ``avatar-essai`` (bloc ``avatar:``) après le retrait
de l'agent (#7635). ``avatars.name_for`` la rendait sans relire le registre :
``staging_pending`` et ``staging_deferred`` l'adressaient, ``mail.send``
levait ``RecipientRetired`` avant les orchestrateurs à chaque tick, et
``decisions.prose_route`` ne retombait pas sur le bureau configuré. Le
correctif : quand l'appelant donne le registre lu, ``name_for`` ignore un
avatar retiré ou absent (la route configurée reprend), et un
``RecipientRetired`` reçu au dépôt est rattrapé cible par cible, consigné au
rapport et au journal, sans interrompre les autres cibles.
"""
from __future__ import annotations

import contextlib
import dataclasses
import io
import os
import shutil
import tempfile
import types
import unittest
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

from ameesh import (avatars, config, decisions, release_cutoff,
                    staging_deferred, staging_pending, storage)

from . import canon_staging

SHA = "a" * 40
OLD = "b" * 40
TAG = {"version": "1.63.0", "tag": "v1.63.0", "commit": SHA, "at": 2.0}
OLD_TAG = {"version": "1.62.0", "tag": "v1.62.0", "commit": OLD, "at": 1.0}
AGENTS = ("ameesh-agents-1", "ameesh-agents-2", "ameesh-agents-3")


def _paris(hour: int, minute: int = 0) -> float:
    return datetime(2026, 11, 19, hour, minute,
                    tzinfo=ZoneInfo("Europe/Paris")).timestamp()


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as stream:
        stream.write(text)


def _card(title: str, human: str = "smichea") -> str:
    return "\n".join([
        "---", "type: Agent", "title: " + title,
        "responsible: human:" + human,
        "avatar:", "  conversation: portable", "---", "",
    ])


def attestation(sha=SHA, *, reportes=None, hotes=None):
    return {"schema": "ameesh-staging-attestation/1", "sha": sha,
            "date": "2026-11-19T10:00:00Z",
            "hotes": (hotes if hotes is not None
                      else {host: {"actif": sha} for host in AGENTS}),
            "reportes": reportes or {}}


class _Ops:
    """Le registre lu par la route humaine : `operations.listing`."""

    def __init__(self, rows: list[dict]):
        self._rows = rows

    def listing(self):
        return list(self._rows)


class _Store:
    def __init__(self, rows: list[dict]):
        self.operations = _Ops(rows)


class _CanonCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="canon-7851-")
        self.addCleanup(shutil.rmtree, self.root, True)
        _write(os.path.join(self.root, "membres", "smichea.md"),
               "---\ntype: Member\ntitle: smichea\n---\n")
        _write(os.path.join(self.root, "agents", "avatar-essai.md"),
               _card("avatar-essai"))
        self.cfg = dataclasses.replace(
            config.load(env={}), canon=self.root, canon_untrusted=True,
            canon_ref="", release={"projects": {"ameesh": {"report": "bureau"}}})

    @staticmethod
    def rows(*, avatar_retired=True):
        row = {"name": "avatar-essai", "responsible": "human:smichea",
               "mode": "execute", "status": "idle"}
        if avatar_retired:
            row["retired_ts"] = 1794997956.0
        return [row, {"name": "bureau", "responsible": "human:smichea",
                      "mode": "externe", "status": "idle"}]


class AvatarAdmissionTest(_CanonCase):
    """Le canon déclare l'avatar ; le registre décide s'il est une route."""

    def test_un_avatar_retire_n_est_pas_une_route(self):
        rows = self.rows()
        # sans registre, la lecture canon seule est conservée (compatibilité)
        self.assertEqual(avatars.name_for(self.cfg, "human:smichea"),
                         "avatar-essai")
        # avec le registre lu, l'avatar retiré est écarté
        self.assertIsNone(
            avatars.name_for(self.cfg, "human:smichea", rows))
        self.assertEqual(
            avatars.configured_desks(self.cfg, "human:smichea", rows),
            ["bureau"])

    def test_un_avatar_absent_du_registre_n_est_pas_admis(self):
        rows = [{"name": "bureau", "responsible": "human:smichea",
                 "mode": "externe", "status": "idle"}]
        self.assertIsNone(
            avatars.name_for(self.cfg, "human:smichea", rows))
        self.assertFalse(avatars.is_admitted("avatar-essai", rows))

    def test_un_avatar_admis_reste_la_route_de_l_humain(self):
        rows = self.rows(avatar_retired=False)
        self.assertEqual(
            avatars.name_for(self.cfg, "human:smichea", rows),
            "avatar-essai")
        self.assertTrue(avatars.is_admitted("avatar-essai", rows))

    def test_un_registre_vide_ne_fait_pas_d_avatar(self):
        # une liste vide est un registre lu : l'avatar n'est pas admis
        self.assertIsNone(avatars.name_for(self.cfg, "human:smichea", []))

    def test_une_route_configuree_retiree_n_est_pas_retenue(self):
        rows = [{"name": "bureau", "responsible": "human:smichea",
                 "mode": "externe", "retired_ts": 1794997956.0}]
        self.assertEqual(
            avatars.configured_desks(self.cfg, "human:smichea", rows), [])


class ProseRouteRetiredTest(_CanonCase):
    """Le troisième appelant, `decisions.prose_route`, profite du filtre."""

    def test_prose_route_compte_le_bureau_quand_l_avatar_est_retire(self):
        store = _Store(self.rows())
        with mock.patch.object(decisions.storage, "of", return_value=store):
            allowed = decisions.prose_route(
                "human:smichea", db=object(), cfg=self.cfg)
        self.assertIn("bureau", allowed)
        self.assertNotIn("avatar-essai", allowed)

    def test_un_avatar_admis_reste_la_route_de_prose(self):
        store = _Store(self.rows(avatar_retired=False))
        with mock.patch.object(decisions.storage, "of", return_value=store):
            allowed = decisions.prose_route(
                "human:smichea", db=object(), cfg=self.cfg)
        self.assertIn("avatar-essai", allowed)


class StagingPendingRetiredTest(_CanonCase):
    """`staging_pending` ne perd plus l'alerte sur l'avatar retiré."""

    def setUp(self) -> None:
        super().setUp()
        self.state = tempfile.mkdtemp(prefix="state-7851-")
        self.addCleanup(shutil.rmtree, self.state, True)
        self.cfg = types.SimpleNamespace(
            state_dir=self.state,
            canon=self.cfg.canon, canon_untrusted=True, canon_ref="",
            release=self.cfg.release,
            packet_watch={"projects": {"ameesh": {"notify": ["pilot"]}}})
        self.project = types.SimpleNamespace(
            name="ameesh", report="bureau", target="main")
        self.report = {"result": "needed", "lots": [6002], "candidate": SHA,
                       "active": [], "candidate_unblocks": []}

    def tick(self, rows, send):
        store = _Store(rows)
        err = io.StringIO()
        with mock.patch.object(staging_pending, "inspect",
                               return_value=dict(self.report)), \
             mock.patch.object(staging_pending.storage, "of",
                               return_value=store), \
             mock.patch("ameesh.mail.send", side_effect=send) as mail_send, \
             contextlib.redirect_stderr(err):
            report = staging_pending.tick(self.cfg, object(), self.project,
                                          now=_paris(22, 30))
        return report, mail_send, err.getvalue()

    def test_l_avatar_retire_prend_la_route_report_et_le_pilote_est_averti(self):
        def send(db, sender, target, text, **kwargs):
            if target == "avatar-essai":
                raise AssertionError("retired avatar must not be a target")
            return 1

        report, mail_send, err = self.tick(self.rows(), send)
        self.assertEqual({call.args[2] for call in mail_send.call_args_list},
                         {"bureau", "pilot"})
        self.assertNotIn("skipped_retired", report)
        self.assertNotIn("is retired; skipped", err)
        [body] = [call.args[3] for call in mail_send.call_args_list
                  if call.args[2] == "pilot"]
        self.assertIn("staging-needed", body)

    def test_un_retire_entre_lecture_et_depot_n_arrete_pas_les_autres(self):
        def send(db, sender, target, text, **kwargs):
            if target == "avatar-essai":
                raise storage.RecipientRetired("avatar-essai is retired")
            return 1

        report, mail_send, err = self.tick(
            self.rows(avatar_retired=False), send)
        self.assertEqual({call.args[2] for call in mail_send.call_args_list},
                         {"avatar-essai", "pilot"})
        self.assertEqual(report["skipped_retired"], ["avatar-essai"])
        self.assertIn("staging-needed ameesh: recipient avatar-essai "
                      "is retired; skipped", err)

    def test_un_destinataire_statique_retire_n_arrete_pas_les_autres(self):
        self.cfg.packet_watch["projects"]["ameesh"]["notify"] = [
            "pilot", "ancien-bureau"]

        def send(db, sender, target, text, **kwargs):
            if target == "ancien-bureau":
                raise storage.RecipientRetired("ancien-bureau is retired")
            return 1

        report, mail_send, err = self.tick(self.rows(), send)
        self.assertEqual({call.args[2] for call in mail_send.call_args_list},
                         {"ancien-bureau", "bureau", "pilot"})
        self.assertEqual(report["skipped_retired"], ["ancien-bureau"])
        self.assertIn("recipient ancien-bureau is retired; skipped", err)


class StagingDeferredRetiredTest(_CanonCase):
    """`staging_deferred` alerte le pilote et le bureau, pas l'avatar retiré."""

    def setUp(self) -> None:
        super().setUp()
        self.state = tempfile.mkdtemp(prefix="state-7851-deferred-")
        self.addCleanup(shutil.rmtree, self.state, True)
        self.cfg = types.SimpleNamespace(
            state_dir=self.state,
            canon=self.cfg.canon, canon_untrusted=True, canon_ref="",
            release=self.cfg.release)
        self.project = release_cutoff.ProjectRelease(
            name="ameesh", repo="/nowhere", target="main", remote="origin",
            report="bureau", github="manaty/ameesh",
            staging_topology=canon_staging.topology())

    def tick(self, rows, send, registry_rows=None):
        def ancestry(repo, ancestor, descendant):
            if ancestor == descendant:
                return True
            return None

        err = io.StringIO()
        with mock.patch.object(release_cutoff, "stored_attestations",
                               return_value=rows), \
             mock.patch.object(release_cutoff, "_tags_by_commit",
                               return_value={SHA: TAG, OLD: OLD_TAG}), \
             mock.patch.object(staging_deferred, "_is_ancestor",
                               side_effect=ancestry), \
             mock.patch.object(staging_deferred, "_registry_rows",
                               return_value=registry_rows if registry_rows
                               is not None else self.rows()), \
             mock.patch("ameesh.alert_briefs.project_pilots",
                        return_value=["pilot"]), \
             mock.patch("ameesh.mail.send", side_effect=send) as mail_send, \
             contextlib.redirect_stderr(err):
            report = staging_deferred.tick(self.cfg, object(), self.project)
        return report, mail_send, err.getvalue()

    def test_le_pilote_et_le_bureau_sont_avertis_sans_l_avatar(self):
        rows = [attestation(SHA, reportes={
            "manaty-mesh-1": {"raison": "central runtime deferred"}}),
            attestation(OLD, hotes={h: {"actif": OLD}
                                    for h in AGENTS + ("manaty-mesh-1",)})]

        def send(db, sender, target, text, **kwargs):
            if target == "avatar-essai":
                raise AssertionError("retired avatar must not be a target")
            return 1

        report, mail_send, err = self.tick(rows, send)
        self.assertEqual(report["result"], "alerted")
        self.assertEqual({call.args[2] for call in mail_send.call_args_list},
                         {"bureau", "pilot"})
        self.assertNotIn("skipped_retired", report)
        self.assertNotIn("is retired; skipped", err)

    def test_un_retire_entre_lecture_et_depot_n_arrete_pas_le_pilote(self):
        rows = [attestation(SHA, reportes={
            "manaty-mesh-1": {"raison": "central runtime deferred"}})]

        def send(db, sender, target, text, **kwargs):
            if target == "avatar-essai":
                raise storage.RecipientRetired("avatar-essai is retired")
            return 1

        report, mail_send, err = self.tick(
            rows, send, registry_rows=self.rows(avatar_retired=False))
        self.assertEqual({call.args[2] for call in mail_send.call_args_list},
                         {"avatar-essai", "pilot"})
        self.assertEqual(report["skipped_retired"], ["avatar-essai"])
        self.assertIn("staging-deferred ameesh: recipient avatar-essai "
                      "is retired; skipped", err)


if __name__ == "__main__":
    unittest.main()
