# SPDX-License-Identifier: AGPL-3.0-only
"""#5542 — la lecture par fenêtre reste un seek par jauge, pas un tri total.

#5466 garde une ligne par jauge en classant `resets_at` d'abord (une échéance
plus tardive prouve que la fenêtre précédente est close), puis la production
et l'insertion. #3922 avait remplacé ce motif par une marche récursive qui
saute chaque groupe entier ; le témoin vérifie que la marche rend exactement
les lignes de l'ancien `DISTINCT ON`, y compris sur les comptes NULL/vides,
les échéances NULL et les égalités parfaites.
"""
from __future__ import annotations

import unittest

from ameesh import storage

from .test_1132_jauges_perimees import _JaugesBase

FIELDS = ("harness, gauge_key AS key, used, "
          "extract(epoch from resets_at)::float8 AS resets_at_ts, window_s, "
          "extract(epoch from observed_at)::float8 AS observed_ts, "
          "extract(epoch from produced_at)::float8 AS produced_ts, "
          "source, account, overage_capable, overage_in_use")

DISTINCT_ON = (
    "SELECT DISTINCT ON (gauge_key) " + FIELDS +
    " FROM quota_gauge_readings WHERE harness = %s"
    "   AND (account IS NULL) IS FALSE AND coalesce(account, '') = %s"
    " ORDER BY gauge_key, resets_at DESC NULLS LAST,"
    "          produced_at DESC NULLS LAST, observed_at DESC, id DESC")


class FenetreSeekTest(_JaugesBase):
    def test_rend_les_memes_lignes_que_le_distinct_on(self):
        self.db.execute("""
            INSERT INTO quota_gauge_readings
                (harness, account, gauge_key, used, window_s, resets_at,
                 observed_at, produced_at, source, overage_capable,
                 overage_in_use)
            SELECT h, a, g, n::float8, 3600,
                   CASE WHEN n = 3 THEN NULL
                        ELSE now() + make_interval(days => n % 3) END,
                   now() - make_interval(secs => n * 60),
                   CASE WHEN n = 4 THEN NULL
                        ELSE now() - make_interval(secs => (n % 2) * 60) END,
                   s, true, n % 2 = 0
            FROM unnest(ARRAY['claude', 'codex']) h
            CROSS JOIN unnest(ARRAY[NULL::text, '', 'a', 'z']) a
            CROSS JOIN unnest(ARRAY['five_hour', 'seven_day']) g
            CROSS JOIN generate_series(1, 4) n
            CROSS JOIN unnest(ARRAY[NULL::text, '', 'host-a', 'host-z']) s
        """)
        # Une égalité parfaite de toutes les colonnes de classement : seul
        # l'identifiant d'insertion départage, comme le DISTINCT ON.
        self.db.execute("""
            INSERT INTO quota_gauge_readings
                (harness, account, gauge_key, used, window_s, resets_at,
                 observed_at, produced_at, source)
            VALUES ('codex', 'a', 'five_hour', 0.90, 3600, now() + interval '9 days',
                    now() - interval '30 seconds', now() - interval '30 seconds',
                    'host-a'),
                   ('codex', 'a', 'five_hour', 0.91, 3600, now() + interval '9 days',
                    now() - interval '30 seconds', now() - interval '30 seconds',
                    'host-z')
        """)
        ops = storage.of(self.db).operations
        for harness in ('claude', 'codex', 'missing'):
            for account in ('', 'a', 'z', 'missing'):
                with self.subTest(harness=harness, account=account):
                    expected = self.db.query(DISTINCT_ON, (harness, account))
                    self.assertEqual(
                        ops.gauge_latest_window(harness=harness, account=account),
                        expected)

    def test_l_echeance_plus_tardive_gagne_meme_inseree_avant(self):
        """#5466 : le tri par fenêtre reste l'invariant de la marche."""
        self.db.execute("""
            INSERT INTO quota_gauge_readings
                (harness, account, gauge_key, used, window_s, resets_at,
                 observed_at, produced_at, source)
            VALUES ('codex', 'primaire', 'codex-10080min', 0.01, 604800,
                    now() + interval '7 days', now() - interval '2 hours',
                    now() - interval '2 hours', 'ameesh-agents-2'),
                   ('codex', 'primaire', 'codex-10080min', 0.81, 604800,
                    now() + interval '2 days', now(),
                    now() - interval '60 seconds', 'ameesh-agents-1')
        """)
        rows = storage.of(self.db).operations.gauge_latest_window(
            harness='codex', account='primaire')
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]['used'], 0.01)
        self.assertEqual(rows[0]['source'], 'ameesh-agents-2')

    def test_un_compte_non_nomme_ou_absent_ne_rend_rien(self):
        ops = storage.of(self.db).operations
        self.assertEqual(ops.gauge_latest_window(harness=None, account='a'), [])
        self.assertEqual(ops.gauge_latest_window(harness='codex', account=None), [])


if __name__ == "__main__":
    unittest.main()
