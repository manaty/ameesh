#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Separate signed, create-only enrollment of the independent release service.

Root stages seven private documents; prepare prints hashes and an exact signing
question, never their contents. A distinct signed phase generates only a new
host-local transport key; no existing credential is copied or moved. The permanent bootstrap lock serializes publication/start/stop. There
is no caller-selected destination, program, unit, database or test root in CLI.
"""
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import re
import stat
import sys
import time


def module(name):
    spec = importlib.util.spec_from_file_location('enrollment_' + name.replace('-', '_'),
                                                  Path(__file__).with_name(name + '.py'))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


admin = module('backend-administrateur')
service = module('service-release')
factory = module('adaptateur-hote-release')
backup = module('sauvegarde-release')
pilot = module('pilote-sante')
unit = module('unite-controleur')
seal = module('scellement-runtime')
protocol, runtime = admin.protocol, admin.runtime
STAGE = '/var/lib/ameesh-release-enrollment'
CONFIG = '/etc/ameesh-release'
UNIT = '/etc/systemd/system/ameesh-release-controller.service'
LOCK = '/etc/ameesh-release/runtime-seal.lock'
FILES = ('enrollment', 'database', 'service', 'card', 'destinations', 'transport', 'backup')
DIRECTORIES = ('/etc/ameesh-release/operations', '/var/lib/ameesh-release/controller',
               '/var/lib/ameesh-release/configuration', '/var/lib/ameesh-release/backup',
               '/var/lib/ameesh-release/retention')
PREFIX = 'Approve the exact independent release controller enrollment below:\n'
KEY_PREFIX = 'Approve generating this host-local release transport key below:\n'
STOP_PREFIX = 'Approve stopping this exact enrolled release controller below:\n'
OPTIONS = [{'key': 'enroll', 'label': 'Enroll and start this exact independent controller'},
           {'key': 'leave', 'label': 'Leave this host unchanged'}]
STOP_OPTIONS = [{'key': 'stop', 'label': 'Stop this exact controller; retain documents and journals'},
                {'key': 'leave', 'label': 'Leave this host unchanged'}]
KEY_OPTIONS = [{'key': 'keygen', 'label': 'Generate this new private host-local transport key'},
               {'key': 'leave', 'label': 'Leave this host unchanged'}]


class EnrollmentError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise EnrollmentError('ambiguous private document')
        result[key] = value
    return result


@contextlib.contextmanager
def isolated_key(system, path, account):
    try:
        yield
    finally:
        # Readability is probed even after a refused mandate or uncertain reply.
        # This observes permissions; no key bytes cross to the executor.
        system.probe_key(path, account)


def read(path):
    runtime.trusted(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                or before.st_size > 65536):
            raise EnrollmentError('private regular document required')
        data = stream.read(65537)
        if len(data) > 65536 or runtime.stamp(before) != runtime.stamp(os.fstat(stream.fileno())):
            raise EnrollmentError('private document changed')
        if runtime.stamp(before) != runtime.stamp(path.lstat()):
            raise EnrollmentError('private document substituted')
    return data


def document(data):
    try:
        return json.loads(data, object_pairs_hook=unique)
    except (ValueError, UnicodeError):
        raise EnrollmentError('invalid private JSON') from None


def question(operation, bundle, expires_at, *, stop=False):
    if (type(expires_at) is not int or expires_at != bundle['expires_at']
            or operation != bundle['operation']):
        raise EnrollmentError('exact authority window required')
    return (STOP_PREFIX if stop else PREFIX) + canonical(bundle)


class SignedEnrollmentAuthority(admin.bootstrap.SignedBootstrapAuthority):
    steps, option, options, prefix = ('publish', 'admit', 'start', 'verify'), 'enroll', OPTIONS, PREFIX

    def __init__(self, db, decision_id, operation, bundle, *, clock=time.time):
        if type(decision_id) is not int or decision_id <= 0:
            raise EnrollmentError('signed enrollment decision required')
        self.db, self.decision_id, self.operation, self.clock = db, decision_id, operation, clock
        self.bundle = json.loads(canonical(bundle))
        self.scopes = {step: protocol.identity(self.bundle) for step in self.steps}

    def render(self, operation, expires_at):
        return question(operation, self.bundle, expires_at)


class SignedStopAuthority(SignedEnrollmentAuthority):
    steps, option, options, prefix = ('stop',), 'stop', STOP_OPTIONS, STOP_PREFIX

    def render(self, operation, expires_at):
        return question(operation, self.bundle, expires_at, stop=True)


class SignedKeyAuthority(SignedEnrollmentAuthority):
    steps, option, options, prefix = ('keygen',), 'keygen', KEY_OPTIONS, KEY_PREFIX

    def render(self, operation, expires_at):
        if operation != self.bundle['operation'] or expires_at != self.bundle['expires_at']:
            raise EnrollmentError('exact key generation scope required')
        return KEY_PREFIX + canonical(self.bundle)


class System:
    def generate_key(self, path):
        runtime.trusted('/usr/bin/ssh-keygen')
        admin.installer.account.LinuxAccounts.command([
            '/usr/bin/ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', '', '-f', str(path)])
        path.chmod(0o600)
        path.with_name(path.name + '.pub').chmod(0o600)

    def public_key(self, path):
        read(path)  # Root-only, single link, bounded; bytes are never returned.
        runtime.trusted('/usr/bin/ssh-keygen')
        text = admin.installer.account.LinuxAccounts.command([
            '/usr/bin/ssh-keygen', '-y', '-f', str(path)])
        fields = text.split()
        if len(fields) != 2 or fields[0] != 'ssh-ed25519':
            raise EnrollmentError('host-local Ed25519 key required')
        return ' '.join(fields)

    def probe_key(self, path, account):
        import subprocess
        runtime.trusted('/usr/sbin/runuser')
        runtime.trusted('/usr/bin/test')
        result = subprocess.run(['/usr/sbin/runuser', '--user', account, '--',
                                 '/usr/bin/test', '-r', str(path)],
                                capture_output=True, timeout=30,
                                env={'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C'})
        if result.returncode != 1:
            raise EnrollmentError('transport key isolation probe refused')

    def state(self):
        text = admin.installer.account.LinuxAccounts.command([
            '/usr/bin/systemctl', 'show', 'ameesh-release-controller.service',
            '--property=LoadState,ActiveState,SubState,MainPID,InvocationID'])
        return dict(line.split('=', 1) for line in text.splitlines() if '=' in line)

    def command(self, action):
        if action not in ('daemon-reload', 'start', 'stop'):
            raise EnrollmentError('fixed service action required')
        args = ['/usr/bin/systemctl', action]
        if action != 'daemon-reload':
            args.append('ameesh-release-controller.service')
        admin.installer.account.LinuxAccounts.command(args)

    def admit(self):
        # Connection and factory use precisely the installed service documents.
        from ameesh import config, db as db_mod
        cfg = config.load({'AMEESH_CONFIG': CONFIG + '/database.json'})
        db = db_mod.connect(cfg, driver='psycopg')
        try:
            enrollment = document(read(Path(CONFIG) / 'enrollment.json'))
            source = Path('/opt/ameesh-release/lib/adaptateur-hote-release.py')
            runtime.trusted(source)
            spec = importlib.util.spec_from_file_location('enrolled_controller_factory', source)
            pinned = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(pinned)
            value = pinned.HostFactory(db, enrollment)
            if set(value.admit(enrollment)) != service.REQUIRED:
                raise EnrollmentError('complete adapter admission required')
        finally:
            db.close()

    def ready(self, bundle):
        request = dict(host=bundle['host'], machine_id=bundle['machine_id'],
                       plan=dict(bundle['pins'], policy=bundle['policy']))
        return admin.installer.LinuxHost().ready(request) is True


class Enrollment:
    def __init__(self, backend, *, system=None, clock=time.time):
        self.backend, self.system, self.clock = backend, system or System(), clock

    def path(self, value):
        return self.backend.path(value)

    def stage(self, operation):
        # Reuse the native typed-operation validator without its stage root.
        self.backend.stage(operation)
        return self.path(STAGE) / operation

    def key_scope(self, operation):
        stage = self.stage(operation)
        runtime.trusted(stage)
        if not stage.is_dir() or stat.S_IMODE(stage.stat().st_mode) != 0o700:
            raise EnrollmentError('private enrollment stage required')
        spec = document(read(stage / 'input.json'))
        if (not isinstance(spec, dict) or set(spec) != {'version', 'expires_at'}
                or type(spec['version']) is not int or spec['version'] != 1
                or type(spec['expires_at']) is not int or spec['expires_at'] <= self.clock()):
            raise EnrollmentError('exact unexpired input required')
        turn_path = self.path(CONFIG + '/turn.json')
        turn = pilot.validate_enrollment(document(read(turn_path)))
        executor = pwd.getpwnam(turn['account'])
        if executor.pw_uid == 0:
            raise EnrollmentError('separate unprivileged executor required')
        key = self.backend.host_key()
        if not isinstance(key, str) or re.fullmatch(r'[0-9a-f]{64}', key) is None:
            raise EnrollmentError('host key unavailable')
        return dict(version=1, operation=operation, host=self.backend.enrollment['host'],
                    machine_id=self.backend.machine(), host_key_sha256=key,
                    code_sha256=self.backend.gestes.code, expires_at=spec['expires_at'],
                    private_path=STAGE + '/' + operation + '/transport/identity',
                    executor_account=turn['account'], executor_uid=executor.pw_uid,
                    turn_sha256=sha(read(turn_path)), backup='excluded; never export; re-enroll if lost')

    def keygen(self, operation, decision):
        scope = self.key_scope(operation)
        path = self.path(scope['private_path'])
        with isolated_key(self.system, path, scope['executor_account']), \
                seal.locked(self.path(LOCK), exclusive=True, uid=os.geteuid()):
            if self.key_scope(operation) != scope:
                raise EnrollmentError('key scope changed while waiting for lock')
            authority = SignedKeyAuthority(self.backend.db, decision, operation, scope, clock=self.clock)
            digest = protocol.identity(scope)
            def approved():
                if authority.validate(operation, digest, 'keygen') is not True:
                    raise EnrollmentError('live signed key generation authority required')
            approved()
            journal = self.stage(operation) / 'keygen.json'
            state = document(read(journal)) if os.path.lexists(journal) else None
            if state is not None:
                if state.get('digest') != digest or state.get('status') != 'complete':
                    raise EnrollmentError('key generation uncertain; retain artifacts')
                self.system.probe_key(path, scope['executor_account'])
                public = self.system.public_key(path)
                if public != state['public_key']:
                    raise EnrollmentError('host-local transport key changed')
                approved()
                return dict(public_key=public, public_key_sha256=sha(public.encode()))
            if os.path.lexists(path.parent):
                raise EnrollmentError('existing transport key directory refused')
            approved()
            protocol.save(journal, dict(digest=digest, status='intent', decision_id=decision))
            path.parent.mkdir(mode=0o700)
            runtime.fsync_directory(path.parent.parent)
            approved()
            self.system.generate_key(path)
            public = self.system.public_key(path)
            approved()
            protocol.save(journal, dict(digest=digest, status='complete', decision_id=decision,
                                       public_key=public, public_key_sha256=sha(public.encode())))
            return dict(public_key=public, public_key_sha256=sha(public.encode()))

    def inputs(self, operation):
        stage = self.stage(operation)
        runtime.trusted(stage)
        if not stage.is_dir() or stat.S_IMODE(stage.stat().st_mode) != 0o700:
            raise EnrollmentError('private enrollment stage required')
        values = {name: read(stage / (name + '.json')) for name in FILES}
        data = {name: document(value) for name, value in values.items()}
        spec = document(read(stage / 'input.json'))
        if (not isinstance(spec, dict) or set(spec) != {'version', 'expires_at'}
                or type(spec['version']) is not int or spec['version'] != 1
                or type(spec['expires_at']) is not int or spec['expires_at'] <= self.clock()):
            raise EnrollmentError('exact unexpired input required')
        self.validate(data)
        self.transport_key(operation, data)
        return stage, spec, data, values

    def transport_key(self, operation, data):
        path = self.stage(operation) / 'transport/identity'
        record = document(read(self.stage(operation) / 'keygen.json'))
        if (record.get('status') != 'complete'
                or data['transport']['identity_file'] != str(path)):
            raise EnrollmentError('new host-local transport key required')
        self.system.probe_key(path, document(read(self.path(CONFIG + '/turn.json')))['account'])
        public = self.system.public_key(path)
        if (record.get('public_key') != public
                or record.get('public_key_sha256') != sha(public.encode())):
            raise EnrollmentError('transport public key changed')
        return dict(public_key=public, public_key_sha256=sha(public.encode()))

    def validate(self, data):
        enrollment, pins = data['enrollment'], data['service']
        if (not isinstance(enrollment, dict)
                or set(enrollment) != {'host', 'machine_id', 'policy', 'database'}
                or enrollment['host'] != self.backend.enrollment['host']
                or enrollment['machine_id'] != self.backend.machine()
                or not isinstance(enrollment['database'], str)
                or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', enrollment['database']) is None):
            raise EnrollmentError('enrollment host binding refused')
        factory.restart.validate_policy(enrollment['policy'])
        if enrollment['policy']['host'] != enrollment['host']:
            raise EnrollmentError('policy host mismatch')
        if (not isinstance(pins, dict) or set(pins) != {'runtime_tree_sha256', 'source_tree_sha256'}
                or any(not isinstance(value, str) or re.fullmatch(r'[0-9a-f]{64}', value) is None
                       for value in pins.values())):
            raise EnrollmentError('exact immutable pins required')
        database = data['database']
        if (not isinstance(database, dict) or set(database) != {'backend', 'dsn', 'schema'}
                or database['backend'] != 'postgres' or not isinstance(database['schema'], str)
                or re.fullmatch(r'[a-z_][a-z0-9_]{0,62}', database['schema']) is None
                or backup.database_target(database['dsn']) is None):
            raise EnrollmentError('explicit independent database required')
        card = factory.verification.validate_card(data['card'])
        if any(name not in card['units'] for name in enrollment['policy']['units']):
            raise EnrollmentError('policy outside host card')
        factory.configuration.validate_destinations(data['destinations'])
        transport = data['transport']
        factory.transport.validate_enrollment(transport, 'ameesh-release')
        if transport['host'] != enrollment['host']:
            raise EnrollmentError('transport host mismatch')
        recovery = backup.configuration(data['backup'])
        if backup.database_target(recovery['dsn']) != backup.database_target(database['dsn']):
            raise EnrollmentError('backup source differs from release database')
        turn = pilot.validate_enrollment(document(read(self.path(CONFIG + '/turn.json'))))
        if turn['unit'] not in enrollment['policy']['units']:
            raise EnrollmentError('probe outside policy')

    def observe(self, data):
        for logical, field in (('/opt/ameesh-release/runtime', 'runtime_tree_sha256'),
                               ('/opt/ameesh-release/lib', 'source_tree_sha256')):
            path = self.path(logical)
            inventory = runtime.inventory(path)
            if (inventory['digest'] != data['service'][field]
                    or any((path / name).stat().st_mode & 0o222
                           for name in inventory['dirs'] + list(inventory['files']))):
                raise EnrollmentError('immutable runtime/source mismatch')
        target = self.path(UNIT)
        runtime.trusted(target)
        if target.read_bytes() != unit.UNIT.encode():
            raise EnrollmentError('exact existing controller unit required')
        for logical in DIRECTORIES:
            path = self.path(logical)
            runtime.trusted(path.parent)
            if os.path.lexists(path):
                runtime.trusted(path)
                if not path.is_dir() or stat.S_IMODE(path.stat().st_mode) != 0o700:
                    raise EnrollmentError('private service state directory required')
        self.quiet_queue()

    def quiet_queue(self):
        operations = self.path(CONFIG + '/operations')
        if operations.exists() or operations.is_symlink():
            runtime.trusted(operations)
            if not operations.is_dir() or list(operations.iterdir()):
                raise EnrollmentError('empty protected operation queue required')
        active = self.path('/var/lib/ameesh-release/controller')
        if active.exists() or active.is_symlink():
            runtime.trusted(active)
            if not active.is_dir() or any(active.glob('*.active.json')):
                raise EnrollmentError('controller reservation present')

    def bundle(self, operation, spec, data, values):
        bundle = dict(version=1, operation=operation, host=data['enrollment']['host'],
                      machine_id=self.backend.machine(), host_key_sha256=self.backend.host_key(),
                      code_sha256=self.backend.gestes.code, expires_at=spec['expires_at'],
                      pins=data['service'], policy=data['enrollment']['policy'],
                      files={CONFIG + '/' + name + '.json': sha(raw) for name, raw in values.items()},
                      turn_sha256=sha(read(self.path(CONFIG + '/turn.json'))),
                      transport_key=self.transport_key(operation, data),
                      unit_sha256=sha(unit.UNIT.encode()), directories=list(DIRECTORIES),
                      service='ameesh-release-controller.service',
                      rollback='separate signed stop; retain documents, state and journals')
        if not isinstance(bundle['host_key_sha256'], str) or re.fullmatch('[0-9a-f]{64}', bundle['host_key_sha256']) is None:
            raise EnrollmentError('host key unavailable')
        return bundle

    def prepare(self, operation):
        stage, spec, data, values = self.inputs(operation)
        self.observe(data)
        if self.system.state().get('ActiveState') not in ('inactive', 'failed'):
            raise EnrollmentError('controller must be stopped before preparation')
        for name in FILES:
            if os.path.lexists(self.path(CONFIG + '/' + name + '.json')):
                raise EnrollmentError('existing enrollment is never overwritten')
        bundle = self.bundle(operation, spec, data, values)
        target = stage / 'bundle.json'
        if os.path.lexists(target):
            if document(read(target)) != bundle:
                raise EnrollmentError('prepared bundle changed')
        else:
            admin.Backend.write(target, canonical(bundle))
        return dict(bundle=bundle, question=question(operation, bundle, spec['expires_at']))

    def load(self, operation):
        stage, spec, data, values = self.inputs(operation)
        bundle = document(read(stage / 'bundle.json'))
        if bundle != self.bundle(operation, spec, data, values):
            raise EnrollmentError('frozen enrollment changed')
        self.observe(data)
        return stage, bundle, values

    def apply(self, operation, decision, *, stop=False):
        # Permanent inode also fences native bootstrap and sealed invocations.
        with seal.locked(self.path(LOCK), exclusive=True, uid=os.geteuid()):
            stage, bundle, values = self.load(operation)
            authority_type = SignedStopAuthority if stop else SignedEnrollmentAuthority
            authority = authority_type(self.backend.db, decision, operation, bundle, clock=self.clock)
            digest = protocol.identity(bundle)

            def approved(step):
                if authority.validate(operation, digest, step) is not True:
                    raise EnrollmentError('live signed enrollment authority required')

            approved('stop' if stop else 'publish')
            journal = stage / 'receipt.json'
            state = document(read(journal)) if os.path.lexists(journal) else dict(
                digest=digest, files=[], intent=None, invocation=None, status='publishing')
            if state['digest'] != digest:
                raise EnrollmentError('another enrollment intent exists')
            # Every replay/stop checks the private destinations again, including
            # a completed enrollment. An old ready receipt cannot bless drift.
            for name in state['files']:
                if name not in values or read(self.path(CONFIG + '/' + name + '.json')) != values[name]:
                    raise EnrollmentError('published private document differs')
            if stop:
                self.quiet_queue()
                current = self.system.state()
                if (state['status'] not in ('ready', 'starting') or not state['invocation']
                        or current.get('InvocationID') != state['invocation']):
                    raise EnrollmentError('only the recorded controller instance may be stopped')
                approved('stop')
                state['intent'] = 'stop'; protocol.save(journal, state)
                self.system.command('stop')
                approved('stop')
                if self.system.state().get('ActiveState') != 'inactive':
                    raise EnrollmentError('controller stop uncertain')
                state.update(status='stopped', intent=None); protocol.save(journal, state)
                return dict(outcome='stopped', documents='retained', journals='retained')
            if state['status'] == 'stopped':
                raise EnrollmentError('stopped enrollment requires a new operation')
            if state['status'] == 'ready':
                approved('verify')
                if self.system.state().get('InvocationID') != state['invocation'] or not self.system.ready(bundle):
                    raise EnrollmentError('recorded service instance changed')
                return dict(outcome='ready', bundle_digest=digest)
            for name, raw in values.items():
                target = self.path(CONFIG + '/' + name + '.json')
                approved('publish')
                if os.path.lexists(target):
                    if name not in state['files'] and state['intent'] != name:
                        raise EnrollmentError('unowned existing enrollment refused')
                    if read(target) != raw:
                        raise EnrollmentError('published private document differs')
                    if name in state['files']:
                        continue  # Preserve any later unresolved publication intent.
                else:
                    if name in state['files']:
                        raise EnrollmentError('published document removed')
                    state['intent'] = name; protocol.save(journal, state)
                    admin.Backend.write(target, raw.decode('utf-8'))
                if name not in state['files']:
                    state['files'].append(name)
                state['intent'] = None; protocol.save(journal, state)
            for logical in DIRECTORIES:
                approved('publish')
                path = self.path(logical)
                if not os.path.lexists(path):
                    runtime.trusted(path.parent)
                    path.mkdir(mode=0o700); runtime.fsync_directory(path.parent)
                runtime.trusted(path)
                if not path.is_dir() or stat.S_IMODE(path.stat().st_mode) != 0o700:
                    raise EnrollmentError('private service state directory required')
            approved('admit')
            self.system.admit()  # Actual complete factory, no readiness written here.
            approved('start')
            current = self.system.state()
            if state['intent'] == 'start':
                # Lost start reply: only observe; never submit a second start.
                if current.get('ActiveState') != 'active':
                    raise EnrollmentError('service start uncertain; reconcile retained intent')
            else:
                if current.get('ActiveState') not in ('inactive', 'failed'):
                    raise EnrollmentError('unexpected active service')
                self.system.command('daemon-reload')
                approved('start')
                state['intent'] = 'start'; protocol.save(journal, state)
                self.system.command('start')
            invocation = self.system.state().get('InvocationID')
            if not isinstance(invocation, str) or re.fullmatch(r'[0-9a-f]{32}', invocation) is None:
                raise EnrollmentError('current service identity unavailable')
            if state['invocation'] is not None and state['invocation'] != invocation:
                raise EnrollmentError('service start instance changed')
            state.update(status='starting', invocation=invocation)
            protocol.save(journal, state)
            deadline = time.monotonic() + 30
            while not self.system.ready(bundle):
                approved('verify')
                if time.monotonic() >= deadline:
                    raise EnrollmentError('controller readiness unavailable; retain start intent')
                time.sleep(0.2)
            approved('verify')
            if self.system.state().get('InvocationID') != invocation:
                raise EnrollmentError('service instance changed during readiness')
            state.update(status='ready', invocation=invocation, intent=None)
            protocol.save(journal, state)
            return dict(outcome='ready', bundle_digest=digest)


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    db = None
    try:
        if (os.geteuid() != 0 or not sys.flags.isolated or not sys.flags.dont_write_bytecode
                or Path(sys.executable) != Path('/opt/ameesh-release/runtime/bin/python')
                or len(args) not in (2, 4)
                or args[0] not in ('key-question', 'keygen', 'prepare', 'enroll', 'stop-question', 'stop')
                or (args[0] in ('keygen', 'enroll', 'stop') and
                    (len(args) != 4 or args[2] != '--decision' or not args[3].isdigit()))
                or (args[0] in ('key-question', 'prepare', 'stop-question') and len(args) != 2)):
            raise EnrollmentError('root key-question|keygen|prepare|enroll|stop-question|stop OPERATION [--decision N] required')
        os.umask(0o077)
        os.environ.clear()
        os.environ.update(PATH='/usr/bin:/bin', LANG='C', LC_ALL='C', HOME='/root')
        # The administrator must enroll the new code digest before any use.
        for path in Path(__file__).parent.glob('*.py'):
            runtime.trusted(path)
        enrollment = document(read(Path(admin.ENROLLMENT)))
        db = admin.connect() if args[0] in ('keygen', 'enroll', 'stop') else None
        native = admin.Backend(system=admin.channel_installer.LinuxChannels(), enrollment=enrollment, db=db)
        tool = Enrollment(native)
        if args[0] == 'key-question':
            scope = tool.key_scope(args[1])
            result = dict(scope=scope, question=KEY_PREFIX + canonical(scope))
        elif args[0] == 'keygen':
            result = tool.keygen(args[1], int(args[3]))
        elif args[0] == 'prepare':
            result = tool.prepare(args[1])
        elif args[0] == 'stop-question':
            _, bundle, _ = tool.load(args[1])
            result = dict(question=question(args[1], bundle, bundle['expires_at'], stop=True))
        else:
            result = tool.apply(args[1], int(args[3]), stop=args[0] == 'stop')
        print(canonical(result))
        return 0
    except Exception:
        # No secret-bearing JSON, DSN, stdout/stderr or exception is printed.
        print('{"error":"controller_enrollment_unavailable"}', file=sys.stderr)
        return 2
    finally:
        if db is not None:
            with contextlib.suppress(Exception):
                db.close()


if __name__ == '__main__':
    sys.exit(main())
