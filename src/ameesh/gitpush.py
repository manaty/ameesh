# SPDX-License-Identifier: AGPL-3.0-only
"""Hand-made git push: one second attempt on a transient remote refusal.

Persisted rule (canon §7, `docs/EXPLOITATION.md` section watch; lots #876 and
#1125): a hand-made chain that pushes — a merge watcher, a follow-up script —
gives the push **one** second attempt after a short delay when the remote
refuses it (`! [remote rejected] … (failed)`), traces both attempts, then
stops and reports if the second attempt fails too.

The observation (13/10 23:36-23:41Z, lots #749 and #752): two pushes refused
then accepted on the second attempt, with no change of head or rights, four
other pushes of the same window passing on the first try; every transient
refusal cost an agent turn.

Use from a chain:

    from ameesh.gitpush import push_with_retry
    proc = push_with_retry(run, ["git", "push", "origin", REF], trace=log)
    if proc.returncode != 0:
        ...  # definitive failure: the chain notes it and stops

`run` keeps the chain's signature (`argv -> CompletedProcess`, `cwd` and `env`
already set by it): this module knows neither the repository nor the remote, it
only carries the two-attempt policy. The default delay is 10 s.

Wiring is deliberately absent (PR #888): the repository's push sites are on
sensitive §8 paths (`land.py`, `mediated_executor/work_repo.py`: concurrency;
`persona_memory.py`: privacy) or already carry their own retry
(`persona_memory`), so wiring this module there is a sensitive lot of its own.
Host chains may import it right now.

Only a non-zero exit code is retried. An exception raised by `run`
(`subprocess.TimeoutExpired`, `OSError`, …) propagates unchanged after a single
call: the policy cannot tell whether the push reached the remote, and a retry
after a timeout is not safe. The caller treats it as a definitive failure.
"""
from __future__ import annotations

import re
import time
from typing import Callable, Optional, Sequence

#: delay between the two attempts (the remote answered the second attempt in
#: ~1 min on 13/10; 10 s is enough and does not delay a definitive failure)
RETRY_DELAY_S = 10
#: maximum length of the traced detail (git output is verbose)
DETAIL_MAX = 300
#: credentials in a URL (`scheme://user:secret@host`): never traced as they are
_CREDENTIALS = re.compile(r"://[^/@\s]+@")


def _detail(proc) -> str:
    """The push output, credentials masked, for one trace line."""
    text = ((proc.stderr or "") + (proc.stdout or "")).strip()
    return _CREDENTIALS.sub("://***@", text)


def push_with_retry(run: Callable[[Sequence[str]], object],
                    argv: Sequence[str], *, delay: float = RETRY_DELAY_S,
                    trace: Optional[Callable[[str], None]] = None):
    """`run(argv)`; on a non-zero exit code, exactly one new attempt.

    Every non-zero code is retried: telling a transient refusal from a
    definitive one (rights, non fast-forward) would mean parsing git's output,
    and the cost of a useless attempt is `delay` seconds — while a transient
    refusal not retried costs an agent turn. An exception raised by `run`
    propagates without a retry (module docstring). Returns the
    `CompletedProcess` of the last attempt; `trace` (a text callable) receives
    one line per refused attempt, and one line when the second attempt passes.
    """
    say = trace if trace is not None else (lambda msg: None)
    proc = run(argv)
    if proc.returncode == 0:
        return proc
    say("push refused (attempt 1/2): %s; retrying in %ss"
        % (_detail(proc)[:DETAIL_MAX], delay))
    time.sleep(delay)
    proc = run(argv)
    if proc.returncode == 0:
        say("push accepted on the second attempt")
    else:
        say("push refused (attempt 2/2): %s" % _detail(proc)[:DETAIL_MAX])
    return proc


def push(run: Callable[[Sequence[str]], object], remote: str, refspec: str,
         **kwargs):
    """Shortcut: `git push <remote> <refspec>` with the second attempt."""
    return push_with_retry(run, ["git", "push", remote, refspec], **kwargs)
