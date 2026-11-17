# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded native access to an agent session (L82, decision 0035)."""
from __future__ import annotations

import math
import time

from . import mail, stagnation

DEFAULT_MAX = 2 * 3600.0
NOTICE = 60.0
INPUT_DELAY = 10 * 60.0
INPUT_CHECK = 30.0
HANDOFF_TIMEOUT = 60.0
#: #7644 : un harnais interactif qui échoue avant ce délai n'a pas ouvert la
#: session (conversation absente du dossier du compte, compte ou dossier
#: invalide) : l'attache dit où elle a cherché et quoi faire, au lieu de
#: laisser l'utilisateur devant le seul message du harnais.
EARLY_FAILURE_S = 10.0


def duration(value: str) -> float:
    seconds = stagnation.parse_duration(value)
    if not math.isfinite(seconds):
        raise ValueError("attach duration must be finite and positive")
    return seconds


class Deadline:
    """Monotonic limit, independent from lease renewal and wall-clock jumps."""
    def __init__(self, seconds: float):
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("attach duration must be finite and positive")
        self.end = time.monotonic() + seconds
        self.notice_at = self.end - min(NOTICE, seconds / 2)
        self.noticed = False

    def notice(self) -> float | None:
        now = time.monotonic()
        if not self.noticed and now >= self.notice_at:
            self.noticed = True
            return max(0.0, self.end - now)
        return None

    def expired(self) -> bool:
        return time.monotonic() >= self.end


def early_failure_lines(cfg, name: str, agent: dict | None, account,
                        elapsed: float, code: int) -> list[str]:
    """Les lignes d'une attache dont le harnais a échoué au démarrage (#7644).

    Le compte et le dossier de sessions cherchés sont nommés, jamais un
    secret : l'échec typique est « No conversation found » parce que la
    session vit dans le dossier d'un autre compte.
    """
    from . import accounts as accounts_mod, reprise
    harness = (agent or {}).get("harness") or "harness"
    session = (agent or {}).get("session_id") or ""
    if account is None:
        account = accounts_mod.Profile(harness=harness, name="(default)")
    folder = account.session_store()
    lines = ["attach: the %s session ended after %.1f s (code %s) before any exchange."
             % (harness, elapsed, code)]
    if session:
        try:
            found = reprise.find_session(harness, account.home(), session)
        except OSError:  # la recherche ne doit pas masquer le diagnostic
            found = None
        lines.append("attach: session %s was %s under account %s (folder %s)."
                     % (session, "found" if found else "not found",
                        account.name, folder))
    else:
        lines.append("attach: no recorded session; the new session would open under "
                     "account %s (folder %s)." % (account.name, folder))
    lines.append("attach: check `ameesh resume %s` (account continuity) or "
                 "`ameesh resume %s --fresh` (new session, resume summary)."
                 % (name, name))
    return lines


def waiting_since(db, name: str, row: dict, prompt_since: float | None) -> float | None:
    """Oldest queued input, including pending instructions. Never consumed.

    L77 chat inputs use the mailbox too, so the same check covers them.
    """
    times = [float(m["created_ts"]) for m in mail.unread_active(db, name, limit=1)]
    if row.get("pending_prompt") and prompt_since is not None:
        times.append(prompt_since)
    return min(times) if times else None
