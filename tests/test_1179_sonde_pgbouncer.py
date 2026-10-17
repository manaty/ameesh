# SPDX-License-Identifier: AGPL-3.0-only
"""#1179 (suite N1 de #532) : la sonde de la console du pool local ne fait
plus échouer la passe ordinaire du notifieur, ni toute la commande
`ameesh alerts`.

La console est la sonde indépendante de #532 : elle répond encore quand le
pool refuse tout le monde. Mais l'inverse doit tenir aussi — une console
illisible alors que la base répond laisse les autres alertes vivantes et
devient une ligne lisible, jamais un `Unavailable` qui emporte le passage."""
import types
import unittest
from unittest import mock

from ameesh import db as db_mod, exploitation, notify, pgbouncer_local


class ConsoleLine(unittest.TestCase):
    """`alerts()` transforme l'échec de la sonde en ligne, il ne le propage
    pas."""

    def db(self):
        return types.SimpleNamespace(cfg=types.SimpleNamespace(host='pc-smichea'))

    def test_unreadable_console_becomes_a_line_not_a_failure(self):
        with mock.patch.object(pgbouncer_local, 'alerts',
                               side_effect=db_mod.Unavailable('console refused after 5s')):
            lines = exploitation._local_pool_alerts(self.db())
        self.assertEqual([a['type'] for a in lines], ['pgbouncer_console_unreadable'])
        self.assertIn('console refused after 5s', lines[0]['detail'])
        self.assertEqual(lines[0]['host'], 'pc-smichea')
        self.assertIn('pgbouncer_console_unreadable', exploitation.ALERT_TYPES)

    def test_a_readable_console_is_passed_through_untouched(self):
        waiting = [{'type': 'pgbouncer_pool_waiting', 'detail': 'queue'}]
        with mock.patch.object(pgbouncer_local, 'alerts', return_value=waiting):
            self.assertEqual(exploitation._local_pool_alerts(self.db()), waiting)

    def test_no_local_pool_is_not_an_error(self):
        with mock.patch.object(pgbouncer_local, 'alerts', return_value=[]):
            self.assertEqual(exploitation._local_pool_alerts(self.db()), [])


class OrdinaryPass(unittest.TestCase):
    """`run_pass` ne lit pas la console locale et ne résout jamais ses
    alertes : `run_local_pass` en reste seul propriétaire (#1179)."""

    def notifier(self, state=None):
        return notify.Notifier(None, notify.parse_config({'default': ['desktop']}),
                               state=state, sender=mock.Mock(send=lambda c, m: None),
                               log=lambda text: None, clock=lambda: 100.0)

    def patched(self):
        return mock.patch.multiple(notify.Notifier, _brief_orchestrators=mock.DEFAULT,
                                   _brief_agents=mock.DEFAULT, _github_issues=mock.DEFAULT,
                                   _ci_failures=mock.DEFAULT)

    def active(self, kind, **fields):
        return {'alert': dict({'type': kind, 'detail': '%s detail' % kind}, **fields),
                'raised_ts': 1.0, 'human': 'human:proprio', 'source': 'agent',
                'status': 'envoi',
                'channels': {'desktop': {'ok': True, 'attempts': 1, 'error': None}}}

    def test_run_pass_asks_alerts_without_the_local_console(self):
        notifier = self.notifier()
        with mock.patch.object(exploitation, 'alerts', return_value=[]) as seen, \
                self.patched():
            notifier.run_pass(None)
        self.assertIs(seen.call_args.kwargs.get('local_pool'), False)

    def test_the_ordinary_pass_never_resolves_a_local_alert(self):
        state = notify.empty_state()
        state['active']['pgbouncer_pool_waiting'] = self.active(
            'pgbouncer_pool_waiting', agent=None)
        state['active']['stopped_with_mail'] = self.active(
            'stopped_with_mail', agent='ouvrier')
        notifier = self.notifier(state)
        with self.patched():
            records = notifier.run_pass(None, current=[])
        # la console n'a pas été lue : l'absence de l'alerte n'est pas une
        # résolution ; les autres alertes, elles, se résolvent normalement.
        self.assertIn('pgbouncer_pool_waiting', notifier.state['active'])
        self.assertNotIn('stopped_with_mail', notifier.state['active'])
        self.assertEqual([r['event'] for r in records
                          if r['type'] == 'stopped_with_mail'], ['resolved'])

    def test_local_types_keep_the_console_line(self):
        self.assertIn('pgbouncer_console_unreadable', notify.LOCAL_TYPES)
        self.assertIn('pgbouncer_console_unreadable', notify.TYPE_LABELS)


if __name__ == '__main__':
    unittest.main()
