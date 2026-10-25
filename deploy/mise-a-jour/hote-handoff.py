#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""H2/H3: preparation, Git, activation and health effects of a host handoff.

Two authorities share this pinned source and never merge:

- ``HostAdapter`` is the controller-side companion the factory composes. It
  checks the durable operation and the destination preflight, then asks one
  host's executor for a typed effect through ``pont-handoff.py``. It never
  runs a host primitive itself and never fabricates a completion.
- ``HostEffects`` is the host-side executor. It performs one narrow effect for
  an exact enrolled agent, keeps a private durable intent/receipt journal,
  refuses a stale controller epoch or an out-of-order step, and observes an
  interrupted effect before returning a receipt. H2 prepares and transfers
  without starting an executor; H3 ``enable`` starts the destination unit,
  ``admit``/``verify`` observe it, ``verify`` proves an authenticated first
  turn (the controller-side companion reads the durable turn ledger) and
  ``restore_source`` restarts a source unit before cancellation.
  ``reconcile_effect`` quiesces the exact interrupted epoch so a delayed host
  call cannot act after revocation.

The effects implemented here are ``inventory``, ``revalidate``, ``prepare``,
``stop_source``, ``publish``, ``transfer``, ``session``, ``admit``,
``enable``, ``verify``, ``restore_source`` and ``reconcile_effect``.
``recover_source`` is the L232e lost-source recovery and stays an explicit
refusal here; a definitive refusal is an ``ok: false`` receipt (or ``False``
for the two boolean checks), never a simulated success. An uncertain call
raises so the controller keeps its durable intent and requires reconciliation.

No source home, harness cache, OAuth token or credential value is copied:
``publish`` reads Git, ``transfer`` fetches the enrolled repository with the
destination account's own credentials, and ``session`` prepares a new private
marker with no credential file. Every subprocess is bounded and uses fixed
argv with a scrubbed environment; the SSH channel is the pinned
``transport-release.py`` substrate through an explicit bridge (its release
authority is never loaded).
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import pwd
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Mapping

from ameesh import handoff_contract, host_preflight, projects, relocation, storage
from ameesh.config import NAME_RE

ENROLLMENT_ROOT = Path('/etc/ameesh-handoff')
EXECUTION_NAME = 'execution.json'
BRIDGE_NAME = 'pont-handoff.py'
SAFE_PATH = '/usr/local/bin:/usr/bin:/bin'
#: Git may only reach the network transports of an enrolled repository. A
#: local path, ``file://``, ``ext::`` or any other protocol is refused by Git
#: itself even if a caller or an account config tries to rewrite the URL.
GIT_PROTOCOLS = ('ssh', 'https', 'http', 'git')
GIT_SECONDS = 20
STOP_SECONDS = 15.0
#: H3: ``enable`` must conclude inside the controller's 60 s deadline; the
#: systemctl start call itself is short, the unit may take longer to become
#: active, so the host observes it for the remaining budget.
START_SECONDS = 8
ENABLE_SECONDS = 40.0
RESTORE_SECONDS = 30.0
#: The outer dispatcher alarm covers the effect, not only the stdin read. H3
#: enable is the longest effect and must stay below the controller deadline.
SECONDS = 52
LOCK_SECONDS = 10.0
MAX_RECEIPT_BYTES = 65536
#: A terminal turn whose ``turn_costs`` line has not committed yet is still in
#: flight: the runner closes ``turn_resources`` before writing the ledger. Past
#: this bounded window the missing line means the turn never authenticated
#: (missing login, failed before any usage, orphaned runner) and ``verify``
#: must return ``authenticated: false`` instead of staying pending forever.
TURN_SETTLE_SECONDS = 30.0
#: ``O_DIRECTORY`` is absent on some platforms; a pinned directory fd still
#: works without it (the path is checked by the caller's private chain).
_O_DIRECTORY = getattr(os, 'O_DIRECTORY', 0)
#: The destination's first session is fresh for a harness without portable
#: native session storage; the others resume from the controller's brief.
SESSION_MODES = {'deepseek': 'fresh', 'dsh': 'fresh'}
#: Effects are ordered inside one operation. A later step may not run after an
#: earlier one, and a stale controller epoch is refused even when it is the
#: same step. ``revalidate`` is a read-only check and is not journaled.
#: ``restore_source`` is the cancellation path: it is journalled after any
#: pre-switch effect, and no later effect follows a cancelled operation.
STEP_SEQUENCE = {'prepare': 10, 'stop_source': 20, 'publish': 30,
                 'transfer': 40, 'session': 50, 'admit': 60,
                 'enable': 70, 'verify': 80, 'restore_source': 90}
#: The furthest journaled effect a controller state may already have reached.
MAX_SEQUENCE = {'planned': 0, 'prepared': 10, 'drained': 20, 'published': 30,
                'transferred': 40, 'session_ready': 50, 'switched': 60,
                'activated': 80, 'held': 90}
#: The effect a state attempts next. A refused attempt advances the journal's
#: order mark but leaves the controller at the same state, so its retry must
#: still revalidate (the step itself is accepted as a same-step retry).
NEXT_STEP = {'planned': 'prepare', 'drained': 'publish',
             'published': 'transfer', 'transferred': 'session'}
#: H3 effects whose stored receipt must be re-observed on replay: a unit can
#: die, or a source can be stopped, between the first reply and the replay.
REOBSERVE = ('stop_source', 'restore_source', 'enable', 'verify')

_ACCOUNT = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$')
_ALIAS = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$')
_TOOL = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')
_SHA = re.compile(r'^[0-9a-f]{40,64}$')
_UNIT = re.compile(r'ameesh-runner(?:-agent)?@[A-Za-z0-9][A-Za-z0-9_.-]{0,100}\.service')
_PATH = re.compile(r'^/[A-Za-z0-9/_.~-]+$')
_REASON = re.compile(r'^[a-z][a-z0-9_.:-]{0,63}$')

EXECUTION_KEYS = ('host', 'state', 'config', 'release_store', 'accounts',
                  'roots', 'tools', 'secrets')
ACCOUNT_KEYS = ('account', 'home', 'unit', 'scope')
_FENCE_KEYS = ('agent', 'account', 'unit', 'owner', 'lease_epoch')
CREDENTIAL_FILES = ('.credentials.yaml', 'auth.json', '.env', '.oauth',
                    'credentials.json')


class Refused(ValueError):
    """A named capability or prerequisite is absent; the controller holds."""


class Uncertain(ValueError):
    """The effect may have happened; the durable intent must be reconciled."""


class BridgeError(ValueError):
    """The enrolled host bridge is absent, closed or returned a bad receipt."""


# ---------------------------------------------------------------------------
# Private files and the host execution enrollment
# ---------------------------------------------------------------------------

def _bounded(value, limit=128):
    return (isinstance(value, str) and 0 < len(value) <= limit
            and all(32 <= ord(char) != 127 for char in value))


def _owned(info, owner=None):
    return info.st_uid in (os.geteuid(), 0) or (
        owner is not None and info.st_uid == owner[0])


def _trusted_file(path, *, private=False, owner=None):
    """A fixed private regular file: no link, owned by root, the caller or
    the enrolled account whose home holds it."""
    path = Path(path)
    try:
        info = path.lstat()
    except OSError:
        raise Refused('file') from None
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
            or not _owned(info, owner) or info.st_mode & 0o022
            or (private and info.st_mode & 0o027) or info.st_nlink != 1):
        raise Refused('file')
    return path


def _read_private_json(path, *, limit=65536, owner=None, root=None):
    """Read one private regular file through its pinned parent descriptor.

    ``root`` is the trusted anchor (enrollment root, state root, account home
    or enrolled work root): every component below it is opened relative to
    its parent with ``O_NOFOLLOW``. The file itself is checked on the
    descriptor that is actually read, never on a path re-resolved afterwards.
    """
    path = Path(path)
    if root is None or path.name in ('', '.', '..') or '/' in path.name:
        raise Refused('file')
    directory = descriptor = None
    try:
        directory = _open_dir(root, path.parent, owner=owner)
        descriptor = os.open(path.name,
                             os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory)
        info = os.fstat(descriptor)
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
                or not _owned(info, owner) or info.st_mode & 0o022
                or info.st_mode & 0o027 or info.st_nlink != 1
                or info.st_size > limit):
            raise Refused('file')
        os.set_blocking(descriptor, True)
        with os.fdopen(descriptor, 'rb') as stream:
            descriptor = None
            data = stream.read(limit + 1)
    except Refused:
        raise
    except OSError:
        raise Refused('file') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory is not None:
            os.close(directory)
    if len(data) > limit:
        raise Refused('file')
    try:
        value = json.loads(data)
    except ValueError:
        raise Refused('file') from None
    if not isinstance(value, dict):
        raise Refused('file')
    return value


def _absolute(value):
    return (isinstance(value, str) and value.startswith('/')
            and '..' not in value.split('/') and _PATH.fullmatch(value) is not None)


def _name(value):
    return isinstance(value, str) and NAME_RE.fullmatch(value) is not None


def _aliases(value, pattern=_ALIAS):
    if not isinstance(value, (list, tuple)):
        return False
    if any(not isinstance(item, str) or pattern.fullmatch(item) is None
           for item in value):
        return False
    return len(set(value)) == len(value)


def _tools(value):
    return _aliases(value, _TOOL)


def validate_execution(value):
    """Refuse a host execution enrollment outside the exact closed shape.

    It names the account, home and runner unit of each agent served by this
    host, the roots a Git worktree may use, the tools and secret aliases it can
    attest, and the pinned release store it may *read*. It cannot name code,
    an arbitrary command or a secret value.
    """
    if not isinstance(value, Mapping) or tuple(sorted(value)) != tuple(sorted(EXECUTION_KEYS)):
        raise Refused('execution')
    if (not _name(value.get('host')) or not _absolute(value.get('state'))
            or not _absolute(value.get('config'))
            or not _absolute(value.get('release_store'))
            or not isinstance(value.get('roots'), (list, tuple)) or not value['roots']
            or len(set(value['roots'])) != len(value['roots'])
            or not all(_absolute(root) for root in value['roots'])
            or not _tools(value.get('tools')) or not _aliases(value.get('secrets'))
            or not isinstance(value.get('accounts'), Mapping) or not value['accounts']):
        raise Refused('execution')
    accounts = {}
    for agent, entry in value['accounts'].items():
        if (not _name(agent) or not isinstance(entry, Mapping)
                or tuple(sorted(entry)) != tuple(sorted(ACCOUNT_KEYS))
                or not isinstance(entry.get('account'), str)
                or not _ACCOUNT.fullmatch(entry['account'])
                or not _absolute(entry.get('home'))
                or not isinstance(entry.get('unit'), str)
                or _UNIT.fullmatch(entry['unit']) is None
                or entry.get('scope') not in ('system', 'user')):
            raise Refused('execution')
        expected = 'ameesh-runner%s@%s.service' % (
            '-agent' if entry['scope'] == 'user' else '', agent)
        if entry['unit'] != expected:
            raise Refused('execution')
        accounts[agent] = dict(entry)
    return {'host': value['host'], 'state': value['state'],
            'config': value['config'], 'release_store': value['release_store'],
            'roots': tuple(value['roots']), 'tools': tuple(sorted(value['tools'])),
            'secrets': tuple(sorted(value['secrets'])), 'accounts': accounts}


def load_execution(root=ENROLLMENT_ROOT):
    """Read the host execution enrollment; absence is a named refusal."""
    root = Path(root)
    try:
        info = root.lstat()
    except OSError:
        raise Refused('execution:root') from None
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
            or not _owned(info) or info.st_mode & 0o022):
        raise Refused('execution:root')
    try:
        return validate_execution(
            _read_private_json(root / EXECUTION_NAME, root=root))
    except Refused:
        raise Refused('execution') from None


def _relative_parts(root, path):
    """The single components of ``path`` below ``root`` (no ``..`` escape)."""
    try:
        relative = Path(path).relative_to(Path(root))
    except ValueError:
        raise Refused('state') from None
    if any(part in ('', '.', '..') for part in relative.parts):
        raise Refused('state')
    return relative.parts


def _open_root(root, *, owner=None, private=False):
    """Open a trusted root once, refusing a link in its final component.

    ``root`` itself is the trust anchor (the account home or an enrolled work
    root); everything below it is opened component by component in
    ``_open_dir``. The descriptor is checked after opening, never a path just
    before it, so the caller keeps the exact object it validated.
    """
    root = Path(root)
    try:
        descriptor = os.open(root,
                             os.O_RDONLY | _O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise Refused('state') from None
    try:
        info = os.fstat(descriptor)
        mask = 0o077 if private else 0o022
        if (not stat.S_ISDIR(info.st_mode) or not _owned(info, owner)
                or info.st_mode & mask):
            raise Refused('state')
    except Refused:
        os.close(descriptor)
        raise
    except OSError:
        os.close(descriptor)
        raise Refused('state') from None
    return descriptor


def _open_dir(root, path, *, create=False, owner=None, final_private=False):
    """Open ``path`` below a trusted root through nested directory descriptors.

    No component is ever reopened by its absolute path: each one is opened
    relative to the descriptor of its parent with ``O_DIRECTORY|O_NOFOLLOW``,
    and its type, owner and mode are checked on that descriptor. A missing
    component is created relative to its pinned parent and handed to ``owner``
    through its descriptor. Replacing any ancestor by a symlink therefore
    makes the walk fail instead of redirecting the next mkdir/open/fchown.
    """
    path = Path(path)
    descriptor = _open_root(root, owner=owner)
    try:
        parts = _relative_parts(root, path)
        for index, part in enumerate(parts):
            last = index == len(parts) - 1
            mask = 0o077 if last and final_private else 0o022
            created = False
            try:
                child = os.open(part,
                                os.O_RDONLY | _O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise Refused('state') from None
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                    created = True
                except FileExistsError:
                    pass
                except OSError:
                    raise Refused('state:create') from None
                try:
                    child = os.open(part,
                                    os.O_RDONLY | _O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=descriptor)
                except OSError:
                    raise Refused('state') from None
            except OSError:
                raise Refused('state') from None
            info = os.fstat(child)
            if (not stat.S_ISDIR(info.st_mode) or not _owned(info, owner)
                    or info.st_mode & mask):
                os.close(child)
                raise Refused('state')
            if created and owner is not None and os.geteuid() == 0:
                try:
                    os.fchown(child, owner[0], owner[1])
                except OSError:
                    os.close(child)
                    raise Refused('state:owner') from None
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _atomic_json(path, value, *, owner=None, root=None):
    """Private durable JSON through the pinned parent descriptor.

    The parent chain is reopened from the trusted root for every write and the
    temporary is created/replaced relative to the resulting descriptor, so a
    directory swapped by the account between the private-chain check and this
    write cannot redirect a privileged marker into another tree. When
    ``owner`` is given, the temporary is handed to that account through its
    descriptor *before* the rename: the account can never make the ownership
    change follow a link.
    """
    path = Path(path)
    if root is None or path.name in ('', '.', '..') or '/' in path.name:
        raise Uncertain('journal:write')
    candidate = '.' + path.name + '-' + os.urandom(6).hex()
    directory = None
    try:
        directory = _open_dir(root, path.parent, owner=owner)
        descriptor = os.open(candidate,
                             os.O_CREAT | os.O_EXCL | os.O_WRONLY
                             | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
            if owner is not None and os.geteuid() == 0:
                os.fchown(stream.fileno(), owner[0], owner[1])
        os.replace(candidate, path.name, src_dir_fd=directory,
                   dst_dir_fd=directory)
        os.fsync(directory)
    except (OSError, Refused):
        if directory is not None:
            try:
                os.unlink(candidate, dir_fd=directory)
            except OSError:
                pass
        raise Uncertain('journal:write') from None
    finally:
        if directory is not None:
            os.close(directory)


def _own_at(path, owner, *, directory=False, root=None):
    """Hand a freshly created entry to the enrolled account, never a link.

    The parent chain is reopened from the trusted root through descriptors;
    the entry is then opened relative to the pinned parent with
    ``O_NOFOLLOW`` and changed through its own descriptor. Replacing any
    ancestor or the entry itself by a link is refused instead of chowning the
    target.
    """
    path = Path(path)
    if root is None or path.name in ('', '.', '..') or '/' in path.name:
        raise Refused('owner')
    parent = descriptor = None
    try:
        parent = _open_dir(root, path.parent, owner=owner)
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        if directory:
            flags |= _O_DIRECTORY
        descriptor = os.open(path.name, flags, dir_fd=parent)
        info = os.fstat(descriptor)
        if directory:
            if not stat.S_ISDIR(info.st_mode):
                raise Refused('owner')
        elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise Refused('owner')
        try:
            os.fchown(descriptor, owner[0], owner[1])
        except OSError:
            raise Refused('owner') from None
    except Refused:
        # A replaced ancestor is not a target to chown either: it is a
        # definitive refusal of this effect.
        raise Refused('owner') from None
    except OSError:
        raise Refused('owner') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent is not None:
            os.close(parent)


def _unlink_private(path, *, owner=None, root=None):
    """Remove one entry through its pinned parent, never a swapped path."""
    path = Path(path)
    if root is None or path.name in ('', '.', '..') or '/' in path.name:
        raise Uncertain('journal:write')
    directory = None
    try:
        directory = _open_dir(root, path.parent, owner=owner)
        os.unlink(path.name, dir_fd=directory)
    except (OSError, Refused):
        raise Uncertain('journal:write') from None
    finally:
        if directory is not None:
            os.close(directory)


def _ensure_private(path, *, owner=None, root=None):
    """Create or verify a private directory through pinned descriptors."""
    path = Path(path)
    if root is None:
        raise Refused('state')
    if path == Path(root):
        descriptor = None
        try:
            descriptor = _open_root(path, owner=owner, private=True)
            info = os.fstat(descriptor)
            if owner is not None and os.geteuid() == 0 and info.st_uid == 0:
                os.fchown(descriptor, owner[0], owner[1])
        except Refused:
            raise Refused('state:owner') from None
        except OSError:
            raise Refused('state:owner') from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
        return path
    descriptor = None
    try:
        descriptor = _open_dir(root, path, create=True, owner=owner,
                               final_private=True)
        info = os.fstat(descriptor)
        if owner is not None and os.geteuid() == 0 and info.st_uid == 0:
            os.fchown(descriptor, owner[0], owner[1])
    except Refused:
        raise
    except OSError:
        raise Refused('state:create') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return path


def _existing_ancestor(path):
    """The nearest existing lexical ancestor of ``path`` (``/`` at worst)."""
    path = Path(path)
    while True:
        try:
            os.lstat(path)
            return path
        except FileNotFoundError:
            if path == path.parent:
                return path
            path = path.parent
        except OSError:
            raise Refused('state') from None


def _ensure_state(path):
    """Create or verify the enrollment state root without a path race.

    An existing root is opened once and checked through its descriptor. A
    missing root is created below the nearest existing ancestor after that
    ancestor has been opened and validated; every component is then created
    relative to the descriptor of its parent.
    """
    path = Path(path)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        anchor = _existing_ancestor(path.parent)
        probe = _open_root(anchor, owner=None)
        os.close(probe)
        return _ensure_private(path, root=anchor)
    except OSError:
        raise Refused('state') from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise Refused('state')
    return _ensure_private(path, root=path)


def _private_chain(path, *, owner=None, root=None):
    """Create each missing component with 0700, never through a symlink.

    ``root`` is the account home (or enrolled root) the chain must stay
    inside. Every component is opened relative to its parent's descriptor
    with ``O_DIRECTORY|O_NOFOLLOW``; a pre-created link cannot make a
    privileged executor write outside that root, and a directory swapped
    during the walk is refused rather than followed.
    """
    path = Path(path)
    if root is None:
        raise Refused('state')
    descriptor = None
    try:
        descriptor = _open_dir(root, path, create=True, owner=owner,
                               final_private=True)
        info = os.fstat(descriptor)
        if owner is not None and os.geteuid() == 0 and info.st_uid == 0:
            os.fchown(descriptor, owner[0], owner[1])
    except Refused:
        raise
    except OSError:
        raise Refused('state:create') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return path


def _stat_at(root, path, *, owner=None):
    """``lstat`` one entry relative to a parent reopened from a trusted root."""
    path = Path(path)
    if path.name in ('', '.', '..') or '/' in path.name:
        raise Refused('state')
    directory = _open_dir(root, path.parent, owner=owner)
    try:
        try:
            return os.stat(path.name, dir_fd=directory,
                           follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError:
            raise Refused('state') from None
    finally:
        os.close(directory)


def _remove_tree_at(parent_fd, name):
    """Remove one tree below an already pinned parent, never following links.

    The recursive walk opens every child relative to its parent descriptor
    with ``O_DIRECTORY|O_NOFOLLOW``; a directory swapped for a link is
    unlinked as a link, never descended into.
    """
    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        return
    child = os.open(name, os.O_RDONLY | _O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=parent_fd)
    try:
        for entry in os.listdir(child):
            _remove_tree_at(child, entry)
    finally:
        os.close(child)
    os.rmdir(name, dir_fd=parent_fd)


# ---------------------------------------------------------------------------
# Host primitives (systemd, Git) - injectable, never shell text
# ---------------------------------------------------------------------------

def _env(home, protocols=GIT_PROTOCOLS):
    env = {'PATH': SAFE_PATH, 'LANG': 'C', 'LC_ALL': 'C', 'HOME': str(home),
           'GIT_TERMINAL_PROMPT': '0', 'GIT_ASKPASS': '/bin/false',
           'SSH_ASKPASS': '/bin/false'}
    if protocols is not None:
        env['GIT_ALLOW_PROTOCOL'] = ':'.join(protocols)
    return env


def _kill_group(process):
    """Kill the whole child session, including git/ssh descendants."""
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except OSError:
        try:
            process.kill()
        except OSError:
            pass


def _bounded_run(argv, *, timeout, env, cwd=None, check=True):
    process = None
    try:
        process = subprocess.Popen(argv, cwd=cwd, env=env,
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True,
                                   start_new_session=True)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(process)
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            raise Uncertain('command:unavailable') from None
        except BaseException:
            # The dispatcher's alarm or a cancellation must not leave a child
            # or its git/ssh descendants running after an uncertain effect.
            _kill_group(process)
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            raise
    except Uncertain:
        raise
    except (OSError, subprocess.SubprocessError):
        raise Uncertain('command:unavailable') from None
    if len(stdout) > MAX_RECEIPT_BYTES or len(stderr) > MAX_RECEIPT_BYTES:
        raise Uncertain('command:output')
    if check and process.returncode != 0:
        raise Uncertain('command:failed')
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


class HostSystem:
    """The one systemd scope an enrolled unit may use, with fixed executables.

    No request supplies a unit, a property, a program or an environment. The
    executor observes a unit, stops it, and starts only that exact enrolled
    unit for H3 activation/restoration; it never enables, edits or disables
    one.
    """

    def __init__(self, *, systemctl='/usr/bin/systemctl',
                 runuser='/usr/sbin/runuser', cgroups='/sys/fs/cgroup',
                 euid=None):
        self.systemctl = str(systemctl)
        self.runuser = str(runuser)
        self.cgroups = Path(cgroups)
        self.euid = os.geteuid() if euid is None else euid

    def _user_prefix(self, account):
        try:
            uid = pwd.getpwnam(account).pw_uid
        except KeyError:
            raise Refused('account') from None
        return [self.runuser, '-u', account, '--', 'env',
                'XDG_RUNTIME_DIR=/run/user/%d' % uid,
                'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/%d/bus' % uid]

    def _argv(self, scope, account, args):
        if scope == 'user':
            base = [self.systemctl, '--user'] + list(args)
            if self.euid == 0:
                return self._user_prefix(account) + base
            return base
        return [self.systemctl] + list(args)

    def _run(self, scope, account, args, timeout=30):
        return _bounded_run(self._argv(scope, account, args), timeout=timeout,
                            env={'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C'})

    def show(self, scope, account, unit, properties):
        if _UNIT.fullmatch(unit) is None or scope not in ('system', 'user'):
            raise Refused('unit')
        text = self._run(scope, account,
                         ['show', unit, '--property=' + ','.join(properties)]).stdout
        fields = {}
        for line in text.splitlines():
            key, sep, value = line.partition('=')
            if sep:
                fields[key] = value
        return fields

    def stop(self, scope, account, unit):
        if _UNIT.fullmatch(unit) is None:
            raise Refused('unit')
        self._run(scope, account, ['stop', unit], timeout=STOP_SECONDS)

    def start(self, scope, account, unit):
        """Start the exact enrolled unit; a timeout is an uncertain effect.

        ``systemctl start`` returns once the job is queued, not when the unit
        is active: the caller observes ``ActiveState`` for the remaining
        enable budget. A non-zero exit is a definitive start refusal.
        """
        if _UNIT.fullmatch(unit) is None or scope not in ('system', 'user'):
            raise Refused('unit')
        result = _bounded_run(
            self._argv(scope, account, ['start', unit]),
            timeout=START_SECONDS,
            env={'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C'},
            check=False)
        if result.returncode != 0:
            raise Refused('start')

    def active(self, scope, account, unit):
        return self.show(scope, account, unit,
                         ('ActiveState',)).get('ActiveState') in (
                             'active', 'activating', 'reloading')

    def quiet(self, scope, account, unit):
        """No harness child or orphan survives in the unit's cgroup subtree."""
        fields = self.show(scope, account, unit,
                           ('LoadState', 'MainPID', 'ControlGroup'))
        if fields.get('LoadState') != 'loaded':
            return False
        group = fields.get('ControlGroup') or ''
        main = fields.get('MainPID') or '0'
        if not group:
            return main == '0'
        path = Path(group)
        if not path.is_absolute() or '..' in path.parts or group == '/':
            return False
        root = self.cgroups / group.lstrip('/')
        if root.is_symlink() or any(part.is_symlink() for part in root.parents):
            return False
        if not root.is_dir():
            return False
        for directory, links, files in os.walk(root, followlinks=False):
            if any((Path(directory) / name).is_symlink() for name in links):
                return False
            if 'cgroup.procs' not in files:
                return False
            try:
                processes = (Path(directory) / 'cgroup.procs').read_text().split()
            except OSError:
                return False
            if any(not pid.isdigit() for pid in processes):
                return False
            if any(pid != main for pid in processes):
                return False
        return True


class HostGit:
    """Bounded Git reads and one exact checkout, run as the enrolled account.

    ``protocols`` is the closed network transport list handed to Git through
    ``GIT_ALLOW_PROTOCOL``. Tests may pass ``None`` to exercise local fixtures;
    the executor never does.
    """

    def __init__(self, *, runuser='/usr/sbin/runuser', euid=None, runner=_bounded_run,
                 protocols=GIT_PROTOCOLS):
        self.runuser = str(runuser)
        self.euid = os.geteuid() if euid is None else euid
        self.runner = runner
        self.protocols = protocols

    def _argv(self, account, cwd, args):
        argv = ['git', '-C', str(cwd), *args]
        if self.euid == 0 and account != pwd.getpwuid(0).pw_name:
            return [self.runuser, '-u', account, '--'] + argv
        return argv

    def run(self, account, home, cwd, args, *, timeout=GIT_SECONDS, check=True):
        result = self.runner(self._argv(account, cwd, args), timeout=timeout,
                             env=_env(home, self.protocols), cwd=None, check=check)
        return result.stdout.strip()

    @staticmethod
    def repository(value):
        """Refuse a repository outside the admitted transports before Git sees it."""
        if not handoff_contract.repository_identity(value):
            raise Refused('repository')
        return value


# ---------------------------------------------------------------------------
# The host-side executor
# ---------------------------------------------------------------------------

class HostEffects:
    """One host's narrow preparation/Git executor with a durable journal.

    The journal is per operation. It records the step order, the last
    controller epoch and a private intent before the effect, then an exact
    allowlisted receipt. Replaying the same step after a lost reply returns the
    stored receipt without repeating the effect; an interrupted intent is
    observed and completed when the host state proves it, otherwise the call
    raises and the controller must reconcile. A durable fence shared by every
    operation of one agent/unit records the highest lease epoch observed and
    refuses a delayed operation from an older lease before it can act.
    """

    def __init__(self, *, enrollment, system=None, git=None, work_dir=None,
                 clock=time.time, root=None, runuser='/usr/sbin/runuser',
                 enable_seconds=ENABLE_SECONDS,
                 restore_seconds=RESTORE_SECONDS):
        self.enrollment = validate_execution(enrollment)
        self.host = self.enrollment['host']
        self.system = system if system is not None else HostSystem(runuser=runuser)
        self.git = git if git is not None else HostGit(runuser=runuser)
        self.work_dir = work_dir
        self.clock = clock
        self.enable_seconds = float(enable_seconds)
        self.restore_seconds = float(restore_seconds)
        state = Path(root or self.enrollment['state'])
        self.state = _ensure_state(state)
        self.operations = _ensure_private(self.state / 'operations',
                                          root=self.state)
        self.fences = _ensure_private(self.state / 'fences', root=self.state)
        self._locks = {}
        self._release = None
        self._canons = None

    # -- discovery ---------------------------------------------------------

    def inventory(self, request):
        request = handoff_contract.validate_inventory_request(request)
        if request['host'] != self.host:
            raise Refused('host')
        return {'host': self.host, 'checked_ts': float(self.clock()),
                'connected': True,
                'tools': {name: self._tool(name) for name in request['tools']},
                'secrets': {name: name in self.enrollment['secrets']
                            for name in request['secrets']}}

    # -- effects -----------------------------------------------------------

    def perform(self, request):
        request = handoff_contract.validate_effect_request(request)
        if request['host'] != self.host:
            raise Refused('host')
        try:
            entry = self._entry(request['agent'])
        except Refused as exc:
            return self._refusal(request, str(exc))
        # ``request.account`` is the harness/provider account validated by the
        # controller's hosts document; ``entry.account`` is the Unix user that
        # owns the home and unit. They are deliberately independent.
        #
        # Every operation of one agent shares one lock and one durable lease
        # fence: a revalidation, an effect and a replayed stop cannot
        # interleave, and a newer lease revokes every older operation on this
        # host before it can act (including a stop replayed from an old
        # receipt). The lock is reentrant for the same thread so an
        # observation nested inside an effect still shares the same fence.
        with self._agent_lock(request['agent']):
            if request['step'] == 'revalidate':
                try:
                    self._revalidate(request, entry)
                except Refused as exc:
                    return self._refusal(request, str(exc))
                return self._receipt(request, {})
            if request['step'] == 'reconcile_effect':
                try:
                    return self._reconcile(request, entry)
                except Refused as exc:
                    return self._refusal(request, str(exc))
            return self._journal(request, entry)

    def _entry(self, agent):
        entry = self.enrollment['accounts'].get(agent)
        if entry is None:
            raise Refused('identity')
        return entry

    def _refusal(self, request, reason):
        if not isinstance(reason, str) or _REASON.fullmatch(reason) is None:
            reason = 'refused'
        return {'host': self.host, 'action_id': request['action_id'],
                'controller_epoch': request['controller_epoch'], 'ok': False,
                'reason': reason}

    def _receipt(self, request, fields):
        return dict({'host': self.host, 'action_id': request['action_id'],
                     'controller_epoch': request['controller_epoch'], 'ok': True},
                    **fields)

    def _journal(self, request, entry):
        operation, step = request['operation'], request['step']
        sequence = STEP_SEQUENCE[step]
        directory = _ensure_private(self.operations / operation,
                                    root=self.state)
        receipt_path = directory / (step + '.json')
        intent_path = directory / (step + '.intent.json')
        state_path = directory / 'state.json'
        with self._agent_lock(request['agent']), self._locked(
                directory, root=self.state):
            state = self._state(state_path)
            state, refusal = self._bind_lease(state, state_path, request)
            if refusal is not None:
                return refusal
            if request['controller_epoch'] > state['epoch']:
                # The newer controller epoch is authoritative even for a
                # replay: a delayed request from an older epoch is refused.
                state = dict(state, epoch=request['controller_epoch'])
                self._store_state(state_path, state)
            if request['controller_epoch'] < state['epoch']:
                return self._refusal(request, 'revoked_epoch')
            try:
                # The shared fence is checked (and advanced by a newer lease)
                # before any receipt replay: an old stop must not act on the
                # executor of a lease that reached this host later.
                self._bind_fence(request, entry)
            except Refused as exc:
                return self._refusal(request, str(exc))
            # A receipted effect is idempotent even after a later step: the
            # controller legitimately repeats ``stop_source`` before retrying a
            # held publication. Only a *new* earlier effect is out of order.
            if receipt_path.exists() or receipt_path.is_symlink():
                record = _read_private_json(receipt_path, root=self.state)
                if (not isinstance(record.get('request'), dict)
                        or not isinstance(record.get('receipt'), dict)):
                    raise Uncertain('journal:receipt')
                if not _same_effect(record['request'], request):
                    return self._refusal(request, 'request_changed')
                stored = record['receipt']
                if step in REOBSERVE:
                    # A stored receipt is not proof that the host state is
                    # still the same: a unit can die or a source can be
                    # stopped between the first reply and the replay. H3
                    # re-observes and repairs the stored receipt.
                    try:
                        fields = self._observe(request, entry)
                    except Refused as exc:
                        return self._refusal(request, str(exc))
                    stored = self._receipt(request, fields)
                    _atomic_json(receipt_path,
                                 {'request': request, 'receipt': stored,
                                  'at': float(self.clock())},
                                 root=self.state)
                if state['last_step'] == step and state['last_ok'] is not True:
                    # Crash after the receipt write, before the state write:
                    # repair the order mark without replaying the effect.
                    state = dict(state, last_ok=True)
                    self._store_state(state_path, state)
                try:
                    _unlink_private(intent_path, root=self.state)
                except Uncertain:
                    pass
                return dict(stored, controller_epoch=request['controller_epoch'])
            # Cancellation is pre-switch only: once the destination was
            # admitted, the controller reconciles forward and never restores
            # the source over possible destination work.
            if (step == 'restore_source'
                    and state['last_seq'] >= STEP_SEQUENCE['admit']):
                return self._refusal(request, 'order')
            if step == state['last_step']:
                allowed = True
            elif (step == 'restore_source'
                  and state['last_seq'] < STEP_SEQUENCE['admit']):
                # Cancellation restores the source after any pre-switch
                # attempt, including a refused prepare/publish.
                allowed = True
            elif sequence > state['last_seq'] and (state['last_ok'] or not state['last_step']):
                allowed = True
            else:
                allowed = False
            if not allowed:
                return self._refusal(request, 'order')
            if intent_path.exists() or intent_path.is_symlink():
                intent = _read_private_json(intent_path, root=self.state)
                stored_request = intent.get('request')
                if not isinstance(stored_request, dict):
                    raise Uncertain('journal:intent')
                if not _same_effect(stored_request, request):
                    # An uncertain effect may only be resumed with its exact
                    # original identity and payload, never re-dispatched.
                    return self._refusal(request, 'intent_changed')
            else:
                _atomic_json(intent_path,
                             {'request': request, 'at': float(self.clock())},
                             root=self.state)
            state = dict(state, epoch=request['controller_epoch'], last_step=step,
                         last_seq=sequence, last_ok=False)
            self._store_state(state_path, state)
            try:
                fields = self._dispatch(request, entry)
            except Refused as exc:
                try:
                    _unlink_private(intent_path, root=self.state)
                except Uncertain:
                    pass
                return self._refusal(request, str(exc))
            receipt = self._receipt(request, fields)
            _atomic_json(receipt_path,
                         {'request': request, 'receipt': receipt,
                          'at': float(self.clock())},
                         root=self.state)
            self._store_state(state_path, dict(state, last_ok=True))
            try:
                _unlink_private(intent_path, root=self.state)
            except Uncertain:
                pass
            return receipt

    def _check_pending(self, directory, state, step, epoch, request):
        """The durable host intent must be exactly the controller's pending one.

        A missing intent means the request never reached the host (no effect
        ran); that is accepted only when the pending step is still ahead of
        the journal, never for an earlier step. A different operation, agent,
        owner or lease is a mismatch: an uncertain effect is never quiesced on
        another operation's behalf.

        The stored controller epoch may be older than the pending one. A retry
        resumes the same intent under a newer controller epoch without
        rewriting it, and the controller can even record its pending intent
        and crash before the retry reaches this host: the effect identity is
        what binds, the epoch only fences delayed calls, so the stored epoch
        must not be newer than the pending one.
        """
        for name in (step + '.intent.json', step + '.json'):
            path = Path(directory) / name
            if not (path.exists() or path.is_symlink()):
                continue
            record = _read_private_json(path, root=self.state)
            stored = record.get('request')
            if not isinstance(stored, dict):
                raise Uncertain('journal:' + name)
            stored_epoch = stored.get('controller_epoch')
            if (stored.get('operation') != request['operation']
                    or stored.get('agent') != request['agent']
                    or stored.get('owner') != request['owner']
                    or stored.get('lease_epoch') != request['lease_epoch']
                    or not isinstance(stored_epoch, int)
                    or isinstance(stored_epoch, bool)
                    or not 1 <= stored_epoch <= epoch):
                raise Refused('intent_changed')
            return
        if state.get('last_step') == step and state.get('last_ok') is True:
            raise Uncertain('journal:' + step)
        if (state.get('last_step') not in ('', step)
                and STEP_SEQUENCE[step] <= state.get('last_seq', 0)):
            raise Refused('intent_changed')

    def _quiesce(self, step, entry):
        """Quiesce the host jobs of the interrupted effect.

        ``enable`` and ``restore_source`` start the enrolled unit: an
        interrupted call may have started it, so reconciliation stops it and
        proves the cgroup quiet. A pending ``stop_source`` is made quiet too.
        Read-only effects leave no host job; the shared epoch fence refuses
        any delayed call from the revoked epoch before it can act.
        """
        if step in ('enable', 'restore_source', 'stop_source'):
            self._stop_unit(entry)

    def _reconcile(self, request, entry):
        """Fence the old controller epoch and quiesce its host jobs.

        The controller calls this before any retry of an uncertain effect. The
        operation state advances to the new controller epoch (a delayed call
        from the old one is then refused), the exact pending intent is checked
        against the request, the host job is quiesced, and a durable proof is
        returned: ``{ok, quiesced, epoch: PENDING_EPOCH}``. The journal's
        order mark is left untouched so the controller's retry can resume the
        same step, idempotently, under the new epoch.

        One operation can be interrupted more than once (an ``enable`` that
        expires again after its retry, a later ``verify``): each uncertainty
        has its own ``(pending_step, pending_epoch)`` and therefore its own
        durable proof, named after both. A single per-operation file would
        refuse the second, legitimate reconciliation as ``intent_changed`` and
        leave the operation held with no way forward.
        """
        operation = request['operation']
        step, pending_epoch = request['pending_step'], request['pending_epoch']
        if (step not in handoff_contract.RECONCILABLE_STEPS
                or not isinstance(pending_epoch, int)
                or isinstance(pending_epoch, bool) or pending_epoch < 1):
            raise Refused('intent')
        directory = _ensure_private(self.operations / operation,
                                    root=self.state)
        state_path = directory / 'state.json'
        # Quiescence stops the agent's unit: it must hold the same agent lock
        # as every effect and fence write. The lock is reentrant, and
        # ``perform`` already holds it around this call; taking it here keeps
        # the invariant local to the function that acts on the host.
        with self._agent_lock(request['agent']), self._locked(
                directory, root=self.state):
            state = self._state(state_path)
            state, refusal = self._bind_lease(state, state_path, request)
            if refusal is not None:
                return refusal
            if request['controller_epoch'] > state['epoch']:
                state = dict(state, epoch=request['controller_epoch'])
                self._store_state(state_path, state)
            elif request['controller_epoch'] < state['epoch']:
                return self._refusal(request, 'revoked_epoch')
            try:
                self._bind_fence(request, entry)
            except Refused as exc:
                return self._refusal(request, str(exc))
            record_path = directory / ('reconcile-%s-%d.json'
                                       % (step, pending_epoch))
            if record_path.exists() or record_path.is_symlink():
                record = _read_private_json(record_path, root=self.state)
                if (record.get('pending_step') != step
                        or record.get('pending_epoch') != pending_epoch
                        or record.get('quiesced') is not True):
                    raise Uncertain('journal:reconcile')
                return self._receipt(request, {'quiesced': True,
                                               'epoch': pending_epoch})
            self._check_pending(directory, state, step, pending_epoch, request)
            self._quiesce(step, entry)
            _atomic_json(record_path,
                         {'version': 1, 'pending_step': step,
                          'pending_epoch': pending_epoch, 'quiesced': True,
                          'at': float(self.clock())},
                         root=self.state)
            return self._receipt(request, {'quiesced': True,
                                           'epoch': pending_epoch})

    def _bind_lease(self, state, path, request):
        """Bind the operation's source lease once, then refuse any change."""
        lease = {'owner': request['owner'], 'lease_epoch': request['lease_epoch']}
        if state.get('lease') is None:
            state = dict(state, lease=lease)
            self._store_state(path, state)
            return state, None
        if state['lease'] != lease:
            return state, self._refusal(request, 'lease_changed')
        return state, None

    def _agent_lock(self, agent):
        return self._locked(_ensure_private(self.fences / agent,
                                            root=self.state),
                            root=self.state)

    def _bind_fence(self, request, entry):
        """Check and advance the durable lease fence shared by one agent.

        The per-operation lease only protects its own directory; an agent can
        have several operations (a handoff and its repeated revalidations, a
        delayed replay, a later attempt). A replayed ``stop_source`` must not
        stop the executor of a lease that reached this host after that
        operation was written. The fence records the highest lease epoch seen
        for the agent/unit identity on this host: a newer lease advances it,
        and every older operation is refused before it can act. It is written
        under the per-agent lock and survives a restart.
        """
        directory = _ensure_private(self.fences / request['agent'],
                                    root=self.state)
        path = directory / 'lease.json'
        fence = {'agent': request['agent'], 'account': entry['account'],
                 'unit': entry['unit'], 'owner': request['owner'],
                 'lease_epoch': request['lease_epoch']}
        if not (path.exists() or path.is_symlink()):
            _atomic_json(path, fence, root=self.state)
            return
        current = _read_private_json(path, root=self.state)
        if (tuple(sorted(current)) != tuple(sorted(_FENCE_KEYS))
                or current.get('agent') != request['agent']
                or not isinstance(current.get('account'), str)
                or _ACCOUNT.fullmatch(current['account']) is None
                or not isinstance(current.get('unit'), str)
                or _UNIT.fullmatch(current['unit']) is None
                or not isinstance(current.get('owner'), str)
                or not (current['owner'] == '' or _bounded(current['owner']))
                or not isinstance(current.get('lease_epoch'), int)
                or isinstance(current.get('lease_epoch'), bool)
                or current['lease_epoch'] < 0):
            raise Uncertain('journal:fence')
        if request['lease_epoch'] < current['lease_epoch']:
            raise Refused('lease_superseded')
        if request['lease_epoch'] == current['lease_epoch']:
            if (current['account'] != fence['account']
                    or current['unit'] != fence['unit']
                    or current['owner'] != fence['owner']):
                raise Refused('lease_changed')
            return
        _atomic_json(path, fence, root=self.state)

    def _store_state(self, path, state):
        """Write an operation state without ever lowering its epoch.

        A revalidation and an effect share the agent lock, but an observation
        nested inside an effect runs after the effect already read its epoch;
        the on-disk controller epoch is monotonic and must never go back to
        the older local copy.
        """
        path = Path(path)
        if path.exists() or path.is_symlink():
            current = self._state(path)
            if current['epoch'] > state['epoch']:
                state = dict(state, epoch=current['epoch'])
        _atomic_json(path, state, root=self.state)

    @contextlib.contextmanager
    def _locked(self, directory, *, root=None):
        key = os.path.abspath(os.fspath(directory))
        thread = threading.get_ident()
        held = self._locks.get(key)
        if held is not None and held[0] == thread:
            held[1] += 1
            try:
                yield
            finally:
                held[1] -= 1
            return
        directory_fd = descriptor = None
        try:
            directory_fd = _open_dir(root, directory)
            descriptor = os.open('.lock',
                                 os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                                 0o600, dir_fd=directory_fd)
        except (OSError, Refused):
            if descriptor is not None:
                os.close(descriptor)
            if directory_fd is not None:
                os.close(directory_fd)
            raise Uncertain('journal:lock') from None
        try:
            deadline = time.monotonic() + LOCK_SECONDS
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise Uncertain('journal:lock') from None
                    time.sleep(0.05)
            self._locks[key] = [thread, 1]
            try:
                yield
            finally:
                self._locks.pop(key, None)
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
                os.close(directory_fd)

    def _state(self, path):
        if not path.exists():
            return {'epoch': 0, 'last_step': '', 'last_seq': 0, 'last_ok': False,
                    'lease': None}
        value = _read_private_json(path, root=self.state)
        if (not isinstance(value.get('epoch'), int) or isinstance(value.get('epoch'), bool)
                or value.get('epoch') < 0
                or not isinstance(value.get('last_step'), str)
                or not isinstance(value.get('last_seq'), int)
                or isinstance(value.get('last_seq'), bool)
                or value.get('last_seq') < 0
                or not isinstance(value.get('last_ok'), bool)):
            raise Uncertain('journal:state')
        lease = value.get('lease')
        if lease is not None:
            if (not isinstance(lease, dict)
                    or set(lease) != {'owner', 'lease_epoch'}
                    or not isinstance(lease.get('owner'), str)
                    or not (lease['owner'] == '' or _bounded(lease['owner']))
                    or not isinstance(lease.get('lease_epoch'), int)
                    or isinstance(lease.get('lease_epoch'), bool)
                    or lease['lease_epoch'] < 0):
                raise Uncertain('journal:state')
        return value

    def _dispatch(self, request, entry):
        step = request['step']
        if step == 'prepare':
            return self._prepare(request, entry)
        if step == 'stop_source':
            return self._stop_source(request, entry)
        if step == 'publish':
            return self._publish(request, entry)
        if step == 'transfer':
            return self._transfer(request, entry)
        if step == 'session':
            return self._session(request, entry)
        if step == 'admit':
            return self._admit(request, entry)
        if step == 'enable':
            return self._enable(request, entry)
        if step == 'verify':
            return self._verify(request, entry)
        if step == 'restore_source':
            return self._restore_source(request, entry)
        raise Refused('method')

    def _observe(self, request, entry):
        """Re-observe a stored H3 receipt without replaying its first effect."""
        step = request['step']
        if step == 'stop_source':
            return self._stop_source(request, entry)
        if step == 'restore_source':
            return self._restore_source(request, entry)
        if step == 'enable':
            return self._enable(request, entry)
        if step == 'verify':
            return self._verify(request, entry)
        raise Refused('method')

    # -- revalidate: observe, never start ----------------------------------

    def _revalidate(self, request, entry):
        with self._agent_lock(request['agent']):
            self._revalidate_locked(request, entry)

    def _revalidate_locked(self, request, entry):
        limit = MAX_SEQUENCE.get(request['state'])
        if limit is None:
            raise Refused('state')
        directory = _ensure_private(self.operations / request['operation'],
                                    root=self.state)
        state_path = directory / 'state.json'
        current = self._state(state_path)
        current, refusal = self._bind_lease(current, state_path, request)
        if refusal is not None:
            raise Refused('lease_changed')
        if request['controller_epoch'] > current['epoch']:
            current = dict(current, epoch=request['controller_epoch'])
            self._store_state(state_path, current)
        elif request['controller_epoch'] < current['epoch']:
            raise Refused('revoked_epoch')
        # A revalidation carries the same lease identity as the effects and
        # participates in the shared fence: a newer lease observed here
        # revokes every delayed operation of an older one.
        self._bind_fence(request, entry)
        if current['last_seq'] > limit and not (
                not current['last_ok']
                and current['last_step'] == NEXT_STEP.get(request['state'])):
            raise Refused('order')
        receipt = self._step_receipt(directory, 'prepare')
        if receipt is not None and self._release_revision() != receipt['release_revision']:
            raise Refused('release')
        transfer = self._step_receipt(directory, 'transfer')
        if transfer is not None:
            cwd = transfer['cwd']
            try:
                head = self._git(entry, cwd, ['rev-parse', 'HEAD'])
                if current['last_seq'] < STEP_SEQUENCE['admit']:
                    clean = not self._git(entry, cwd,
                                          ['status', '--porcelain',
                                           '--untracked-files=all'])
                    if head != transfer['commit'] or not clean:
                        raise Refused('checkout')
                elif head != transfer['commit']:
                    # After the switch, the destination's own turn may have
                    # committed on top of the exact checkout. The transfer
                    # commit must remain an ancestor; a reset or another
                    # branch is a checkout mismatch, not new work.
                    self._git(entry, cwd, ['merge-base', '--is-ancestor',
                                           transfer['commit'], head])
            except Uncertain:
                raise Refused('checkout') from None
        session = self._step_receipt(directory, 'session')
        # H3: after the switch, the destination unit is expected to become
        # active; only a pre-switch state refuses an active target.
        if (session is not None and self._active(entry)
                and current['last_seq'] < STEP_SEQUENCE['admit']):
            raise Refused('target_active')

    def _step_receipt(self, directory, step):
        path = Path(directory) / (step + '.json')
        if not path.exists():
            return None
        record = _read_private_json(path, root=self.state)
        receipt = record.get('receipt')
        if not isinstance(receipt, dict) or receipt.get('ok') is not True:
            raise Uncertain('journal:receipt')
        return receipt

    # -- preparation -------------------------------------------------------

    def _prepare(self, request, entry):
        home = self._home(entry)
        if not self._tooling(request['tools']):
            raise Refused('tooling')
        if not all(alias in self.enrollment['secrets'] for alias in request['secrets']):
            raise Refused('credentials')
        caps = self.system.show(entry['scope'], entry['account'], entry['unit'],
                                ('LoadState', 'ActiveState', 'MemoryMax',
                                 'MemoryHigh', 'Slice'))
        if not _caps_ready(caps):
            raise Refused('caps')
        if caps.get('ActiveState') in ('active', 'activating', 'reloading'):
            raise Refused('target_active')
        release = self._release_revision()
        dsh = self._dsh_profile(request['harness'], request['agent'], entry, home)
        return {'release_revision': release, 'credentials_ready': True,
                'caps_ready': True, 'tooling_ready': True,
                'dsh_profile_ready': dsh, 'executor_enabled': False}

    def _dsh_profile(self, harness, agent, entry, home):
        """A private, credential-free dsh profile directory; never a cache.

        Hooks and provider configuration belong to the pinned release and the
        L232c account delivery (H4). This step only proves that the destination
        profile is new, private and carries no credential file.
        """
        if harness not in ('deepseek', 'dsh'):
            return True
        owner = self._owner(entry)
        base = _private_chain(Path(home) / '.ameesh-handoff',
                              owner=owner, root=home)
        directory = _private_chain(base / 'dsh', owner=owner, root=home)
        descriptor = _open_dir(home, directory, owner=owner)
        try:
            for name in os.listdir(descriptor):
                if name in CREDENTIAL_FILES:
                    raise Refused('dsh_credentials')
        finally:
            os.close(descriptor)
        marker = directory / 'profile.json'
        expected = {'version': 1, 'agent': agent, 'harness': harness,
                    'credentials': False}
        if marker.exists():
            if _read_private_json(marker, owner=owner, root=home) != expected:
                raise Refused('dsh_profile')
        else:
            _atomic_json(marker, expected, owner=owner, root=home)
        return True

    # -- source stop -------------------------------------------------------

    def _stop_unit(self, entry):
        """Stop the exact enrolled unit and prove its cgroup is quiescent."""
        if not self._active(entry) and self.system.quiet(
                entry['scope'], entry['account'], entry['unit']):
            return
        try:
            self.system.stop(entry['scope'], entry['account'], entry['unit'])
        except (Refused, Uncertain):
            raise
        except Exception:
            raise Uncertain('stop:unavailable') from None
        deadline = time.monotonic() + STOP_SECONDS
        while time.monotonic() < deadline:
            if (not self._active(entry) and self.system.quiet(
                    entry['scope'], entry['account'], entry['unit'])):
                return
            time.sleep(0.2)
        raise Uncertain('stop:deadline')

    def _stop_source(self, request, entry):
        self._stop_unit(entry)
        return {'unit': entry['unit'], 'stopped': True, 'quiet': True,
                'executor_enabled': False}

    def _restore_source(self, request, entry):
        """Restart the source unit before cancellation, then prove it active.

        The source worktree and session were never touched by the pre-switch
        effects; restoration only returns the enrolled unit to service. A
        start refusal is definitive (the controller holds), a timeout is
        uncertain (the unit may be starting) and requires reconciliation.
        """
        if not self._active(entry):
            self.system.start(entry['scope'], entry['account'], entry['unit'])
        deadline = time.monotonic() + self.restore_seconds
        while time.monotonic() < deadline:
            caps = self.system.show(
                entry['scope'], entry['account'], entry['unit'],
                ('LoadState', 'ActiveState', 'MemoryMax', 'MemoryHigh', 'Slice'))
            if caps.get('ActiveState') == 'failed':
                raise Refused('restore')
            if caps.get('ActiveState') == 'active' and _caps_ready(caps):
                return {'unit': entry['unit'], 'active': True,
                        'executor_enabled': True}
            time.sleep(0.2)
        raise Uncertain('restore:deadline')

    # -- publication and transfer -----------------------------------------

    def _publish(self, request, entry):
        repository = self.git.repository(request['repository'])
        cwd = self._worktree(request['cwd'], entry, must_exist=True)
        try:
            # The stored enrolled URL, not ``remote get-url``: the latter
            # applies ``url.*.insteadOf`` from the account's own config.
            remote_url = self._git(entry, cwd, ['config', '--get',
                                                'remote.origin.url'])
        except Uncertain:
            raise Refused('repository') from None
        if remote_url != repository:
            raise Refused('repository')
        try:
            dirty = self._git(entry, cwd, ['status', '--porcelain',
                                           '--untracked-files=all'])
        except Uncertain:
            raise Refused('repository') from None
        if dirty:
            raise Refused('dirty')
        try:
            branch = self._git(entry, cwd, ['symbolic-ref', '--quiet', '--short', 'HEAD'])
        except Uncertain:
            raise Refused('branch') from None
        if (not branch or len(branch) > 255
                or any(ord(char) < 32 or ord(char) == 127 for char in branch)):
            raise Refused('branch')
        try:
            commit = self._git(entry, cwd, ['rev-parse', 'HEAD'])
        except Uncertain:
            raise Refused('commit') from None
        if not _SHA.fullmatch(commit):
            raise Refused('commit')
        try:
            # Without ``--exit-code`` a branch absent from the remote is an
            # empty, successful read: ``unpublished`` is a definitive,
            # retryable refusal. A failed remote read is read-only too, so it
            # is also a refusal: this step never leaves an ambiguous intent
            # that only H3 reconciliation could settle.
            remote = self._git(entry, cwd,
                               ['ls-remote', 'origin',
                                'refs/heads/' + branch])
        except Uncertain:
            raise Refused('unpublished') from None
        first = remote.split()[0] if remote.split() else ''
        if first != commit:
            raise Refused('unpublished')
        return {'repository': repository, 'branch': branch,
                'commit': commit, 'remote_commit': commit, 'clean': True}

    def _transfer(self, request, entry):
        repository = self.git.repository(request['repository'])
        cwd, root = self._target_cwd(request['agent'], request['project'],
                                     entry)
        owner = self._owner(entry)
        current = None
        if cwd.exists() or cwd.is_symlink():
            current = _stat_at(root, cwd, owner=owner)
        if current is not None:
            if (stat.S_ISLNK(current.st_mode)
                    or not stat.S_ISDIR(current.st_mode)):
                raise Refused('cwd')
            try:
                remote_url = self._git(entry, cwd, ['config', '--get',
                                                    'remote.origin.url'])
                head = self._git(entry, cwd, ['rev-parse', 'HEAD'])
                dirty = self._git(entry, cwd, ['status', '--porcelain',
                                               '--untracked-files=all'])
            except Uncertain:
                raise Refused('cwd') from None
            if remote_url != repository:
                raise Refused('cwd')
            try:
                branch = self._git(entry, cwd,
                                   ['symbolic-ref', '--quiet', '--short', 'HEAD'])
            except Uncertain:
                raise Refused('branch') from None
            if branch != request['branch']:
                # The same commit on another branch (or a detached HEAD) is
                # never adopted silently: the worktree must be this branch.
                raise Refused('branch')
            if head == request['commit'] and not dirty:
                return {'repository': repository, 'branch': request['branch'],
                        'commit': request['commit'], 'clean': True, 'cwd': str(cwd)}
            raise Refused('cwd_occupied')
        parent = cwd.parent
        self._ensure_work_parent(parent, entry, root=root)
        parent_fd = _open_dir(root, parent, owner=owner)
        temporary = parent / ('.' + cwd.name + '.handoff-' + hashlib.sha256(
            request['action_id'].encode()).hexdigest()[:12])
        try:
            try:
                info = os.stat(temporary.name, dir_fd=parent_fd,
                               follow_symlinks=False)
            except FileNotFoundError:
                info = None
            if info is not None:
                marker = temporary / '.git' / 'ameesh-handoff-intent'
                try:
                    marker_info = _stat_at(root, marker, owner=owner)
                except Refused:
                    raise Refused('cwd') from None
                if (stat.S_ISLNK(info.st_mode)
                        or not stat.S_ISDIR(info.st_mode)
                        or marker_info is None
                        or not stat.S_ISREG(marker_info.st_mode)):
                    raise Refused('cwd')
                try:
                    record = _read_private_json(marker, root=root, owner=owner)
                except Refused:
                    raise Refused('cwd') from None
                if record.get('action_id') != request['action_id']:
                    raise Refused('cwd')
                _remove_tree_at(parent_fd, temporary.name)
            keep = False
            try:
                os.mkdir(temporary.name, 0o700, dir_fd=parent_fd)
                if os.geteuid() == 0:
                    _own_at(temporary, owner, directory=True, root=root)
                self._git(entry, temporary, ['init', '--quiet'])
                marker = temporary / '.git' / 'ameesh-handoff-intent'
                _atomic_json(marker, {'action_id': request['action_id']},
                             root=root, owner=owner)
                self._git(entry, temporary,
                          ['remote', 'add', 'origin', repository])
                self._git(entry, temporary,
                          ['fetch', '--no-tags', '--quiet', 'origin',
                           request['branch']])
                self._git(entry, temporary,
                          ['checkout', '--quiet', '-B', request['branch'],
                           'FETCH_HEAD'])
                if self._git(entry, temporary,
                             ['rev-parse', 'HEAD']) != request['commit']:
                    raise Refused('commit')
                if self._git(entry, temporary,
                             ['status', '--porcelain', '--untracked-files=all']):
                    raise Refused('dirty')
                _unlink_private(marker, root=root, owner=owner)
                if _stat_at(root, cwd, owner=owner) is not None:
                    raise Refused('cwd')
                os.replace(temporary.name, cwd.name,
                           src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            except Uncertain:
                # The temporary tree carries its exact action marker and the
                # durable intent is on disk: the next pass removes only its
                # own tree and resumes instead of cloning a second one.
                keep = True
                raise
            finally:
                if not keep:
                    try:
                        os.stat(temporary.name, dir_fd=parent_fd,
                                follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    except OSError:
                        pass
                    else:
                        try:
                            _remove_tree_at(parent_fd, temporary.name)
                        except OSError:
                            pass
        finally:
            os.close(parent_fd)
        return {'repository': repository, 'branch': request['branch'],
                'commit': request['commit'], 'clean': True, 'cwd': str(cwd)}

    # -- session -----------------------------------------------------------

    def _session(self, request, entry):
        transfer = self._step_receipt(self.operations / request['operation'], 'transfer')
        if (transfer is None or transfer.get('cwd') != request['cwd']
                or transfer.get('commit') != request['commit']):
            raise Refused('order')
        if self._active(entry):
            raise Refused('target_active')
        cwd = self._worktree(request['cwd'], entry, must_exist=True)
        try:
            head = self._git(entry, cwd, ['rev-parse', 'HEAD'])
            dirty = self._git(entry, cwd, ['status', '--porcelain',
                                           '--untracked-files=all'])
        except Uncertain:
            raise Refused('checkout') from None
        if head != request['commit']:
            raise Refused('commit')
        if dirty:
            raise Refused('dirty')
        mode = SESSION_MODES.get(request['harness'], 'brief')
        home = self._home(entry)
        owner = self._owner(entry)
        base = _private_chain(Path(home) / '.ameesh-handoff',
                              owner=owner, root=home)
        sessions = _private_chain(base / 'sessions', owner=owner, root=home)
        directory = _private_chain(sessions / request['operation'],
                                   owner=owner, root=home)
        descriptor = _open_dir(home, directory, owner=owner)
        try:
            for name in os.listdir(descriptor):
                if name in CREDENTIAL_FILES:
                    raise Refused('oauth_cache')
        finally:
            os.close(descriptor)
        marker = directory / 'session.json'
        expected = {'version': 1, 'operation': request['operation'],
                    'agent': request['agent'], 'harness': request['harness'],
                    'account': request['account'], 'mode': mode,
                    'source_session': None, 'oauth_cache': False}
        if marker.exists():
            if _read_private_json(marker, owner=owner, root=home) != expected:
                raise Refused('session')
        else:
            _atomic_json(marker, expected, owner=owner, root=home)
        return {'mode': mode, 'executor_enabled': False,
                'oauth_cache_transferred': False}

    # -- activation and health (H3) ---------------------------------------

    def _dsh_profile_check(self, harness, agent, entry, home):
        """Observe the private dsh profile prepared by H2; never create it."""
        if harness not in ('deepseek', 'dsh'):
            return True
        owner = self._owner(entry)
        base = _private_chain(Path(home) / '.ameesh-handoff',
                              owner=owner, root=home)
        directory = _private_chain(base / 'dsh', owner=owner, root=home)
        marker = directory / 'profile.json'
        expected = {'version': 1, 'agent': agent, 'harness': harness,
                    'credentials': False}
        if marker.is_symlink() or not marker.exists():
            raise Refused('dsh_profile')
        if _read_private_json(marker, owner=owner, root=home) != expected:
            raise Refused('dsh_profile')
        descriptor = _open_dir(home, directory, owner=owner)
        try:
            for name in os.listdir(descriptor):
                if name in CREDENTIAL_FILES:
                    raise Refused('dsh_credentials')
        finally:
            os.close(descriptor)
        return True

    def _session_credentials_ok(self, entry, operation):
        """Refuse a prepared session directory that carries a credential."""
        home = self._home(entry)
        owner = self._owner(entry)
        base = _private_chain(Path(home) / '.ameesh-handoff',
                              owner=owner, root=home)
        sessions = _private_chain(base / 'sessions', owner=owner, root=home)
        directory = _private_chain(sessions / operation, owner=owner, root=home)
        descriptor = _open_dir(home, directory, owner=owner)
        try:
            for name in os.listdir(descriptor):
                if name in CREDENTIAL_FILES:
                    raise Refused('oauth_cache')
        finally:
            os.close(descriptor)

    def _admit(self, request, entry):
        """Recheck the prepared destination without changing or starting it."""
        prepare = self._step_receipt(self.operations / request['operation'],
                                     'prepare')
        if prepare is None:
            raise Refused('order')
        if not self._tooling(request['tools']):
            raise Refused('tooling')
        if not all(alias in self.enrollment['secrets']
                   for alias in request['secrets']):
            raise Refused('credentials')
        caps = self.system.show(entry['scope'], entry['account'], entry['unit'],
                                ('LoadState', 'ActiveState', 'MemoryMax',
                                 'MemoryHigh', 'Slice'))
        if not _caps_ready(caps):
            raise Refused('caps')
        if caps.get('ActiveState') in ('active', 'activating', 'reloading'):
            raise Refused('target_active')
        release = self._release_revision()
        if release != prepare.get('release_revision'):
            raise Refused('release')
        dsh = self._dsh_profile_check(request['harness'], request['agent'],
                                      entry, self._home(entry))
        return {'release_revision': release, 'credentials_ready': True,
                'caps_ready': True, 'tooling_ready': True,
                'dsh_profile_ready': dsh, 'executor_enabled': False}

    def _enable(self, request, entry):
        """Start the exact destination unit and prove it active with its caps.

        The controller holds the destination gate publication lock around this
        call; a queued/late start from a revoked epoch is fenced by the
        journal state before it can act. A unit already active (a replayed
        receipt or a completed first start) is accepted only after the exact
        checkout and caps are re-observed. A timeout is uncertain: the start
        may still complete and reconciliation must quiesce it.
        """
        transfer = self._step_receipt(self.operations / request['operation'],
                                      'transfer')
        session = self._step_receipt(self.operations / request['operation'],
                                     'session')
        if transfer is None or session is None:
            raise Refused('order')
        cwd, commit = transfer.get('cwd'), transfer.get('commit')
        if not isinstance(cwd, str) or not isinstance(commit, str) \
                or not _SHA.fullmatch(commit):
            raise Refused('order')
        try:
            head = self._git(entry, cwd, ['rev-parse', 'HEAD'])
            dirty = self._git(entry, cwd,
                              ['status', '--porcelain', '--untracked-files=all'])
        except Uncertain:
            raise Refused('checkout') from None
        if head != commit or dirty:
            raise Refused('checkout')
        self._session_credentials_ok(entry, request['operation'])
        if not self._active(entry):
            self.system.start(entry['scope'], entry['account'], entry['unit'])
        deadline = time.monotonic() + self.enable_seconds
        while time.monotonic() < deadline:
            caps = self.system.show(
                entry['scope'], entry['account'], entry['unit'],
                ('LoadState', 'ActiveState', 'MemoryMax', 'MemoryHigh', 'Slice'))
            if caps.get('ActiveState') == 'failed':
                raise Refused('enable')
            if caps.get('ActiveState') == 'active' and _caps_ready(caps):
                return {'unit': entry['unit'], 'active': True,
                        'executor_enabled': True}
            time.sleep(0.2)
        raise Uncertain('enable:deadline')

    def _verify(self, request, entry):
        """Observe the exact checkout, live unit and caps after the first turn.

        The authenticated turn itself is proven by the controller's durable
        turn ledger, not by this host observation.
        """
        transfer = self._step_receipt(self.operations / request['operation'],
                                      'transfer')
        session = self._step_receipt(self.operations / request['operation'],
                                     'session')
        if transfer is None or session is None:
            raise Refused('order')
        commit = request['commit']
        cwd = transfer.get('cwd')
        if transfer.get('commit') != commit or not isinstance(cwd, str):
            raise Refused('commit')
        try:
            head = self._git(entry, cwd, ['rev-parse', 'HEAD'])
            if head != commit:
                # The destination's own first turn may have committed new
                # work; the exact transfer commit must remain an ancestor,
                # never a reset or another branch.
                self._git(entry, cwd, ['merge-base', '--is-ancestor',
                                       commit, head])
        except Uncertain:
            raise Refused('checkout') from None
        caps = self.system.show(entry['scope'], entry['account'], entry['unit'],
                                ('LoadState', 'ActiveState', 'MemoryMax',
                                 'MemoryHigh', 'Slice'))
        if caps.get('ActiveState') != 'active' or not _caps_ready(caps):
            raise Refused('target_inactive')
        self._session_credentials_ok(entry, request['operation'])
        return {'unit': entry['unit'], 'commit': commit,
                'executor_enabled': True, 'caps_ready': True}

    # -- host facts --------------------------------------------------------

    def _tooling(self, tools):
        return all(self._tool(name) for name in tools)

    def _tool(self, name):
        return (name in self.enrollment['tools']
                and shutil.which(name, path=SAFE_PATH) is not None)

    def _home(self, entry):
        path = Path(entry['home'])
        try:
            info = path.lstat()
        except OSError:
            raise Refused('home') from None
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
                or info.st_mode & 0o022):
            raise Refused('home')
        return path

    def _owner(self, entry):
        try:
            record = pwd.getpwnam(entry['account'])
        except KeyError:
            raise Refused('account') from None
        return record.pw_uid, record.pw_gid

    def _active(self, entry):
        fields = self.system.show(entry['scope'], entry['account'], entry['unit'],
                                  ('LoadState', 'ActiveState'))
        return fields.get('ActiveState') in ('active', 'activating', 'reloading')

    def _git(self, entry, cwd, args, *, check=True):
        return self.git.run(entry['account'], entry['home'], cwd, args, check=check)

    def _worktree(self, value, entry, *, must_exist, with_root=False):
        path = Path(value)
        owner = self._owner(entry)
        if not path.is_absolute() or '..' in path.parts:
            raise Refused('cwd')
        root = next((Path(root) for root in self.enrollment['roots']
                     if str(path).startswith(root.rstrip('/') + '/')), None)
        if root is None:
            raise Refused('cwd')
        try:
            probe = _open_root(root, owner=owner)
        except Refused:
            raise Refused('cwd') from None
        else:
            os.close(probe)
        try:
            relative = path.relative_to(root)
        except ValueError:
            raise Refused('cwd') from None
        current = root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise Refused('cwd')
        if must_exist:
            info = _stat_at(root, path, owner=owner)
            if info is None or not stat.S_ISDIR(info.st_mode):
                raise Refused('cwd')
        if with_root:
            return path, root
        return path

    def _target_cwd(self, agent, project, entry):
        if self.work_dir is not None:
            value = self.work_dir(agent, project)
        else:
            value = self._canon_work_dir(agent, project)
        if not isinstance(value, str) or not value:
            raise Refused('cwd')
        if value.startswith('~/'):
            value = entry['home'].rstrip('/') + value[1:]
        return self._worktree(value, entry, must_exist=False, with_root=True)

    def _canon_work_dir(self, agent, project):
        """The receiving host's own canonical Work directory (L31/L35)."""
        if self._canons is None:
            from ameesh import canon as canon_mod, config as config_mod
            try:
                cfg = config_mod.load(env={'AMEESH_CONFIG': self.enrollment['config']})
                self._canons = (canon_mod.load_configured(cfg), canon_mod)
            except Exception:
                raise Refused('canon') from None
        canons, canon_mod = self._canons
        host = canon_mod.find_host(canons, self.host)
        card = next((found for found in (c.agent(agent) for c in canons)
                     if found is not None), None)
        if host is None or card is None:
            raise Refused('canon')
        value, _ = canon_mod.work_dir_for(host, card, None)
        return value

    def _ensure_work_parent(self, parent, entry, *, root):
        owner = self._owner(entry)
        existed = parent.exists()
        descriptor = None
        try:
            descriptor = _open_dir(root, parent, create=True, owner=owner)
        except Refused:
            raise Refused('cwd') from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if not existed and os.geteuid() == 0:
            # Only a directory this executor just created is handed to the
            # account; an enrolled shared root keeps its own ownership. The
            # walk reopens every component from the trusted root, so an
            # ancestor swapped before this call is refused, not followed.
            _own_at(parent, owner, directory=True, root=root)

    def _release_revision(self):
        """Read the pinned release receipt; never activate or switch it."""
        if self._release is None:
            path = Path(__file__).absolute().parent / 'environnement-versionne.py'
            try:
                _trusted_file(path)
                spec = importlib.util.spec_from_file_location(
                    'ameesh_handoff_release', path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            except (Refused, OSError):
                raise Refused('release') from None
            except Exception:
                raise Refused('release') from None
            self._release = module
        store = Path(self.enrollment['release_store'])
        current = store / 'current'
        if current.is_symlink():
            release_id = os.readlink(current)
        else:
            raise Refused('release')
        try:
            root = self._release.completed(store, release_id)
            manifest = json.loads((root / 'ready.json').read_text())
        except Exception:
            raise Refused('release') from None
        commit = manifest.get('commit') if isinstance(manifest, dict) else None
        if not isinstance(commit, str) or _SHA.fullmatch(commit) is None:
            raise Refused('release')
        return commit


def _caps_ready(fields):
    """A finite positive memory cap on the exact enrolled runner unit."""
    if fields.get('LoadState') != 'loaded':
        return False
    try:
        maximum = int(fields.get('MemoryMax') or '')
        high = int(fields.get('MemoryHigh') or '')
    except (TypeError, ValueError):
        return False
    return maximum > 0 and high > 0


def _same_effect(stored, request):
    """A retry may raise the controller epoch; nothing else may change.

    The journal round-trips through JSON, so tuples and lists must compare
    canonically rather than by Python type.
    """
    if not isinstance(stored, dict):
        return False
    for key in set(stored) | set(request):
        if key == 'controller_epoch':
            continue
        left = json.loads(json.dumps(stored.get(key), sort_keys=True))
        right = json.loads(json.dumps(request.get(key), sort_keys=True))
        if left != right:
            return False
    return True


# ---------------------------------------------------------------------------
# The controller-side companion
# ---------------------------------------------------------------------------

class HostAdapter:
    """The enrolled adapter; host effects go through the pinned bridge."""

    def __init__(self, *, db=None, enrollment=None, hosts=None, references=None,
                 repositories=None, transports=None, bridge=None,
                 enrollment_root=None):
        self.db = db
        self.enrollment = dict(enrollment or {})
        self.enrollment_root = Path(enrollment_root or ENROLLMENT_ROOT)
        self.host = self.enrollment.get('host')
        self.hosts = dict(hosts or {})
        self.references = dict(references or {})
        self.repositories = dict(repositories or {})
        self.transports = dict(transports or {})
        self._plan = {}
        self.bridge = bridge if bridge is not None else self._bridge()

    # -- discovery ---------------------------------------------------------

    def inventory(self, host):
        if not _name(host) or host not in self.hosts:
            raise Refused('identity:host')
        tools, secrets = set(), set()
        for entry in self.hosts[host].values():
            tools.update(entry['tools'])
            secrets.update(entry['secrets'])
        request = {'step': 'inventory', 'host': host, 'tools': sorted(tools),
                   'secrets': sorted(secrets)}
        receipt = self.bridge.inventory(host, sorted(tools), sorted(secrets))
        return handoff_contract.validate_inventory_receipt(receipt, request)

    def repository(self, agent):
        raise Refused('repository')  # the factory owns the enrolled lookup

    # -- checks ------------------------------------------------------------

    def revalidate(self, operation):
        try:
            missing = self._prerequisites(operation, step='revalidate')
        except (Refused, BridgeError, handoff_contract.ContractError):
            return False
        if missing:
            return False
        try:
            receipt = self._call(operation, 'revalidate',
                                 {'state': operation['state']},
                                 target=_target(operation))
        except (Refused, BridgeError, handoff_contract.ContractError):
            return False
        return receipt.get('ok') is True

    def prepare(self, operation):
        try:
            missing = self._prerequisites(operation, step='prepare')
        except (Refused, handoff_contract.ContractError):
            return {'ok': False, 'reason': 'prerequisites'}
        if missing:
            return {'ok': False, 'reason': 'prerequisites'}
        entry = self._entry(operation['target'])
        request = self._wire(operation, 'prepare', target=_target(operation), extra={
            'harness': operation['plan']['harness'],
            'account': _account(operation['plan']),
            'tools': sorted(entry['tools']), 'secrets': sorted(entry['secrets'])})
        receipt = self._call(operation, 'prepare', request, target=_target(operation),
                             validated=True)
        if receipt.get('ok') is not True:
            return receipt
        if (receipt.get('credentials_ready') is not True
                or receipt.get('caps_ready') is not True
                or receipt.get('tooling_ready') is not True):
            return {'ok': False, 'reason': 'prepare'}
        if (operation['plan']['harness'] in SESSION_MODES
                and receipt.get('dsh_profile_ready') is not True):
            return {'ok': False, 'reason': 'dsh_profile'}
        return {'ok': True,
                'canon_revision': operation['plan']['canon_revision'],
                'release_revision': receipt['release_revision'],
                'credentials_ready': True, 'caps_ready': True,
                'tooling_ready': True,
                'dsh_profile_ready': receipt.get('dsh_profile_ready') is True}

    def stop_source(self, operation):
        try:
            self._identity(operation, _source(operation))
            self._intent(operation, 'stop_source')
        except Refused:
            return False
        # A bridge or contract failure is ambiguous (the stop may have run and
        # the reply been lost or the alarm expired): let it propagate so the
        # controller keeps its durable intent for reconciliation. Only an
        # explicit ``ok: false`` receipt is a definitive refusal.
        receipt = self._call(operation, 'stop_source', {},
                             target=_source(operation))
        if receipt.get('ok') is not True:
            return False
        return (receipt.get('stopped') is True and receipt.get('quiet') is True
                and receipt.get('executor_enabled') is False)

    def publish(self, operation):
        try:
            self._identity(operation, _source(operation))
            self._intent(operation, 'publish')
        except Refused:
            return {'ok': False, 'reason': 'identity'}
        plan = operation['plan']
        receipt = self._call(operation, 'publish', {
            'repository': plan['repository'], 'cwd': plan.get('source_cwd') or ''},
            target=_source(operation))
        if receipt.get('ok') is not True:
            return receipt
        return dict({'ok': True}, **{key: receipt[key] for key in
                    ('repository', 'branch', 'commit', 'remote_commit', 'clean')})

    def transfer(self, operation):
        try:
            self._identity(operation, _target(operation))
            self._intent(operation, 'transfer')
        except Refused:
            return {'ok': False, 'reason': 'identity'}
        plan, evidence = operation['plan'], operation['evidence']
        for key in ('branch', 'commit'):
            if not evidence.get(key):
                return {'ok': False, 'reason': 'publish'}
        receipt = self._call(operation, 'transfer', {
            'repository': plan['repository'], 'branch': evidence['branch'],
            'commit': evidence['commit'], 'project': plan.get('project') or ''},
            target=_target(operation))
        if receipt.get('ok') is not True:
            return receipt
        return dict({'ok': True}, **{key: receipt[key] for key in
                    ('repository', 'branch', 'commit', 'clean', 'cwd')})

    def session(self, operation):
        try:
            self._identity(operation, _target(operation))
            self._intent(operation, 'session')
        except Refused:
            return {'ok': False, 'reason': 'identity'}
        plan, evidence = operation['plan'], operation['evidence']
        for key in ('commit', 'cwd'):
            if not evidence.get(key):
                return {'ok': False, 'reason': 'transfer'}
        receipt = self._call(operation, 'session', {
            'harness': plan['harness'],
            'account': _account(plan),
            'cwd': evidence['cwd'], 'commit': evidence['commit']},
            target=_target(operation))
        if receipt.get('ok') is not True:
            return receipt
        expected = SESSION_MODES.get(plan['harness'], 'brief')
        if receipt.get('mode') != expected or receipt.get('executor_enabled') is not False:
            return {'ok': False, 'reason': 'session'}
        if receipt.get('oauth_cache_transferred') is not False:
            return {'ok': False, 'reason': 'oauth_cache'}
        return {'ok': True, 'mode': expected, 'executor_enabled': False}

    # -- H3: activation, health and reconciliation ------------------------

    def admit(self, operation):
        """Recheck the prepared destination without starting it."""
        try:
            self._identity(operation, _target(operation))
            self._intent(operation, 'admit')
            missing = self._prerequisites(operation, step='admit')
        except (Refused, handoff_contract.ContractError):
            return {'ok': False, 'reason': 'prerequisites'}
        if missing:
            return {'ok': False, 'reason': 'prerequisites'}
        plan = operation['plan']
        entry = self._entry(_target(operation))
        request = self._wire(operation, 'admit', target=_target(operation), extra={
            'harness': plan['harness'], 'account': _account(plan),
            'tools': sorted(entry['tools']), 'secrets': sorted(entry['secrets'])})
        receipt = self._call(operation, 'admit', request,
                             target=_target(operation), validated=True)
        if receipt.get('ok') is not True:
            return receipt
        if (receipt.get('credentials_ready') is not True
                or receipt.get('caps_ready') is not True
                or receipt.get('tooling_ready') is not True):
            return {'ok': False, 'reason': 'admit'}
        if (plan['harness'] in SESSION_MODES
                and receipt.get('dsh_profile_ready') is not True):
            return {'ok': False, 'reason': 'dsh_profile'}
        if receipt.get('release_revision') != operation['evidence'].get('release_revision'):
            return {'ok': False, 'reason': 'release'}
        return {'ok': True, 'canon_revision': plan['canon_revision'],
                'release_revision': receipt['release_revision'],
                'credentials_ready': True, 'caps_ready': True,
                'tooling_ready': True,
                'dsh_profile_ready': receipt.get('dsh_profile_ready') is True}

    def enable(self, operation):
        """Start the exact destination unit through the enrolled helper."""
        try:
            self._identity(operation, _target(operation))
            self._intent(operation, 'enable')
            missing = self._prerequisites(operation, step='enable')
        except (Refused, handoff_contract.ContractError):
            return {'ok': False, 'reason': 'prerequisites'}
        if missing:
            return {'ok': False, 'reason': 'prerequisites'}
        receipt = self._call(operation, 'enable', {}, target=_target(operation))
        if receipt.get('ok') is not True:
            return receipt
        if (receipt.get('active') is not True
                or receipt.get('executor_enabled') is not True):
            return {'ok': False, 'reason': 'enable'}
        return True

    def verify(self, operation):
        """Prove the first authenticated destination turn and its host state.

        The host receipt proves the live unit, exact checkout and caps; the
        durable turn ledger proves an authenticated turn on the target after
        the switch. While that turn is still running the receipt is
        ``pending`` and the controller keeps the operation active.
        """
        try:
            self._identity(operation, _target(operation))
            self._intent(operation, 'verify')
            missing = self._prerequisites(operation, step='verify')
        except (Refused, handoff_contract.ContractError):
            return {'ok': False, 'reason': 'prerequisites'}
        if missing:
            return {'ok': False, 'reason': 'prerequisites'}
        commit = operation['evidence'].get('commit')
        if not isinstance(commit, str) or not _SHA.fullmatch(commit):
            return {'ok': False, 'reason': 'verify'}
        receipt = self._call(operation, 'verify', {'commit': commit},
                             target=_target(operation))
        if receipt.get('ok') is not True:
            return receipt
        if (receipt.get('commit') != commit
                or receipt.get('executor_enabled') is not True
                or receipt.get('caps_ready') is not True):
            return {'ok': False, 'reason': 'verify'}
        turn = self._turn_proof(operation)
        if turn is None:
            return {'ok': True, 'pending': True}
        return {'ok': True, 'commit': commit, 'turn_completed': True,
                'authenticated': turn.get('authenticated') is True,
                'caps_ready': True}

    def restore_source(self, operation):
        """Return the source unit to service before a cancellation."""
        try:
            self._identity(operation, _source(operation))
            self._intent(operation, 'restore_source')
        except Refused:
            return False
        receipt = self._call(operation, 'restore_source', {},
                             target=_source(operation))
        if receipt.get('ok') is not True:
            return False
        return (receipt.get('active') is True
                and receipt.get('executor_enabled') is True)

    def recover_source(self, operation):
        raise Refused('capability:recover_source')

    def reconcile_effect(self, operation):
        """Quiesce the exact epoch of an interrupted host effect."""
        try:
            self._identity(operation, _target(operation))
        except Refused:
            return {'ok': False, 'reason': 'identity'}
        pending = (operation.get('evidence') or {}).get('pending_effect')
        if (not isinstance(pending, dict)
                or not isinstance(pending.get('name'), str)
                or not isinstance(pending.get('epoch'), int)
                or isinstance(pending.get('epoch'), bool)
                or pending['epoch'] < 1):
            return {'ok': False, 'reason': 'intent'}
        return self._call(operation, 'reconcile_effect', {
            'pending_step': pending['name'], 'pending_epoch': pending['epoch']},
            target=_target(operation))

    def _turn_proof(self, operation):
        """The destination's first authenticated turn, from the turn ledger.

        The switch event is the durable boundary. The latest turn recorded on
        the target host after it is the evidence: while it runs, the receipt
        stays pending; once terminal, its cost line decides. A running turn
        never falls through to an older failed one — after a login repair a
        new attempt is in flight, not a proof of failure.

        A terminal turn (``done`` or ``orphan``) without a cost line is not
        skipped either: that is the missing-login/crashed-before-usage case the
        proof must catch, and skipping it left ``verify`` pending forever. The
        runner closes the resource before committing the ledger line, so a
        just-ended turn is still given the short ``TURN_SETTLE_SECONDS`` window;
        after it the turn is returned as ``authenticated: false`` and holds the
        operation. The source is fenced and cannot produce a target turn.
        """
        if self.db is None:
            return None
        rows = self.db.query(
            "SELECT created_at FROM host_handoff_events"
            " WHERE operation=%s AND state='switched'"
            " ORDER BY id DESC LIMIT 1", (operation['id'],))
        if not rows:
            return None
        switched = rows[0]['created_at']
        rows = self.db.query(
            "SELECT tr.turn_id, tr.status, tr.ended_at,"
            " extract(epoch from now() - tr.ended_at)::float8 AS ended_ago,"
            " tc.id AS cost_id,"
            " tc.input_tokens, tc.cached_input_tokens, tc.output_tokens"
            " FROM turn_resources tr"
            " LEFT JOIN turn_costs tc ON tc.turn_id = tr.turn_id"
            " WHERE tr.agent=%s AND tr.host=%s AND tr.started_at >= %s"
            " ORDER BY tr.started_at DESC, tr.id DESC LIMIT 20",
            (operation['agent'], operation['target'], switched))
        for row in rows:
            if (row.get('status') not in ('done', 'orphan')
                    or row.get('ended_at') is None):
                # The latest turn is still running: its outcome is the proof.
                return None
            if row.get('cost_id') is None:
                ago = row.get('ended_ago')
                if (isinstance(ago, (int, float)) and not isinstance(ago, bool)
                        and ago < TURN_SETTLE_SECONDS):
                    return None
                return {'authenticated': False}
            tokens = sum(int(row.get(key) or 0) for key in
                         ('input_tokens', 'cached_input_tokens',
                          'output_tokens'))
            return {'authenticated': tokens > 0}
        return None

    # -- internals ---------------------------------------------------------

    def _bridge(self):
        path = Path(__file__).absolute().parent / BRIDGE_NAME
        _trusted_file(path)
        try:
            spec = importlib.util.spec_from_file_location(
                'ameesh_handoff_bridge', path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception:
            raise Refused('source:' + BRIDGE_NAME) from None
        local = self._local()
        return module.Bridge(local_host=self.host, transports=self.transports,
                             local=local,
                             enrollment_root=self.enrollment_root)

    def _local(self):
        try:
            execution = load_execution(self.enrollment_root)
        except Refused:
            return None
        return HostEffects(enrollment=execution)

    def _entry(self, host):
        admitted = self.hosts.get(host)
        harness = self._plan.get('harness')
        if not admitted or harness not in admitted:
            raise Refused('identity')
        return admitted[harness]

    def _requirements(self, operation):
        plan = operation['plan']
        host = _target(operation)
        entry = self._entry(host)
        account = _account(plan)
        if account not in entry['accounts']:
            raise Refused('identity:account')
        return host_preflight.Requirements(
            host=host, harness=plan['harness'], account=account,
            tools=entry['tools'], secret_refs=entry['secrets'])

    def _prerequisites(self, operation, *, step):
        """Durable operation, placement, canon, gate and login preflight."""
        if self.db is None:
            raise Refused('db')
        self._plan = operation.get('plan') or {}
        if not isinstance(self._plan, dict):
            raise Refused('plan')
        api = storage.of(self.db)
        row = api.handoffs.get(operation.get('id'))
        if not row:
            raise Refused('operation')
        if row['state'] != operation['state']:
            raise Refused('state')
        pending = row['evidence'].get('pending_effect')
        if (not isinstance(pending, dict) or pending.get('name') != step
                or pending.get('epoch') != operation.get('controller_epoch')):
            raise Refused('intent')
        for key in ('repository', 'project', 'canon', 'canon_revision',
                    'harness', 'source', 'target', 'owner', 'epoch', 'reason'):
            if row['plan'].get(key) != self._plan.get(key):
                raise Refused('plan')
        self._enrolled_repository(operation)
        if (row['memory_bytes'] != self._plan.get('memory_bytes')
                or row['disk_bytes'] != self._plan.get('disk_bytes')):
            raise Refused('reservation')
        source, target = _source(operation), _target(operation)
        pre_switch = operation['state'] not in ('switched', 'activated', 'verified')
        if pre_switch:
            from ameesh import registry
            agent = registry.get(self.db, operation['agent'])
            if (agent is None
                    or target not in (agent.get('admitted_hosts') or ())
                    or agent.get('mode') != 'execute'
                    or agent.get('status') in ('stopped', 'paused')):
                raise Refused('placement')
            if (projects.agent_project(agent) or agent.get('project')
                    ) != self._plan.get('project'):
                raise Refused('placement')
            if self._plan.get('reason') != 'host_loss' and (
                    agent.get('host') != source
                    or agent.get('lease_owner') != self._plan.get('owner')
                    or agent.get('lease_epoch') != self._plan.get('epoch')):
                raise Refused('placement')
        state = api.canon.state(target, self._plan['canon'])
        if (not state or state.get('status') != 'ok'
                or state.get('last_good_commit') != self._plan['canon_revision']):
            raise Refused('canon')
        if pre_switch and self._plan.get('reason') != 'host_loss':
            source_state = api.canon.state(source, self._plan['canon'])
            if (not source_state or source_state.get('status') != 'ok'
                    or source_state.get('last_good_commit') != self._plan['canon_revision']):
                raise Refused('canon')
        reading = api.hosts.latest(target)
        if (not reading or not _fresh(reading, time.time())
                or not relocation.gate_open(reading,
                    {} if self._plan.get('requires_gate') else None)):
            raise Refused('gate')
        inventory = self.inventory(target)
        credentials = api.credentials.listing(target)
        check = host_preflight.check(self._requirements(operation), inventory,
                                     credentials, now=time.time())
        return list(check['missing'])

    @staticmethod
    def _intent(operation, step):
        """The controller's durable effect intent must name this exact step."""
        evidence = operation.get('evidence')
        if not isinstance(evidence, dict):
            raise Refused('intent')
        pending = evidence.get('pending_effect')
        if (not isinstance(pending, dict) or pending.get('name') != step
                or pending.get('epoch') != operation.get('controller_epoch')):
            raise Refused('intent')

    def _enrolled_repository(self, operation):
        """The plan's repository is this project's exact enrolled identity.

        ``--repository`` is a caller choice; it never widens the enrollment.
        The durable row is checked too, so a mismatch between the operation and
        ``repositories.json`` cannot be hidden by writing the same wrong value
        in both places.
        """
        plan = operation.get('plan') or {}
        project = plan.get('project')
        if not _name(project) or project not in self.repositories:
            raise Refused('repository')
        if plan.get('repository') != self.repositories[project]:
            raise Refused('repository')
        return self.repositories[project]

    def _identity(self, operation, host):
        plan = operation.get('plan') or {}
        self._plan = plan
        if plan.get('target') != _target(operation) or plan.get('source') != _source(operation):
            raise Refused('plan')
        self._enrolled_repository(operation)
        if not _name(operation.get('agent')) or host not in self.hosts:
            raise Refused('identity')
        if plan.get('harness') not in self.hosts[host]:
            raise Refused('identity')

    def _wire(self, operation, step, *, target, extra=None):
        fields = {'step': step, 'host': target, 'operation': operation['id'],
                  'action_id': operation['id'] + ':' + step,
                  'controller_epoch': operation['controller_epoch'],
                  'agent': operation['agent'],
                  'owner': operation['plan'].get('owner') or '',
                  'lease_epoch': operation['plan'].get('epoch')}
        fields.update(extra or {})
        return handoff_contract.validate_effect_request(fields)

    def _call(self, operation, step, fields, *, target, validated=False):
        if not validated:
            fields = self._wire(operation, step, target=target, extra=fields)
        receipt = self.bridge.perform(target, fields)
        return handoff_contract.validate_effect_receipt(receipt, fields)


def _target(operation):
    return operation['target']


def _source(operation):
    return operation['source']


def _account(plan):
    """H4 raccord: the destination account is frozen in the plan.

    The switch clears the registry's source `session_account`; a destination
    effect must never re-read it. H4 lands `plan.account` from the same agent
    snapshot as `plan.session_account`; both are equal when present, and any
    divergence refuses rather than picking one.
    """
    if not isinstance(plan, Mapping):
        raise Refused('identity:account')
    account = plan.get('account')
    session = plan.get('session_account')
    if account is None:
        account = session if session is not None else ''
    if session is None:
        session = account
    if (not isinstance(account, str) or not isinstance(session, str)
            or (account and session and account != session)):
        raise Refused('identity:account')
    return account or session or ''


def _fresh(reading, now, max_age=60.0):
    stamp = reading.get('sampled_ts') if reading else None
    return (isinstance(stamp, (int, float)) and not isinstance(stamp, bool)
            and math.isfinite(stamp) and 0 <= now - stamp <= max_age)


# ---------------------------------------------------------------------------
# The root stdin dispatcher installed by H7
# ---------------------------------------------------------------------------


def execute(request, *, root=ENROLLMENT_ROOT):
    execution = load_execution(root)
    effects = HostEffects(enrollment=execution)
    if request.get('step') == 'inventory':
        return effects.inventory(request)
    return effects.perform(request)


def main(argv=None):
    def expired(signum, frame):
        # The alarm may fire inside an effect: the stop/Git/publish may have
        # run and only its reply is missing. That is an uncertain outcome, not
        # a definitive refusal: keep the durable intent and fail the transport
        # so the controller reconciles instead of clearing ``pending_effect``.
        raise Uncertain('deadline')

    argv = sys.argv[1:] if argv is None else list(argv)
    try:
        if argv:
            raise Refused('arguments')
        signal.signal(signal.SIGALRM, expired)
        # The alarm covers the effect, not only the stdin read: a stalled
        # git/systemd child is killed by _bounded_run and the durable intent
        # stays for explicit reconciliation.
        signal.alarm(SECONDS)
        data = sys.stdin.buffer.read(MAX_RECEIPT_BYTES + 1)
        if len(data) > MAX_RECEIPT_BYTES:
            raise Refused('request')
        request = json.loads(data)
        if not isinstance(request, dict):
            raise Refused('request')
        receipt = execute(request)
        signal.alarm(0)
        print(json.dumps(receipt, sort_keys=True))
        return 0
    except Exception:
        signal.alarm(0)
        print('{"error":"host_action_unavailable"}')
        return 1


if __name__ == '__main__':
    sys.exit(main())
