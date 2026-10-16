#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Pinned concrete host factory for the independent release controller (L232b).

`service-release.py` loads this exact source, never a module named by a job,
transport message or enrollment document. The factory composes the
administrator-enrolled host documents into the controller's authority/adapter
contract:

- protected documents under `/etc/ameesh-release` (root-owned, private, exact
  keys): `card.json` (effective configuration card), `destinations.json`
  (configuration destination map), `transport.json` (SSH restart endpoint),
  `turn.json` (enrolled probe agent and executor account) and `backup.json`
  (recovery credential references and retention policy);
- fixed private state directories under `/var/lib/ameesh-release` and the
  versioned release store under `/opt/ameesh-release/store`.

Admission is honest: every controller step must be bound to a concrete
capability, and a missing or incomplete backend refuses the whole service
rather than simulating it. Steps whose concrete adapter is a separate pinned
companion source stay unbound until that source is installed:

- `sauvegarde-release.py` with `RecoverySet(db, store=, root=, retention=,
  config=)` binds `backup`, `pin` and `accept`;
- `sante-release.py` with `HealthProbe(db, store=, card=, system=, turn=)`
  binds `health` and `recovery_health`; the required `turn` is the
  `pilote-sante.py` `HealthTurn(db, enrollment=, host=, system=)` running the
  enrolled probe through `TurnRunner`'s fixed one-turn command.

Companion objects expose `perform(step, request, release)` and answer True
after verified completion, False for a definitive refusal, or raise. The factory
never lets provider/credential exception text escape and never fabricates a
completed receipt: a definitive refusal becomes the controller's `ok: false`,
an ambiguous failure raises so the durable intent is reconciled instead of
repeated.

This module is source only. It is not installed, no service is enabled and no
root, SSH, systemd, PostgreSQL migration or production effect is executed by
building or testing it.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import time

ENROLLMENT_ROOT = Path('/etc/ameesh-release')
STATE_ROOT = Path('/var/lib/ameesh-release')
STORE_ROOT = Path('/opt/ameesh-release/store')
CONFIGURATION_JOURNAL = STATE_ROOT / 'configuration'
BACKUP_ROOT = STATE_ROOT / 'backup'
RETENTION_ROOT = STATE_ROOT / 'retention'
# A closed gate waits at most this long for running turns to drain; it stays
# closed for reconciliation afterwards. Not a credential or a release deadline.
DRAIN_SECONDS = 600
# Fixed floor for a release store with room for one venv/source pair.
MINIMUM_FREE_BYTES = 512 * 1024 * 1024
COMPANIONS = {'sauvegarde-release': ('backup', 'pin', 'accept'),
              'sante-release': ('health', 'recovery_health')}


def module(name):
    spec = importlib.util.spec_from_file_location('factory_' + name.replace('-', '_'),
                                                  Path(__file__).with_name(name + '.py'))
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


protocol = module('controleur-release')
restart = module('helper-redemarrage')
fences = module('fence-release')
transport = module('transport-release')
configuration = module('configuration-release')
verification = module('verification-release')
environnement = module('environnement-versionne')
REQUIRED = frozenset(protocol.STEPS + protocol.RECOVERY)


class FactoryError(ValueError):
    pass


class Refused(FactoryError):
    """A definitive refusal of the signed operation's own references."""


def trusted(path):
    """No linked ancestor, controller-owned, no group/world write anywhere."""
    expected = os.geteuid()
    path = Path(path).absolute()
    if '..' in path.parts:
        raise FactoryError('factory path refused')
    for ancestor in (path, *path.parents):
        try:
            info = ancestor.lstat()
        except OSError:
            raise FactoryError('factory path unavailable') from None
        if (stat.S_ISLNK(info.st_mode) or info.st_uid != expected
                or info.st_mode & 0o022):
            raise FactoryError('untrusted factory path')
    return path


class Documents:
    """Exact, controller-owned private JSON documents; bodies never enter errors."""

    def __init__(self, root=ENROLLMENT_ROOT):
        self.root = Path(root)

    def path(self, name):
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', name):
            raise FactoryError('invalid enrolled document name')
        return self.root / (name + '.json')

    def json(self, name, *, exact=None):
        path = self.path(name)
        trusted(path)
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            raise FactoryError('enrolled document unavailable') from None
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1
                    or info.st_size > 65536):
                raise FactoryError('private enrolled document required')
            data = stream.read(65537)
        if len(data) > 65536:
            raise FactoryError('enrolled document exceeds limit')
        try:
            value = json.loads(data)
        except ValueError:
            raise FactoryError('enrolled document is not JSON') from None
        if exact is not None and (not isinstance(value, dict) or set(value) != set(exact)):
            raise FactoryError('invalid enrolled document: ' + name)
        return value

    def optional(self, name, *, exact=None):
        """An absent optional document is None; a present unsafe one is refused."""
        if not os.path.lexists(self.path(name)):
            return None
        return self.json(name, exact=exact)


def release_document(store, release_id):
    """The exact completed release receipt; identity is rederived, not trusted."""
    root = environnement.completed(Path(store), release_id)
    try:
        document = json.loads((root / 'ready.json').read_text())
    except (OSError, ValueError):
        raise Refused('release receipt unavailable') from None
    if not isinstance(document, dict) or document.get('commit') != release_id[:40]:
        raise Refused('release receipt mismatch')
    identity = hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()
    if document.get('commit', '') + '-' + identity != release_id:
        raise Refused('release receipt identity mismatch')
    return document


class HostAdapter:
    """One operation's exact adapter; every receipt is request + {'ok': bool}.

    `ok: false` is a definitive refusal the controller may recover from,
    anything else is uncertain and raises so the durable intent reconciles.
    """

    DEFINITIVE = (configuration.ConfigurationError, verification.VerificationError,
                  environnement.ReleaseError, Refused)

    def __init__(self, *, db, authority, destinations, card, policy,
                 store, configuration_journal, transport_enrollment, system,
                 root='/', recovery_set=None, health=None, probe=None):
        self.db, self.authority = db, authority
        self.destinations, self.card = destinations, card
        self.policy, self.store = policy, Path(store)
        self.configuration_journal = Path(configuration_journal)
        self.system = system
        self.recovery_set, self.health = recovery_set, health
        # Enrolled probe agent of the host: recorded on the fence so the
        # controller's health turn stays admissible on a drained host.
        self.probe = probe
        self.fence = fences.SignedReleaseFence(db, authority)
        self.files = configuration.ConfigurationFiles(
            self.configuration_journal, destinations, authority=authority, gate=self.fence, root=root)
        self.host_configuration = verification.HostConfiguration(self.files, card, system=system)
        self.transport = transport.SSHRestartTransport(transport_enrollment)
        self._releases = {}

    def release(self, request):
        release_id = request['manifest']['release']
        document = self._releases.get(release_id)
        if document is None:
            document = release_document(self.store, release_id)
            self._releases[release_id] = document
        return document

    def fenced(self, request):
        return self.fence.fenced(request) is True

    def perform(self, step, request):
        request = json.loads(json.dumps(request))
        if step not in REQUIRED or request.get('step') != step:
            raise FactoryError('invalid host action')
        if self.authority.validate(request['operation'], request['manifest_digest'], step) is not True:
            # Authority is rechecked before every effect; a denial holds.
            raise FactoryError('host action authority denied')
        try:
            if step in ('fence', 'unfence', 'recovery_fence', 'recovery_unfence'):
                return self.fence.perform(step, request, drain_seconds=DRAIN_SECONDS,
                                          probe=self.probe)
            if step in ('restart', 'recovery_restart'):
                return self.transport.perform(step, request)
            if step in ('health', 'recovery_health'):
                if self.health is None:
                    raise FactoryError('health capability unavailable')
                return self._complete(request, self.health.perform(step, request, self.release(request)))
            if step in ('backup', 'pin', 'accept'):
                if self.recovery_set is None:
                    raise FactoryError('recovery set capability unavailable')
                if step == 'pin':
                    return self._pin(request)
                return self._complete(request, self.recovery_set.perform(step, request, self.release(request)))
            return self._complete(request, self._intrinsic(step, request))
        except self.DEFINITIVE:
            return dict(request, ok=False)
        except FactoryError:
            raise
        except Exception:
            # Provider/credential text (and any partial effect) stays private.
            raise FactoryError('host action unavailable; reconcile') from None

    @staticmethod
    def _complete(request, value):
        # A bound capability answers True (verified completion) or False
        # (definitive refusal). Anything else is an ambiguous result to reconcile.
        if value is False:
            return dict(request, ok=False)
        if value is not True:
            raise FactoryError('host action incomplete')
        return dict(request, ok=True)

    def _pin(self, request):
        """One pin step binds every durable prerequisite, not just the dump.

        `configure` refuses without the private configuration journal, and
        `activate`/`rollback` refuse without the operation's pinned release
        pair. All three pins are therefore performed here, before the fence,
        and each one is idempotent so a lost reply can be replayed safely.
        """
        release = self.release(request)
        receipt = self._complete(request, self.recovery_set.perform('pin', request, release))
        if receipt['ok'] is not True:
            return receipt
        self.files.perform(request, release)
        environnement.pin_switch(self.store, request['operation'], request['manifest']['release'])
        return receipt

    def _intrinsic(self, step, request):
        release = self.release(request)
        manifest = request['manifest']
        if step == 'prepare':
            return self._prepare(request, release)
        if step == 'migrate':
            return self._migrate(request, release)
        if step == 'configure':
            return self._configuration(request, release)
        if step == 'rollback':
            return self._rollback(request, release)
        if step == 'activate':
            return self._activate(request)
        raise FactoryError('invalid host action')

    def _prepare(self, request, release):
        manifest = request['manifest']
        if release.get('commit') != manifest['commit']:
            return False
        entries = release.get('configuration')
        if not isinstance(entries, dict) or set(entries) != set(self.destinations):
            return False
        if any(unit not in self.policy['units'] for unit in manifest['units']):
            return False
        if shutil.disk_usage(self.store).free < MINIMUM_FREE_BYTES:
            return False
        return True

    def _migrate(self, request, release):
        from ameesh import migrations
        directory = (self.store / request['manifest']['release'] / 'source'
                     / 'src' / 'ameesh' / 'migrations')
        if not directory.is_dir():
            return False
        try:
            migrations.migrate(self.db, directory=str(directory), additive_only=True)
        except migrations.NonAdditiveMigration:
            return False
        return True

    def _configuration(self, request, release):
        self.files.perform(request, release)
        self.host_configuration.perform(request, release)
        return True

    def _rollback(self, request, release):
        self.files.perform(request, release)
        self.host_configuration.perform(request, release)
        environnement.rollback(self.store, fenced=True, operation=request['operation'])
        return True

    def _activate(self, request):
        environnement.activate(self.store, request['manifest']['release'],
                               fenced=True, operation=request['operation'])
        return True


class HostFactory:
    """Compose enrolled backends; no partial or boolean capability set admits."""

    def __init__(self, db, enrollment, *, documents=None, root='/', store=STORE_ROOT,
                 configuration_journal=CONFIGURATION_JOURNAL, backup_root=BACKUP_ROOT,
                 retention_root=RETENTION_ROOT, system=None, capabilities=None,
                 clock=time.time):
        self.db = db
        self.documents = Documents() if documents is None else documents
        self.root = Path(root).absolute()
        self.store = Path(store)
        self.configuration_journal = Path(configuration_journal)
        self.backup_root, self.retention_root = Path(backup_root), Path(retention_root)
        self.system = verification.Systemd() if system is None else system
        self.capabilities = {} if capabilities is None else dict(capabilities)
        self.clock = clock
        self.enrollment = json.loads(json.dumps(enrollment))
        self.card = self.destinations = self.policy = self.backup_config = None
        self.companions = {}
        self._load_documents()

    def _load_documents(self):
        value = self.enrollment
        if (not isinstance(value, dict) or set(value) != {'host', 'machine_id', 'policy', 'database'}
                or not isinstance(value['host'], str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', value['host'])
                or not isinstance(value['machine_id'], str)
                or not re.fullmatch(r'[0-9a-f]{32}', value['machine_id'])
                or not isinstance(value['database'], str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', value['database'])):
            raise FactoryError('exact factory enrollment required')
        self.policy = json.loads(json.dumps(value['policy']))
        restart.validate_policy(self.policy)
        if self.policy['host'] != value['host']:
            raise FactoryError('factory host binding mismatch')
        self.card = verification.validate_card(self.documents.json('card', exact=(
            'units', 'properties', 'guard', 'seconds')))
        self.destinations = self.documents.json('destinations')
        configuration.validate_destinations(self.destinations)
        if any(unit not in self.card['units'] for unit in self.policy['units']):
            raise FactoryError('restart units outside enrolled host card')
        self.transport_enrollment = self.documents.json('transport')
        # The enrolled probe turn is part of admission: without it the health
        # step has no authenticated turn and must stay unbound.
        self.turn_enrollment = self.documents.json(
            'turn', exact=('agent', 'unit', 'account', 'home', 'seconds'))
        # Optional only while a companion is supplied for tests/alternate
        # backends; the pinned recovery companion refuses a missing enrollment.
        self.backup_config = self.documents.optional('backup')

    def _companion(self, name):
        if name in self.companions:
            return self.companions[name]
        if name in self.capabilities:
            # An explicit None marks a deliberately absent backend so a test or
            # a partial installation names the unbound steps.
            supplied = self.capabilities[name]
            if supplied is None:
                self.companions[name] = None
                return None
            self.companions[name] = supplied
            return supplied
        path = Path(__file__).with_name(name + '.py')
        if not path.exists():
            self.companions[name] = None
            return None
        module_value = module(name)
        try:
            if name == 'sauvegarde-release':
                value = module_value.RecoverySet(self.db, store=self.store,
                    root=self.backup_root, retention=self.retention_root,
                    config=self.backup_config)
            else:
                pilot = module('pilote-sante')
                value = module_value.HealthProbe(self.db, store=self.store,
                    card=self.card, system=self.system,
                    turn=pilot.HealthTurn(self.db, enrollment=self.turn_enrollment,
                        host=self.enrollment['host'], system=pilot.TurnRunner()))
        except Exception as exc:
            # Une absence de parseur libpq (pilote psql) est un refus NOMMÉ,
            # fail-closed : la cible ne peut pas être vérifiée, l'hôte n'est
            # pas admis, et le motif reste lisible au lieu d'« unavailable ».
            refusal = getattr(module_value, 'LibpqParserUnavailable', None)
            if isinstance(refusal, type) and isinstance(exc, refusal):
                raise FactoryError('pinned host capability refused: %s: %s'
                                   % (name, exc)) from None
            raise FactoryError('pinned host capability unavailable: ' + name) from None
        if not callable(getattr(value, 'perform', None)):
            raise FactoryError('pinned host capability incomplete: ' + name)
        self.companions[name] = value
        return value

    def admit(self, enrollment):
        if json.loads(json.dumps(enrollment)) != self.enrollment:
            raise FactoryError('factory enrollment changed')
        self._tree(self.store, private=False, traversable=True)
        self._tree(self.configuration_journal, private=True, create=True)
        self._tree(self.backup_root, private=True, create=True)
        self._tree(self.retention_root, private=True, create=True)
        self._system_executables()
        # The SSH transport revalidates its private endpoint files here.
        try:
            transport.SSHRestartTransport(self.transport_enrollment)
        except transport.TransportError:
            raise FactoryError('enrolled SSH restart transport unavailable') from None
        missing = set()
        for name, steps in COMPANIONS.items():
            if self._companion(name) is None:
                missing.update(steps)
        if missing:
            raise FactoryError('missing pinned host capability: ' + ','.join(sorted(missing)))
        try:
            self.db.ping()
            self.db.query('SELECT count(*) AS fences FROM host_release_fence_receipts')
        except Exception:
            raise FactoryError('independent release database unavailable') from None
        return frozenset(REQUIRED)

    def _tree(self, path, *, private, create=False, traversable=False):
        path = Path(path)
        if create and not path.exists():
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        trusted(path)
        if not path.is_dir() or path.is_symlink():
            raise FactoryError('enrolled private state required')
        mode = path.stat().st_mode
        if private and mode & 0o077:
            raise FactoryError('enrolled private state required')
        if traversable and not mode & 0o001:
            # The release store is 0711: the enrolled executor traverses it to
            # run the active release's own runner; nothing else is relaxed.
            raise FactoryError('release store not traversable by the executor')

    def _system_executables(self):
        for name in ('systemctl', 'analyze'):
            value = getattr(self.system, name, None)
            if not isinstance(value, str) or not Path(value).is_absolute():
                raise FactoryError('fixed system executables required')
            trusted(Path(value))

    def for_operation(self, job, authority):
        if not isinstance(job, dict) or not job.get('operation'):
            raise FactoryError('typed host operation required')
        missing = set()
        for name, steps in COMPANIONS.items():
            if self._companion(name) is None:
                missing.update(steps)
        if missing:
            raise FactoryError('missing pinned host capability: ' + ','.join(sorted(missing)))
        return HostAdapter(db=self.db, authority=authority,
            destinations=self.destinations, card=self.card, policy=self.policy,
            store=self.store, configuration_journal=self.configuration_journal,
            transport_enrollment=self.transport_enrollment, system=self.system, root=self.root,
            recovery_set=self._companion('sauvegarde-release'),
            health=self._companion('sante-release'),
            probe=self.turn_enrollment['agent'])
