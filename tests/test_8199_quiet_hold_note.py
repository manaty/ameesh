# SPDX-License-Identifier: AGPL-3.0-only
"""#8199 — un hold/une veille du pilote ne remet pas `quiet_s` à zéro.

Constat de l'auditeur (nuit du 24/11) : #8035 avait 49 h, mais une note du
pilote à 00:22Z (et un hold sur #8082 à 00:25Z) suffisaient à rendre le lot
« frais » (`quiet_s` 2379 s), donc à taire l'alerte de découpage alors que
580 h-agent dormaient et qu'aucun lot n'était démarrable. Cause C1 (évolution
de #7848) : `_SPLIT_MOVES_SQL` comptait **toute** note non vide de l'assigné
comme mouvement.

Correctif : seuls la création d'un lot qui cite le découpage et une note
`roadmap:` (préfixe `hold:`/`unhold:` facultatif, #8198) sont un mouvement ;
les notes `hold:`/`unhold:` et de veille ne le sont pas. Une fenêtre
d'observation non échue sur le lot de découpage tait en plus le repli
(débit et remplissage de file) jusqu'à son échéance, et un agent
`auth_blocked` n'est plus compté réveillable dans la capacité (#8201 b).
"""
from __future__ import annotations

import datetime
import time
import unittest
from unittest import mock

from ameesh import debit, file_decoupage as fd, registry, work

from .support import PgTestCase
from .test_3256_debit_capacity import H, agent, busy, mechanics_doc, split_lot, team

SPLIT = "file-decoupage:ameesh"
REPOS = 1800.0
PART = 0.20


def utc_text(ts: float) -> str:
    return datetime.datetime.fromtimestamp(
        ts, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


class _SplitBase(PgTestCase):
    """Un lot de découpage de 49 h, assigné au pilote, et ses notes."""

    def setUp(self) -> None:
        super().setUp()
        self.now = time.time()
        self.split = int(work.add(
            self.db, title="Découper la feuille de route de ameesh",
            app="ameesh", source=SPLIT,
            actor="ameesh:file-decoupage")["id"])
        self.db.execute(
            "UPDATE work_items SET created_at = to_timestamp(%s),"
            " assignee = 'pilote' WHERE id = %s",
            (self.now - 49 * H, self.split))

    def note(self, text: str, *, actor: str = "pilote",
             at: float | None = None) -> None:
        self.db.execute(
            "INSERT INTO work_item_events"
            " (work_item_id, state, note, actor, created_at)"
            " VALUES (%s, 'build', %s, %s, to_timestamp(%s))",
            (self.split, text, actor, self.now - 600 if at is None else at))

    def moved(self):
        rows = debit._rows(self.db, debit._SPLIT_MOVES_SQL, (str(self.split),))
        return rows[0]["moved_ts"]

    def split_row(self) -> dict:
        row = dict(self.db.query(
            "SELECT id, source, app, state,"
            " extract(epoch FROM created_at)::float8 AS created_ts"
            " FROM work_items WHERE id = %s", (self.split,))[0])
        moved = self.moved()
        if moved is not None:
            row["moved_ts"] = moved
        return row


class SplitMovementTest(_SplitBase):
    """`_SPLIT_MOVES_SQL` : ce qui compte comme mouvement du découpage."""

    def test_a_hold_note_is_not_a_movement(self):
        self.note("hold: attente du paquet de 11:00 — rien à découper avant")
        self.assertIsNone(self.moved())

    def test_an_unhold_note_is_not_a_movement(self):
        self.note("unhold: paquet livré, le hold est levé")
        self.assertIsNone(self.moved())

    def test_a_watch_note_is_not_a_movement(self):
        self.note("veille : RAS sur la file, aucun lot démarrable de neuf")
        self.assertIsNone(self.moved())

    def test_a_roadmap_note_is_a_movement(self):
        self.note("roadmap: épuisée ameesh (plan/ameesh)")
        self.assertAlmostEqual(self.moved(), self.now - 600, delta=2)

    def test_a_hold_wrapped_roadmap_sentinel_is_a_movement(self):
        # #8198 : la sentinelle peut vivre dans la raison d'un `hold:`.
        self.note("hold: roadmap: épuisée ameesh — attente de l'owner")
        self.assertAlmostEqual(self.moved(), self.now - 600, delta=2)

    def test_a_lot_created_citing_the_split_lot_is_a_movement(self):
        work.add(self.db, title="Découpage de #%d : une étape" % self.split,
                 app="ameesh", actor="pilote")
        self.assertIsNotNone(self.moved())


class QuietCapacityTest(_SplitBase):
    """Le symptôme : une note récente ne doit plus rendre l'alerte muette."""

    def capacity(self):
        return debit.capacity(
            agents=team(2, 8), open_lots=[self.split_row()], now=self.now,
            window_s=6 * H, repos_part=PART, repos_s=REPOS)

    def test_a_recent_hold_note_does_not_mute_the_split_alert(self):
        # Témoin de l'auditeur : 49 h, `hold:` il y a 10 min, 0 lot
        # démarrable, 20 % au repos → l'alerte sort (aucune avant #8199).
        self.note("hold: fenêtre du paquet, rien à découper")
        cap = self.capacity()
        self.assertEqual(len(cap["alerts"]), 1)
        self.assertIn("sans mouvement", cap["alerts"][0]["gesture"])
        entry = cap["projects"][0]["decoupage"]
        self.assertAlmostEqual(entry["age_s"], 49 * H, delta=5)
        self.assertAlmostEqual(entry["quiet_s"], 49 * H, delta=5)

    def test_a_recent_roadmap_note_still_holds_the_alert(self):
        # Une note `roadmap:` est un vrai geste sur la feuille de route.
        self.note("roadmap: épuisée ameesh (plan/ameesh)")
        self.assertEqual(self.capacity()["alerts"], [])


class ObservationWindowTest(unittest.TestCase):
    """#8199 : une fenêtre non échue tait le repli, pas la prise de lots."""

    def window(self, now: float, *, due: float | None = None) -> list[dict]:
        return [{"lot": 90, "kind": "hold_window", "by": "mesh-design",
                 "since_ts": now - H, "due_ts": now + 2 * H if due is None else due,
                 "due": utc_text(now + 2 * H if due is None else due),
                 "declared": "attendre le paquet de 11:00"}]

    def capacity(self, now: float, *, window_waits=None, **extra) -> dict:
        extra.setdefault("open_lots", [split_lot(created_ts=now - 49 * H,
                                                 moved_ts=now - 2 * H)])
        extra.setdefault("window_waits",
                         self.window(now) if window_waits is None else window_waits)
        return debit.capacity(agents=team(2, 8), now=now, window_s=6 * H,
                              repos_part=PART, repos_s=REPOS, **extra)

    def test_an_open_window_silences_the_fallback(self):
        # Témoin de l'auditeur : fenêtre non échue, 0 lot démarrable, 100 %
        # au repos → aucun repli dû.
        now = time.time()
        cap = self.capacity(now)
        self.assertEqual(cap["alerts"], [])
        entry = cap["projects"][0]["decoupage"]
        self.assertAlmostEqual(entry["window"]["due_ts"], now + 2 * H, delta=2)
        self.assertIn("fenêtre d'observation", "\n".join(
            debit._capacity_text(cap)))

    def test_an_expired_window_does_not_silence_the_fallback(self):
        now = time.time()
        cap = self.capacity(now, window_waits=self.window(now, due=now - 60))
        self.assertEqual(len(cap["alerts"]), 1)
        self.assertIn("sans mouvement", cap["alerts"][0]["gesture"])
        self.assertNotIn("window", cap["projects"][0]["decoupage"])

    def test_a_startable_lot_still_alerts_during_a_window(self):
        # La fenêtre porte sur le repli : un lot démarrable qui attend un
        # preneur reste un geste dû, et l'alerte le nomme.
        now = time.time()
        cap = self.capacity(now, startable=[{"id": 12, "app": "ameesh"}])
        self.assertEqual(len(cap["alerts"]), 1)
        self.assertIn("confier les lots démarrables",
                      cap["alerts"][0]["gesture"])


class AuthBlockedTest(unittest.TestCase):
    """#8201 b : un agent `auth_blocked` n'est pas une capacité réveillable."""

    def listing(self) -> list[dict]:
        return ([agent("sain"),
                 # Le cas que le statut seul ne voit pas : la ligne est
                 # `idle`, mais son texte dit `auth_blocked` (L226b), et
                 # aucun compte sain ne peut la faire tourner.
                 agent("bloque",
                       status_text="auth_blocked: deepseek/(default)"),
                 agent("rate",
                       status_text="[rate_limited x1, retry in 60s]")]
                + [busy("occupe-%d" % i) for i in range(8)])

    def test_an_auth_blocked_agent_is_not_wakeable(self):
        names = [row["name"] for row in fd.wakeable(
            self.listing(), [], configured=False)]
        self.assertIn("sain", names)
        # Une pause courte (rate limit d'une minute) reste une capacité.
        self.assertIn("rate", names)
        self.assertNotIn("bloque", names)

    def test_capacity_does_not_count_an_auth_blocked_agent(self):
        cap = debit.capacity(agents=self.listing(), now=time.time(),
                             window_s=6 * H, repos_part=PART, repos_s=REPOS)
        self.assertEqual(cap["mesh"]["wakeable"], 10)
        self.assertNotIn("bloque", cap["mesh"]["idle_agents"])


class ReadWindowTest(PgTestCase):
    """Le débit de bout en bout lit la fenêtre et tait son alerte."""

    def setUp(self) -> None:
        super().setUp()
        self.now = time.time()
        for index in range(10):
            name = "w%02d" % index
            registry.upsert(self.db, name, harness="deepseek",
                            host=self.cfg.host, cwd=self.tmp)
            self.db.execute(
                "UPDATE agent_registry SET status = 'idle', mode = 'execute',"
                " lease_owner = %s, lease_expires_at = now() + interval '1 hour',"
                " team = 'ameesh' WHERE name = %s",
                ("runner:w%02d" % index, name))
        self.split = int(work.add(
            self.db, title="Découper la feuille de route de ameesh",
            app="ameesh", source=SPLIT,
            actor="ameesh:file-decoupage")["id"])
        self.db.execute(
            "UPDATE work_items SET created_at = to_timestamp(%s) WHERE id = %s",
            (self.now - 49 * H, self.split))
        self.due = self.now + 2 * H
        self.db.execute(
            "INSERT INTO work_item_events"
            " (work_item_id, state, note, actor, created_at)"
            " VALUES (%s, 'build', %s, 'human:owner', to_timestamp(%s))",
            (self.split,
             "hold: fenêtre : attendre le paquet (due %s)" % utc_text(self.due),
             self.now - H))

    def test_the_read_report_taits_the_fallback_during_a_window(self):
        # Sans le passage de `window_waits` par `_read`, la capacité ne voit
        # pas la fenêtre et `debit --check` sort 2 (le constat #8082).
        with mock.patch("ameesh.mechanics.configured",
                        return_value=mechanics_doc()), \
                mock.patch("ameesh.sous_utilisation.critical_verdict",
                           return_value=(False, False)):
            report = debit.read(self.db, cfg=self.cfg, now=self.now, hours=6,
                                canons=[], jobs=False)
        capacity = report["capacity"]
        self.assertEqual(capacity["alerts"], [])
        project = [row for row in capacity["projects"]
                   if row["project"] == "ameesh"][0]
        self.assertEqual(project["decoupage"]["lot"], self.split)
        # La note ne porte que la minute : l'échéance relue peut précéder de
        # moins d'une minute celle qu'on avait en tête.
        self.assertAlmostEqual(project["decoupage"]["window"]["due_ts"],
                               self.due, delta=61)
        self.assertEqual(report["totals"]["capacity_alerts"], 0)


class FallbackWindowTest(PgTestCase):
    """Le remplissage de file ne relance pas l'auditeur pendant la fenêtre."""

    def setUp(self) -> None:
        super().setUp()
        from ameesh import mechanics
        from .test_3256_file_decoupage import agent as fd_agent
        self.fd_agent = fd_agent
        self.settings = mechanics.QueueFill(repos_part=PART, repos_s=REPOS)
        self.state: dict = {}
        self.t0 = time.time()
        for name in ("pilote", "dev0", "dev1", "devb0", "devb1", "devb2"):
            registry.upsert(self.db, name, harness="deepseek",
                            host=self.cfg.host, cwd=self.tmp)
        self.db.execute("UPDATE agent_registry SET responsible = 'human:alice'")
        pilots = mock.patch("ameesh.alert_briefs.project_pilots",
                            return_value=["pilote"])
        pilots.start()
        self.addCleanup(pilots.stop)

    def listing(self) -> list[dict]:
        return ([self.fd_agent("dev0"), self.fd_agent("dev1"),
                 self.fd_agent("devb0", idle=False),
                 self.fd_agent("devb1", idle=False),
                 self.fd_agent("devb2", idle=False),
                 self.fd_agent("auditeur", roles=["auditeur"])])

    def run_pass(self, offset: float, listing=None) -> list[dict]:
        return fd.run(None, self.db, now=self.t0 + offset, state=self.state,
                      listing=self.listing() if listing is None else listing,
                      canons=[], settings=self.settings)

    def test_a_dated_window_holds_the_fallback_until_its_due(self):
        listing = self.listing()
        self.run_pass(0, listing)
        self.run_pass(1800, listing)
        split = int(self.db.query(
            "SELECT id FROM work_items WHERE source = %s",
            (SPLIT,))[0]["id"])
        due = self.t0 + 1800 + 3700 + 3600
        self.db.execute(
            "INSERT INTO work_item_events"
            " (work_item_id, state, note, actor, created_at)"
            " VALUES (%s, 'build', %s, 'human:owner', to_timestamp(%s))",
            (split,
             "hold: fenêtre : attendre la découpe (due %s)" % utc_text(due),
             self.t0 + 1800))
        out = self.run_pass(1800 + 3700, listing)
        self.assertNotIn("fallback", [gesture["kind"] for gesture in out])
        mails = self.db.query(
            "SELECT recipient FROM agent_mailbox WHERE work_item_id = %s",
            (str(split),))
        self.assertEqual(mails, [])
        # À l'échéance, le repli reprend son cours.
        out = self.run_pass(1800 + 3700 + 3600 + 1, listing)
        self.assertIn("fallback", [gesture["kind"] for gesture in out])


if __name__ == "__main__":
    unittest.main()
