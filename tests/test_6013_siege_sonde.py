# SPDX-License-Identifier: AGPL-3.0-only
"""#6013 : le siege de sonde leve le sommeil automatique, pour la sonde seule.

Staging 1.40.0 (AG3, 06/11) : toutes les personas menees dormaient du sommeil
de la feuille de route epuisee ; la sonde est sortie sans cible, attestation
partielle chaque nuit. Le siege leve ce seul sommeil, le temps du tour de
sonde, puis le repose ; pause budget et pause humaine restent intouchees.
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

DEPLOY = Path(__file__).resolve().parents[1] / 'deploy' / 'mise-a-jour'
SOMMEIL = 'roadmap: épuisée ameesh (#3256 §2.3)'
MAINTENANT = 1000.0


class Monde:
    """Registre, courrier et executeur factices : chaque `time.sleep` est une
    passe de l'executeur (remise si la pause est levee, puis accuse et fin)."""

    def __init__(self, rows, *, repond=True, lots=0):
        self.etats = {r['name']: dict(r) for r in rows}
        self.gestes = []
        self.message = None
        self.repond = repond
        self.lots = lots
        self.t = 0.0

    def get(self, db, nom):
        return dict(self.etats[nom]) if nom in self.etats else None

    def change(self, db, nom, *, paused, reason, actor, principal,
               expect_reason=None, until=None, **kw):
        etat = self.etats[nom]
        self.gestes.append(dict(nom=nom, paused=paused, reason=reason, actor=actor,
                                principal=principal, expect=expect_reason,
                                until=until))
        if not paused:
            if not etat.get('operator_paused') or (
                    expect_reason is not None
                    and etat.get('operator_pause_reason') != expect_reason):
                return {'changed': False, 'mode': 'execute'}
            etat.update(operator_paused=False, operator_pause_reason=None)
        else:
            etat.update(operator_paused=True, operator_pause_reason=reason)
        return {'changed': True, 'mode': 'execute'}

    def send(self, db, sender, recipient, body, **kw):
        self.message = dict(id=7, recipient=recipient, delivered_ts=None,
                            jeton=body.split('--ack ')[1].split()[0])
        return 7

    def mail_get(self, db, mid):
        return dict(self.message)

    def history(self, db, nom, **kw):
        if self.message and self.message.get('repondu'):
            return [{'sender': nom, 'body': '--ack %s' % self.message['jeton']}]
        return []

    def dormir(self, d):
        self.t += d
        m = self.message
        if m is None:
            return
        etat = self.etats[m['recipient']]
        if m['delivered_ts'] is None:
            if not etat.get('operator_paused'):
                m['delivered_ts'] = 1001.0
                etat['status'] = 'running'
        elif self.repond and not m.get('repondu'):
            m['repondu'] = True
            etat.update(status='idle', last_turn_ts=1002.0)

    def listing(self):
        return [dict(r, actionable_lots=self.lots, actionable_lot_id=42 if self.lots else None)
                for r in self.etats.values()]

    def patches(self, mod):
        return [mock.patch.object(mod.registry, 'get', side_effect=self.get),
                mock.patch.object(mod.agent_pause, 'change', side_effect=self.change),
                mock.patch.object(mod.mail, 'send', side_effect=self.send),
                mock.patch.object(mod.mail, 'get', side_effect=self.mail_get),
                mock.patch.object(mod.mail, 'history', side_effect=self.history),
                mock.patch.object(mod.mail, 'unread_active',
                                  side_effect=lambda db, nom, **kw: []),
                mock.patch.object(mod.work, 'open_lots', return_value=[]),
                mock.patch.object(mod.storage, 'of', return_value=SimpleNamespace(
                    operations=SimpleNamespace(listing=self.listing))),
                mock.patch.object(mod.time, 'time', return_value=MAINTENANT),
                mock.patch.object(mod.time, 'monotonic', side_effect=lambda: self.t),
                mock.patch.object(mod.time, 'sleep', side_effect=self.dormir)]


class SiegeSondeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            'sante_courrier_6013', DEPLOY / 'sante-courrier.py')
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    def rangs(self, **pause):
        mene = dict(mode='execute', host='ameesh-agents-3', lease_owner='r',
                    lease_expires_ts=MAINTENANT + 900, status='idle',
                    operator_paused=True, operator_pause_reason=SOMMEIL,
                    operator_pause_by='ameesh:file-decoupage',
                    operator_pause_for='human:smichea',
                    operator_pause_until_ts=MAINTENANT + 3600)
        mene.update(pause)
        return [dict(mene, name='impl-13'), dict(mene, name='impl-18')]

    def lancer(self, monde, rows, sonde='auto', attente=300.0):
        with _ensemble(monde.patches(self.mod)):
            cible, pourquoi, remplacement = self.mod.selection(
                object(), rows, sonde, 300.0, MAINTENANT)
            resultat = (self.mod.probe(object(), cible, 'abc', attente,
                                       'ameesh-agents-3', alternatives=rows)
                        if cible is not None else None)
        return cible, pourquoi, remplacement, resultat

    def test_hote_endormi_sonde_concluante_et_sommeil_repose(self):
        monde = Monde(self.rangs())
        cible, pourquoi, remplacement, resultat = self.lancer(monde, self.rangs())
        self.assertEqual(cible['name'], 'impl-13')
        self.assertIn('siege de sonde : impl-13', pourquoi)
        self.assertIsNone(remplacement)
        self.assertEqual(resultat['tour'], 'ok')
        self.assertIsNotNone(resultat['delai_s'])
        siege = resultat['siege']
        self.assertTrue(siege['leve'])
        self.assertTrue(siege['retabli'])
        self.assertEqual(siege['sommeil'], SOMMEIL)
        self.assertNotIn('principal', siege)
        levee, pose = monde.gestes
        self.assertFalse(levee['paused'])
        self.assertEqual(levee['expect'], SOMMEIL)
        self.assertEqual(levee['actor'], 'ameesh:file-decoupage')
        self.assertEqual(levee['principal'], 'human:smichea')
        # pilote 01:40Z : la raison nomme la sonde, l'hote et le message
        self.assertIn('sonde ameesh-agents-3 7 ', levee['reason'])
        self.assertTrue(levee['reason'].startswith('automatic sleep lifted:'))
        self.assertTrue(pose['paused'])
        self.assertEqual(pose['reason'], SOMMEIL)
        self.assertEqual(pose['until'], MAINTENANT + 3600)
        # le sommeil revient des le debut du tour, avant l'accuse
        self.assertTrue(monde.etats['impl-13']['operator_paused'])
        self.assertEqual(monde.etats['impl-13']['operator_pause_reason'], SOMMEIL)
        self.assertTrue(monde.etats['impl-18']['operator_paused'])

    def test_sommeil_repose_des_la_remise(self):
        """Le tour en cours n'est pas encore fini que la pause est revenue."""
        monde = Monde(self.rangs(), repond=False)
        _, _, _, resultat = self.lancer(monde, self.rangs(), attente=10.0)
        self.assertEqual(len(monde.gestes), 2)
        self.assertTrue(resultat['siege']['retabli'])
        self.assertIsNotNone(resultat['remise_s'])
        self.assertEqual(resultat['tour'], 'remis, sans reponse de l\'outil')

    def test_cible_nommee_endormie_tient_le_siege(self):
        monde = Monde(self.rangs())
        cible, pourquoi, remplacement, resultat = self.lancer(
            monde, self.rangs(), sonde='impl-18')
        self.assertEqual(cible['name'], 'impl-18')
        self.assertIsNone(remplacement)
        self.assertIn('sommeil automatique', pourquoi)
        self.assertEqual(resultat['tour'], 'ok')
        self.assertEqual(monde.t, 2.0 * 2)  # aucune attente de la borne nommee

    def test_siege_a_la_moins_chargee(self):
        monde = Monde(self.rangs())
        charges = {'impl-13': 40, 'impl-18': 1}
        with _ensemble(monde.patches(self.mod)), \
                mock.patch.object(self.mod.mail, 'unread_active',
                                  side_effect=lambda db, nom, **kw: [{}] * charges[nom]):
            cible, _, _ = self.mod.selection(object(), self.rangs(), 'auto', 300.0,
                                             MAINTENANT)
        self.assertEqual(cible['name'], 'impl-18')

    def test_pause_budget_reste_non_concluante_sans_siege(self):
        """Pause budget : aucune levee, sans-sonde comme avant #6013."""
        rows = self.rangs(operator_paused=False, operator_pause_reason=None,
                          status='blocked', status_text='budget : compte en pause')
        monde = Monde(rows)
        cible, pourquoi, _, resultat = self.lancer(monde, rows)
        self.assertIsNone(cible)
        self.assertIsNone(resultat)
        self.assertNotIn('siege', pourquoi)
        self.assertEqual(monde.gestes, [])

    def test_sommeil_et_attente_budget_pas_de_siege(self):
        rows = self.rangs(status_text='budget : compte en pause')
        monde = Monde(rows)
        cible, _, _, _ = self.lancer(monde, rows)
        self.assertIsNone(cible)
        self.assertEqual(monde.gestes, [])

    def test_pause_humaine_jamais_levee(self):
        rows = self.rangs(operator_pause_reason='maintenance du 06/11',
                          operator_pause_by='human:smichea')
        monde = Monde(rows)
        cible, _, _, _ = self.lancer(monde, rows)
        self.assertIsNone(cible)
        self.assertEqual(monde.gestes, [])

    def test_levee_refusee_non_concluante_sans_pose(self):
        monde = Monde(self.rangs())
        rows = self.rangs()
        with _ensemble(monde.patches(self.mod)):
            cible, _, _ = self.mod.selection(object(), rows, 'auto', 300.0, MAINTENANT)
            # une pause humaine remplace le sommeil entre le choix et la levee
            monde.etats['impl-13']['operator_pause_reason'] = 'maintenance'
            resultat = self.mod.probe(object(), cible, 'abc', 300.0, 'ameesh-agents-3')
        self.assertFalse(resultat['concluant'])
        self.assertIn('siege', resultat['tour'])
        self.assertFalse(resultat['siege']['leve'])
        self.assertIsNone(resultat['siege']['retabli'])
        self.assertEqual(monde.gestes, [])

    def test_lot_actionnable_garde_l_agent_eveille(self):
        monde = Monde(self.rangs(), lots=1)
        _, _, _, resultat = self.lancer(monde, self.rangs())
        self.assertEqual(resultat['tour'], 'ok')
        self.assertFalse(resultat['siege']['retabli'])
        self.assertIn('#42', resultat['siege']['retablissement'])
        self.assertEqual(len(monde.gestes), 1)

    def test_tour_en_echec_sommeil_repose(self):
        """Une erreur pendant la sonde : le sommeil est repose (finally)."""
        monde = Monde(self.rangs())
        rows = self.rangs()
        with _ensemble(monde.patches(self.mod)), \
                mock.patch.object(self.mod.mail, 'get', side_effect=RuntimeError('base')):
            cible, _, _ = self.mod.selection(object(), rows, 'auto', 300.0, MAINTENANT)
            with self.assertRaises(RuntimeError):
                self.mod.probe(object(), cible, 'abc', 300.0, 'ameesh-agents-3')
        levee, pose = monde.gestes
        self.assertFalse(levee['paused'])
        self.assertTrue(pose['paused'])
        self.assertEqual(monde.etats['impl-13']['operator_pause_reason'], SOMMEIL)

    def test_non_remise_sommeil_repose_a_la_fin(self):
        """Executeur absent : la borne passe, le sommeil est repose quand meme."""
        monde = Monde(self.rangs())
        monde.dormir = lambda d: setattr(monde, 't', monde.t + d)
        _, _, _, resultat = self.lancer(monde, self.rangs(), attente=10.0)
        self.assertIsNone(resultat['remise_s'])
        self.assertTrue(resultat['siege']['retabli'])
        self.assertTrue(monde.etats['impl-13']['operator_paused'])

    def test_sommeil_repose_par_la_passe_avant_le_tour_est_releve(self):
        monde = Monde(self.rangs())
        dormir = monde.dormir
        reposes = []

        def passe(d):
            if not reposes and monde.message and not monde.etats['impl-13'].get(
                    'operator_paused'):
                reposes.append(1)
                monde.etats['impl-13'].update(operator_paused=True,
                                              operator_pause_reason=SOMMEIL)
                monde.t += d
                return
            dormir(d)
        monde.dormir = passe
        _, _, _, resultat = self.lancer(monde, self.rangs())
        self.assertEqual(resultat['tour'], 'ok')
        self.assertEqual(resultat['siege']['levees'], 2)
        self.assertTrue(resultat['siege']['retabli'])

    def test_siege_force_malgre_une_persona_libre(self):
        # #6106 : le temoin exerce le siege meme si une persona est libre
        rows = self.rangs()
        rows[1].update(operator_paused=False, operator_pause_reason=None)
        monde = Monde(rows)
        cible, pourquoi, remplacement, resultat = self.lancer(monde, rows, sonde='siege')
        self.assertEqual(cible['name'], 'impl-13')
        self.assertEqual(pourquoi, 'siege de sonde force : impl-13')
        self.assertIsNone(remplacement)
        self.assertEqual(resultat['tour'], 'ok')
        self.assertTrue(resultat['siege']['leve'])
        self.assertTrue(resultat['siege']['retabli'])
        self.assertEqual([g['paused'] for g in monde.gestes], [False, True])

    def test_siege_force_sans_dormeur_sans_sonde(self):
        rows = self.rangs(operator_paused=False, operator_pause_reason=None)
        monde = Monde(rows)
        cible, pourquoi, _, resultat = self.lancer(monde, rows, sonde='siege')
        self.assertIsNone(cible)
        self.assertIsNone(resultat)
        self.assertIn('aucune persona menee en sommeil automatique', pourquoi)
        self.assertEqual(monde.gestes, [])

    def test_siege_force_pause_humaine_jamais_levee(self):
        rows = self.rangs(operator_pause_reason='maintenance disque')
        monde = Monde(rows)
        cible, _, _, _ = self.lancer(monde, rows, sonde='siege')
        self.assertIsNone(cible)
        self.assertEqual(monde.gestes, [])

    def test_hote_libre_inchange(self):
        """Une persona libre : pas de siege, aucune pause touchee."""
        rows = self.rangs()
        rows[1] = dict(rows[1], operator_paused=False, operator_pause_reason=None)
        monde = Monde(rows)
        cible, pourquoi, _, resultat = self.lancer(monde, rows)
        self.assertEqual(cible['name'], 'impl-18')
        self.assertIsNone(pourquoi)
        self.assertNotIn('siege', resultat)
        self.assertEqual(monde.gestes, [])


class _ensemble:
    def __init__(self, patches):
        self.patches = patches

    def __enter__(self):
        for p in self.patches:
            p.start()

    def __exit__(self, *exc):
        for p in reversed(self.patches):
            p.stop()


if __name__ == '__main__':
    unittest.main()
