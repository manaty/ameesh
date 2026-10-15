# SPDX-License-Identifier: AGPL-3.0-only
"""Poussée git faite main : un second essai sur un rejet distant transitoire.

Règle persistée (canon §7, `docs/EXPLOITATION.md` section watch ; lot #1125) :
une chaîne faite main qui pousse — veilleur de fusion, script de suite — donne
**un** second essai après un court délai quand le distant refuse la poussée
(`! [remote rejected] … (failed)`), trace les deux tentatives, puis s'arrête
et rend compte si le second essai échoue aussi.

Le constat (13/10 23:36-23:41Z, lots #749 et #752) : deux poussées refusées
puis acceptées au deuxième essai, sans changement de tête ni de droits, quatre
autres poussées de la même fenêtre passées du premier coup ; chaque refus
transitoire coûtait un tour d'agent.

Usage dans une chaîne :

    from ameesh.gitpush import push_with_retry
    proc = push_with_retry(run, ["git", "push", "origin", REF], trace=log)
    if proc.returncode != 0:
        ...  # échec définitif : la chaîne note et s'arrête

`run` garde la signature de la chaîne (`argv -> CompletedProcess`, `cwd` et
`env` déjà posés par ses soins) : ce module ne connaît ni le dépôt ni le
distant, il ne fait que la politique des deux essais. Le délai par défaut est
de 10 s.
"""
from __future__ import annotations

import time
from typing import Callable, Optional, Sequence

#: délai entre les deux essais (le distant a répondu au deuxième essai en
#: ~1 min le 13/10 ; 10 s suffisent et ne retardent pas un échec définitif)
RETRY_DELAY_S = 10
#: longueur maximale du détail tracé (la sortie de git est bavarde)
DETAIL_MAX = 300


def _detail(proc) -> str:
    return ((proc.stderr or "") + (proc.stdout or "")).strip()


def push_with_retry(run: Callable[[Sequence[str]], object],
                    argv: Sequence[str], *, delay: float = RETRY_DELAY_S,
                    trace: Optional[Callable[[str], None]] = None):
    """`run(argv)` ; sur un code de sortie non nul, un seul nouvel essai.

    Tout code non nul est réessayé : distinguer un refus transitoire d'un
    refus définitif (droits, non fast-forward) demanderait de lire la sortie
    de git, et le coût d'un essai inutile est `delay` secondes — alors qu'un
    refus transitoire non réessayé coûte un tour d'agent. Rend le
    `CompletedProcess` du dernier essai ; `trace` (callable texte) reçoit une
    ligne par tentative refusée, et une ligne si le second essai passe.
    """
    say = trace if trace is not None else (lambda msg: None)
    proc = run(argv)
    if proc.returncode == 0:
        return proc
    say("poussée refusée (essai 1/2) : %s ; nouvel essai dans %ss"
        % (_detail(proc)[:DETAIL_MAX], delay))
    time.sleep(delay)
    proc = run(argv)
    if proc.returncode == 0:
        say("poussée acceptée au deuxième essai")
    else:
        say("poussée refusée (essai 2/2) : %s" % _detail(proc)[:DETAIL_MAX])
    return proc


def push(run: Callable[[Sequence[str]], object], remote: str, refspec: str,
         **kwargs):
    """Raccourci : `git push <remote> <refspec>` avec le second essai."""
    return push_with_retry(run, ["git", "push", remote, refspec], **kwargs)
