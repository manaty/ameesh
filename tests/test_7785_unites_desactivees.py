# SPDX-License-Identifier: AGPL-3.0-only
"""#7785 : une unité runner désactivée volontairement n'est pas un KO de santé.

Le nom vient de l'hôte (systemd), pas d'une supposition sur le silence : la
santé le reçoit en `--unite-desactivee` et rend le courrier de cet agent en
avertissement, avec le geste de retrait définitif. Sans ce nom, la règle
stricte de #2899 reste entière : un agent encore déclaré sans exécuteur et
avec du courrier est un KO.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest import mock

from . import support  # isole l'environnement de l'hôte

ROOT = Path(__file__).resolve().parents[1]
HEALTH = ROOT / 'deploy/mise-a-jour/sante-courrier.py'


class UnitesDesactiveesSanteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('sante_7785', HEALTH)
        cls.health = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.health)

    def rows(self):
        return [dict(name='agent-eteint', host='vm', mode='execute', status='idle',
                     retired_ts=None, lease_owner=None, lease_expires_ts=None),
                dict(name='agent-actif', host='vm', mode='execute', status='idle',
                     retired_ts=None, lease_owner='runner', lease_expires_ts=1100.0)]

    def unread(self, name):
        return [{'id': 1, 'created_ts': 900.0}] if name == 'agent-eteint' else []

    def run_main(self, rows=None, unread=None, get=None, extra=()):
        h = self.health
        rows = self.rows() if rows is None else rows
        par_nom = {row['name']: row for row in rows}
        if get is None:
            get = lambda _db, name: par_nom.get(name)  # noqa: E731
        lire = self.unread if unread is None else unread

        def unread_active(_db, name):
            return lire(name)

        output = io.StringIO()
        with mock.patch.object(h.config, 'load', return_value=object()), \
             mock.patch.object(h.db_mod, 'connect', return_value=mock.Mock()), \
             mock.patch.object(h.registry, 'overview', return_value=rows), \
             mock.patch.object(h.registry, 'get', side_effect=get), \
             mock.patch.object(h.mail, 'unread_active', side_effect=unread_active), \
             mock.patch.object(h.mail, 'maybe_redelivered', return_value=False), \
             mock.patch.object(h, 'dead_letters', return_value=[]), \
             mock.patch.object(h, 'canal_humain', return_value=[]), \
             mock.patch.object(h.time, 'time', return_value=1000.0), \
             mock.patch.object(h, 'probe', side_effect=lambda _db, row, *_a,
                               **_k: {'destinataire': row['name'],
                                      'delai_s': 0.1}), \
             contextlib.redirect_stdout(output):
            code = h.main(['--hote', 'vm', '--json', '--souffrance', '10',
                           '--attente', '0', *extra])
        return code, json.loads(output.getvalue())

    def test_unite_desactivee_ne_fait_pas_echouer_la_sante(self):
        code, report = self.run_main(extra=['--unite-desactivee', 'agent-eteint'])
        self.assertEqual(code, 0)
        self.assertEqual(report['verdict'], 'ok')
        self.assertEqual(report['sans_executeur'], [])
        self.assertEqual([c['destinataire'] for c in report['sans_executeur_desactive']],
                         ['agent-eteint'])
        self.assertIn('ameesh agent retire agent-eteint',
                      report['sans_executeur_desactive'][0]['motif'])
        # la persona éteinte n'est pas une cible de sonde, l'active reçoit la
        # sonde : le nom toléré ne sert jamais à masquer un vrai tour.
        self.assertEqual(report['sonde']['destinataire'], 'agent-actif')

    def test_sans_la_liste_le_meme_courrier_reste_ko(self):
        code, report = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(report['verdict'], 'ko')
        self.assertEqual([c['destinataire'] for c in report['sans_executeur']],
                         ['agent-eteint'])
        self.assertEqual(report['sans_executeur_desactive'], [])

    def test_un_autre_nom_dans_la_liste_ne_tolere_rien(self):
        code, report = self.run_main(extra=['--unite-desactivee', 'agent-absent'])
        self.assertEqual(code, 1)
        self.assertEqual(report['verdict'], 'ko')
        self.assertEqual(report['sans_executeur_desactive'], [])

    def test_retrait_entre_temps_efface_l_avertissement(self):
        rows = self.rows()
        rows[0] = dict(rows[0], retired_ts=999.0)
        code, report = self.run_main(
            rows=rows,
            get=lambda _db, name: rows[0] if name == 'agent-eteint' else rows[1],
            extra=['--unite-desactivee', 'agent-eteint'])
        self.assertEqual(code, 0)
        self.assertEqual(report['verdict'], 'ok')
        self.assertEqual(report['sans_executeur_desactive'], [])

    def test_bail_vivant_d_une_unite_desactivee_n_est_pas_une_souffrance(self):
        rows = self.rows()
        rows[0] = dict(rows[0], lease_owner='ancien-runner',
                       lease_expires_ts=1100.0)
        code, report = self.run_main(rows=rows,
                                     extra=['--unite-desactivee', 'agent-eteint'])
        self.assertEqual(code, 0)
        self.assertEqual(report['verdict'], 'ok')
        self.assertEqual(report['souffrance'], [])
        self.assertEqual([c['destinataire'] for c in report['sans_executeur_desactive']],
                         ['agent-eteint'])
        # sans le nom, le même bail vivant et le même courrier sont un KO.
        code, report = self.run_main(rows=rows)
        self.assertEqual(code, 1)
        self.assertEqual([c['destinataire'] for c in report['souffrance']],
                         ['agent-eteint'])

    def test_cible_de_sonde_desactivee_est_non_concluante(self):
        code, report = self.run_main(
            extra=['--unite-desactivee', 'agent-eteint', '--sonde', 'agent-eteint'])
        self.assertEqual(code, 0)
        self.assertEqual(report['verdict'], 'sans-sonde')
        self.assertIsNone(report['sonde'])
        self.assertIn('unite desactivee', report['pourquoi'])


if __name__ == '__main__':
    unittest.main()
