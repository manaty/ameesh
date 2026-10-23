# SPDX-License-Identifier: AGPL-3.0-only
"""#3036 : une alerte ne se dépose pas chez un agent qui ne lira pas.

Un agent sans bail vivant, ou sur un hôte aux claims fermés (`draining`,
`lost`, clôture de release), ne peut pas prendre de tour : lui remettre le
brief le compte pour prévenu et l'alerte ne repart jamais. Les témoins :

* `briefs()` écarte l'auditeur sur un hôte perdu et adresse le palier suivant ;
* aucun courrier n'est déposé dans la boîte d'un agent mort ;
* la remise n'est comptée faite qu'une fois déposée chez un vivant ; la passe
  suivante, après le retour, remet — sans doublon ;
* `mail_undeliverable` et `idle_capacity` se replient sur l'auditeur vivant,
  puis sur le fil lisible du bureau.
"""
from __future__ import annotations

import time
import unittest
from unittest import mock

from ameesh import alert_briefs, fil, mail, notify, registry

from .support import PgTestCase

HEURE = 3600.0


def _row(name, *, host="h1", lease=True, team="ameesh", mode="execute",
         status="idle"):
    row = {"name": name, "team": team, "host": host, "lease_live": lease,
           "status": status}
    if mode:
        row["mode"] = mode
    return row


def _slo(*, level=3, targets=("bureau", "auditeur"), **kw):
    alert = {"type": "slo_breach", "agent": "dev1", "lot": 42, "title": "un lot",
             "lot_state": "build", "value": 7200, "threshold": 1800,
             "step": "build_quiet", "step_label": "build", "level": level,
             "targets": list(targets), "reason": "build_quiet/level-%d" % level,
             "orchestrators": ["orch"], "project": "ameesh"}
    alert.update(kw)
    return alert


class BriefsLivenessTest(unittest.TestCase):
    """La résolution pure : qui reçoit, qui est nommé écarté, où l'on replie."""

    def test_auditeur_hote_perdu_ecarte_et_palier_suivant(self):
        # Niveau 2 : le pilote est mort, le bureau vit encore ; l'auditeur du
        # niveau 3 est sur un hôte perdu. Le bureau reçoit, l'auditeur non,
        # et le brief dit que le pilote manquait.
        listing = [_row("dev1"), _row("orch", lease=False), _row("bureau"),
                   _row("auditeur", host="lost")]
        briefs = alert_briefs.briefs(
            _slo(level=2, targets=("pilot",)), listing,
            auditor_names=["auditeur"], orchestrator_names=("orch",),
            closed_hosts={"lost"})
        self.assertEqual(sorted(briefs), ["bureau"])
        self.assertIn("orch (no live lease)", briefs["bureau"])
        self.assertIn("auditeur (host lost: claims closed)", briefs["bureau"])
        self.assertIn("Escalated from level 2 to level 3", briefs["bureau"])
        self.assertIn("SLO breach, level 3", briefs["bureau"])

    def test_aucun_vivant_va_au_fil_et_nomme_les_absents(self):
        listing = [_row("dev1", lease=False), _row("orch", host="lost"),
                   _row("bureau", host="lost"), _row("auditeur", host="lost")]
        briefs = alert_briefs.briefs(
            _slo(), listing, auditor_names=["auditeur"],
            closed_hosts={"lost"})
        self.assertEqual(list(briefs), ["thread:bureau"])
        text = briefs["thread:bureau"]
        self.assertIn("No live recipient:", text)
        self.assertIn("auditeur (host lost: claims closed)", text)
        self.assertIn("bureau (host lost: claims closed)", text)

    def test_un_seul_vivant_au_palier_suffit(self):
        listing = [_row("dev1"), _row("orch"), _row("bureau"),
                   _row("auditeur", lease=False)]
        briefs = alert_briefs.briefs(
            _slo(), listing, auditor_names=["auditeur"])
        self.assertEqual(list(briefs), ["bureau"])
        self.assertIn("auditeur (no live lease)", briefs["bureau"])
        self.assertNotIn("Escalated", briefs["bureau"])

    def test_niveau_1_assigne_mort_passe_au_pilote(self):
        # Assigné sans bail au niveau 1 : le pilote (palier 2) reçoit, pas
        # l'auditeur ; un lot sans assigné ne laisse pas une liste vide.
        # L'alerte ne porte pas `orchestrators` à ce niveau : le brief reprend
        # les rôles vivants fournis par le notificateur (revert du correctif
        # exploitation.py, cf. note du lot).
        listing = [_row("dev1", lease=False), _row("orch"), _row("auditeur")]
        briefs = alert_briefs.briefs(
            _slo(level=1, targets=("assignee",), orchestrators=()), listing,
            auditor_names=["auditeur"], orchestrator_names=("orch",))
        self.assertEqual(list(briefs), ["orch"])
        self.assertIn("dev1 (no live lease). Escalated from level 1 to level 2",
                      briefs["orch"])
        briefs = alert_briefs.briefs(
            _slo(level=1, targets=("assignee",), agent=None, orchestrators=()),
            listing, auditor_names=["auditeur"], orchestrator_names=("orch",))
        self.assertIn("No live recipient: none designated.", briefs["orch"])

    def test_agent_arrete_ecarte_meme_si_le_bail_court_encore(self):
        # Un agent `stopped` peut garder son bail quelques secondes (fin de
        # tour) : il ne prendra plus de tour, le brief ne doit pas y rester.
        listing = [_row("dev1"), _row("orch", status="stopped"),
                   _row("bureau"), _row("auditeur")]
        briefs = alert_briefs.briefs(
            _slo(level=2, targets=("pilot",)), listing,
            auditor_names=["auditeur"], orchestrator_names=("orch",))
        self.assertEqual(sorted(briefs), ["auditeur", "bureau"])
        self.assertIn("orch (status stopped)", briefs["bureau"])
        self.assertIn("Escalated from level 2 to level 3", briefs["bureau"])

    def test_ligne_sans_information_de_bail_reste_jugee_vivante(self):
        # Appel hors base (tests, outils) : l'absence de preuve n'est pas une
        # preuve de mort ; le comportement d'avant est conservé.
        listing = [{"name": "orch", "team": "ameesh"},
                   {"name": "auditeur", "team": ""}]
        briefs = alert_briefs.briefs(
            _slo(level=2, targets=("pilot",)), listing,
            auditor_names=["auditeur"], orchestrator_names=("orch",))
        self.assertEqual(list(briefs), ["orch"])

    def test_bureau_externe_recoit_une_copie_au_fil(self):
        listing = [_row("dev1", lease=False), _row("orch", lease=False),
                   _row("bureau", mode="externe"), _row("auditeur", lease=False)]
        briefs = alert_briefs.briefs(
            _slo(), listing, auditor_names=["auditeur"])
        self.assertEqual(list(briefs), ["thread:bureau"])

    def test_mail_undeliverable_replie_sur_l_auditeur(self):
        alert = {"type": "mail_undeliverable", "agent": "claude3", "value": 1,
                 "threshold": 900, "reason": "arrete", "senders": ["claude1"],
                 "orchestrators": ["orch"]}
        listing = [_row("orch", lease=False), _row("auditeur"), _row("bureau")]
        briefs = alert_briefs.briefs(alert, listing, auditor_names=["auditeur"])
        self.assertEqual(list(briefs), ["auditeur"])
        self.assertIn("Undeliverable mail: 1 message(s)", briefs["auditeur"])
        self.assertIn("No live recipient: orch (no live lease)", briefs["auditeur"])

    def test_idle_capacity_replie_sur_l_auditeur(self):
        alert = {"type": "idle_capacity", "agents": ["dev1"], "lots": [],
                 "orchestrators": ["orch"], "threshold": 1800.0,
                 "detail": "1 agent au repos. Action : répartir"}
        listing = [_row("dev1"), _row("orch", lease=False), _row("auditeur")]
        briefs = alert_briefs.briefs(alert, listing, auditor_names=["auditeur"])
        self.assertEqual(list(briefs), ["auditeur"])
        self.assertIn("Idle capacity:", briefs["auditeur"])

    def test_stale_lot_ne_reveille_toujours_pas_l_auditeur(self):
        # L713 prime : sans orchestrateur vivant, rien ne part (pas de réveil
        # de l'auditeur) ; le notificateur réessaiera à la passe suivante.
        alert = {"type": "stale_lot", "agent": "dev1", "lot": 42, "title": "t",
                 "lot_state": "build", "value": 7 * HEURE,
                 "orchestrators": ["orch"]}
        listing = [_row("dev1"), _row("orch", lease=False), _row("auditeur")]
        self.assertEqual(alert_briefs.briefs(alert, listing,
                                             auditor_names=["auditeur"]), {})


class NotifierLivenessTest(PgTestCase):
    """La livraison réelle : pas de dépôt chez un mort, remise au retour."""

    def setUp(self):
        super().setUp()
        for name in ("dev1", "orch", "bureau", "auditeur", "claude3"):
            registry.upsert(self.db, name, harness="claude", host=self.cfg.host)
            self.db.execute("UPDATE agent_registry SET team = 'ameesh'"
                            " WHERE name = %s", (name,))
        self.lease("dev1", "orch", "bureau", "auditeur")
        self.now = time.time()

    def lease(self, *names):
        for name in names:
            self.db.execute(
                "UPDATE agent_registry SET lease_owner = 'runner-' || name,"
                " lease_expires_at = now() + interval '1 hour' WHERE name = %s",
                (name,))

    def dead(self, *names):
        for name in names:
            self.db.execute(
                "UPDATE agent_registry SET lease_owner = NULL,"
                " lease_expires_at = NULL WHERE name = %s", (name,))

    def notifier(self):
        return notify.Notifier(self.cfg, notify.parse_config({"default": []}),
                               sender=object(), log=lambda text: None,
                               clock=lambda: self.now)

    def mails(self, name):
        return self.db.query("SELECT body, payload FROM agent_mailbox"
                             " WHERE recipient = %s ORDER BY id", (name,))

    def thread(self, project, lot):
        return fil.transport_for(self.cfg).read(fil.ThreadRef(project, lot))

    def test_agent_mort_sur_hote_perdu_ne_recoit_rien_et_est_nomme(self):
        self.dead("orch")
        self.db.execute("INSERT INTO host_lifecycle(host, mode, actor)"
                        " VALUES ('lost-host', 'lost', 'test')"
                        " ON CONFLICT (host) DO UPDATE SET mode = 'lost'")
        self.db.execute("UPDATE agent_registry SET host = 'lost-host'"
                        " WHERE name = 'auditeur'")
        notifier = self.notifier()
        alert = _slo(level=2, targets=("pilot",))
        notifier.run_pass(self.db, current=[alert])
        self.assertEqual(self.mails("orch"), [])      # bail mort : aucun dépôt
        self.assertEqual(self.mails("auditeur"), [])  # hôte perdu : aucun dépôt
        [mail] = self.mails("bureau")
        self.assertIn("orch (no live lease)", mail["body"])
        self.assertIn("auditeur (host lost-host: claims closed)", mail["body"])
        # la remise au bureau est comptée ; les morts ne le sont pas
        [entry] = list(notifier.state["briefs"].values())
        self.assertEqual(entry["to"], ["bureau"])

    def test_remise_a_la_premiere_passe_apres_le_retour(self):
        self.dead("orch")
        self.db.execute("INSERT INTO host_lifecycle(host, mode, actor)"
                        " VALUES ('lost-host', 'lost', 'test')"
                        " ON CONFLICT (host) DO UPDATE SET mode = 'lost'")
        self.db.execute("UPDATE agent_registry SET host = 'lost-host'"
                        " WHERE name = 'auditeur'")
        notifier = self.notifier()
        alert = _slo(level=2, targets=("pilot",))
        notifier.run_pass(self.db, current=[alert])
        self.assertEqual(self.mails("auditeur"), [])
        # retour de l'auditeur : bail vivant et hôte rouvert
        self.lease("auditeur")
        self.db.execute("UPDATE host_lifecycle SET mode = 'available'"
                        " WHERE host = 'lost-host'")
        self.now += 60.0
        notifier.run_pass(self.db, current=[alert])
        [mail] = self.mails("auditeur")
        self.assertIn("SLO breach, level 3", mail["body"])
        # pas de seconde remise au même palier
        self.now += 60.0
        notifier.run_pass(self.db, current=[alert])
        self.assertEqual(len(self.mails("auditeur")), 1)

    def test_aucun_vivant_ecrit_au_fil_puis_remet_au_retour(self):
        self.dead("dev1", "orch", "bureau", "auditeur")
        notifier = self.notifier()
        alert = _slo(level=1, targets=("assignee",))
        notifier.run_pass(self.db, current=[alert])
        for name in ("dev1", "orch", "bureau", "auditeur"):
            self.assertEqual(self.mails(name), [], name)
        [entry] = self.thread("ameesh", 42)
        self.assertIn("No live recipient:", entry.text)
        self.assertIn("dev1 (no live lease)", entry.text)
        [state] = list(notifier.state["briefs"].values())
        self.assertEqual(state["to"], ["thread:bureau"])
        # l'assigné revient : le palier 1 le reçoit (le fil a la trace humaine)
        self.lease("dev1")
        self.now += 60.0
        notifier.run_pass(self.db, current=[alert])
        [mail] = self.mails("dev1")
        self.assertIn("SLO breach, level 1", mail["body"])
        self.assertEqual(len(self.thread("ameesh", 42)), 1)

    def test_mail_undeliverable_mort_ecarte_auditeur_vivant(self):
        self.dead("orch")
        alert = {"type": "mail_undeliverable", "agent": "claude3", "value": 1,
                 "threshold": 900, "reason": "arrete", "senders": ["claude1"],
                 "orchestrators": ["orch"], "since": self.now - 1200,
                 "detail": "undeliverable", "urgent": True}
        notifier = self.notifier()
        notifier.run_pass(self.db, current=[alert])
        self.assertEqual(self.mails("orch"), [])
        [mail] = self.mails("auditeur")
        self.assertIn("Undeliverable mail:", mail["body"])
        self.assertEqual(mail["payload"]["recipient"], "claude3")
        # l'orchestrateur revient : il reçoit la levée, sans doublon
        self.lease("orch")
        self.now += 60.0
        notifier.run_pass(self.db, current=[alert])
        self.assertEqual(len(self.mails("orch")), 1)
        self.assertEqual(len(self.mails("auditeur")), 1)

    def test_ledger_ancien_remet_au_destinataire_revenu(self):
        # #3036 B/P1 cx3 : une entrée de l'ancien notificateur compte une
        # remise faite sur un hôte perdu. La passe où le destinataire est mort
        # doit le retirer de `to` ; son retour reçoit alors un courrier.
        notifier = self.notifier()
        alert = _slo(level=3, targets=("bureau", "auditeur"))
        key = notify.key_of(alert)
        notifier.state["briefs"][key] = {
            "type": "slo_breach", "sent_ts": self.now, "lot": 42,
            "lot_state": "build", "agent": "dev1", "reminded": True,
            "to": ["bureau", "auditeur"]}
        self.dead("bureau", "auditeur")
        notifier.run_pass(self.db, current=[alert])
        self.assertEqual(self.mails("auditeur"), [])
        self.assertEqual(self.mails("bureau"), [])
        self.assertNotIn("auditeur", notifier.state["briefs"][key]["to"])
        self.assertNotIn("bureau", notifier.state["briefs"][key]["to"])
        # retour de l'auditeur : la passe remet, le bureau resté mort non
        self.lease("auditeur")
        self.now += 60.0
        notifier.run_pass(self.db, current=[alert])
        [sent] = self.mails("auditeur")
        self.assertIn("SLO breach, level 3", sent["body"])
        self.assertEqual(self.mails("bureau"), [])
        self.assertIn("auditeur", notifier.state["briefs"][key]["to"])
        self.assertNotIn("bureau", notifier.state["briefs"][key]["to"])

    def test_mail_undeliverable_echec_transitoire_est_retente(self):
        # #3036 B/P1 cx3 : un envoi en échec entre deux réussites ne doit pas
        # être oublié ; la passe suivante le remet, sans doubler les réussites.
        registry.upsert(self.db, "orch2", harness="claude", host=self.cfg.host)
        self.db.execute("UPDATE agent_registry SET team = 'ameesh'"
                        " WHERE name = 'orch2'")
        self.lease("orch2")
        alert = {"type": "mail_undeliverable", "agent": "claude3", "value": 1,
                 "threshold": 900, "reason": "arrete", "senders": ["claude1"],
                 "orchestrators": ["orch", "orch2"], "since": self.now - 1200,
                 "detail": "undeliverable", "urgent": True}
        notifier = self.notifier()
        real_send = mail.send

        def flaky(db, sender, to, *args, **kwargs):
            if to == "orch":
                raise RuntimeError("temporarily unavailable")
            return real_send(db, sender, to, *args, **kwargs)

        with mock.patch.object(mail, "send", side_effect=flaky):
            notifier.run_pass(self.db, current=[alert])
        self.assertEqual(self.mails("orch"), [])
        [sent] = self.mails("orch2")
        self.assertIn("Undeliverable mail:", sent["body"])
        [entry] = notifier.state["briefs"].values()
        self.assertEqual(entry["to"], ["orch2"])
        self.assertEqual(entry["pending"], ["orch"])
        # l'erreur disparaît : le destinataire en échec reçoit, une seule fois
        self.now += 60.0
        notifier.run_pass(self.db, current=[alert])
        self.assertEqual(len(self.mails("orch")), 1)
        self.assertEqual(len(self.mails("orch2")), 1)
        [entry] = notifier.state["briefs"].values()
        self.assertEqual(entry["pending"], [])
        # rien de plus à la passe suivante
        self.now += 60.0
        notifier.run_pass(self.db, current=[alert])
        self.assertEqual(len(self.mails("orch")), 1)
        self.assertEqual(len(self.mails("orch2")), 1)

    def test_bail_qui_cligne_ne_redouble_ni_n_escalade(self):
        # #3100 : remis au pilote, puis son bail meurt une passe (redémarrage
        # d'exécuteur) et revient. Le courrier était dans sa boîte : un seul
        # courrier, et ni le bureau ni l'auditeur ne reçoivent d'escalade.
        notifier = self.notifier()
        alert = _slo(level=2, targets=("pilot",))
        notifier.run_pass(self.db, current=[alert])
        self.assertEqual(len(self.mails("orch")), 1)
        self.dead("orch")
        self.now += 60.0
        notifier.run_pass(self.db, current=[alert])
        self.lease("orch")
        self.now += 60.0
        notifier.run_pass(self.db, current=[alert])
        self.assertEqual(len(self.mails("orch")), 1)
        self.assertEqual(self.mails("bureau"), [])
        self.assertEqual(self.mails("auditeur"), [])
        self.assertEqual(self.thread("ameesh", 42), [])
        [entry] = notifier.state["briefs"].values()
        self.assertEqual(entry["to"], ["orch"])

    def test_cloture_de_release_de_l_hote_ne_redouble_rien(self):
        # #3100 : une clôture de release ferme les claims de l'hôte pour tous
        # ses agents à la fois ; les paliers déjà prévenus restent couverts.
        notifier = self.notifier()
        alerts = [_slo(level=1, targets=("assignee",)),
                  _slo(level=2, targets=("pilot",), lot=43, agent="dev1")]
        notifier.run_pass(self.db, current=alerts)
        self.assertEqual(len(self.mails("dev1")), 1)
        self.assertEqual(len(self.mails("orch")), 1)
        self.db.execute("INSERT INTO host_lifecycle(host, mode, actor)"
                        " VALUES (%s, 'draining', 'test')"
                        " ON CONFLICT (host) DO UPDATE SET mode = 'draining'",
                        (self.cfg.host,))
        self.now += 60.0
        notifier.run_pass(self.db, current=alerts)
        self.db.execute("UPDATE host_lifecycle SET mode = 'available'"
                        " WHERE host = %s", (self.cfg.host,))
        self.now += 60.0
        notifier.run_pass(self.db, current=alerts)
        self.assertEqual(len(self.mails("dev1")), 1)
        self.assertEqual(len(self.mails("orch")), 1)
        self.assertEqual(self.mails("bureau"), [])
        self.assertEqual(self.mails("auditeur"), [])


if __name__ == "__main__":
    unittest.main()
