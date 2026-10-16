# SPDX-License-Identifier: AGPL-3.0-only
"""#1883 — un compte en dépassement n'est jamais candidat, et le choix ne
bascule plus en boucle.

Incident du 16/10/2026 (pc-smichea, comptes Codex) : le même nom de compte
« secondaire » était déclaré dans plusieurs configurations de l'hôte, vers des
dossiers différents. Un runner y lisait ses vraies jauges (fenêtre 5 h à
100 %, « in overage, billed beyond the plan »), un autre y lisait celles d'un
compte à 34 % ; le choix partagé (`account_active`) basculait donc
primaire ↔ secondaire toutes les quelques secondes, et chaque bascule pouvait
lancer un tour facturé au-delà du forfait.

La règle « un compte en dépassement n'est jamais choisi » (`evaluate` →
`in_overage` → `hard`) n'était pas fautive : c'est la LECTURE du compte qui
n'était pas la sienne par moments, dans un autre processus. Témoins ici :

* deux comptes déclarés qui résolvent vers le MÊME dossier (chemin réel, liens
  compris) sont refusés à la lecture de la configuration — la dérive devient
  une erreur bruyante, jamais un choix payant ;
* l'overage vu par UN processus est publié pour l'hôte (`account_holds`) quand
  une bascule quitte le compte : les autres runners l'écartent même si leur
  lecture locale le dit sain, tant que la retenue court — la boucle s'arrête,
  et la rotation normale reprend dès que la retenue expire ;
* la capacité d'overage inconnue garde le plafond de base (#1176, inchangé) :
  seule une capacité explicite applique `overage_cap` ;
* une alerte `account_overage` part dès qu'une lecture dit le compte facturé
  au-delà de son plan, avec un `since` stable tant que la fenêtre court.
"""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from ameesh import accounts, cost, exploitation, notify, storage

from .test_l30_accounts import _Base, _dossier

HOUR = 3600.0
DAY = 86400.0
WEEK = 7 * DAY


def _journal(home: str, *, short=None, week=None, credits=None) -> None:
    """Un journal de session Codex enregistré : `short`/`week` sont des
    `(used_percent, resets_at)`. `credits` = l'objet `credits` du harnais
    (`has_credits` vrai + une fenêtre à 100 % ⇒ « en dépassement »)."""
    folder = os.path.join(home, "sessions", "2026", "10", "16")
    os.makedirs(folder, exist_ok=True)
    limits = {"plan_type": "pro", "primary": None, "secondary": None}
    if short is not None:
        limits["primary"] = {"used_percent": short[0], "window_minutes": 300,
                             "resets_at": short[1]}
    if week is not None:
        limits["secondary"] = {"used_percent": week[0], "window_minutes": 10080,
                               "resets_at": week[1]}
    if credits is not None:
        limits["credits"] = credits
    with open(os.path.join(folder, "rollout-test.jsonl"), "w",
              encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "session_meta", "payload": {"id": "x"}}) + "\n")
        fh.write(json.dumps({"type": "event_msg", "payload": {
            "type": "token_count", "rate_limits": limits}}) + "\n")


#: capacité de dépassement : le compte peut être facturé au-delà du forfait
CREDITS = {"has_credits": True, "unlimited": False}


class DossiersPartagesTest(unittest.TestCase):
    """`parse` refuse deux comptes qui lisent le même dossier."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-1883-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_deux_comptes_meme_dossier_refuses(self):
        dossier = _dossier(os.path.join(self.tmp, "codex-1"), creds="auth.json")
        with self.assertRaises(accounts.AccountError) as ctx:
            accounts.parse({"codex": [{"name": "primaire", "path": dossier},
                                      {"name": "secondaire", "path": dossier}]})
        message = str(ctx.exception)
        self.assertIn("primaire", message)
        self.assertIn("secondaire", message)

    def test_lien_symbolique_vers_le_meme_dossier_refuse(self):
        """La comparaison est faite sur le chemin **résolu** : un lien vers le
        même dossier est le même compte."""
        dossier = _dossier(os.path.join(self.tmp, "codex-1"), creds="auth.json")
        lien = os.path.join(self.tmp, "codex-2")
        os.symlink(dossier, lien)
        with self.assertRaises(accounts.AccountError):
            accounts.parse({"codex": [{"name": "primaire", "path": dossier},
                                      {"name": "secondaire", "path": lien}]})

    def test_compte_sans_dossier_et_defaut_explicite_refuses(self):
        """Un compte déclaré sans `path` lit le dossier par défaut du harnais :
        le redéclarer explicitement est le même dossier."""
        defaut = os.path.join(os.path.expanduser("~"), ".codex")
        with self.assertRaises(accounts.AccountError):
            accounts.parse({"codex": [{"name": "primaire"},
                                      {"name": "secondaire", "path": defaut}]})

    def test_dossiers_distincts_acceptes(self):
        a = _dossier(os.path.join(self.tmp, "codex-1"), creds="auth.json")
        b = _dossier(os.path.join(self.tmp, "codex-2"), creds="auth.json")
        profils = accounts.parse({"codex": [{"name": "primaire", "path": a},
                                            {"name": "secondaire", "path": b}]})["codex"]
        self.assertEqual([p.name for p in profils], ["primaire", "secondaire"])


class CapaciteTest(unittest.TestCase):
    """#1176, inchangé : une capacité inconnue ne prouve pas la gratuité du
    dépassement — mais elle ne l'applique pas non plus. Le plafond de base
    (80 % par défaut, `PACE_CEILING`) tient ; seule une capacité **explicite**
    ajoute le plafond de dépassement de 80 %."""

    NOW = 1_800_000_000.0

    def _profil(self):
        return accounts.Profile("codex", "secondaire", path="/x/codex-2",
                                pace_multiplier=10.0, pace_ceiling=0.95)

    def test_capacite_inconnue_garde_le_plafond_de_base(self):
        jauge = cost.Gauge("codex", "10080min", 0.85, self.NOW + 0.9 * WEEK, WEEK)
        inconnue = cost.Gauges([jauge], cost.Overage())
        caps = accounts.limits(self._profil(), inconnue, self.NOW)
        self.assertEqual(caps["10080min"][1], "pace cap")
        self.assertAlmostEqual(caps["10080min"][0], 0.95)
        # capacité explicite : le plafond de dépassement s'applique
        capable = cost.Gauges([jauge], cost.Overage(capable=True))
        caps = accounts.limits(self._profil(), capable, self.NOW)
        self.assertEqual(caps["10080min"][1], "overage cap")
        self.assertAlmostEqual(caps["10080min"][0], accounts.OVERAGE_CAP)

    def test_capacite_et_incapacite(self):
        profil = accounts.Profile("codex", "secondaire")
        self.assertTrue(accounts.overage_capable(profil, cost.Overage(capable=True)))
        self.assertFalse(accounts.overage_capable(profil, cost.Overage(capable=False)))
        self.assertFalse(accounts.overage_capable(profil, cost.Overage()))
        self.assertFalse(accounts.overage_capable(profil, None))

    def test_le_reglage_declare_prime(self):
        self.assertFalse(accounts.overage_capable(
            accounts.Profile("codex", "a", overage="none"), cost.Overage(capable=True)))
        self.assertTrue(accounts.overage_capable(
            accounts.Profile("codex", "a", overage="allowed"), None))


class OveragePartageTest(_Base):
    """L'overage vu par un runner écarte le compte pour les autres, sans
    retarder la rotation normale (le classement L117 reste intact)."""

    def setUp(self) -> None:
        super().setUp()
        reserve = mock.patch.object(accounts, "RESERVE", 0.0)
        reserve.start()
        self.addCleanup(reserve.stop)

    def _items(self, secondaire: str):
        a = _dossier(os.path.join(self.tmp, "comptes", "codex-1"), creds="auth.json")
        self.declared = [
            {"name": "primaire", "path": a, "pace_multiplier": 10},
            {"name": "secondaire", "path": secondaire,
             "pace_multiplier": 10, "priority": 20},
        ]
        return accounts.parse({"codex": self.declared})["codex"]

    def _book(self):
        return cost.CostBook(state_dir=self.cfg.state_dir, db=self.db)

    def _depassement(self, items, now):
        """Le runner A : sa lecture du secondaire est en dépassement, sa
        priorité 20 le mettrait premier ; le choix part au primaire."""
        storage.of(self.db).accounts.init_active(self.cfg.host, "codex", "secondaire")
        _journal(items[1].path, short=(100, now + 2 * HOUR),
                 week=(47, now + 5 * DAY), credits=CREDITS)
        _journal(items[0].path, week=(34, now + 6 * DAY))
        choix = accounts.choose(self.db, self.cfg.host, "codex", items,
                                self._book(), now=now, agent="runner-a")
        self.assertEqual(choix.profile.name, "primaire")
        self.assertEqual(choix.switched["to"], "primaire")

    def test_un_runner_ecarte_l_overage_pour_les_autres(self):
        b = _dossier(os.path.join(self.tmp, "comptes", "codex-2"), creds="auth.json")
        items = self._items(b)
        now = float(int(time.time()))
        self._depassement(items, now)

        # l'overage est publié pour l'hôte (retenue datée)
        retenues = storage.of(self.db).accounts.holds(self.cfg.host, "codex")
        self.assertIn("secondaire", retenues)
        self.assertGreater(float(retenues["secondaire"]["until_ts"]), now)

        # runner B : une AUTRE configuration du même nom lit un compte sain
        # (5 %, priorité 20) — la retenue ET le conflit d'identité (deux
        # dossiers sous le même nom) l'emportent sur sa lecture locale
        c = _dossier(os.path.join(self.tmp, "comptes", "codex-3"), creds="auth.json")
        _journal(c, week=(5, now + 6 * DAY))
        autres = self._items(c)
        bascules = len(self._bascules())
        choix = accounts.choose(self.db, self.cfg.host, "codex", autres,
                                self._book(), now=now + 1.0, agent="runner-b")
        self.assertEqual(choix.profile.name, "primaire", choix.why)
        self.assertEqual(len(self._bascules()), bascules)

        # la retenue expirée et la configuration divergente nettoyée (le
        # dossier de B n'existe plus : une seule identité pour ce nom), la
        # rotation normale reprend sur le dossier de A, redevenu sain
        self.db.execute("DELETE FROM account_identities")
        self.db.execute("UPDATE account_holds"
                        " SET until_ts = extract(epoch from now()) - 1")
        _journal(items[1].path, week=(5, now + 6 * DAY))
        choix = accounts.choose(self.db, self.cfg.host, "codex", items,
                                self._book(), now=time.time(), agent="runner-a")
        self.assertEqual(choix.profile.name, "secondaire", choix.why)
        self.assertEqual(choix.switched["to"], "secondaire")

    def test_un_forcage_humain_ne_fait_pas_tourner_un_compte_en_depassement(self):
        """`ameesh accounts use` ne fait pas tourner un compte en dépassement :
        la garde absolue prime, le forçage est levé pour un compte sain (et
        sans autre compte, il tient l'agent en pause, jamais un tour payé)."""
        b = _dossier(os.path.join(self.tmp, "comptes", "codex-2"), creds="auth.json")
        items = self._items(b)
        now = float(int(time.time()))
        self._depassement(items, now)
        self.assertTrue(accounts.force(self.db, self.cfg.host, "codex", items,
                                       "secondaire", by="test"))
        choix = accounts.choose(self.db, self.cfg.host, "codex", items,
                                self._book(), now=now + 1.0)
        self.assertEqual(choix.profile.name, "primaire", choix.why)
        self.assertFalse(choix.forced)

    def test_la_continuite_ne_garde_pas_un_compte_retenu(self):
        """B1 de la R1 auditeur : `account_turn.choose` décide le PROCHAIN tour
        de la session — un compte retenu ne bénéficie plus de la continuité,
        même si la lecture locale le dit sain."""
        b = _dossier(os.path.join(self.tmp, "comptes", "codex-2"), creds="auth.json")
        items = self._items(b)
        now = float(int(time.time()))
        self._depassement(items, now)
        # runner B : deuxième configuration du même nom, lecture saine, session
        # en cours sur le compte retenu
        c = _dossier(os.path.join(self.tmp, "comptes", "codex-3"), creds="auth.json")
        _journal(c, week=(5, now + 6 * DAY))
        autres = self._items(c)
        choix = accounts.choose(self.db, self.cfg.host, "codex", autres,
                                self._book(), now=now + 1.0,
                                session_account="secondaire")
        self.assertFalse(choix.kept)
        self.assertEqual(choix.profile.name, "primaire", choix.why)

    def test_le_forcage_ne_contourne_pas_une_retenue_publiee_apres(self):
        """B1 : une retenue publiée APRÈS le forçage (par un autre runner) ne
        peut pas être contournée par le forçage — le compte retenu ne reçoit
        aucun tour, le forçage est levé pour un compte libre."""
        b = _dossier(os.path.join(self.tmp, "comptes", "codex-2"), creds="auth.json")
        items = self._items(b)
        now = float(int(time.time()))
        _journal(items[0].path, week=(34, now + 6 * DAY))
        _journal(items[1].path, week=(5, now + 6 * DAY))
        self.assertTrue(accounts.force(self.db, self.cfg.host, "codex", items,
                                       "secondaire", by="test"))
        # un autre runner lit le secondaire en dépassement et publie la retenue
        storage.of(self.db).accounts.hold(self.cfg.host, "codex", "secondaire",
                                          now + 600, "codex plan: in overage")
        choix = accounts.choose(self.db, self.cfg.host, "codex", items,
                                self._book(), now=now + 1.0)
        self.assertEqual(choix.profile.name, "primaire", choix.why)
        self.assertFalse(choix.forced)

    def test_bascule_refusee_si_la_retenue_apparait_apres_le_classement(self):
        """B2 : un classement sur un instantané périmé ne fait pas partir un
        tour — la retenue de la cible est revérifiée sous le verrou de la
        bascule, et la relecture après refus ne rend jamais la cible retenue."""
        b = _dossier(os.path.join(self.tmp, "comptes", "codex-2"), creds="auth.json")
        items = self._items(b)
        now = float(int(time.time()))
        _journal(items[0].path, week=(34, now + 6 * DAY))
        _journal(items[1].path, week=(5, now + 6 * DAY))
        store = storage.of(self.db).accounts
        store.init_active(self.cfg.host, "codex", "primaire")
        # la retenue est publiée après le classement : l'instantané lu en tête
        # de choose l'ignore, la transaction de bascule la voit
        store.hold(self.cfg.host, "codex", "secondaire", now + 600,
                   "codex plan: in overage")
        before = len(self._bascules())
        with mock.patch.object(accounts, "_held_for_overage",
                               side_effect=[set(), {"secondaire"}]):
            choix = accounts.choose(self.db, self.cfg.host, "codex", items,
                                    self._book(), now=now + 1.0, agent="runner-b")
        self.assertIsNone(choix.profile)          # aucun tour, aucune bascule
        self.assertIn("held for overage", choix.reason)
        self.assertEqual(len(self._bascules()), before)
        self.assertEqual(store.active(self.cfg.host, "codex")["account"], "primaire")

    def test_pas_de_tour_sur_le_compte_actif_retenu_entre_temps(self):
        """B1/B2, variante de l'auditeur : cible == compte actif lu en tête ;
        une retenue publiée entre-temps est relue avant de rendre le compte —
        la reprise place le tour sur un compte libre, jamais sur le retenu."""
        b = _dossier(os.path.join(self.tmp, "comptes", "codex-2"), creds="auth.json")
        items = self._items(b)
        now = float(int(time.time()))
        _journal(items[0].path, week=(34, now + 6 * DAY))
        _journal(items[1].path, week=(5, now + 6 * DAY))
        store = storage.of(self.db).accounts
        store.init_active(self.cfg.host, "codex", "secondaire")
        store.hold(self.cfg.host, "codex", "secondaire", now + 600,
                   "codex plan: in overage")
        with mock.patch.object(accounts, "_held_for_overage",
                               side_effect=[set(), {"secondaire"}]):
            choix = accounts.choose(self.db, self.cfg.host, "codex", items,
                                    self._book(), now=now + 1.0, agent="runner-b")
        self.assertEqual(choix.profile.name, "primaire", choix.why)
        self.assertEqual(choix.switched["to"], "primaire")
        self.assertEqual(store.active(self.cfg.host, "codex")["account"], "primaire")

    def test_le_forcage_est_leve_si_la_retenue_apparait_pendant_le_choix(self):
        """B1 de la R2 : le forçage n'est pas validé d'avance — une retenue
        publiée pendant la décision (après la lecture en tête) le fait lever
        pour un compte libre, comme si elle avait été lue en tête."""
        b = _dossier(os.path.join(self.tmp, "comptes", "codex-2"), creds="auth.json")
        items = self._items(b)
        now = float(int(time.time()))
        _journal(items[0].path, week=(34, now + 6 * DAY))
        _journal(items[1].path, week=(5, now + 6 * DAY))
        self.assertTrue(accounts.force(self.db, self.cfg.host, "codex", items,
                                       "secondaire", by="test"))
        store = storage.of(self.db).accounts
        with mock.patch.object(accounts, "_held_for_overage",
                               side_effect=[set(), {"secondaire"}]):
            choix = accounts.choose(self.db, self.cfg.host, "codex", items,
                                    self._book(), now=now + 1.0, agent="runner-b")
        self.assertEqual(choix.profile.name, "primaire", choix.why)
        self.assertFalse(choix.forced)
        self.assertFalse(store.active(self.cfg.host, "codex")["forced"])

    def test_la_continuite_ne_garde_pas_une_retenue_apparue_pendant_le_choix(self):
        """B1 de la R2 : la continuité est un retour qui lance un tour — une
        retenue publiée pendant la décision la rompt, le tour part sur un
        compte libre."""
        b = _dossier(os.path.join(self.tmp, "comptes", "codex-2"), creds="auth.json")
        items = self._items(b)
        now = float(int(time.time()))
        _journal(items[0].path, week=(34, now + 6 * DAY))
        _journal(items[1].path, week=(5, now + 6 * DAY))
        storage.of(self.db).accounts.init_active(self.cfg.host, "codex", "primaire")
        with mock.patch.object(accounts, "_held_for_overage",
                               side_effect=[set(), {"secondaire"}]):
            choix = accounts.choose(self.db, self.cfg.host, "codex", items,
                                    self._book(), now=now + 1.0, agent="runner-b",
                                    session_account="secondaire")
        self.assertFalse(choix.kept)
        self.assertEqual(choix.profile.name, "primaire", choix.why)

    def test_le_choix_critique_ne_rend_pas_une_retenue_apparue_pendant_le_choix(self):
        """B1 de la R2 : le choix propre d'un agent critique (`per_agent`) passe
        par le même point de validation — la retenue publiée pendant la
        décision écarte le compte du classement."""
        b = _dossier(os.path.join(self.tmp, "comptes", "codex-2"), creds="auth.json")
        items = self._items(b)
        now = float(int(time.time()))
        _journal(items[0].path, week=(34, now + 6 * DAY))
        _journal(items[1].path, week=(5, now + 6 * DAY))
        storage.of(self.db).accounts.init_active(self.cfg.host, "codex", "primaire")
        with mock.patch.object(accounts, "_held_for_overage",
                               side_effect=[set(), {"secondaire"}]):
            choix = accounts.choose(self.db, self.cfg.host, "codex", items,
                                    self._book(), now=now + 1.0, critical=True)
        self.assertTrue(choix.per_agent)
        self.assertEqual(choix.profile.name, "primaire", choix.why)

    def test_une_retenue_publiee_apres_la_bascule_survit_et_ecarte_le_compte(self):
        """B2 de la R2 : la retenue publiée par un autre runner APRÈS le commit
        de la bascule n'est pas effacée par un nettoyage hors verrou, et le
        choix ne rend jamais le compte retenu (reprise sur l'état frais)."""
        b = _dossier(os.path.join(self.tmp, "comptes", "codex-2"), creds="auth.json")
        items = self._items(b)
        now = float(int(time.time()))
        _journal(items[0].path, week=(34, now + 6 * DAY))
        _journal(items[1].path, week=(5, now + 6 * DAY))
        store = storage.of(self.db).accounts
        store.init_active(self.cfg.host, "codex", "primaire")
        vrai = type(store).switch_guarded

        def bascule_puis_retenue(pilote, host, harness, *, to, **kw):
            # le runner A quitte le secondaire en dépassement juste après le
            # commit de la bascule de B : sa retenue est publiée sous le verrou
            resultat = vrai(pilote, host, harness, to=to, **kw)
            if resultat and to == "secondaire":
                pilote.hold(host, harness, to, kw["now"] + 600,
                            "codex plan: in overage (runner A)")
            return resultat

        with mock.patch.object(type(store), "switch_guarded", bascule_puis_retenue):
            choix = accounts.choose(self.db, self.cfg.host, "codex", items,
                                    self._book(), now=now, agent="runner-b")
        self.assertEqual(choix.profile.name, "primaire", choix.why)
        retenues = store.holds(self.cfg.host, "codex")
        self.assertIn("secondaire", retenues)
        self.assertGreater(float(retenues["secondaire"]["until_ts"]), now)
        self.assertEqual(store.active(self.cfg.host, "codex")["account"], "primaire")

    def test_switch_guarded_refuse_une_cible_retenue(self):
        """Le verrou de la bascule : cible retenue ⇒ refus sans écriture ;
        retenue expirée ⇒ bascule, et retenue du compte quitté dans la MÊME
        transaction."""
        store = storage.of(self.db).accounts
        store.init_active(self.cfg.host, "codex", "primaire")
        maintenant = time.time()
        store.hold(self.cfg.host, "codex", "secondaire", maintenant + 600, "overage")
        self.assertFalse(store.switch_guarded(
            self.cfg.host, "codex", expected="primaire", to="secondaire",
            kind="bascule", reason="r", agent="t", now=maintenant))
        self.assertEqual(store.active(self.cfg.host, "codex")["account"], "primaire")
        self.assertEqual(self._bascules(), [])
        self.db.execute("UPDATE account_holds"
                        " SET until_ts = extract(epoch from now()) - 1")
        self.assertTrue(store.switch_guarded(
            self.cfg.host, "codex", expected="primaire", to="secondaire",
            kind="bascule", reason="r", agent="t", now=maintenant,
            hold_reason="primaire plan: in overage",
            hold_until=maintenant + 600))
        self.assertEqual(len(self._bascules()), 1)
        self.assertIn("primaire", store.holds(self.cfg.host, "codex"))
        # R2 auditeur, B2 : la retenue ÉCHUE de la cible est nettoyée dans la
        # transaction de bascule (jamais un DELETE après le commit)
        self.assertNotIn("secondaire", store.holds(self.cfg.host, "codex"))

    def test_la_pause_dit_la_retenue(self):
        """Tous les comptes retenus : la pause le dit, elle ne prétend pas
        qu'ils sont tous au seuil."""
        b = _dossier(os.path.join(self.tmp, "comptes", "codex-2"), creds="auth.json")
        items = self._items(b)
        now = float(int(time.time()))
        self._depassement(items, now)
        # le primaire passe lui aussi en dépassement (lecture du runner B) :
        # plus aucun compte plaçable, et les deux sont retenus
        _journal(items[0].path, short=(100, now + 2 * HOUR),
                 week=(47, now + 5 * DAY), credits=CREDITS)
        choix = accounts.choose(self.db, self.cfg.host, "codex", items,
                                self._book(), now=now + 1.0, agent="runner-b")
        self.assertIsNone(choix.profile)
        self.assertIn("held for overage", choix.reason)


class IdentiteComptesTest(_Base):
    """#1883, arbitrage pilote (option a) : l'identité d'un compte est le
    dossier résolu qu'il lit — deux configurations de l'hôte qui déclarent le
    même nom vers deux dossiers ne servent pas de preuve l'une à l'autre. Le
    compte est refusé au placement et signalé à l'owner, au lieu de prendre la
    lecture d'un autre compte pour une lecture saine (incident du 16/10)."""

    def setUp(self) -> None:
        super().setUp()
        reserve = mock.patch.object(accounts, "RESERVE", 0.0)
        reserve.start()
        self.addCleanup(reserve.stop)

    def _book(self):
        return cost.CostBook(state_dir=self.cfg.state_dir, db=self.db)

    def _declares(self, secondaire: str):
        a = _dossier(os.path.join(self.tmp, "comptes", "codex-1"), creds="auth.json")
        self.declared = [
            {"name": "primaire", "path": a, "pace_multiplier": 10},
            {"name": "secondaire", "path": secondaire,
             "pace_multiplier": 10, "priority": 20},
        ]
        return accounts.parse({"codex": self.declared})["codex"]

    def test_deux_configurations_deux_dossiers_refusent_le_compte(self):
        b = _dossier(os.path.join(self.tmp, "comptes", "codex-2"), creds="auth.json")
        items_a = self._declares(b)
        now = float(int(time.time()))
        store = storage.of(self.db).accounts
        # le runner A publie son empreinte : aucune autre, aucun conflit
        self.assertEqual(accounts.identity_conflicts(store, self.cfg.host, "codex",
                                                     items_a, now), set())
        # le runner B déclare le MÊME nom vers un autre dossier (c), sain
        c = _dossier(os.path.join(self.tmp, "comptes", "codex-3"), creds="auth.json")
        _journal(items_a[0].path, week=(34, now + 6 * DAY))
        _journal(c, week=(5, now + 6 * DAY))
        items_b = self._declares(c)
        self.assertEqual(accounts.identity_conflicts(store, self.cfg.host, "codex",
                                                     items_b, now), {"secondaire"})
        choix = accounts.choose(self.db, self.cfg.host, "codex", items_b,
                                self._book(), now=now, agent="runner-b")
        self.assertEqual(choix.profile.name, "primaire", choix.why)
        # l'alerte owner dit le conflit, sans date inventée (identité stable)
        cfg = dataclasses.replace(self.cfg, accounts={"codex": self.declared})
        alertes = [a for a in exploitation.alerts(cfg, self.db, now=now)
                   if a["type"] == "account_identity"]
        self.assertEqual([(a["harness"], a["account"]) for a in alertes],
                         [("codex", "secondaire")])
        self.assertIsNone(alertes[0]["since"])
        self.assertEqual(alertes[0]["value"], 2.0)

    def test_le_meme_dossier_dans_deux_configurations_ne_conflit_pas(self):
        b = _dossier(os.path.join(self.tmp, "comptes", "codex-2"), creds="auth.json")
        now = float(int(time.time()))
        store = storage.of(self.db).accounts
        self.assertEqual(accounts.identity_conflicts(
            store, self.cfg.host, "codex", self._declares(b), now), set())
        # deuxième configuration : les mêmes dossiers, donc la même identité
        self.assertEqual(accounts.identity_conflicts(
            store, self.cfg.host, "codex", self._declares(b), now), set())

    def test_une_empreinte_perimee_ne_bloque_plus(self):
        """Une configuration divergente qui cesse de tourner cesse d'être un
        conflit après `IDENTITY_WINDOW_S` (sinon un profil retiré bloquerait le
        compte pour toujours)."""
        b = _dossier(os.path.join(self.tmp, "comptes", "codex-2"), creds="auth.json")
        items_a = self._declares(b)
        now = float(int(time.time()))
        store = storage.of(self.db).accounts
        accounts.identity_conflicts(store, self.cfg.host, "codex", items_a, now)
        c = _dossier(os.path.join(self.tmp, "comptes", "codex-3"), creds="auth.json")
        items_b = self._declares(c)
        self.assertEqual(accounts.identity_conflicts(store, self.cfg.host, "codex",
                                                     items_b, now), {"secondaire"})
        # l'empreinte divergente n'est plus republiée : elle vieillit
        self.db.execute(
            "UPDATE account_identities SET last_seen = now() - interval '2 hours'"
            " WHERE account = 'secondaire' AND identity <> %s",
            (accounts.identity_of(items_b[1]),))
        self.assertEqual(accounts.identity_conflicts(store, self.cfg.host, "codex",
                                                     items_b, now), set())

    def test_simulation_et_choix_critique_ne_publient_pas(self):
        """La publication d'empreinte est une écriture partagée : un choix
        simulé ou critique (L242b) se contente de relire."""
        b = _dossier(os.path.join(self.tmp, "comptes", "codex-2"), creds="auth.json")
        items = self._declares(b)
        now = float(int(time.time()))
        _journal(items[0].path, week=(34, now + 6 * DAY))
        _journal(items[1].path, week=(5, now + 6 * DAY))
        accounts.choose(self.db, self.cfg.host, "codex", items, self._book(),
                        now=now, critical=True)
        accounts.choose(self.db, self.cfg.host, "codex", items, self._book(),
                        now=now, simulate=True)
        vues = storage.of(self.db).accounts.identities(
            self.cfg.host, "codex", now - accounts.IDENTITY_WINDOW_S)
        self.assertEqual(vues, {})


class AlerteOverageTest(_Base):
    """L'alerte `account_overage` (attendu 4 du lot)."""

    def _cfg(self):
        a = _dossier(os.path.join(self.tmp, "comptes", "codex-1"), creds="auth.json")
        b = _dossier(os.path.join(self.tmp, "comptes", "codex-2"), creds="auth.json")
        self.declared = [
            {"name": "primaire", "path": a, "pace_multiplier": 10},
            {"name": "secondaire", "path": b, "pace_multiplier": 10, "priority": 20},
        ]
        return dataclasses.replace(self.cfg, accounts={"codex": self.declared})

    def _alertes(self, cfg, now):
        return [a for a in exploitation.alerts(cfg, self.db, now=now)
                if a["type"] == "account_overage"]

    def test_alerte_des_la_lecture_en_depassement(self):
        cfg = self._cfg()
        now = float(int(time.time()))
        _journal(self.declared[1]["path"], short=(100, now + 2 * HOUR),
                 week=(47, now + 5 * DAY), credits=CREDITS)
        _journal(self.declared[0]["path"], week=(34, now + 6 * DAY))
        alertes = self._alertes(cfg, now)
        self.assertEqual(len(alertes), 1)
        alerte = alertes[0]
        self.assertEqual(alerte["account"], "secondaire")
        self.assertEqual(alerte["harness"], "codex")
        self.assertEqual(alerte["host"], cfg.host)
        # `since` = le début de la fenêtre qui a débordé : stable tant qu'elle
        # court, l'alerte n'est pas renvoyée à chaque passage
        self.assertAlmostEqual(alerte["since"], now + 2 * HOUR - 300 * 60, places=3)
        self.assertAlmostEqual(alerte["value"], 1.0)
        self.assertEqual([a["since"] for a in self._alertes(cfg, now + 5.0)],
                         [alerte["since"]])

    def test_aucune_alerte_sans_depassement(self):
        cfg = self._cfg()
        now = float(int(time.time()))
        _journal(self.declared[1]["path"], short=(10, now + 2 * HOUR),
                 week=(47, now + 5 * DAY), credits=CREDITS)
        _journal(self.declared[0]["path"], week=(34, now + 6 * DAY))
        self.assertEqual(self._alertes(cfg, now), [])

    def test_depassement_sans_fenetre_datee_ne_renotifie_pas(self):
        """P2 de la R1 auditeur : sans fenêtre datée, `since` reste None —
        l'identité de l'alerte ne dépend pas de l'heure de passage (une date
        inventée, `now`, changeait la clé à chaque passe)."""
        cfg = self._cfg()
        now = float(int(time.time()))
        # la fenêtre courte est saturée mais sans remise à zéro connue
        _journal(self.declared[1]["path"], short=(100, None),
                 week=(47, now + 5 * DAY), credits=CREDITS)
        _journal(self.declared[0]["path"], week=(34, now + 6 * DAY))
        premiere = self._alertes(cfg, now)
        self.assertEqual(len(premiere), 1)
        self.assertIsNone(premiere[0]["since"])
        seconde = self._alertes(cfg, now + 5.0)
        self.assertEqual(len(seconde), 1)
        self.assertIsNone(seconde[0]["since"])
        self.assertEqual(notify.key_of(premiere[0]), notify.key_of(seconde[0]))


if __name__ == "__main__":
    unittest.main()
