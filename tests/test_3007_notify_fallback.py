# SPDX-License-Identifier: AGPL-3.0-only
"""#3007 : aucun notificateur vivant ne joint l'humain quand le poste tombe.

Constat #3006 (21/10/2026) : le seul notificateur à canaux est
`ameesh-notify` de pc-smichea ; l'hôte perdu, les hôtes sains n'ont aucune
route (`ameesh notify --test human:smichea` → « no channel », code 2) et
personne ne le voit sans y penser. Le lot accepte deux issues : un canal de
secours, ou l'absence de voie signalée là où un agent la voit.

La revue du 23/10 a écarté le constat local (« cet hôte n'a pas de route ») :
il est vrai en permanence sur les VM, même poste sain, et le brief du
notificateur ne part jamais quand le notificateur est le disparu. Ces témoins
couvrent le constat partagé : battement `notify_liveness` par hôte et humain,
alerte `notify_undeliverable` levée seulement sans battement frais, brief porté
par `notify_gap.brief_once` hors du poste (ménage du runner), avertissement
d'`ameesh decisions`.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import os
import time
import types
import unittest
from unittest import mock

from ameesh import actions_cli, alert_briefs, decisions, exploitation, fil, mail, notify, \
    notify_gap, registry, storage, work

from .support import PgTestCase

ALICE = "human:alice"
BOB = "human:bob"
HOST = "pc-smichea"


def _pending(ids=(7, 8), owner=ALICE, *, now=1000.0):
    # Plus vieille que la grâce ALIVE_S (600 s) : une demande établie, pas la
    # course d'une demande qui vient de naître (N2, dev4).
    return [{"id": ident, "owner": owner, "created_ts": now - 900.0 + ident,
             "overdue": ident == ids[0]} for ident in ids]


def _row(human=ALICE, seen_ts=100.0, host=HOST):
    return {"host": host, "human": human, "seen_ts": seen_ts}


class GapsTest(unittest.TestCase):
    def test_sans_battement_aucun_constat(self):
        # Une base sans notificateur n'a jamais prouvé d'écart : le banc (et
        # une installation neuve) ne doit pas lever une fausse alerte.
        self.assertEqual(notify_gap.gaps_for(_pending(), [], 1000.0), [])

    def test_battement_perime_nomme_l_hote_l_humain_et_les_demandes(self):
        (gap,) = notify_gap.gaps_for(_pending(), [_row(seen_ts=100.0)], 1000.0)
        self.assertEqual((gap["human"], gap["last_host"], gap["count"],
                          gap["overdue"], gap["decisions"], gap["oldest"],
                          gap["reason"], gap["since"]),
                         (ALICE, HOST, 2, [7], [7, 8], 7, "notifier_stale", 100.0))
        for texte in ("no living notifier covers human:alice",
                      "last heartbeat from host pc-smichea",
                      "15min ago", "nothing is pushed to human:alice",
                      "role timers --install", "ameesh decisions"):
            self.assertIn(texte, gap["detail"])
        # deux demandes du même humain ne font qu'un écart
        self.assertEqual(len(notify_gap.gaps_for(_pending(ids=(7, 8, 9)),
                                                 [_row(seen_ts=100.0)], 1000.0)), 1)

    def test_un_battement_frais_ferme_l_ecart(self):
        self.assertEqual(notify_gap.gaps_for(_pending(), [_row(seen_ts=900.0)],
                                             1000.0), [])

    def test_une_demande_toute_neuve_n_est_pas_encore_un_ecart(self):
        # Grâce ALIVE_S : une demande qui vient de naître peut précéder le
        # prochain battement (30 s) ; un autre notificateur est vivant.
        rows = [_row(human=BOB, seen_ts=900.0)]
        neuve = [{"id": 9, "owner": ALICE, "created_ts": 995.0, "overdue": False}]
        self.assertEqual(notify_gap.gaps_for(neuve, rows, 1000.0), [])
        vieille = [{"id": 9, "owner": ALICE, "created_ts": 300.0, "overdue": False}]
        (gap,) = notify_gap.gaps_for(vieille, rows, 1000.0)
        self.assertEqual((gap["human"], gap["reason"]), (ALICE, "uncovered"))

    def test_une_demande_neuve_ne_rouvre_pas_un_battement_perime(self):
        # N2 (dev4) : un humain couvert seulement par default, absent des noms
        # et des routes, dont la ligne a péri pendant une accalmie : la demande
        # neuve laisse le prochain battement la rafraîchir.
        rows = [_row(human=ALICE, seen_ts=300.0)]
        neuve = [{"id": 9, "owner": ALICE, "created_ts": 995.0, "overdue": False}]
        self.assertEqual(notify_gap.gaps_for(neuve, rows, 1000.0), [])
        vieille = [{"id": 9, "owner": ALICE, "created_ts": 300.0, "overdue": False}]
        (gap,) = notify_gap.gaps_for(vieille, rows, 1000.0)
        self.assertEqual(gap["reason"], "notifier_stale")

    def test_un_autre_humain_couvert_ne_couvre_pas_alice(self):
        (gap,) = notify_gap.gaps_for(_pending(),
                                     [_row(human=BOB, seen_ts=900.0)], 1000.0)
        self.assertEqual((gap["reason"], gap["last_host"], gap["last_seen"]),
                         ("uncovered", None, None))
        self.assertIn("no notifier has ever reported covering human:alice",
                      gap["detail"])

    def test_une_demande_sans_proprietaire_n_est_pas_devinee(self):
        self.assertEqual(notify_gap.gaps_for(_pending(owner=None),
                                             [_row(seen_ts=100.0)], 1000.0), [])

    def test_deux_humains_deux_ecarts_ordre_stable(self):
        pending = _pending(ids=(7,), owner=BOB) + _pending(ids=(8,), owner=ALICE)
        gaps = notify_gap.gaps_for(pending, [_row(human=BOB, seen_ts=100.0)], 1000.0)
        self.assertEqual([g["human"] for g in gaps], [ALICE, BOB])

    def test_la_cle_distingue_les_humains(self):
        (gap,) = notify_gap.gaps_for(_pending(), [_row(seen_ts=100.0)], 1000.0)
        un = notify_gap.alert_of(gap)
        deux = dict(un, human=BOB)
        self.assertNotEqual(exploitation.alert_key(un), exploitation.alert_key(deux))
        self.assertEqual((un["human"], un["responsible"], un["last_host"]),
                         (ALICE, ALICE, HOST))

    def test_l_alerte_est_declaree_et_briefee(self):
        self.assertIn(notify_gap.GAP_TYPE, exploitation.ALERT_TYPES)
        self.assertIn(notify_gap.GAP_TYPE, notify.TYPE_LABELS)
        self.assertIn(notify_gap.GAP_TYPE, alert_briefs.BRIEFED_TYPES)
        self.assertIn(notify_gap.GAP_TYPE, alert_briefs.NOTIFIER_EXCLUDED)
        self.assertNotIn(notify_gap.GAP_TYPE, notify.DEFAULT_TYPES)

    def test_le_brief_va_aux_orchestrateurs_et_a_l_auditeur(self):
        (gap,) = notify_gap.gaps_for(_pending(), [_row(seen_ts=100.0)], 1000.0)
        alert = notify_gap.alert_of(gap, ["orch"])
        listing = [{"name": "orch"}, {"name": "auditeur"}]
        briefs = alert_briefs.briefs(alert, listing, auditor_names=("auditeur",),
                                     orchestrator_names=("orch",))
        self.assertEqual(sorted(briefs), ["auditeur", "orch"])
        self.assertIn("role timers --install", briefs["orch"])
        self.assertIn("no living notifier", briefs["orch"])

    def test_notify_test_nomme_le_repli(self):
        cfg = types.SimpleNamespace(host="ameesh-agents-2")
        ncfg = notify.parse_config({})
        args = argparse.Namespace(test=ALICE, json=False)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(notify.cmd_test(cfg, ncfg, args), 2)
        self.assertIn("no channel for human:alice", err.getvalue())
        self.assertIn("role timers --install", err.getvalue())


class AlertesTest(PgTestCase):
    def setUp(self):
        super().setUp()
        for name in ("orch", "auditeur"):
            registry.upsert(self.db, name, harness="claude", host=self.cfg.host)
            # #3036 : un brief ne se dépose que chez un agent qui peut le lire
            # (bail vivant) ; la porte de #3007 est testée avec des
            # destinataires vivants, le cas mort étant couvert par #3036.
            self.assertIsNotNone(
                registry.claim(self.db, name, "runner-test", 3600))
        self.db.execute("UPDATE agent_registry SET responsible = %s WHERE name = 'orch'",
                        (ALICE,))
        self.lot = work.add(self.db, title="RT-9", assignee="orch")
        work.move(self.db, self.lot["id"], "build")

    def ask(self):
        view = decisions.ask(
            self.cfg, self.db, requester="orch",
            question="Fusionner la branche avant la démo ?",
            options=["a=oui", "b=non"], lot=str(self.lot["id"]),
            due_ts=time.time() + 3600)
        # échue : l'échéance passe en base, comme #789 (`due_ts` refusé passé)
        self.db.execute(
            "UPDATE agent_mailbox SET payload = jsonb_set(payload, '{decision,due_ts}',"
            " to_jsonb(%s::float8)), created_at = now() - interval '20 minutes'"
            " WHERE id = %s",
            (time.time() - 60, view["id"]))
        return decisions.get(self.db, view["id"])

    def beat(self, human=ALICE, *, age=0.0, host=HOST, now=None):
        now = time.time() if now is None else now
        storage.of(self.db).notify_liveness.beat(host, [human], now=now - age)
        return now

    def alerts(self, now=None):
        now = time.time() if now is None else now
        return [a for a in exploitation.alerts(self.cfg, self.db, now=now)
                if a["type"] == notify_gap.GAP_TYPE]

    def gap_mails(self, agent="auditeur"):
        return [row for row in mail.unread(self.db, agent)
                if (row.get("payload") or {}).get("alert") == notify_gap.GAP_TYPE]

    def test_sans_battement_aucune_fausse_alerte(self):
        # B1 de la revue : une VM sans route, poste sain (ou inconnu), ne
        # produit ni alerte ni avertissement.
        self.ask()
        self.assertEqual(self.alerts(), [])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(actions_cli.cmd_decisions(
                self.cfg, argparse.Namespace(for_human=None, all=False, json=False)), 0)
        self.assertNotIn("warning:", out.getvalue())

    def test_un_battement_frais_ferme_l_alerte(self):
        self.ask()
        now = self.beat()
        self.assertEqual(self.alerts(now=now), [])

    def test_une_route_vide_exclut_meme_avec_default(self):
        # B1 de dev4/cx1 : channels_for(ALICE)=[] alors que le default existe ;
        # le battement ne doit pas déclarer ALICE couverte.
        self.ask()
        now = self.beat(age=3600)       # le poste perdu l'avait couverte
        ncfg = notify.parse_config({"routes": {ALICE: []}, "default": ["app"]})
        self.assertEqual(ncfg.channels_for(ALICE), [])
        notify_gap.beat(self.cfg, self.db, ncfg, now=now)
        (alert,) = self.alerts(now=now)
        self.assertEqual((alert["human"], alert["reason"]), (ALICE, "notifier_stale"))

    def test_default_couvre_un_humain_sans_route(self):
        self.ask()
        now = self.beat(age=3600)
        ncfg = notify.parse_config({"default": ["app"]})
        notify_gap.beat(self.cfg, self.db, ncfg, now=now)
        self.assertEqual(self.alerts(now=now), [])

    def test_les_humains_de_config_sont_lus_comme_des_noms(self):
        # B2 (cx1, dev4) : cfg.humans est une chaîne CSV ; le battement doit
        # lire cfg.human_names, pas itérer les caractères.
        fake = types.SimpleNamespace(host=self.cfg.host, humans="alice,bob",
                                     human_names=frozenset({"alice", "bob"}))
        ncfg = notify.parse_config({"default": ["app"]})
        notify_gap.beat(fake, self.db, ncfg)
        humans = {row["human"] for row in storage.of(self.db).notify_liveness.latest()}
        self.assertEqual(humans, {"human:alice", "human:bob"})

    def test_le_brief_suit_les_orchestrateurs_declares(self):
        # N1 de dev4 : les destinataires du brief sont ceux de l'alerte
        # (`alert_orchestrators`, y compris AMEESH_ALERT_ORCHESTRATORS).
        self.ask()
        now = self.beat(age=3600)
        with mock.patch.dict(os.environ,
                             {"AMEESH_ALERT_ORCHESTRATORS": "orch"}):
            report = notify_gap.brief_once(self.cfg, self.db, now=now)
        self.assertIn("orch", {entry["recipient"] for entry in report["sent"]})
        self.assertIn("auditeur", {entry["recipient"] for entry in report["sent"]})

    def test_sans_vivant_le_brief_va_au_fil_et_pas_dans_une_boite(self):
        # B1 dev1/cx1 : #3036 replie sur `thread:bureau` quand plus aucun
        # destinataire ne lit ; ce n'est pas une boîte d'agent. Le brief doit
        # aller au fil lisible, sans courrier `recipient=thread:bureau` ni
        # réclamation comptée sur une remise irréelle.
        self.ask()
        now = self.beat(age=3600)
        for name in ("orch", "auditeur"):
            self.db.execute(
                "UPDATE agent_registry SET lease_owner = NULL,"
                " lease_expires_at = NULL WHERE name = %s", (name,))
        registry.upsert(self.db, "bureau", harness="claude", host=self.cfg.host)
        self.db.execute("UPDATE agent_registry SET mode = 'externe'"
                        " WHERE name = 'bureau'")
        report = notify_gap.brief_once(self.cfg, self.db, now=now)
        self.assertEqual([e["recipient"] for e in report["sent"]], ["thread:bureau"])
        self.assertEqual(
            self.db.query("SELECT count(*) FROM agent_mailbox"
                          " WHERE recipient = 'thread:bureau'"), [{"count": 0}])
        entries = fil.transport_for(self.cfg).read(
            fil.ThreadRef(self.cfg.project, None))
        self.assertEqual(len(entries), 1)
        self.assertIn("no living notifier covers human:alice", entries[-1].text)

    def test_un_hote_ferme_avec_bail_valide_ne_recoit_pas_le_brief(self):
        # N1 dev1/cx1 : un bail encore valide sur un hôte aux claims fermés ne
        # prouve pas que l'agent lira ; `closed_hosts` l'écarte et le brief ne
        # le compte pas prévenu.
        self.ask()
        now = self.beat(age=3600)
        self.db.execute(
            "INSERT INTO host_lifecycle(host, mode, actor)"
            " VALUES ('lost-host', 'lost', 'test')"
            " ON CONFLICT (host) DO UPDATE SET mode = 'lost'")
        self.db.execute("UPDATE agent_registry SET host = 'lost-host'"
                        " WHERE name = 'auditeur'")
        with mock.patch.dict(os.environ,
                             {"AMEESH_ALERT_ORCHESTRATORS": "orch"}):
            report = notify_gap.brief_once(self.cfg, self.db, now=now)
        sent = {e["recipient"] for e in report["sent"]}
        self.assertIn("orch", sent)
        self.assertNotIn("auditeur", sent)

    def test_un_battement_perime_leve_l_alerte(self):
        view = self.ask()
        now = self.beat(age=3600)
        (alert,) = self.alerts(now=now)
        self.assertEqual((alert["human"], alert["responsible"], alert["last_host"],
                          alert["decisions"], alert["overdue"], alert["reason"]),
                         (ALICE, ALICE, HOST, [view["id"]], [view["id"]],
                          "notifier_stale"))
        self.assertAlmostEqual(alert["since"], now - 3600, places=3)
        self.assertIsNone(alert.get("host"))
        self.assertIn("last heartbeat from host pc-smichea", alert["detail"])
        self.assertIn("nothing is pushed to human:alice", alert["detail"])
        # la même panne qui dure est la même alerte
        (again,) = self.alerts(now=now + 60)
        self.assertEqual(exploitation.alert_key(alert), exploitation.alert_key(again))

    def test_un_autre_hote_qui_couvre_ferme_l_ecart(self):
        # cx1 : un autre hôte réellement couvrant doit fermer l'écart, même
        # quand le poste perdu avait couvert l'humain et que la VM l'exclut.
        self.ask()
        now = self.beat(age=3600)
        ncfg = notify.parse_config({"routes": {ALICE: []}, "default": ["app"]})
        notify_gap.beat(self.cfg, self.db, ncfg, now=now)
        self.assertEqual(len(self.alerts(now=now)), 1)
        storage.of(self.db).notify_liveness.beat("ameesh-agents-1", [ALICE], now=now)
        self.assertEqual(self.alerts(now=now), [])

    def test_un_autre_humain_couvert_laisse_alice_decouverte(self):
        view = self.ask()
        # Grâce ALIVE_S : la demande doit être plus vieille que la fenêtre
        # pour que l'absence de couverture soit un constat, pas une course.
        self.db.execute(
            "UPDATE agent_mailbox SET created_at = now() - interval '20 minutes'"
            " WHERE id = %s", (view["id"],))
        now = self.beat(human=BOB)
        (alert,) = self.alerts(now=now)
        self.assertEqual((alert["reason"], alert["last_host"]), ("uncovered", None))
        self.assertEqual(alert["decisions"], [view["id"]])
        self.assertIn("no notifier has ever reported covering human:alice",
                      alert["detail"])

    def test_ameesh_decisions_avertit_sur_le_battement_perime(self):
        self.ask()
        self.beat(age=3600)
        args = argparse.Namespace(for_human=None, all=False, json=False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(actions_cli.cmd_decisions(self.cfg, args), 0)
        texte = out.getvalue()
        self.assertIn("warning: no living notifier covers human:alice", texte)
        self.assertIn("nothing is pushed to human:alice", texte)
        # un battement frais : plus d'avertissement
        self.beat()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(actions_cli.cmd_decisions(self.cfg, args), 0)
        self.assertNotIn("warning:", out.getvalue())

    def test_le_notificateur_bat_et_ne_briefe_pas_sa_propre_alerte(self):
        self.ask()
        now = self.beat(age=3600)
        ncfg = notify.parse_config({})
        notifier = notify.Notifier(
            self.cfg, ncfg, notify.empty_state(), sender=notify.DrySender(),
            log=lambda text: None, clock=lambda: now)
        notifier.run_pass(self.db)
        # le notificateur sans route voit l'alerte, mais ne la briefe pas :
        # c'est la passe hors du poste qui porte ce courrier.
        self.assertEqual(self.gap_mails(), [])
        self.assertEqual(len(self.alerts(now=now)), 1)
        report = notify_gap.brief_once(self.cfg, self.db, now=now)
        self.assertEqual([(e["recipient"], e["human"]) for e in report["sent"]],
                         [("auditeur", ALICE)])
        self.assertEqual(len(self.gap_mails()), 1)

    def test_un_notificateur_a_route_bat_et_ferme_la_fausse_alerte(self):
        self.ask()
        ncfg = notify.parse_config({"routes": {ALICE: ["app"]}})
        notifier = notify.Notifier(
            self.cfg, ncfg, notify.empty_state(), sender=notify.DrySender(),
            log=lambda text: None)
        notifier.run_pass(self.db, current=[])
        rows = storage.of(self.db).notify_liveness.latest()
        self.assertIn((self.cfg.host, ALICE),
                      [(r["host"], r["human"]) for r in rows])
        self.assertEqual(self.alerts(), [])

    def test_une_passe_restreinte_ne_bat_pas(self):
        # B/P1 cx2, auditeur (#3202) : `_run_tail` ne livre aucune alerte
        # humaine ; une minuterie de rôle ne doit donc pas rafraîchir
        # `notify_liveness`, sinon un vrai notificateur arrêté resterait masqué.
        ncfg = notify.parse_config({"routes": {ALICE: ["app"]}})
        for only in ({"ci"}, {"take"}):
            notifier = notify.Notifier(
                self.cfg, ncfg, notify.empty_state(), sender=notify.DrySender(),
                log=lambda text: None)
            notifier.run_pass(self.db, only=only)
            self.assertEqual(storage.of(self.db).notify_liveness.latest(), [], only)

    def test_le_brief_est_unique_puis_relance(self):
        self.ask()
        now = self.beat(age=3600)
        first = notify_gap.brief_once(self.cfg, self.db, now=now)
        self.assertEqual(len(first["sent"]), 1)
        self.assertFalse(first["sent"][0]["reminder"])
        # même fenêtre : aucune duplication (deux hôtes ne doublonnent pas)
        second = notify_gap.brief_once(self.cfg, self.db, now=now + 60)
        self.assertEqual(second["sent"], [])
        self.assertEqual(second["skipped"], 1)
        # après six heures : une relance, pas un nouvel envoi
        third = notify_gap.brief_once(
            self.cfg, self.db, now=now + alert_briefs.REMIND_AFTER_S + 1)
        self.assertEqual(len(third["sent"]), 1)
        self.assertTrue(third["sent"][0]["reminder"])
        bodies = [row["body"] for row in self.gap_mails()]
        self.assertEqual(len(bodies), 2)
        self.assertIn("Reminder", bodies[-1])

    def test_un_nouvel_episode_n_est_pas_une_relance(self):
        # #3913 : la réclamation de brief survit au trou résorbé ; un nouvel
        # épisode, même après six heures, part avec le texte simple et son
        # propre âge — pas « Reminder, alert still active for 15 h 53 ».
        self.ask()
        now = self.beat(age=3600)
        first = notify_gap.brief_once(self.cfg, self.db, now=now)
        self.assertEqual(len(first["sent"]), 1)
        # le trou se résorbe, puis un nouveau trou naît plus de 6 h après le
        # premier brief : la réclamation existe encore mais précède l'épisode.
        later = now + 10 * 3600
        self.beat(now=later)
        self.db.execute(
            "UPDATE notify_liveness SET seen_at = to_timestamp(%s)"
            " WHERE host = %s", (later - 3600, HOST))
        third = notify_gap.brief_once(self.cfg, self.db, now=later)
        self.assertEqual(len(third["sent"]), 1)
        bodies = [row["body"] for row in self.gap_mails()]
        self.assertEqual(len(bodies), 2)
        self.assertFalse(bodies[0].startswith("Reminder"))
        self.assertFalse(bodies[1].startswith("Reminder"))
        self.assertIn("no living notifier", bodies[1])

    @mock.patch("ameesh.migrations.require_compatible_writer", new=lambda db: None)
    def test_le_runner_porte_le_brief_sans_le_poste(self):
        from ameesh import runner as runner_mod
        self.ask()
        now = self.beat(age=3600)
        fake = types.SimpleNamespace(cfg=self.cfg, dry_run=False, mediated=False, db=None)
        report = runner_mod.Runner.notify_health_once(fake, db=self.db, now=now)
        self.assertEqual([(e["recipient"], e["human"]) for e in report["sent"]],
                         [("auditeur", ALICE)])
        self.assertEqual(len(self.gap_mails()), 1)

    def test_la_reclamation_de_brief_est_partagee_et_relachee(self):
        liveness = storage.of(self.db).notify_liveness
        first = liveness.claim_brief("orch", ALICE, since=1000.0, now=1000.0)
        self.assertEqual(first, {"claimed": True, "previous_ts": None})
        again = liveness.claim_brief("orch", ALICE, since=1000.0, now=1001.0)
        self.assertFalse(again["claimed"])
        liveness.release_brief("orch", ALICE)
        retry = liveness.claim_brief("orch", ALICE, since=1000.0, now=1002.0)
        self.assertTrue(retry["claimed"])
        self.assertIsNone(retry["previous_ts"])


if __name__ == "__main__":
    unittest.main()
