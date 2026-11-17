# SPDX-License-Identifier: AGPL-3.0-only
"""#7712 : une origine de session étrangère ne bloque plus la vague Claude.

L'auditeur revenu de Codex vers Claude proposait une bascule depuis un compte
« primaire » Codex qui contredisait sa dernière bascule effective ; le
planificateur refusait, `coordinate` rendait « account wave changed while
planning » à chaque tour, et l'alerte lisait une pause budget alors qu'aucun
plafond n'était en cause.

Témoins : (a) une session d'un autre harnais ne fournit aucune origine ;
(b) une bascule effective déjà arrivée à la destination proposée fait partir
le tour sur ce compte ; (c) un vrai changement concurrent refuse au plus un
tour, puis passe. Le refus répété est aussi nommé par son alerte (d).
"""
from __future__ import annotations

import os
import types
import unittest
from unittest import mock

from ameesh import account_rotation, account_turn, accounts, cost, exploitation
from ameesh import registry, storage

from .test_l30_accounts import _Base, _dossier

NOW = 1_800_000_000.0


class _WaveBase(_Base):
    """Le banc de #7441, rejoué avec les noms de l'incident."""

    def setUp(self):
        super().setUp()
        self.items = accounts.parse({'claude': [
            {'name': name, 'type': 'config_dir',
             'path': _dossier(os.path.join(self.tmp, name), creds='.credentials.json')}
            for name in ('primaire', 'secondaire', 'tertiaire')]})['claude']
        self.store = storage.of(self.db).accounts
        self.book = cost.CostBook(state_dir=self.cfg.state_dir, db=self.db)
        self.used = {'primaire': .9, 'secondaire': .2, 'tertiaire': .1}
        self.hard = set()
        self.problems = set()
        self.patch = mock.patch.object(accounts, 'evaluate', side_effect=self.evaluate)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        claim = mock.patch('ameesh.account_claims.mark_refused',
                           side_effect=lambda cfg, items, **kw: items)
        claim.start()
        self.addCleanup(claim.stop)

    def evaluate(self, book, profile, items, now, *args, **kwargs):
        used = self.used[profile.name]
        return accounts.Evaluation(
            profile, reason='pace threshold' if used >= .5 else '',
            gauges=[cost.Gauge('claude', 'seven_day', used, now + 86400, 604800,
                               read_at=now, produced_at=now)],
            hard=profile.name in self.hard,
            problems=['invalid profile'] if profile.name in self.problems else [],
            limits={'seven_day': (.5, 'pace cap')})

    def effective(self, from_account='primaire', to_account='secondaire',
                  name='auditeur', age=7200):
        """Une bascule effective, datée : 2 h suffisent à faire tomber le dwell."""
        self.store.record_agent_switch('host-a', 'claude', from_account=from_account,
                                       to_account=to_account, reason='previous switch',
                                       agent=name)
        row = self.store.last_agent_switch('claude', name)
        self.db.execute('UPDATE account_switches SET at = to_timestamp(%s) WHERE id = %s',
                        (NOW - age, row['id']))
        return row['id']

    def choose(self, name='auditeur', session='primaire'):
        return accounts.choose(self.db, self.cfg.host, 'claude', self.items, self.book,
                               now=NOW, agent=name, critical=True,
                               session_account=session)

    def worker(self, name='auditeur', session='primaire', harness='claude',
               session_harness='claude'):
        state = {'session_harness': session_harness}

        def read(key):
            return state.get(key, '')

        def note(key, value):
            state[key] = value

        return types.SimpleNamespace(
            cfg=self.cfg, db=self.db, name=name,
            agent={'harness': harness, 'session_account': session},
            runner=types.SimpleNamespace(is_critical=lambda n, row=None: True),
            current_session=lambda: 'session-1',
            _compte_session=session,
            _fil_note=mock.Mock(),
            _state_read=read,
            _state_note=note)

    def journal(self, kind):
        return self.db.query('SELECT * FROM account_switches WHERE kind = %s ORDER BY id',
                             (kind,))


class OriginHarnessTest(_WaveBase):
    def test_a_cross_harness_session_is_not_a_claude_origin(self):
        worker = self.worker(session='primaire', session_harness='codex')
        self.assertIsNone(account_turn.session_account(worker, self.items))

        worker._state_read = lambda key: ('claude' if key == 'session_harness' else '')
        self.assertEqual(account_turn.session_account(worker, self.items), 'primaire')

    def test_a_cross_harness_peer_is_not_scheduled_in_the_wave(self):
        worker = self.worker()
        # Le pair est un agent Claude, mais sa session COURANTE est une
        # session Codex : son compte « primaire » n'est pas une origine Claude.
        self.db.execute(
            "INSERT INTO agent_registry (name, harness, host, status, model,"
            " session_account, session_harness)"
            " VALUES ('auditeur-pair', 'claude', 'host-remote', 'idle', '',"
            "         'primaire', 'codex')")
        choice = self.choose()
        self.assertEqual(choice.switched['from'], 'primaire')
        self.assertEqual(choice.switched['to'], 'tertiaire')
        with mock.patch.object(account_rotation.registry, 'overview',
                               wraps=registry.overview):
            choice = account_rotation.coordinate(worker, self.items, self.book, choice,
                                                 now=NOW)
        self.assertEqual(choice.profile.name, 'tertiaire')
        planned = self.journal('planned')
        self.assertEqual([row['agent'] for row in planned], ['auditeur'])


class EffectiveDestinationTest(_WaveBase):
    def test_b_last_effective_switch_at_the_proposed_destination_goes_there(self):
        # 704 dans l'incident : la bascule effective a→tertiaire existe déjà,
        # mais la session (rendue par un harnais étranger) dit encore « primaire ».
        self.effective('primaire', 'tertiaire')
        choice = self.choose(session='primaire')
        self.assertEqual((choice.switched['from'], choice.switched['to']),
                         ('primaire', 'tertiaire'))
        worker = self.worker(session='primaire')
        with mock.patch.object(account_rotation.registry, 'overview', return_value=[]):
            choice = account_rotation.coordinate(worker, self.items, self.book, choice,
                                                 now=NOW)
        self.assertEqual(choice.profile.name, 'tertiaire')
        self.assertNotIn('retry next turn', choice.reason or '')
        self.assertEqual(self.journal('planned'), [])
        self.assertEqual(getattr(worker, '_wave_retry', ''), '')

    def test_c_a_genuine_concurrent_change_refuses_once_then_passes(self):
        self.effective('primaire', 'tertiaire')
        choice = self.choose(session='primaire')
        # Un autre hôte a déjà appliqué primaire→secondaire entre le choix et
        # le plan : la destination proposée (tertiaire) n'est plus l'effective.
        self.effective('primaire', 'secondaire', age=1)
        worker = self.worker(session='primaire')
        with mock.patch.object(account_rotation.registry, 'overview', return_value=[]):
            first = account_rotation.coordinate(worker, self.items, self.book, choice,
                                                now=NOW)
            self.assertIsNone(first.profile)
            self.assertIn('retry next turn', first.reason)
            # Le tour suivant relit le choix (la session dit toujours primaire)
            # mais reconciles sur la bascule effective, puis passe.
            second = account_rotation.coordinate(worker, self.items, self.book,
                                                 self.choose(session='primaire'), now=NOW)
        self.assertIsNotNone(second.profile)
        self.assertEqual(second.profile.name, 'secondaire')
        self.assertNotIn('retry next turn', second.reason or '')
        self.assertEqual(self.journal('planned'), [])


class WaveStatusTest(_WaveBase):
    def test_d_wave_refusal_is_not_marked_as_a_budget_pause(self):
        comptes = self._claude_comptes()
        _runner, worker = self._worker('auditeur', 'claude', {'claude': comptes},
                                       critical_agents=('auditeur',),
                                       critical_fallback_harnesses=())
        try:
            with mock.patch.object(account_turn, 'choose',
                                   return_value=account_rotation.WAVE_RETRY_REASON):
                self.assertFalse(worker.budget_ok())
            texte = registry.get(self.db, 'auditeur')['status_text']
            self.assertEqual(texte, account_rotation.WAVE_RETRY_REASON)
            self.assertFalse(texte.startswith('budget : '))
            worker.agent = registry.get(self.db, 'auditeur')
            with mock.patch.object(account_turn, 'choose', return_value=''):
                self.assertTrue(worker.budget_ok())
            row = registry.get(self.db, 'auditeur')
            self.assertEqual(row['status'], 'idle')
            self.assertEqual(row['status_text'], '')
        finally:
            worker.watchdog_stop.set()


class WaveAlertTest(unittest.TestCase):
    def row(self, since):
        return dict(name='auditeur', host='ameesh-agents-1', harness='claude',
                    responsible='human:smichea', status='blocked',
                    status_text=account_rotation.WAVE_RETRY_REASON,
                    status_reason_since_ts=since, status_since_ts=since - 30)

    def test_a_fresh_refusal_raises_nothing(self):
        self.assertIsNone(exploitation._account_wave_alert(
            self.row(NOW - 30), account_rotation.WAVE_RETRY_REASON, NOW, 120))

    def test_a_repeated_refusal_names_the_wave_coordination(self):
        alert = exploitation._account_wave_alert(
            self.row(NOW - 300), account_rotation.WAVE_RETRY_REASON, NOW, 120)
        self.assertEqual(alert['type'], 'account_wave_coordination')
        self.assertEqual(alert['value'], 300)
        self.assertIn('wave coordination', alert['detail'])
        self.assertIn('account_switches', alert['detail'])
        self.assertNotIn('paused on budget', alert['detail'])

    def test_a_budget_pause_is_not_a_wave_refusal(self):
        texte = 'budget : critical agent: pause (account secondaire, five_hour 96 %)'
        self.assertIsNone(exploitation._account_wave_alert(
            dict(self.row(NOW - 300), status_text=texte), texte, NOW, 120))


if __name__ == '__main__':
    unittest.main()
