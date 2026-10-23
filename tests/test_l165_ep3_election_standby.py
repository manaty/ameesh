# SPDX-License-Identifier: AGPL-3.0-only
"""L165 (EP3) : exécuteurs en attente, aucun tour sans bail, brief de relève.

Une fiche Agent `releve: election` est servie par un exécuteur sur chaque hôte
candidat. Seul le détenteur du bail ouvre un tour ; quand la relève est
CROISÉE (la ligne était chez un autre hôte), le gagnant n'importe aucune
session : la même instruction de réclamation remet la session à zéro et écrit
un briefing de reprise déterministe, que le premier tour consomme comme résumé
de la session neuve.
"""
from __future__ import annotations

import dataclasses
import json
import os
import time
import unittest
from unittest import mock

from ameesh import mail, registry, reprise, storage, work
from ameesh.runner import Runner

from .support import FAKEBIN
from .test_canon import _CanonDbCase, fiche, write
from .test_l165_election import election_admission, election_agent


def host_work(name: str, root: str) -> str:
    """Une fiche Host qui admet le harnais du rôle et un dossier existant."""
    return fiche(
        type="Host", title=name, responsible="human:alice",
        policy="{harnesses: [claude, codex, deepseek], providers: [anthropic, openai, deepseek], "
               "credential_modes: [api-key, subscription], "
               "work_roots: {acme-web: %s}, max_agents: 2}" % root)


class _ElectionCase(_CanonDbCase):
    """Deux hôtes candidats réels (canon synchronisé), dossier de travail local."""

    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM host_lifecycle")
        self.db.execute("DELETE FROM host_holds")
        self.db.execute("DELETE FROM host_resources")
        self.workdirs: dict[str, str] = {}
        for host in ("atelier", "banc"):
            root = os.path.join(self.tmp, "acme-web-%s" % host)
            os.makedirs(root, exist_ok=True)
            self.workdirs[host] = root
            write(self.root, "hotes/%s.md" % host, host_work(host, root))
        write(self.root, "agents/relecteur.md", election_agent())
        write(self.root, "placements/relecteur-atelier.md", election_admission())

    def sync_both(self) -> None:
        self.sync("atelier")
        self.sync("banc")

    def hold_atelier(self, *, session: str = "session-atelier-1",
                     owner: str = "runner-atelier", expire: bool = True) -> dict:
        """Un détenteur sur `atelier`, avec une session locale, puis expiration."""
        lease = registry.claim(self.db, "relecteur", owner, 600, host="atelier")
        self.assertIsNotNone(lease)
        registry.set_session(self.db, "relecteur", session, "codex-1")
        if expire:
            self.db.execute(
                "UPDATE agent_registry SET lease_expires_at = clock_timestamp()"
                " - interval '1 second' WHERE name = 'relecteur'")
        return lease


class ElectionClaimTest(_ElectionCase):
    """Le CAS de réclamation porte la relève : reset durable + brief atomiques."""

    def test_releve_croisee_oublie_la_session_et_ecrit_le_brief(self):
        self.sync_both()
        self.hold_atelier()
        lease = registry.claim(self.db, "relecteur", "runner-banc", 600,
                               host="banc", election_brief="BRIEF-EP3")
        self.assertIsNotNone(lease)
        self.assertTrue(lease["elected"])
        self.assertEqual(lease["previous_host"], "atelier")
        self.assertEqual(lease["previous_owner"], "runner-atelier")
        self.assertEqual(lease["previous_session"], "session-atelier-1")
        self.assertEqual(lease["election_brief"], "BRIEF-EP3")
        row = registry.get(self.db, "relecteur")
        self.assertEqual((row["host"], row["cwd"]), ("banc", self.workdirs["banc"]))
        self.assertIsNone(row["session_id"])
        self.assertIsNone(row["session_account"])
        self.assertIsNone(row["session_work_item"])
        self.assertIsNotNone(row["session_reset_ts"])
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["election_brief"], "BRIEF-EP3")
        self.assertIn("Elected on host banc", row["pending_prompt"])
        # Le brief n'est PAS la consigne : il ouvre la session comme résumé.
        self.assertNotIn("BRIEF-EP3", row["pending_prompt"])

    def test_meme_hote_garde_la_session_et_ignore_le_brief(self):
        self.sync_both()
        self.hold_atelier()
        lease = registry.claim(self.db, "relecteur", "runner-atelier", 600,
                               host="atelier", election_brief="PAS-UN-BRIEF")
        self.assertIsNotNone(lease)
        self.assertFalse(lease["elected"])
        row = registry.get(self.db, "relecteur")
        self.assertEqual(row["session_id"], "session-atelier-1")
        self.assertIsNone(row["election_brief"])
        self.assertNotIn("Elected on host", row["pending_prompt"] or "")

    def test_la_session_ouverte_consomme_le_brief(self):
        self.sync_both()
        self.hold_atelier()
        lease = registry.claim(self.db, "relecteur", "runner-banc", 600,
                               host="banc", election_brief="BRIEF-EP3")
        self.assertTrue(lease["elected"])
        registry.set_session(self.db, "relecteur", "nouvelle-session", "codex-1")
        self.assertIsNone(registry.get(self.db, "relecteur")["election_brief"])

    def test_l_oubli_de_session_consomme_le_brief(self):
        self.sync_both()
        self.hold_atelier()
        lease = registry.claim(self.db, "relecteur", "runner-banc", 600,
                               host="banc", election_brief="BRIEF-EP3")
        self.assertIs(registry.clear_session(self.db, "relecteur", "runner-banc",
                                             lease["lease_epoch"]), True)
        row = registry.get(self.db, "relecteur")
        self.assertIsNone(row["election_brief"])
        self.assertIsNone(row["session_id"])

    def test_un_agent_hote_ignore_le_brief(self):
        self.sync_both()
        self.db.execute(
            "UPDATE agent_registry SET releve = 'hote', candidats = NULL"
            " WHERE name = 'relecteur'")
        lease = registry.claim(self.db, "relecteur", "runner-atelier", 600,
                               host="atelier", election_brief="BRIEF-EP3")
        self.assertIsNotNone(lease)
        self.assertFalse(lease["elected"])
        self.assertIsNone(registry.get(self.db, "relecteur")["election_brief"])


class ElectionBriefTest(_ElectionCase):
    """Le brief vient du dernier état persisté, borné, sans mémoire de session."""

    def setUp(self) -> None:
        super().setUp()
        self.sync_both()
        registry.upsert(self.db, "pilote", harness="codex", host="banc",
                        cwd=self.workdirs["banc"])
        self.db.execute(
            "UPDATE agent_registry SET team = 'acme-web', responsible = 'human:alice',"
            " releve = 'election', candidats = '{atelier,banc}', lease_epoch = 7,"
            " status = 'idle' WHERE name = 'pilote'")
        lot = work.add(self.db, title="un lot du pilote", app="ameesh", body="x")
        self.lot = int(lot["id"])
        self.db.execute("UPDATE work_items SET assignee = 'pilote' WHERE id = %s",
                        (self.lot,))
        mail.send(self.db, "agent:autre", "pilote", "un message non lu")
        self.db.execute(
            "INSERT INTO agent_mailbox (sender, recipient, body, kind, payload)"
            " VALUES ('agent:autre', 'human:alice', 'question', 'request', %s::jsonb)",
            (json.dumps({"decision": {"question": "Faut-il fusionner le paquet ?",
                                      "options": []}}),))
        self.cfg = dataclasses.replace(
            self.cfg,
            github={"projects": {"acme-web": "manaty/ameesh"}, "host": "banc"})

    def brief(self, row: dict | None = None, now: float | None = None) -> str:
        return reprise.election_brief(
            self.cfg, self.db, row if row is not None else registry.get(self.db, "pilote"),
            previous_host="atelier", previous_owner="runner-atelier",
            previous_epoch=6, previous_session="session-atelier-1", now=now)

    def test_le_brief_porte_l_etat_persiste_du_role(self):
        text = self.brief()
        self.assertIn("## Election", text)
        self.assertIn("host atelier", text)
        self.assertIn("runner-atelier", text)
        self.assertIn("session-atelier-1", text)
        self.assertIn("#%s" % self.lot, text)
        self.assertIn("1 unread message(s)", text)
        self.assertIn("Faut-il fusionner le paquet ?", text)
        self.assertIn("## Alerts in progress", text)
        self.assertIn("## GitHub observation", text)
        self.assertIn("manaty/ameesh", text)
        self.assertIn("## Takeable lots", text)
        self.assertNotIn("\x00", text)
        self.assertLessEqual(len(text.encode("utf-8")), reprise.BRIEF_MAX_BYTES)

    def test_le_brief_est_deterministe(self):
        now = time.time()
        self.assertEqual(self.brief(now=now), self.brief(now=now))

    def test_sans_depot_github_le_brief_le_dit(self):
        self.cfg = dataclasses.replace(self.cfg, github={})
        text = self.brief()
        self.assertIn("no GitHub repository configured on this host", text)


class RunnerStandbyTest(_ElectionCase):
    """Aucun tour sans bail, et le premier tour du gagnant est le brief."""

    def runner_for(self, host: str) -> Runner:
        cfg = dataclasses.replace(
            self.cfg, host=host, runner_id="runner-%s" % host,
            state_dir=os.path.join(self.tmp, "state-%s" % host))
        return Runner(cfg, self.db, once=True, agents=["relecteur"])

    def test_le_standby_ne_lance_aucun_tour_pendant_le_bail(self):
        self.sync_both()
        self.hold_atelier(expire=False)
        runner = self.runner_for("banc")
        with mock.patch.dict(os.environ, {
                "AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log},
                clear=False):
            runner.sweep()
        self.assertFalse(runner.did_turn)
        self.assertFalse(os.path.exists(self.turns_log)
                         and os.path.getsize(self.turns_log))

    def test_le_gagnant_ouvre_une_session_neuve_sur_le_brief(self):
        self.sync_both()
        self.hold_atelier()
        runner = self.runner_for("banc")
        with mock.patch.dict(os.environ, {
                "AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log},
                clear=False):
            runner.sweep()
        self.assertTrue(runner.did_turn, "le gagnant doit prendre son premier tour")
        with open(self.turns_log, encoding="utf-8") as fh:
            entries = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(len(entries), 1)
        argv = entries[0]["argv"]
        # Aucune reprise de la session de l'ancien hôte : le prompt est le
        # dernier argument, la marque `resume` serait AVANT lui.
        self.assertNotIn("resume", argv[:-1])
        prompt = argv[-1]
        self.assertIn("## Election", prompt)
        self.assertIn("previous holder: host atelier", prompt)
        self.assertIn("Elected on host banc", prompt)
        row = registry.get(self.db, "relecteur")
        self.assertEqual(row["host"], "banc")
        self.assertEqual(row["session_id"], "codex-thread-1")  # session NEUVE
        self.assertIsNone(row["election_brief"])              # brief consommé


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
