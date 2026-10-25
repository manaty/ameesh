#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""H1/H2: the pinned enrolled factory for host handoffs.

This file is the sole handoff factory source. A controller loads this exact
file, never a module named by a job, a transport message or an enrollment
document. It composes:

- exact private enrollment documents under ``/etc/ameesh-handoff``:
  ``enrollment.json`` (version and controller host), ``hosts.json`` (admitted
  host/harness/account/tool/secret-alias identities), ``repositories.json``
  (project to enrolled repository identity), ``secrets.json`` (adapter alias
  to private ``fichier#CLE`` reference) and ``transports.json`` (one private
  SSH endpoint per other admitted host);
- the fixed companion ``hote-handoff.py`` next to this file, whose methods are
  the enrolled adapter, and the fixed ``pont-handoff.py`` bridge it uses. H2
  fills the preparation/Git effects; H3 fills activation/health. An absent
  effect is an explicit refusal, never a simulated success.

Admission is all-or-nothing: a missing or unsafe document, an unenrolled host,
harness or account, an unknown repository, a privacy-unsafe repository URL or
a missing adapter method refuses before any host effect. A document cannot
choose code: its keys are closed and the companion path is derived from this
file's own location.

This module is source only. It performs no SSH, root operation, DB migration
or production effect, and its tests need neither root nor a real host.
"""
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
from collections.abc import Mapping

from ameesh import handoff_contract, host_preflight, projects
from ameesh.config import NAME_RE


ENROLLMENT_ROOT = Path('/etc/ameesh-handoff')
COMPANION_NAME = 'hote-handoff.py'
#: H2 bridge to the admitted SSH transport and to each host's pinned executor.
BRIDGE_NAME = 'pont-handoff.py'
#: Closed document set: an extra key is refused, never ignored.
DOCUMENTS = {
    'enrollment': ('version', 'host'),
    'hosts': ('hosts',),
    'repositories': ('repositories',),
    'secrets': ('references',),
    'transports': ('transports',),
}
#: The factory owns repository enrollment; the companion supplies the rest.
COMPANION_METHODS = tuple(name for name in handoff_contract.METHODS
                          if name != handoff_contract.REPOSITORY)

#: Same grammar as ``host_preflight.Requirements`` (the consumer), so an
#: account admitted by a document cannot fail later in requirements_for.
_ACCOUNT = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$')
_ALIAS = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$')
#: A tool is a program name; a path is never admitted as a preflight tool.
_TOOL = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')
_STORE_FILE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')
_STORE_KEY = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,127}$')


class FactoryError(ValueError):
    """A handoff factory that cannot honestly bind the enrolled adapter."""


def _owned(info):
    """A document or directory is owned by the controller or by root."""
    return info.st_uid in (os.geteuid(), 0)


class Documents:
    """Exact private JSON documents; bodies never enter errors.

    The enrollment root is the trust anchor (owned by the controller or root,
    never group/world writable, no symlink). Every component below it is
    checked before the document is opened and the final document must be a
    regular file; ancestors above the fixed production root are outside the
    enrollment's own boundary. The open is non-blocking, so a FIFO/device
    swapped in after the lstat cannot hang admission, and the descriptor's
    fstat decides. A document is owned by the controller or root and is
    neither group-writable nor world-accessible, so the L232c system mode
    (0640 root:group) is admitted without widening anything.
    """

    def __init__(self, root=ENROLLMENT_ROOT):
        self.root = Path(root)
        self._anchor()

    def _anchor(self):
        root = self.root
        if not root.is_absolute() or '..' in root.parts:
            raise FactoryError('document:root')
        try:
            info = root.lstat()
        except OSError:
            raise FactoryError('document:root') from None
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
                or not _owned(info) or info.st_mode & 0o022):
            raise FactoryError('document:root')

    def _trusted(self, path):
        try:
            relative = path.relative_to(self.root)
        except ValueError:
            raise FactoryError('document:path') from None
        parts = relative.parts
        if not parts:
            raise FactoryError('document:path')
        current = self.root
        for index, part in enumerate(parts):
            current = current / part
            try:
                info = current.lstat()
            except OSError:
                raise FactoryError('document:missing') from None
            last = index == len(parts) - 1
            expected = stat.S_ISREG if last else stat.S_ISDIR
            if (stat.S_ISLNK(info.st_mode) or not _owned(info)
                    or info.st_mode & 0o022
                    or not expected(info.st_mode)):
                raise FactoryError('document:untrusted')

    def json(self, name, *, exact):
        if name not in DOCUMENTS or tuple(exact) != DOCUMENTS[name]:
            raise FactoryError('document:name')
        path = self.root / (name + '.json')
        try:
            self._trusted(path)
            # O_NONBLOCK bounds a FIFO/device swapped in after the lstat; the
            # fstat below then refuses anything that is not the expected
            # regular file before any byte is read.
            descriptor = os.open(path,
                                 os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except (OSError, FactoryError):
            raise FactoryError('document:' + name) from None
        try:
            with os.fdopen(descriptor, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or not _owned(info)
                        or info.st_mode & 0o027 or info.st_nlink != 1
                        or info.st_size > 65536):
                    raise FactoryError('document:' + name)
                data = stream.read(65537)
        except OSError:
            raise FactoryError('document:' + name) from None
        if len(data) > 65536:
            raise FactoryError('document:' + name)
        try:
            value = json.loads(data)
        except ValueError:
            raise FactoryError('document:' + name) from None
        if not isinstance(value, dict) or set(value) != set(exact):
            raise FactoryError('document:' + name)
        return value


def _name(value):
    return isinstance(value, str) and NAME_RE.fullmatch(value) is not None


def _sequence(value):
    return isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value)


def _hosts(value):
    """Validate admitted host/harness identities and their bounded attributes."""
    if not isinstance(value, Mapping) or not value:
        raise FactoryError('document:hosts')
    result = {}
    for host, harnesses in value.items():
        if not _name(host) or not isinstance(harnesses, Mapping) or not harnesses:
            raise FactoryError('document:hosts')
        admitted = {}
        for harness, entry in harnesses.items():
            if (not _name(harness) or not isinstance(entry, Mapping)
                    or set(entry) != {'accounts', 'tools', 'secrets'}):
                raise FactoryError('document:hosts')
            accounts, tools, secrets = entry['accounts'], entry['tools'], entry['secrets']
            if (not _sequence(accounts) or not accounts
                    or any(account and not _ACCOUNT.fullmatch(account) for account in accounts)
                    or len(set(accounts)) != len(accounts)):
                raise FactoryError('document:hosts')
            if (not _sequence(tools) or len(set(tools)) != len(tools)
                    or any(not _TOOL.fullmatch(name) for name in tools)):
                raise FactoryError('document:hosts')
            if (not _sequence(secrets) or len(set(secrets)) != len(secrets)
                    or any(not _ALIAS.fullmatch(name) for name in secrets)):
                raise FactoryError('document:hosts')
            admitted[harness] = {'accounts': tuple(accounts), 'tools': tuple(tools),
                                 'secrets': tuple(secrets)}
        result[host] = admitted
    return result


def _repository(value):
    """An enrolled repository identity: no credentials, query or control byte."""
    return handoff_contract.repository_identity(value)


def _repositories(value):
    if not isinstance(value, Mapping) or not value:
        raise FactoryError('document:repositories')
    result = {}
    for project, repository in value.items():
        if not _name(project) or not _repository(repository):
            raise FactoryError('document:repositories')
        result[project] = repository
    return result


def _references(value):
    """Aliases are preflight names; references are the private L232c grammar."""
    if not isinstance(value, Mapping):
        raise FactoryError('document:secrets')
    result = {}
    for alias, reference in value.items():
        if not _ALIAS.fullmatch(alias) or not isinstance(reference, str):
            raise FactoryError('document:secrets')
        parts = reference.split('#')
        if (len(parts) != 2 or parts[0] in ('.', '..')
                or not _STORE_FILE.fullmatch(parts[0])
                or not _STORE_KEY.fullmatch(parts[1])):
            raise FactoryError('document:secrets')
        result[alias] = reference
    return result


def _transports(value, hosts, local_host, root):
    """One private SSH endpoint per other admitted host, exact keys only.

    The controller host itself runs the pinned executor locally; every other
    admitted host needs its own endpoint. An endpoint names no command, unit
    or path outside this enrollment: the wire carries only typed effects.
    """
    if not isinstance(value, Mapping):
        raise FactoryError('document:transports')
    result = {}
    for host, endpoint in value.items():
        if not _name(host) or host not in hosts:
            raise FactoryError('document:transports')
        try:
            result[host] = handoff_contract.validate_transport(endpoint, host, root=root)
        except handoff_contract.ContractError:
            raise FactoryError('document:transports') from None
    missing = sorted(set(hosts) - {local_host} - set(result))
    if missing:
        raise FactoryError('document:transports:' + ','.join(missing))
    return result


def _trusted_source(path, name):
    """A companion is a fixed file next to this factory, not group-writable.

    The installed tree is pinned by the H7 enrollment; this check refuses a
    replaced or writable companion without constraining a development
    checkout (whose shared directory mode is not the installation mode).
    """
    path = Path(path)
    if path.is_symlink() or path.name != name:
        raise FactoryError('source:' + name)
    try:
        info = path.lstat()
    except OSError:
        raise FactoryError('source:' + name) from None
    if (not stat.S_ISREG(info.st_mode) or not _owned(info)
            or info.st_mode & 0o022):
        raise FactoryError('source:' + name)
    return path


class EnrolledAdapter:
    """The contract-checked adapter handed to the controller.

    ``repository`` is the factory's enrolled identity lookup; every other
    method delegates to the pinned companion after the same H1 request check.
    A companion refusal stays a refusal: nothing here fabricates ``ok``.
    """

    def __init__(self, factory):
        self.factory = factory

    def inventory(self, host):
        if not _name(host) or host not in self.factory.hosts:
            raise FactoryError('identity:host')
        return self.factory.provider.inventory(host)

    def repository(self, agent):
        return self.factory.repository_for(agent)

    def _request(self, method, operation):
        try:
            handoff_contract.validate_request(operation, method)
        except handoff_contract.ContractError:
            raise FactoryError('contract:' + method) from None
        return getattr(self.factory.provider, method)(operation)

    def revalidate(self, operation):
        return self._request('revalidate', operation)

    def prepare(self, operation):
        return self._request('prepare', operation)

    def stop_source(self, operation):
        return self._request('stop_source', operation)

    def publish(self, operation):
        return self._request('publish', operation)

    def transfer(self, operation):
        return self._request('transfer', operation)

    def session(self, operation):
        return self._request('session', operation)

    def admit(self, operation):
        return self._request('admit', operation)

    def enable(self, operation):
        return self._request('enable', operation)

    def verify(self, operation):
        return self._request('verify', operation)

    def restore_source(self, operation):
        return self._request('restore_source', operation)

    def recover_source(self, operation):
        return self._request('recover_source', operation)

    def reconcile_effect(self, operation):
        return self._request('reconcile_effect', operation)


class HandoffFactory:
    """Compose one host's enrolled documents and the pinned handoff adapter.

    ``implementation`` is for tests and alternate backends only; production
    callers omit it and get the fixed ``hote-handoff.py`` companion. The
    factory never selects a source from a document, a message or an operation.
    """

    def __init__(self, db=None, *, root=ENROLLMENT_ROOT, implementation=None,
                 bridge=None):
        self.db = db
        self.bridge = bridge
        self.documents = Documents(root)
        enrollment = self.documents.json('enrollment', exact=DOCUMENTS['enrollment'])
        if (not isinstance(enrollment.get('version'), int)
                or isinstance(enrollment.get('version'), bool)
                or enrollment['version'] != 1 or not _name(enrollment.get('host'))):
            raise FactoryError('identity:host')
        self.enrollment = enrollment
        self.host = enrollment['host']
        self.hosts = _hosts(self.documents.json('hosts', exact=DOCUMENTS['hosts'])['hosts'])
        self.repositories = _repositories(
            self.documents.json('repositories', exact=DOCUMENTS['repositories'])['repositories'])
        self.references = _references(
            self.documents.json('secrets', exact=DOCUMENTS['secrets'])['references'])
        if self.host not in self.hosts:
            raise FactoryError('identity:host')
        for admitted in self.hosts.values():
            for entry in admitted.values():
                for alias in entry['secrets']:
                    if alias not in self.references:
                        raise FactoryError('document:secrets:' + alias)
        self.transports = _transports(
            self.documents.json('transports', exact=DOCUMENTS['transports'])['transports'],
            self.hosts, self.host, self.documents.root)
        self.bridge_path = _trusted_source(Path(__file__).absolute().parent / BRIDGE_NAME,
                                           BRIDGE_NAME)
        self.source_path, self.provider = self._load(implementation)
        missing = [name for name in COMPANION_METHODS
                   if not callable(getattr(self.provider, name, None))]
        if missing:
            raise FactoryError('method:' + ', '.join(sorted(missing)))
        handoff_contract.require_methods(self.provider, COMPANION_METHODS)
        self.adapter = EnrolledAdapter(self)

    def _load(self, implementation):
        if implementation is not None:
            return None, implementation
        source = Path(__file__).absolute().parent / COMPANION_NAME
        _trusted_source(source, COMPANION_NAME)
        spec = importlib.util.spec_from_file_location('ameesh_handoff_host', source)
        if spec is None or spec.loader is None:
            raise FactoryError('source:' + COMPANION_NAME)
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception:
            raise FactoryError('source:' + COMPANION_NAME) from None
        enrolled = getattr(module, 'HostAdapter', None)
        if not callable(enrolled):
            raise FactoryError('source:HostAdapter')
        try:
            provider = enrolled(db=self.db, enrollment=dict(self.enrollment),
                                hosts=self.hosts, references=self.references,
                                transports=self.transports, bridge=self.bridge,
                                enrollment_root=self.documents.root)
        except Exception:
            raise FactoryError('source:HostAdapter') from None
        return source, provider

    def _identity(self, agent, host):
        if not isinstance(agent, Mapping):
            raise FactoryError('identity:agent')
        if not _name(agent.get('name')):
            raise FactoryError('identity:name')
        harness = agent.get('harness')
        if not _name(harness):
            raise FactoryError('identity:harness')
        if not _name(host):
            raise FactoryError('identity:host')
        admitted = self.hosts.get(host)
        if not admitted:
            raise FactoryError('identity:host')
        entry = admitted.get(harness)
        if entry is None:
            raise FactoryError('identity:harness:' + harness)
        account = agent.get('session_account')
        if account is None:
            account = ''
        if (not isinstance(account, str)
                or (account and not _ACCOUNT.fullmatch(account))
                or account not in entry['accounts']):
            raise FactoryError('identity:account')
        return harness, account, entry

    def requirements_for(self, agent, host):
        """The exact preflight identities for one receiving host.

        ``secret_refs`` stay alias names (the preflight grammar is unchanged);
        the private ``fichier#CLE`` references are available separately with
        :meth:`references_for` for the L232c delivery bridge.
        """
        harness, account, entry = self._identity(agent, host)
        return host_preflight.Requirements(
            host=host, harness=harness, account=account,
            tools=entry['tools'], secret_refs=entry['secrets'])

    def references_for(self, agent, host):
        """Map the enrolled aliases to private L232c references; never a value."""
        _, _, entry = self._identity(agent, host)
        return tuple(self.references[alias] for alias in entry['secrets'])

    def repository_for(self, agent):
        """The enrolled repository identity of the agent's project."""
        if not isinstance(agent, Mapping) or not _name(agent.get('name')):
            raise FactoryError('identity:name')
        project = projects.agent_project(agent) or agent.get('project')
        if not _name(project):
            raise FactoryError('repository:project')
        repository = self.repositories.get(project)
        if repository is None:
            raise FactoryError('repository:' + project)
        return repository
