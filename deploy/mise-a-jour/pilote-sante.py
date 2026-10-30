#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""One controlled authenticated turn for the release health step (L232b).

`HealthTurn(db, enrollment=, host=, system=)` binds the study's "one controlled
authenticated turn" to evidence the controller may honestly read: the enrolled
probe agent's own registry row and its completed turn resource. The host effect
itself — running the active release's own runner once, under the enrolled
executor account, in one bounded process — belongs to the enrolled backend's
`one_turn(...)`; `TurnRunner` is the fixed-binary implementation of that
contract. This module never starts a harness itself and never fabricates a
turn.

Backend contract: `one_turn(*, unit, agent, host, account, home, seconds,
executable)` runs exactly one bounded turn of the enrolled probe agent on this
host and returns the runner's exit status (0: a turn ran, 3: no turn ran). A
timeout, an unavailable executable or any other ambiguity raises; health then
reconciles instead of repeating or inventing the effect.

Evidence, all read around the call:
- before it, the probe's registry row is on this host, is not running and holds
  no live lease: a live turn is never disturbed;
- after it, the row shows exactly one more completed turn (`turns`), a newer
  claim generation (`lease_epoch`), a completion timestamp, no held lease and a
  successful outcome (status `idle`, no recorded error: a turn that ended
  `blocked`, for example on an authentication failure, is a failed probe);
- exactly one `turn_resources` row was opened for the probe on this host after
  the call started and is closed (`done`);
- nothing of the probe is left running.

The enrolled `turn.json` document carries exact keys `agent`, `unit`, `account`,
`home` and `seconds`; the runner unit must be the enrolled agent's own unit, in
either of its two deployed spellings (`ameesh-runner-agent@<agent>.service`, the
user template, or `ameesh-runner@<agent>.service`, the system template), and the
account a plain login name. `validate_enrollment` is the single validator of
that document, so an operator entry that assembles an enrollment is judged by
exactly the rules the controller enforces. Document bodies, provider text and
session identifiers never enter an error or a receipt.

This module is source only: it is not installed, no service is enabled and no
root, SSH, systemd, PostgreSQL migration or production effect is executed by
building or testing it. Tests use a disposable release tree and a simulated
backend; the only fixed system command is built, never run, by `TurnRunner`.
"""
import os
from pathlib import Path
import re
import stat
import subprocess

AGENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z')
ACCOUNT = re.compile(r'[a-z_][a-z0-9_-]{0,31}\Z')
HOME = re.compile(r'/[A-Za-z0-9._/-]{0,127}\Z')
OUTPUT = 65536

REGISTRY = ("SELECT name, host, status, lease_owner, lease_expires_at, lease_epoch, turns,"
            " last_error, last_turn_at FROM agent_registry WHERE name = %s")
TURNS = ("SELECT turn_id, status, started_at, ended_at FROM turn_resources"
         " WHERE agent = %s AND host = %s AND started_at >= %s ORDER BY id")
RUNNING = ("SELECT count(*) AS running FROM turn_resources"
           " WHERE agent = %s AND status = 'running'")
MARKER = "SELECT clock_timestamp() AS now"
#: The two deployed spellings of an agent's own runner unit. The user template
#: (`ameesh-runner-agent@`) is the one installed on the mesh hosts; the system
#: template (`ameesh-runner@`) remains valid on an L55 server. No other name is
#: a runner unit of this agent.
RUNNER_UNITS = ('ameesh-runner@%s.service', 'ameesh-runner-agent@%s.service')


class TurnError(ValueError):
    pass


def runner_units(agent):
    """The exact runner units of `agent`, in both deployed spellings only."""
    if not isinstance(agent, str) or AGENT.fullmatch(agent) is None:
        return ()
    return tuple(template % agent for template in RUNNER_UNITS)


def validate_enrollment(enrollment):
    """Exact enrolled probe document (`turn.json`), validated once for everyone.

    Keys are exactly `agent`, `unit`, `account`, `home` and `seconds`; the unit
    is the agent's own runner unit, in either deployed spelling, and the
    deadline is bounded (5 to 120 seconds). Returns a copy, so a caller cannot
    keep a handle on the values the controller later reads.
    """
    if (not isinstance(enrollment, dict)
            or set(enrollment) != {'agent', 'unit', 'account', 'home', 'seconds'}):
        raise TurnError('invalid turn enrollment')
    agent, unit = enrollment['agent'], enrollment['unit']
    account, home, seconds = enrollment['account'], enrollment['home'], enrollment['seconds']
    if not isinstance(agent, str) or AGENT.fullmatch(agent) is None:
        raise TurnError('invalid probe agent')
    if unit not in runner_units(agent):
        raise TurnError('invalid probe runner unit')
    if not isinstance(account, str) or ACCOUNT.fullmatch(account) is None:
        raise TurnError('invalid probe account')
    if (not isinstance(home, str) or '..' in home or HOME.fullmatch(home) is None
            or home == '/'):
        raise TurnError('invalid probe home')
    if isinstance(seconds, bool) or not isinstance(seconds, int) or not 5 <= seconds <= 120:
        raise TurnError('invalid turn deadline')
    return dict(agent=agent, unit=unit, account=account, home=home, seconds=seconds)


class HealthTurn:
    """One enrolled probe's completed turn, verified and never invented."""

    def __init__(self, db, *, enrollment, host, system):
        if not callable(getattr(system, 'one_turn', None)):
            raise TurnError('enrolled one-turn runner required')
        if not callable(getattr(db, 'query', None)):
            raise TurnError('enrolled turn database required')
        if not isinstance(host, str) or AGENT.fullmatch(host) is None:
            raise TurnError('invalid enrolled host')
        enrollment = validate_enrollment(enrollment)
        self.db, self.host, self.system = db, host, system
        self.agent, self.unit = enrollment['agent'], enrollment['unit']
        self.account, self.home, self.seconds = (enrollment['account'],
                                                 enrollment['home'],
                                                 enrollment['seconds'])

    # -- controller contract ------------------------------------------------
    def one_turn(self, *, operation, step, release, root):
        if step not in ('health', 'recovery_health') or not isinstance(operation, str) or not operation:
            raise TurnError('invalid turn request')
        executable = self._executable(root)
        marker = self._marker()
        before = self._registry()
        if before is None or before.get('host') != self.host:
            return False
        # A live lease or a running turn is never disturbed by a health probe.
        if before.get('status') == 'running' or self._live_lease(before, marker):
            return False
        if executable is None:
            return False
        code = self.system.one_turn(unit=self.unit, agent=self.agent, host=self.host,
                                    account=self.account, home=self.home,
                                    seconds=self.seconds, executable=executable)
        if isinstance(code, bool) or not isinstance(code, int):
            raise TurnError('controlled turn result unavailable')
        if code != 0:
            return False
        after = self._registry()
        if after is None or after.get('host') != self.host:
            return False
        if int(after.get('turns') or 0) != int(before.get('turns') or 0) + 1:
            return False
        if int(after.get('lease_epoch') or 0) <= int(before.get('lease_epoch') or 0):
            return False
        if after.get('last_turn_at') is None:
            return False
        if after.get('status') == 'running' or self._live_lease(after, marker):
            return False
        # The turn outcome is part of the evidence: a turn that ended blocked
        # (an authentication failure, a host refusal, a harness error) still
        # increments `turns` and closes its resource. Accepting it would accept
        # a release that cannot authenticate, then defend it.
        if after.get('status') != 'idle' or after.get('last_error'):
            return False
        rows = self._rows(TURNS, (self.agent, self.host, marker))
        if len(rows) != 1:
            return False
        row = rows[0]
        if row.get('status') != 'done' or row.get('ended_at') is None:
            return False
        running = self._rows(RUNNING, (self.agent,))
        if len(running) != 1:
            raise TurnError('turn evidence unavailable')
        return int(running[0].get('running') or 0) == 0

    # -- evidence -----------------------------------------------------------
    def _marker(self):
        rows = self._rows(MARKER, ())
        if len(rows) != 1 or rows[0].get('now') is None:
            raise TurnError('turn evidence unavailable')
        return rows[0]['now']

    def _registry(self):
        rows = self._rows(REGISTRY, (self.agent,))
        if not rows:
            return None
        if len(rows) != 1 or not isinstance(rows[0], dict):
            raise TurnError('turn evidence unavailable')
        return rows[0]

    def _rows(self, sql, params):
        try:
            rows = self.db.query(sql, params)
        except Exception:
            raise TurnError('turn evidence unavailable') from None
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise TurnError('turn evidence unavailable')
        return rows

    @staticmethod
    def _live_lease(row, now):
        if not row.get('lease_owner'):
            return False
        expires = row.get('lease_expires_at')
        if expires is None:
            return True
        try:
            return expires > now
        except TypeError:
            # An unreadable expiry is treated as a live lease, never as free.
            return True

    @staticmethod
    def _executable(root):
        path = Path(root) / 'venv' / 'bin' / 'agent-runner'
        try:
            info = path.lstat()
        except OSError:
            return None
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid() or not info.st_mode & 0o100):
            return None
        return str(path)


class TurnRunner:
    """Fixed one-turn command: the release's own runner as the enrolled account.

    No caller supplies a path: `runuser` and the release runner are fixed, the
    argv is built from the validated enrollment, and the environment is
    scrubbed. The child runs the active release's own `agent-runner --once`,
    which claims the probe under a lease, runs at most one turn and releases
    it. A timeout or an unavailable binary raises so health reconciles.
    """

    def __init__(self, *, runuser='/usr/sbin/runuser'):
        if not Path(runuser).is_absolute() or '..' in Path(runuser).parts:
            raise TurnError('absolute runner executable required')
        self.runuser = str(Path(runuser))

    def one_turn(self, *, unit, agent, host, account, home, seconds, executable):
        args = [self.runuser, '-u', account, '--', executable,
                '--host', host, '--agents', agent, '--once', '--wait', str(seconds)]
        env = {'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C', 'HOME': home,
               'USER': account, 'LOGNAME': account, 'PYTHONNOUSERSITE': '1'}
        try:
            result = subprocess.run(args, capture_output=True, env=env,
                                    timeout=seconds + 30, check=False)
        except subprocess.TimeoutExpired:
            raise TurnError('controlled turn timed out') from None
        except (OSError, subprocess.SubprocessError):
            raise TurnError('controlled turn unavailable') from None
        if len(result.stdout) > OUTPUT or len(result.stderr) > OUTPUT:
            raise TurnError('controlled turn output limit exceeded')
        return result.returncode
