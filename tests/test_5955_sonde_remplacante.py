# SPDX-License-Identifier: AGPL-3.0-only
"""#5955 : la remplacante de la sonde de courrier est la moins chargee.

Staging 1.39.0 : cible nommee en sommeil automatique (#5312), remplacante
prise dans l'ordre du registre avec 101 non-lus ; le tour de sonde lit tout
ce retard et depasse la borne de 300 s (non concluant).
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest import mock

DEPLOY = Path(__file__).resolve().parents[1] / 'deploy' / 'mise-a-jour'
SOMMEIL = 'roadmap: épuisée ameesh (#3256 §2.3)'


class SondeRemplacanteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            'sante_courrier_5955', DEPLOY / 'sante-courrier.py')
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    def rangs(self):
        mene = dict(mode='execute', host='ameesh-agents-1', lease_owner='r',
                    lease_expires_ts=1900.0, status='idle')
        return [dict(mene, name='impl-02', operator_paused=True,
                     operator_pause_reason=SOMMEIL),
                dict(mene, name='impl-03'),
                dict(mene, name='impl-04'),
                dict(mene, name='impl-05')]

    def selection(self, rows, non_lus):
        etats = {r['name']: r for r in rows}
        with mock.patch.object(self.mod.registry, 'get',
                               side_effect=lambda db, nom: etats[nom]), \
             mock.patch.object(self.mod.mail, 'unread_active',
                               side_effect=lambda db, nom: [{}] * non_lus[nom]), \
             mock.patch.object(self.mod.time, 'sleep') as dormir, \
             mock.patch.object(self.mod.time, 'time', return_value=1000.0):
            resultat = self.mod.selection(object(), rows, 'impl-02', 300.0, 1000.0)
        dormir.assert_not_called()
        return resultat

    def test_remplacante_la_moins_chargee(self):
        """Le cas du staging : impl-03 (101 non-lus) n'est plus prise."""
        cible, pourquoi, remplacement = self.selection(
            self.rangs(), {'impl-02': 0, 'impl-03': 101, 'impl-04': 3, 'impl-05': 0})
        self.assertEqual(cible['name'], 'impl-05')
        self.assertEqual(remplacement['remplacante'], 'impl-05')
        self.assertEqual(remplacement['non_lus'], 0)
        self.assertIn('sommeil automatique', remplacement['raison'])
        self.assertIn('remplacante impl-05', pourquoi)

    def test_charges_egales_gardent_l_ordre_du_registre(self):
        cible, _, remplacement = self.selection(
            self.rangs(), {'impl-02': 0, 'impl-03': 2, 'impl-04': 2, 'impl-05': 2})
        self.assertEqual(cible['name'], 'impl-03')
        self.assertEqual(remplacement['non_lus'], 2)

    def test_la_moins_chargee_occupee_cede_a_la_suivante(self):
        rows = self.rangs()
        rows[3] = dict(rows[3], status='running')
        cible, _, remplacement = self.selection(
            rows, {'impl-02': 0, 'impl-03': 101, 'impl-04': 3, 'impl-05': 0})
        self.assertEqual(cible['name'], 'impl-04')
        self.assertEqual(remplacement['non_lus'], 3)

    def test_seule_remplacante_chargee_reste_prise_et_nommee(self):
        """Pas d'autre libre : la chargee est sondee, son retard nomme."""
        rows = self.rangs()[:2]
        cible, _, remplacement = self.selection(rows, {'impl-02': 0, 'impl-03': 101})
        self.assertEqual(cible['name'], 'impl-03')
        self.assertEqual(remplacement['non_lus'], 101)

    def test_charge_illisible_va_en_dernier_sans_ecarter(self):
        def lecture(db, nom):
            if nom == 'impl-03':
                raise RuntimeError('base')
            return [{}] * 5
        rows = self.rangs()[:3]
        with mock.patch.object(self.mod.mail, 'unread_active', side_effect=lecture):
            ordre = [r['name'] for r in self.mod.par_charge(object(), rows[1:])]
            self.assertIsNone(self.mod.charge(object(), 'impl-03'))
        self.assertEqual(ordre, ['impl-04', 'impl-03'])

    def test_candidat_unique_sans_lecture(self):
        with mock.patch.object(self.mod.mail, 'unread_active') as lecture:
            self.assertEqual(self.mod.par_charge(object(), self.rangs()[1:2]),
                             self.rangs()[1:2])
        lecture.assert_not_called()


if __name__ == '__main__':
    unittest.main()
