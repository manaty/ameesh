# SPDX-License-Identifier: AGPL-3.0-only
"""#3007 : aucune voie de notification vers l'humain quand le poste tombe.

Constat #3006 (21/10/2026) : le seul notificateur à canaux est
`ameesh-notify` de pc-smichea ; l'hôte perdu, les hôtes sains n'ont aucune
route (`ameesh notify --test human:smichea` → « no channel », code 2) et
personne ne le voit sans y penser. Le lot accepte deux issues : un canal de
secours, ou l'absence de voie signalée là où un agent la voit. Ces témoins
couvrent la seconde : l'alerte `notify_undeliverable` (levée par
`exploitation.alerts`, donc `ameesh alerts`), le courrier aux orchestrateurs
et à l'auditeur (`alert_briefs`), et l'avertissement d'`ameesh decisions`.
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import io
import time
import types
import unittest

from ameesh import actions_cli, alert_briefs, decisions, exploitation, mail, notify, \
    notify_gap, registry, work

from .support import PgTestCase

ALICE = "human:alice"
BOB = "human:bob"


def _cfg(notify_raw=None, host="ameesh-agents-2"):
    """Une configuration minimale pour les fonctions pures (sans base)."""
    return types.SimpleNamespace(host=host, notify={} if notify_raw is None else notify_raw)


def _pending(ids=(7, 8), owner=ALICE, *, now=1000.0):
    return [{"id": ident, "owner": owner, "created_ts": now - 600.0 + ident,
             "overdue": ident == ids[0]} for ident in ids]


class GapsTest(unittest.TestCase):
    def test_sans_demande_aucun_ecart(self):
        self.assertEqual(notify_gap.gaps(_cfg(), [], now=1000.0), [])

    def test_sans_route_l_hote_nomme_l_humain_et_les_demandes(self):
        (gap,) = notify_gap.gaps(_cfg(), _pending(), now=1000.0)
        self.assertEqual((gap["human"], gap["host"], gap["count"], gap["overdue"],
                          gap["decisions"], gap["oldest"], gap["reason"]),
                         (ALICE, "ameesh-agents-2", 2, [7], [7, 8], 7, "no_channel"))
        self.assertEqual(gap["since"], 407.0)
        for texte in ("no notification channel on host ameesh-agents-2",
                      ALICE, "2 decision request(s)", "1 overdue", "oldest #7",
                      "fallback route", "ameesh decisions"):
            self.assertIn(texte, gap["detail"])
        # deux demandes du même humain ne font qu'un écart
        self.assertEqual(len(notify_gap.gaps(_cfg(), _pending(ids=(7, 8, 9)),
                                             now=1000.0)), 1)

    def test_une_route_pour_l_humain_ferme_l_ecart(self):
        cfg = _cfg({"routes": {ALICE: ["app"]}})
        self.assertEqual(notify_gap.gaps(cfg, _pending(), now=1000.0), [])
        cfg = _cfg({"default": ["app"]})
        self.assertEqual(notify_gap.gaps(cfg, _pending(), now=1000.0), [])

    def test_une_route_pour_un_autre_humain_ne_compte_pas(self):
        cfg = _cfg({"routes": {BOB: ["app"]}})
        (gap,) = notify_gap.gaps(cfg, _pending(), now=1000.0)
        self.assertEqual(gap["human"], ALICE)

    def test_configuration_illisible_est_un_ecart_sans_secret(self):
        cfg = _cfg({"channels": {"slack": {"url": "https://hooks.example/X"}}})
        (gap,) = notify_gap.gaps(cfg, _pending(), now=1000.0)
        self.assertEqual(gap["reason"], "invalid_config")
        self.assertIn("invalid notify configuration on host", gap["detail"])
        self.assertNotIn("hooks.example", gap["detail"])

    def test_une_demande_sans_proprietaire_n_est_pas_devinee(self):
        self.assertEqual(notify_gap.gaps(_cfg(), _pending(owner=None), now=1000.0), [])

    def test_deux_humains_deux_ecarts_ordre_stable(self):
        pending = _pending(ids=(7,), owner=BOB) + _pending(ids=(8,), owner=ALICE)
        gaps = notify_gap.gaps(_cfg(), pending, now=1000.0)
        self.assertEqual([g["human"] for g in gaps], [ALICE, BOB])

    def test_la_cle_distingue_les_humains(self):
        un = exploitation._alert("notify_undeliverable", None, 1.0, 1, 0, "d",
                                 human=ALICE, host="h")
        deux = dict(un, human=BOB)
        self.assertNotEqual(exploitation.alert_key(un), exploitation.alert_key(deux))

    def test_l_alerte_est_declaree_et_briefee(self):
        self.assertIn(notify_gap.GAP_TYPE, exploitation.ALERT_TYPES)
        self.assertIn(notify_gap.GAP_TYPE, notify.TYPE_LABELS)
        self.assertIn(notify_gap.GAP_TYPE, alert_briefs.BRIEFED_TYPES)
        self.assertNotIn(notify_gap.GAP_TYPE, notify.DEFAULT_TYPES)

    def test_le_brief_va_aux_orchestrateurs_et_a_l_auditeur(self):
        alert = {"type": notify_gap.GAP_TYPE, "human": ALICE, "host": "h",
                 "detail": "no channel", "orchestrators": ["orch"]}
        listing = [{"name": "orch"}, {"name": "auditeur"}]
        briefs = alert_briefs.briefs(alert, listing, auditor_names=("auditeur",),
                                     orchestrator_names=("orch",))
        self.assertEqual(sorted(briefs), ["auditeur", "orch"])
        self.assertIn("fallback route", briefs["orch"])
        self.assertIn("no channel", briefs["orch"])


class AlertesTest(PgTestCase):
    def setUp(self):
        super().setUp()
        for name in ("orch", "auditeur"):
            registry.upsert(self.db, name, harness="claude", host=self.cfg.host)
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
            " to_jsonb(%s::float8)) WHERE id = %s",
            (time.time() - 60, view["id"]))
        return decisions.get(self.db, view["id"])

    def alerts(self, cfg=None, now=None):
        return [a for a in exploitation.alerts(cfg or self.cfg, self.db, now=now)
                if a["type"] == notify_gap.GAP_TYPE]

    def test_ameesh_alerts_leve_l_alerte_sur_un_hote_sans_route(self):
        view = self.ask()
        (alert,) = self.alerts()
        self.assertEqual((alert["human"], alert["host"], alert["responsible"],
                          alert["decisions"], alert["overdue"], alert["reason"]),
                         (ALICE, self.cfg.host, ALICE, [view["id"]], [view["id"]],
                          "no_channel"))
        self.assertIn("nothing is pushed to %s" % ALICE, alert["detail"])

    def test_une_route_ferme_l_alerte(self):
        self.ask()
        cfg = dataclasses.replace(self.cfg, notify={"routes": {ALICE: ["app"]}})
        self.assertEqual(self.alerts(cfg), [])

    def test_ameesh_decisions_imprime_l_avertissement(self):
        self.ask()
        args = argparse.Namespace(for_human=None, all=False, json=False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(actions_cli.cmd_decisions(self.cfg, args), 0)
        texte = out.getvalue()
        self.assertIn("warning: no notification channel on host", texte)
        self.assertIn("nothing is pushed to %s" % ALICE, texte)
        # une route sur l'hôte : pas d'avertissement
        cfg = dataclasses.replace(self.cfg, notify={"routes": {ALICE: ["app"]}})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(actions_cli.cmd_decisions(cfg, args), 0)
        self.assertNotIn("warning:", out.getvalue())

    def test_le_notificateur_sans_canal_briefe_les_agents(self):
        self.ask()
        notifier = notify.Notifier(
            self.cfg, notify.parse_config({}), notify.empty_state(),
            sender=notify.DrySender(), log=lambda text: None)
        records = notifier.run_pass(self.db)
        # pas de livraison à l'humain : la route manque, tout est journalisé
        self.assertEqual([r for r in records if r["type"] == notify_gap.GAP_TYPE], [])
        # mais l'auditeur (et, déclarés au canon, les orchestrateurs) reçoit
        # le constat par courrier `event` — c'est le signal qui reste
        messages = [row for row in mail.unread(self.db, "auditeur")
                    if row.get("kind") == "event"
                    and (row.get("payload") or {}).get("alert") == notify_gap.GAP_TYPE]
        self.assertEqual(len(messages), 1, messages)
        self.assertIn("fallback route", messages[0]["body"])
        self.assertIn("nothing is pushed to %s" % ALICE, messages[0]["body"])


if __name__ == "__main__":
    unittest.main()
