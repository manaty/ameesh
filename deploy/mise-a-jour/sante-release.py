#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Health evidence for the independent release controller (L232b).

`HealthProbe(db, store=, card=, system=)` binds the controller's `health` and
`recovery_health` steps to checks that a source-only build can honestly make:

- the versioned pointer names the activated release (`health`) or the prior
  release restored by rollback (`recovery_health`), and that release's own
  completed receipt is rederived, never trusted;
- the released schema is complete: every migration of the active release's
  source is applied with its recorded checksum, and a forward release carries
  no version unknown to its own code (an additive leftover after rollback is
  expected and allowed);
- the enrolled host card holds in the effective systemd view: every card unit
  is loaded, no daemon reload is pending, its declared state and capabilities
  match byte for byte, and a declared guard unit is active;
- a controlled `doctor --probe` runs from the active release's own pinned
  interpreter with a private credential file, a scrubbed environment and a
  bounded deadline.

The study's full health step also runs one controlled authenticated turn. It is
provided by the pinned `pilote-sante.py` companion (`HealthTurn`), through the
enrolled backend's `one_turn(...)`; this module requires that companion and
never claims a turn it did not receive.

This module is source only: it is not installed, no service is enabled and no
root, SSH, systemd, PostgreSQL migration or production effect is executed by
building or testing it. A definitive health failure answers False (the
controller enters recovery); an unavailable command raises so the durable
intent reconciles.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time

REQUIRED_STEPS = ('health', 'recovery_health')
REQUEST_FIELDS = {'operation', 'manifest_digest', 'action_id', 'manifest', 'step'}
SHA256 = re.compile(r'[0-9a-f]{64}')
OPERATION = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}')


def module(name):
    spec = importlib.util.spec_from_file_location('health_' + name.replace('-', '_'),
                                                  Path(__file__).with_name(name + '.py'))
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


protocol = module('controleur-release')
verification = module('verification-release')
environnement = module('environnement-versionne')


class HealthError(ValueError):
    pass


class HealthProbe:
    """One release's health evidence; no caller-supplied command or path."""

    def __init__(self, db, *, store, card, system, turn=None, clock=time.time):
        if not callable(getattr(system, 'show', None)):
            raise HealthError('enrolled system adapter required')
        if turn is not None and not callable(getattr(turn, 'one_turn', None)):
            raise HealthError('enrolled authenticated turn required')
        try:
            self.card = verification.validate_card(card)
        except verification.VerificationError:
            raise HealthError('invalid enrolled host card') from None
        dsn = getattr(getattr(db, 'cfg', None), 'dsn', None)
        if (not callable(getattr(db, 'query', None)) or not isinstance(dsn, str)
                or not dsn or '\x00' in dsn):
            raise HealthError('enrolled health database required')
        self.db = db
        self.store = Path(store)
        self.system = system
        self.turn = turn
        self.dsn = dsn
        self.clock = clock

    # -- controller contract ------------------------------------------------
    def perform(self, step, request, release):
        request = json.loads(json.dumps(request))
        self._validate(request, step)
        manifest = request['manifest']
        if not isinstance(release, dict) or release.get('commit') != manifest['commit']:
            return False
        identity = hashlib.sha256(json.dumps(release, sort_keys=True).encode()).hexdigest()
        if manifest['release'] != manifest['commit'] + '-' + identity:
            return False
        active = manifest['release'] if step == 'health' else self._prior(request)
        if active is None or not self._pointer(active):
            return False
        root = self.store / active
        if not self._environment(root, active):
            return False
        if not self._migrations(root, step):
            return False
        if not self._configuration():
            return False
        if not self._probe(root):
            return False
        return self._turn(request, active, root)

    def _turn(self, request, active, root):
        """One controlled authenticated turn; never claimed without a backend."""
        if self.turn is None:
            raise HealthError('authenticated turn unavailable')
        return self.turn.one_turn(operation=request['operation'], step=request['step'],
                                  release=active, root=root) is True

    @staticmethod
    def _validate(request, step):
        if step not in REQUIRED_STEPS:
            raise HealthError('invalid health step')
        if not isinstance(request, dict) or set(request) != REQUEST_FIELDS:
            raise HealthError('invalid health request')
        operation = request['operation']
        if (not isinstance(operation, str) or OPERATION.fullmatch(operation) is None
                or request['step'] != step or request['action_id'] != operation + ':' + step):
            raise HealthError('invalid health action')
        protocol.validate(request['manifest'])
        if protocol.identity(request['manifest']) != request['manifest_digest']:
            raise HealthError('invalid health manifest digest')

    # -- release identity and pointer --------------------------------------
    def _prior(self, request):
        try:
            record = environnement.switch_record(self.store, request['operation'])
        except environnement.ReleaseError:
            return None
        return record['previous']

    def _pointer(self, active):
        current = self.store / 'current'
        if not current.is_symlink():
            return False
        try:
            return os.readlink(current) == active
        except OSError:
            return False

    def _environment(self, root, active):
        try:
            info = root.lstat()
        except OSError:
            return False
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.geteuid() or info.st_mode & 0o077):
            return False
        try:
            document = json.loads((root / 'ready.json').read_text())
        except (OSError, ValueError):
            return False
        if not isinstance(document, dict) or document.get('commit') != active[:40]:
            return False
        identity = hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()
        if active != document['commit'] + '-' + identity:
            return False
        if (root / 'source').is_symlink() or not (root / 'source').is_dir():
            return False
        executable = root / 'venv' / 'bin' / 'python'
        return not executable.is_symlink() and executable.is_file()

    # -- schema, effective configuration and controlled probe --------------
    def _migrations(self, root, step):
        directory = root / 'source' / 'src' / 'ameesh' / 'migrations'
        if directory.is_symlink() or not directory.is_dir():
            return False
        from ameesh import migrations
        try:
            missing, modified, unknown = migrations.check(self.db, directory=str(directory))
        except Exception:
            raise HealthError('schema readiness unavailable') from None
        if missing or modified:
            return False
        # After a rollback the additive migrations of the failed release remain
        # applied and are unknown to the prior code; that is not a health fault.
        return step != 'health' or not unknown

    def _configuration(self):
        for unit in self.card['units']:
            if not self._effective(unit):
                return False
        guard = self.card['guard']
        return guard is None or self._effective(guard)

    def _effective(self, unit):
        declared = dict(self.card['properties'].get(unit, {}))
        try:
            info = self.system.show(unit, declared, self.card['seconds'])
        except Exception:
            raise HealthError('effective unit state unavailable') from None
        if not isinstance(info, dict):
            raise HealthError('effective unit state unavailable')
        if info.get('LoadState') != 'loaded' or info.get('NeedDaemonReload') != 'no':
            return False
        if info.get('ActiveState') != declared.get('ActiveState', 'active'):
            return False
        return all(info.get(name) == value for name, value in declared.items())

    def _probe(self, root):
        executable = root / 'venv' / 'bin' / 'python'
        if executable.is_symlink() or not executable.is_file():
            return False
        with tempfile.TemporaryDirectory(prefix='.probe-', dir=self.store) as staging:
            config = Path(staging) / 'config.json'
            with config.open('x') as stream:
                config.chmod(0o600)
                json.dump({'backend': 'postgres', 'dsn': self.dsn}, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            env = {'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C',
                   'AMEESH_CONFIG': str(config), 'PYTHONNOUSERSITE': '1'}
            try:
                result = subprocess.run([str(executable), '-I', '-B', '-m', 'ameesh.cli',
                                         'doctor', '--probe'], cwd=str(root / 'source'), env=env,
                                        check=False, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, timeout=self.card['seconds'])
            except subprocess.TimeoutExpired:
                raise HealthError('controlled health probe timed out') from None
            except OSError:
                raise HealthError('controlled health probe unavailable') from None
        return result.returncode == 0
