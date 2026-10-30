#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Prepare host-bound bootstrap inputs outside root; never execute them (#4767).

assemble REQUEST REPOSITORY RUNTIME OUTPUT
fabrique REQUEST OUTPUT

REQUEST is closed JSON: version, host, agent, sha, operation, expires_at,
observation, choices. observation contains host, machine_id, host_key_sha256,
account, home, uid, gid, unit, users, groups, source, observed_at. users and
groups are complete numeric/name inventories, not lists of proposed free IDs.
choices contains account_ids (release/handoff/secrets), controllers (the same
three names, each with source and public_key_file), restart_seconds and
probe_seconds, manager and execution (state/config/release_store/roots/tools/secrets/scope).
manager is explicitly {"kind":"system"} or {"kind":"user","account":"…"}.
Keys are PUBLIC Ed25519 keys in owned 0600 files; no private
key, credential or implicit CIDR is accepted. The registry is checked live.

The runtime must be a supplied isolated, regular-file tree (no pip, download,
or host venv mutation). Sources come from the exact Git object, never checkout
bytes. An empty initial store is measured because bootstrap does not publish
store contents. The generated manifest is preparatory evidence, not factory
enrollment or a signed install mandate. Administrative commands are emitted
as argv arrays with host/account; they are never run. Installation of the
administrator backend itself remains a separately reviewed administrator step.

fabrique accepts exactly {version, documents}, where documents contains the
five existing factory documents (enrollment, hosts, repositories, secrets,
transports), each in its existing closed shape. It validates their cross
references and writes them in a new private directory, without reading any
referenced credential or initiating a transport (#4770).
"""
import argparse
import base64
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time


def module(name):
    spec = importlib.util.spec_from_file_location('preparation_' + name.replace('-', '_'),
                                                Path(__file__).with_name(name + '.py'))
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


backend = module('backend-administrateur')
entry, runtime = backend.entry, backend.runtime
release, channels = entry.delegation, backend.channels.delegation
seal = module('scellement-runtime')
host_executor = module('hote-handoff')
local_dispatch = module('dispatch-local-handoff')
NAMES = ('release', 'handoff', 'secrets')
REQUEST = {'version', 'host', 'agent', 'sha', 'operation', 'expires_at', 'observation', 'choices'}
OBSERVATION = {'host', 'machine_id', 'host_key_sha256', 'account', 'home', 'uid', 'gid',
               'unit', 'users', 'groups', 'source', 'observed_at'}
CHOICES = {'account_ids', 'controllers', 'restart_seconds', 'probe_seconds', 'execution', 'manager'}
EXECUTION = {'state', 'config', 'release_store', 'roots', 'tools', 'secrets', 'scope'}
ADMIN_CODE = '/opt/ameesh-admin/backend'


class PreparationError(ValueError):
    pass


def exact(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise PreparationError('exact fields required')
    return value


def unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise PreparationError('duplicate JSON key')
        value[key] = item
    return value


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def read_private(path, limit=1 << 20):
    path = runtime.trusted(Path(path).absolute())
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_size > limit):
            raise PreparationError('owned private input required')
        value = stream.read(limit + 1)
        if len(value) > limit or runtime.stamp(info) != runtime.stamp(os.fstat(stream.fileno())):
            raise PreparationError('input changed or oversized')
    return value


def git(repository, *args):
    result = subprocess.run(['/usr/bin/git', '-C', str(repository), *args], check=True,
                            capture_output=True, timeout=30,
                            env={'PATH': '/usr/bin:/bin', 'LANG': 'C', 'GIT_CONFIG_NOSYSTEM': '1',
                                 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_NO_REPLACE_OBJECTS': '1'})
    return result.stdout


def validate(request, db, *, now):
    exact(request, REQUEST)
    if type(request['version']) is not int or request['version'] != 1:
        raise PreparationError('version required')
    if not isinstance(request['sha'], str) or not re.fullmatch('[0-9a-f]{40}', request['sha']):
        raise PreparationError('full Git SHA required')
    if (not isinstance(request['operation'], str)
            or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_-]{0,70}', request['operation'])):
        raise PreparationError('operation required')
    if type(request['expires_at']) is not int or request['expires_at'] <= now:
        raise PreparationError('future execution deadline required')
    observed = exact(request['observation'], OBSERVATION)
    choices = exact(request['choices'], CHOICES)
    if observed['host'] != request['host']:
        raise PreparationError('observation host mismatch')
    if (not isinstance(observed['source'], str) or not observed['source'].strip()
            or type(observed['observed_at']) is not int or not 0 < observed['observed_at'] <= now):
        raise PreparationError('attributed dated observation required')
    for field, identity in (('users', 'uid'), ('groups', 'gid')):
        inventory = observed[field]
        if not isinstance(inventory, list) or not inventory:
            raise PreparationError('complete account inventory required')
        for item in inventory:
            exact(item, {'name', identity})
            if (not isinstance(item['name'], str) or not item['name']
                    or type(item[identity]) is not int or item[identity] < 0):
                raise PreparationError('invalid observed account inventory')
        if len({item['name'] for item in inventory}) != len(inventory):
            raise PreparationError('duplicate observed account')
    if {'name': observed['account'], 'uid': observed['uid']} not in observed['users']:
        raise PreparationError('executor identity missing from inventory')
    if type(observed['uid']) is not int or observed['uid'] <= 0:
        raise PreparationError('non-root executor identity required')
    if type(observed['gid']) is not int or observed['gid'] not in {g['gid'] for g in observed['groups']}:
        raise PreparationError('executor group missing from inventory')
    identities = exact(choices['account_ids'], NAMES)
    for ids in identities.values():
        exact(ids, {'uid', 'gid'})
        release.validate_account_ids(ids)
    for field, inventory in (('uid', observed['users']), ('gid', observed['groups'])):
        proposed = [identities[name][field] for name in NAMES]
        if len(set(proposed)) != len(NAMES) or set(proposed) & {item[field] for item in inventory}:
            raise PreparationError('proposed account identities are not free and distinct')
    if any(item['name'] in {'ameesh-' + name for name in NAMES}
           for item in observed['users'] + observed['groups']):
        raise PreparationError('delegated account or group already exists')
    manager = choices['manager']
    if not isinstance(manager, dict) or manager.get('kind') not in ('system', 'user'):
        raise PreparationError('explicit restart manager required')
    exact(manager, {'kind'} if manager['kind'] == 'system' else {'kind', 'account'})
    if manager['kind'] == 'user' and manager['account'] != observed['account']:
        raise PreparationError('restart manager account differs from observed executor')
    execution_choices = exact(choices['execution'], EXECUTION)
    if manager['kind'] != execution_choices['scope']:
        raise PreparationError('restart manager differs from execution scope')
    policy = dict(host=request['host'], units=[observed['unit']], manager=manager,
                  controller_unit='ameesh-release-controller.service',
                  restart_seconds=choices['restart_seconds'])
    # This calls the shared consumer validators and the real registry. #4765
    # selects exactly the observed runner spelling, with no synthesized alias.
    turn = entry.probe(db=db, host=request['host'], agent=request['agent'],
                       account=observed['account'], home=observed['home'],
                       seconds=choices['probe_seconds'], policy=policy)
    if turn['unit'] != observed['unit']:
        raise PreparationError('observed runner unit mismatch')
    execution = dict(host=request['host'], accounts={request['agent']: dict(
        account=observed['account'], home=observed['home'], unit=observed['unit'],
        scope=execution_choices['scope'])},
        **{key: value for key, value in execution_choices.items() if key != 'scope'})
    host_executor.validate_execution(execution)
    if execution['release_store'] != '/opt/ameesh-release/store':
        raise PreparationError('execution must name the sealed release store')
    controllers = exact(choices['controllers'], NAMES)
    access = {}
    for name, value in controllers.items():
        exact(value, {'source', 'public_key_file'})
        key = read_private(value['public_key_file'], 256).decode('ascii').strip()
        candidate = dict(host=request['host'], machine_id=observed['machine_id'],
                         host_key_sha256=observed['host_key_sha256'], runtime_tree_sha256='0' * 64,
                         source_tree_sha256='0' * 64, controller_source=value['source'], controller_key=key)
        release.validate_access(candidate)
        access[name] = dict(controller_source=value['source'], controller_key=key)
    if len({base64.b64decode(item['controller_key'].split()[1], validate=True)
            for item in access.values()}) != len(NAMES):
        raise PreparationError('three distinct controller public keys required')
    return policy, turn, access, execution


def write(path, value):
    with path.open('xb') as stream:
        os.chmod(path, 0o600)
        stream.write((canonical(value) + '\n').encode())


def copy_tree(source, destination):
    before = runtime.inventory(source)
    destination.mkdir(mode=0o700)
    for name in before['dirs']:
        if name != '.':
            (destination / name).mkdir(mode=0o700)
    for name, info in before['files'].items():
        with (destination / name).open('xb') as output:
            measured = runtime.digest_file(Path(source) / name, output=output, remaining=info['size'])
        if measured != info:
            raise PreparationError('tree changed while copying')
        (destination / name).chmod(0o755 if info['mode'] == 0o555 else 0o644)
    if runtime.inventory(destination) != before or runtime.inventory(source) != before:
        raise PreparationError('tree changed while copying')
    return before


def staging_script(manifest, output):
    """A reviewable, fixed-purpose administrator command; never executed here.

    Stages verified bytes only. It does not enroll the backend, install the
    signed bundles, create accounts, or run a harness. Files are copied through
    descriptors and checked after copying; failed/partial stages are retained
    for administrator inspection, never automatically reused or erased.
    """
    document = dict(host=manifest['host'], machine_id=manifest['enrollment']['machine_id'],
                    host_key_sha256=manifest['observation']['host_key_sha256'],
                    payload=str(output / 'payload'), files=manifest['payload_inventory'],
                    directories=manifest['payload_directories'],
                    operations=list(dict.fromkeys(item['operation'] for item in manifest['commands'])))
    return '''#!/usr/bin/python3
# SPDX-License-Identifier: AGPL-3.0-only
# Generated preparation, for administrator review. Staging only.
import base64, hashlib, json, os, pathlib, stat, sys
PLAN = json.loads(''' + repr(canonical(document)) + ''')
if os.geteuid() != 0 or sys.argv[1:]:
    raise SystemExit('root, no arguments required')
if pathlib.Path('/etc/machine-id').read_text().strip() != PLAN['machine_id']:
    raise SystemExit('machine mismatch')
key = pathlib.Path('/etc/ssh/ssh_host_ed25519_key.pub').read_text().split()
if key[0] != 'ssh-ed25519' or hashlib.sha256(base64.b64decode(key[1], validate=True)).hexdigest() != PLAN['host_key_sha256']:
    raise SystemExit('host key mismatch')
root = pathlib.Path('/var/lib/ameesh-release-bootstrap')
def trusted(path):
    for parent in (*reversed(path.parents), path):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise SystemExit('untrusted staging parent')
trusted(root)
for operation in PLAN['operations']:
    target = root / operation
    target.mkdir(mode=0o700)  # Existing and partial stages refuse.
    target.chmod(0o700)
    for name in PLAN['directories']:
        relative = pathlib.Path(name)
        if relative.parts[0] == operation and len(relative.parts) > 1:
            target.joinpath(*relative.parts[1:]).mkdir(mode=0o700)
    for name, expected in sorted(PLAN['files'].items()):
        relative = pathlib.Path(name)
        if relative.parts[0] != operation:
            continue
        source = pathlib.Path(PLAN['payload']) / relative
        destination = target.joinpath(*relative.parts[1:])
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        trusted(destination.parent)
        with os.fdopen(os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 128*1024*1024:
                raise SystemExit('unsafe source')
            digest = hashlib.sha256()
            with destination.open('xb') as sink:
                os.fchmod(sink.fileno(), expected['mode'])
                total = 0
                while True:
                    part = stream.read(65536)
                    if not part:
                        break
                    total += len(part)
                    if total > before.st_size:
                        raise SystemExit('source grew')
                    digest.update(part)
                    sink.write(part)
                sink.flush()
                os.fsync(sink.fileno())
            after = os.fstat(stream.fileno())
            if total != before.st_size or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns) or digest.hexdigest() != expected['sha256']:
                raise SystemExit('copied payload mismatch; partial stage retained')
print('staged verified payload; backend enrollment, inventory and signed preparation remain separate')
'''


def assemble(request, repository, runtime_source, output, *, db, now=None):
    if os.geteuid() == 0:
        raise PreparationError('preparation must run outside root')
    now = int(time.time()) if now is None else now
    policy, turn, access, execution = validate(request, db, now=now)
    sha = request['sha']
    if git(repository, 'rev-parse', '--verify', sha + '^{commit}').decode().strip() != sha:
        raise PreparationError('Git commit mismatch')
    # NUL records prevent quoting or newline tricks. Only direct regular .py
    # companions are included, exactly as backend.code_digest measures them.
    records = git(repository, 'ls-tree', '-z', sha + ':deploy/mise-a-jour').split(b'\0')
    sources = {}
    for record in filter(None, records):
        metadata, raw_name = record.split(b'\t', 1)
        mode, kind, oid = metadata.split()
        name = raw_name.decode('ascii')
        if name.endswith('.py'):
            if mode not in (b'100644', b'100755') or kind != b'blob' or not re.fullmatch('[A-Za-z0-9_-]+\\.py', name):
                raise PreparationError('regular source companion required')
            sources[name] = git(repository, 'cat-file', 'blob', oid.decode())
    if 'backend-administrateur.py' not in sources or 'scellement-runtime.py' not in sources:
        raise PreparationError('required source companions missing')
    # The rendering implementation must match the pinned source bytes. Never
    # import or execute unreviewed code extracted from a caller's repository.
    for name, data in sources.items():
        local = Path(__file__).with_name(name)
        if not local.is_file() or local.read_bytes() != data:
            raise PreparationError('run the preparer from the pinned source revision')
    unit_sources = {}
    for name in ('ameesh-handoff-local.socket', 'ameesh-handoff-local@.service'):
        unit_sources[name] = git(repository, 'show', sha + ':deploy/mise-a-jour/systemd/' + name)
        if Path(__file__).with_name('systemd').joinpath(name).read_bytes() != unit_sources[name]:
            raise PreparationError('run the preparer from the pinned systemd revision')
    output = Path(output).absolute()
    runtime.trusted(output.parent)
    source = Path(runtime_source).absolute()
    if output == source or source in output.parents or output in source.parents:
        raise PreparationError('output must be separate from runtime input')
    output.mkdir(mode=0o700)  # Never overwrite or resume a partial preparation.
    output.chmod(0o700)
    try:
        payload = output / 'payload'
        payload.mkdir(mode=0o700)
        release_stage = payload / (request['operation'] + '-release')
        release_stage.mkdir(mode=0o700)
        runtime_manifest = copy_tree(source, release_stage / 'runtime')
        if 'bin/python' not in runtime_manifest['files'] or 'pyvenv.cfg' not in runtime_manifest['files']:
            raise PreparationError('isolated runtime interpreter and configuration required')
        if runtime_manifest['files']['bin/python']['mode'] != 0o555:
            raise PreparationError('executable runtime interpreter required')
        if any(Path(name).suffix.lower() == '.pth' for name in runtime_manifest['files']):
            raise PreparationError('runtime path injection refused')
        settings = {}
        for line in (release_stage / 'runtime/pyvenv.cfg').read_text().splitlines():
            if '=' in line:
                key, value = (part.strip() for part in line.split('=', 1))
                if key in settings:
                    raise PreparationError('ambiguous runtime configuration')
                settings[key] = value
        if settings.get('include-system-site-packages') != 'false' or not settings.get('home', '').startswith('/'):
            raise PreparationError('isolated runtime configuration required')
        lib = release_stage / 'sources'
        lib.mkdir(mode=0o700)
        for name, data in sources.items():
            (lib / name).write_bytes(data)
            (lib / name).chmod(0o644)
        (lib / 'systemd').mkdir(mode=0o700)
        for name, data in unit_sources.items():
            (lib / 'systemd' / name).write_bytes(data)
            (lib / 'systemd' / name).chmod(0o644)
        source_manifest = runtime.inventory(lib)
        store = output / 'store'
        store.mkdir(mode=0o700)
        store_info = store.lstat()
        if not stat.S_ISDIR(store_info.st_mode) or store_info.st_uid != os.geteuid() or list(store.iterdir()):
            raise PreparationError('initial store is not observed empty')
        empty = dict(version=1, dirs=['.'], files={}, bytes=0)
        empty['digest'] = seal.identity(empty)
        runtime_seal = dict(version=1, sha=sha, machine_id=request['observation']['machine_id'],
                            trees=dict(runtime=runtime_manifest, lib=source_manifest, store=empty))
        seal.validate(runtime_seal)
        documents = output / 'documents'
        documents.mkdir(mode=0o700)
        local_rules = dict(uid=request['observation']['uid'], host=request['host'], agents=[request['agent']])
        local_dispatch.consistent_policy(local_rules, execution)
        document_values = {
            release.TURN_DOCUMENT: turn, release.SEAL_DOCUMENT: runtime_seal,
            '/etc/ameesh-handoff/execution.json': execution,
            '/etc/ameesh-handoff/local.json': local_rules}
        for destination, value in document_values.items():
            # Enrollment consumers require compact sorted JSON without an
            # extra newline; write() is for ordinary preparation documents.
            path = documents / Path(destination).name
            with path.open('xb') as stream:
                os.chmod(path, 0o600)
                stream.write(canonical(value).encode())
        release.enrollment_documents({path: canonical(value) for path, value in document_values.items()
                                      if path in release.ENROLLMENT_DOCUMENTS})
        common = dict(host=request['host'], machine_id=request['observation']['machine_id'],
                      host_key_sha256=request['observation']['host_key_sha256'],
                      runtime_tree_sha256=runtime_manifest['digest'], source_tree_sha256=source_manifest['digest'])
        release_plan = dict(common, **access['release'], policy=policy)
        channel_plan = dict(common, channels={name: access[name] for name in ('handoff', 'secrets')})
        ids = request['choices']['account_ids']
        release_value = dict(plan=release_plan, account_ids=ids['release'], expires_at=request['expires_at'],
                             probe_turn=turn, runtime_seal=runtime_seal)
        channel_value = dict(plan=channel_plan, account_ids={name: ids[name] for name in ('handoff', 'secrets')},
                             expires_at=request['expires_at'], delegation='canaux')
        destinations = {
            'release': entry.destinations(plan=release_plan, account_ids=ids['release'],
                                          probe_turn=turn, runtime_seal=runtime_seal),
            'canaux': sorted(channels.render(channel_plan, account_ids=channel_value['account_ids'])['files'])}
        write(release_stage / 'plan.json', release_value)
        channel_stage = payload / (request['operation'] + '-canaux')
        channel_stage.mkdir(mode=0o700)
        write(channel_stage / 'plan.json', channel_value)
        enrollment = dict(version=1, host=request['host'], machine_id=common['machine_id'],
                          code_sha256=backend.code_digest(lib))
        write(output / 'backend.json', enrollment)
        commands = []
        for kind, directory in (('release', release_stage), ('canaux', channel_stage)):
            operation = directory.name
            commands.append(dict(host=request['host'], account='root', operation=operation,
                inventory=['/usr/bin/python3', ADMIN_CODE + '/backend-administrateur.py', 'inventory', operation],
                prepare=['/usr/bin/python3', ADMIN_CODE + '/backend-administrateur.py', 'prepare', operation]))
        payload_inventory = {str(path.relative_to(payload)): dict(
            sha256=backend.hashlib.sha256(path.read_bytes()).hexdigest(),
            mode=stat.S_IMODE(path.stat().st_mode))
            for path in sorted(payload.rglob('*')) if path.is_file()}
        manifest = dict(version=1, kind='handoff-preparation', host=request['host'], agent=request['agent'],
                        sha=sha, observation=request['observation'], choices=request['choices'],
                        runtime_seal=runtime_seal, enrollment=enrollment, destinations=destinations,
                        payload_inventory=payload_inventory,
                        payload_directories=sorted(str(path.relative_to(payload))
                            for path in payload.rglob('*') if path.is_dir()),
                        documents={destination: dict(source=str(documents / Path(destination).name),
                            mode=0o600, sha256=backend.hashlib.sha256(canonical(value).encode()).hexdigest())
                            for destination, value in document_values.items()},
                        commands=commands, prerequisites=[
                            'Administrator verifies the attributed observation against the live host, full account inventories and exact unused IDs.',
                            'Administrator separately enrolls the measured backend sources outside agent-writable paths at ' + ADMIN_CODE + ', backend.json and its existing private controller config.',
                            'Administrator verifies payload hashes, machine-id, pinned host key and source path before staging; no command here was executed.',
                            'Administrator provides /var/lib/ameesh-release-bootstrap as an existing root-owned directory without group/other write; staging refuses an absent or untrusted root.',
                            'Existing runtime, lib and store must be absent; the bootstrap creates an empty initial store.',
                            'Release bootstrap precedes channel bootstrap; each prepare renders a separate question for a scoped signature.',
                            'execution.json and local.json are prepared for a separate reviewed enrollment; bootstrap does not install them.',
                            'H7 factory host/repository/secret-reference/transport documents require their own complete reviewed enrollment; this manifest installs none.'])
        script = output / 'stage-administrateur.py'
        with script.open('xb') as stream:
            os.chmod(script, 0o600)
            stream.write(staging_script(manifest, output).encode())
        manifest['staging'] = dict(host=request['host'], account='root',
            argv=['/usr/bin/python3', str(script)], sha256=backend.hashlib.sha256(script.read_bytes()).hexdigest())
        manifest['digest'] = seal.identity(manifest)
        write(output / 'manifest.json', manifest)
        return manifest
    except BaseException:
        shutil.rmtree(output)
        raise


def assemble_factory(request, output):
    """Render the factory's actual documents, with no alternate schema."""
    if os.geteuid() == 0:
        raise PreparationError('preparation must run outside root')
    factory = module('fabrique-handoff')
    exact(request, {'version', 'documents'})
    if type(request['version']) is not int or request['version'] != 1:
        raise PreparationError('version required')
    documents = exact(request['documents'], factory.DOCUMENTS)
    for name, fields in factory.DOCUMENTS.items():
        exact(documents[name], fields)
        if len(canonical(documents[name]).encode()) > 65536:
            raise PreparationError('factory document exceeds consumer limit')
    enrollment = documents['enrollment']
    if (type(enrollment['version']) is not int or enrollment['version'] != 1
            or not factory._name(enrollment['host'])):
        raise PreparationError('factory host identity required')
    hosts = factory._hosts(documents['hosts']['hosts'])
    if enrollment['host'] not in hosts:
        raise PreparationError('local factory host is not admitted')
    factory._repositories(documents['repositories']['repositories'])
    references = factory._references(documents['secrets']['references'])
    for harnesses in hosts.values():
        for admitted in harnesses.values():
            if not set(admitted['secrets']) <= set(references):
                raise PreparationError('factory secret alias has no reference')
    # Paths are validated against the final enrolled root, never the temporary
    # preparation directory. No identity file or secret store is opened.
    factory._transports(documents['transports']['transports'], hosts, enrollment['host'], factory.ENROLLMENT_ROOT)
    output = Path(output).absolute()
    runtime.trusted(output.parent)
    output.mkdir(mode=0o700)
    output.chmod(0o700)
    try:
        for name, document in documents.items():
            write(output / (name + '.json'), document)
        reader = factory.Documents(output)
        for name, fields in factory.DOCUMENTS.items():
            if reader.json(name, exact=fields) != documents[name]:
                raise PreparationError('factory document changed during preparation')
        manifest = dict(version=1, host=enrollment['host'], files={
            str(factory.ENROLLMENT_ROOT / (name + '.json')): dict(
                source=str(output / (name + '.json')), mode=0o600,
                sha256=backend.hashlib.sha256((output / (name + '.json')).read_bytes()).hexdigest())
            for name in factory.DOCUMENTS})
        manifest['digest'] = seal.identity(manifest)
        write(output / 'manifest-fabrique.json', manifest)
        return manifest
    except BaseException:
        shutil.rmtree(output)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    assembly = commands.add_parser('assemble')
    for name in ('request', 'repository', 'runtime', 'output'):
        assembly.add_argument(name)
    factory = commands.add_parser('fabrique')
    factory.add_argument('request')
    factory.add_argument('output')
    args = parser.parse_args(argv)
    try:
        request = json.loads(read_private(args.request), object_pairs_hook=unique)
        if args.command == 'fabrique':
            manifest = assemble_factory(request, args.output)
            print(canonical(dict(host=manifest['host'], digest=manifest['digest'])))
            return 0
        from ameesh.config import load
        from ameesh.db import connect
        manifest = assemble(request, args.repository, args.runtime, args.output, db=connect(load()))
        print(canonical(dict(host=manifest['host'], digest=manifest['digest'])))
        return 0
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        print('preparation refused; verify exact inputs and pinned source revision', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
