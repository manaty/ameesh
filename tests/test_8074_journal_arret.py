# SPDX-License-Identifier: AGPL-3.0-only
"""#8074 — le fil `journal` est vidé et arrêté à la sortie de l'interpréteur.

Le journal asynchrone de l'exécuteur (`ameesh.runner._AsyncLog`) est un fil
démon global au processus. `_sortie_sure` le ferme dans le `finally` de
`main()`, mais tout autre processus qui importe `runner` — une sous-suite de
tests, une prévalidation, une CLI — le laissait vivre après la fin du script :
les messages encore en file étaient perdus, et si le fil écrivait sur stderr
pendant la finalisation de CPython, le vidage final échouait en erreur fatale
(« could not acquire lock for <stderr> at interpreter shutdown », SIGABRT).

Le correctif est un `atexit` borné dans `runner` : il vide la file, arrête le
fil, et si le puits est bloqué sort par `os._exit` plutôt que de laisser
CPython abandonner le processus. Un enfant forké ne touche pas au fil du
parent. Le témoin ci-dessous fait écrire 1000 lignes par `log_async` à un
interpréteur qui sort aussitôt : sans le correctif, aucune n'atteint stderr.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import unittest
from unittest import mock

from ameesh import runner

from . import support

#: Un interpréteur qui remplit la file du journal puis sort sans passer par
#: `main()` : c'est exactement la forme d'une sous-suite de tests.
FLOOD = """
from ameesh import runner
for i in range(1000):
    runner.log_async("marqueur-8074-%03d" % i)
"""


class JournalAtexitTest(unittest.TestCase):
    def test_la_file_est_videe_a_la_sortie_de_l_interpreteur(self):
        for _ in range(3):
            proc = subprocess.run(
                [sys.executable, "-c", FLOOD], cwd=support.REPO,
                env=support.child_env(), capture_output=True, text=True,
                timeout=support.scaled(120))
            self.assertEqual(proc.returncode, 0, proc.stderr[-4000:])
            self.assertNotIn("Fatal Python error", proc.stderr)
            self.assertNotIn("journal thread still active", proc.stderr)
            marqueurs = re.findall(r"marqueur-8074-(\d{3})", proc.stderr)
            self.assertEqual(sorted(marqueurs),
                             ["%03d" % i for i in range(1000)],
                             proc.stderr[-2000:])

    def test_arret_borne_puis_sortie_sans_vidage_si_le_puits_bloque(self):
        with mock.patch.object(runner, "_JOURNAL_PID", os.getpid()), \
                mock.patch.object(runner, "_journal") as journal, \
                mock.patch.object(runner, "_ecrire_sans_attendre") as ecrire, \
                mock.patch("os._exit") as sortie:
            journal.close.return_value = False
            runner._arret_journal()
        journal.close.assert_called_once_with(runner.JOURNAL_CLOSE_TIMEOUT)
        ecrire.assert_called_once()
        self.assertEqual(ecrire.call_args.args[0], 2)
        sortie.assert_called_once_with(1)

    def test_enfant_forke_ne_ferme_pas_le_journal_du_parent(self):
        with mock.patch.object(runner, "_JOURNAL_PID", os.getpid() + 1), \
                mock.patch.object(runner, "_journal") as journal:
            runner._arret_journal()
        journal.close.assert_not_called()
