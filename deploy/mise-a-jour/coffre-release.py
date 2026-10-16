#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Magasin de secrets protégé : livraison par référence et rotation (L232c).

Le mesh n'a pas de coffre ni d'API de livraison : son magasin est un
répertoire protégé (`/etc/ameesh`, `0750 root:ameesh`) de fichiers
d'environnement `0640`. Ce module en fait un magasin à références :

- la référence est un nom de fichier simple et un nom de clé
  (`harnais.env#DEEPSEEK_API_KEY`), jamais un chemin, un motif ou un extrait
  de valeur ;
- une politique privée (`/etc/ameesh-secrets/policy.json`, propriété root,
  clés exactes) dit quelles références peuvent aller vers quels couples
  hôte/compte, avec le genre de destination, le mode, le propriétaire et le
  nombre de versions conservées ;
- la livraison passe par un transport injecté (SSH de L232b) : la valeur est
  encadrée à destination du helper d'installation, qui la lit sur son stdin.
  Rien de la valeur ne va dans argv, stdout/stderr, les reçus, le journal ou
  les erreurs : les erreurs ne portent qu'une catégorie stable ;
- l'installation est atomique (fichier temporaire dans le même répertoire,
  `fsync`, `os.replace`, `fsync` du répertoire) et refaite à l'identique
  (rejeu d'un reçu perdu) sans réécrire ; la version précédente reste privée
  jusqu'à la confirmation, puis se révoque ;
- l'ensemble valeur + version est transactionnel : une intention durable
  (`.pending`) et une copie de retour (`.rollback`) rendent toute interruption
  décidable sans condensat, et un verrou par destination (`flock`) sérialise
  installations, rejeux, rotations et révocations entre processus ;
- `install` ne remplace jamais une valeur gérée : une mutation passe par
  `rotate`, donc par la frontière de tour tenue côté mesh ;
- le pont vers un compte de runner écrit un fichier `0600` au nom du compte
  dans son répertoire de configuration `0700`, puis exige un contrôle de
  santé injecté (lecture par le compte : l'ouverture réelle, pas une
  supposition). Sans contrôle de santé enrôlé, l'installation d'un
  `account_file` est refusée avant tout effet : on ne simule pas un backend
  absent.

Ce module est de la source seule : il n'est pas installé, n'ouvre aucune
connexion SSH, ne touche à aucun magasin réel et n'exige pas root. Les tests
l'exercent sur des racines jetables avec des transports et des contrôles de
santé simulés.
"""
from __future__ import annotations

import contextlib
import fcntl
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import stat
import subprocess
import sys
import time

#: protocole de la trame d'installation (une ligne JSON puis la valeur brute)
PROTOCOL = 'ameesh-secret/1'
#: documents enrôlés, propriété root, privés
ENROLLMENT_ROOT = Path('/etc/ameesh-secrets')
POLICY_PATH = ENROLLMENT_ROOT / 'policy.json'
HELPER_PATH = ENROLLMENT_ROOT / 'helper.json'
#: journal de rotation (identifiants de version et catégories, jamais de valeur)
JOURNAL_ROOT = Path('/var/lib/ameesh-secrets')
MAX_BYTES = 65536
#: une référence : nom de fichier simple (sans chemin) et clé d'environnement
REFERENCE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}#[A-Za-z_][A-Za-z0-9_]{0,63}\Z')
KEY = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,63}\Z')
VERSION = re.compile(r'^[0-9a-f]{32}\Z')
_OPERATIONS = ('install', 'rotate', 'revoke')
_HEALTH = {'account_file': ('account_read',), 'env_file': ('root_read',)}
_KEYS = {
    'destination': frozenset(('host', 'account', 'kind', 'path', 'owner', 'group',
                              'mode', 'dir_mode', 'keep_previous', 'health')),
    'policy': frozenset(('version', 'host', 'store', 'references')),
    'store': frozenset(('root', 'max_bytes')),
    'reference': frozenset(('destinations',)),
    'helper': frozenset(('version', 'host', 'max_bytes', 'destinations')),
    'request': frozenset(('protocol', 'operation', 'reference', 'version', 'bytes',
                          'destination')),
    'receipt': frozenset(('protocol', 'operation', 'reference', 'version', 'ok',
                          'category', 'replayed', 'destination', 'previous')),
    'journal': frozenset(('ts', 'reference', 'host', 'account', 'operation',
                          'version', 'category')),
    'pending': frozenset(('version', 'previous', 'backup', 'keep', 'restore')),
}


class SecretError(ValueError):
    """Erreur de catégorie stable ; le message ne porte jamais de valeur."""

    def __init__(self, category: str, message: str = ''):
        super().__init__(message or category)
        self.category = category


class Refused(SecretError):
    """Refus définitif : aucun effet, aucune réconciliation nécessaire."""


class Uncertain(SecretError):
    """Effet possible : l'appelant réconcilie avec le même identifiant."""


def _exact(value, keys, what):
    if not isinstance(value, dict):
        raise Refused('%s_document' % what)
    if keys - set(value) or set(value) - keys:
        raise Refused('%s_keys' % what)


def parse_reference(text: str) -> tuple[str, str]:
    """`fichier.env#CLE` -> (nom, clé). Aucun chemin, aucun motif."""
    if not isinstance(text, str) or not REFERENCE.match(text):
        raise Refused('reference')
    name, key = text.split('#', 1)
    if name in ('.', '..') or '/' in name:
        raise Refused('reference')
    return name, key


def parse_mode(text: str, allowed: tuple) -> int:
    if not isinstance(text, str) or text not in tuple('0%o' % value for value in allowed):
        raise Refused('mode')
    return int(text, 8)


def _ancestors_trusted(path: Path, uid: int, what: str) -> None:
    """Chaîne d'ancêtres : pas de lien, pas d'écriture par un autre.

    Le groupe est le modèle de confiance du mesh (étude L232 : « un membre du
    groupe Unix partagé peut lire ces secrets ») : un ancêtre inscriptible par
    le groupe n'est admis que s'il appartient à root ou à l'appelant. Le
    parent immédiat, lui, est contrôlé au mode exact par l'appelant.
    """
    for ancestor in path.parents:
        try:
            up = ancestor.lstat()
        except OSError:
            raise Refused(what) from None
        if stat.S_ISLNK(up.st_mode):
            raise Refused(what)
        if up.st_mode & 0o002 and not up.st_mode & stat.S_ISVTX:
            raise Refused(what)
        if up.st_mode & 0o020 and up.st_uid not in (0, uid):
            raise Refused(what)


def _trusted_dir(path: Path, *, owner: int, mode: int) -> None:
    """Répertoire absolu, du bon propriétaire, au mode exact."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise Refused('directory_path')
    try:
        info = path.lstat()
    except OSError:
        raise Refused('directory_missing') from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise Refused('directory_type')
    if info.st_uid != owner or stat.S_IMODE(info.st_mode) != mode:
        raise Refused('directory_mode')
    _ancestors_trusted(path, owner, 'directory_ancestor')


def _trusted_store(path: Path, *, uid: int) -> None:
    """Racine du magasin : propriétaire (root ou appelant), non inscriptible par d'autres."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise Refused('store_path')
    try:
        info = path.lstat()
    except OSError:
        raise Refused('store_missing') from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise Refused('store_type')
    if info.st_uid not in (0, uid) or info.st_mode & 0o022:
        raise Refused('store_mode')
    _ancestors_trusted(path, uid, 'store_ancestor')


def _protected_regular(path: Path, *, owners: tuple, modes: tuple) -> os.stat_result:
    """Fichier régulier, sans lien, propriétaire et mode exacts."""
    try:
        info = os.lstat(path)
    except OSError:
        raise Refused('file_missing') from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise Refused('file_type')
    if info.st_uid not in owners or stat.S_IMODE(info.st_mode) not in modes:
        raise Refused('file_mode')
    if info.st_nlink != 1:
        # un lien dur ferait lire (ou prendre pour géré) l'inode d'un autre
        # fichier, peut-être hors du répertoire contrôlé.
        raise Refused('file_links')
    return info


def _read_private(path: Path, *, owners: tuple, modes: tuple, limit: int) -> bytes:
    info = _protected_regular(path, owners=owners, modes=modes)
    if info.st_size <= 0 or info.st_size > limit:
        raise Refused('file_size')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        with os.fdopen(fd, 'rb') as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise Refused('file_changed')
            data = stream.read(limit + 1)
    except OSError:
        raise Refused('file_unreadable') from None
    if len(data) > limit:
        raise Refused('file_size')
    return data


def env_value(data: bytes, key: str) -> bytes:
    """Valeur de `CLE=` dans un fichier d'environnement, sans shell ni guillemets."""
    found = None
    for line in data.split(b'\n'):
        line = line.rstrip(b'\r')
        if not line or line.startswith(b'#'):
            continue
        name, separator, value = line.partition(b'=')
        try:
            label = name.decode('ascii')
        except UnicodeDecodeError:
            raise Refused('env_format') from None
        if not separator or not KEY.match(label):
            raise Refused('env_format')
        if label == key:
            if found is not None:
                raise Refused('env_duplicate')
            found = value
    if found is None:
        raise Refused('env_missing')
    if not found or b'\x00' in found:
        raise Refused('env_value')
    return found


def frame(request: dict, value: bytes) -> bytes:
    """Une ligne JSON exacte, puis la valeur brute : rien de la valeur en JSON."""
    _exact(request, _KEYS['request'], 'request')
    if request.get('operation') not in _OPERATIONS:
        raise Refused('operation')
    if request['bytes'] != len(value) or (not value and request['operation'] != 'revoke'):
        raise Refused('frame_length')
    line = json.dumps(request, sort_keys=True, separators=(',', ':')).encode()
    if b'\n' in line or len(line) > 4096:
        raise Refused('frame_request')
    return line + b'\n' + value


def unframe(payload: bytes) -> tuple[dict, bytes]:
    line, separator, value = payload.partition(b'\n')
    if not separator or len(line) > 4096:
        raise Refused('frame')
    try:
        request = json.loads(line)
    except ValueError:
        raise Refused('frame_request') from None
    _exact(request, _KEYS['request'], 'request')
    if request.get('protocol') != PROTOCOL:
        raise Refused('protocol')
    if request.get('operation') not in _OPERATIONS:
        raise Refused('operation')
    if not isinstance(request.get('bytes'), int) or request['bytes'] != len(value):
        raise Refused('frame_length')
    return request, value


class Destination:
    """Destination enrôlée : couple hôte/compte, genre, chemin et mode exacts."""

    def __init__(self, document: dict):
        _exact(document, _KEYS['destination'], 'destination')
        self.host = document['host']
        self.account = document['account']
        self.kind = document['kind']
        self.path = Path(document['path'])
        self.owner = document['owner']
        self.group = document['group']
        self.keep_previous = document['keep_previous']
        self.health = document['health']
        for name, value in (('host', self.host), ('account', self.account),
                            ('owner', self.owner), ('group', self.group)):
            if not isinstance(value, str) or not value:
                raise Refused('destination_%s' % name)
        if self.kind not in _HEALTH:
            raise Refused('destination_kind')
        self.mode = parse_mode(document['mode'], (0o600, 0o640))
        self.dir_mode = parse_mode(document['dir_mode'], (0o700, 0o750))
        if self.kind == 'account_file':
            if self.mode != 0o600 or self.dir_mode != 0o700 or self.owner != self.account:
                raise Refused('destination_bridge')
        elif self.mode != 0o640 or self.dir_mode != 0o750 or self.owner != 'root':
            raise Refused('destination_system')
        if self.health not in _HEALTH[self.kind]:
            raise Refused('destination_health')
        if type(self.keep_previous) is not int or self.keep_previous not in (0, 1):
            raise Refused('destination_keep')
        if not self.path.is_absolute() or '..' in self.path.parts:
            raise Refused('destination_path')
        if not self.path.name or self.path.name.startswith('.'):
            raise Refused('destination_path')

    def document(self) -> dict:
        return {'host': self.host, 'account': self.account, 'kind': self.kind,
                'path': str(self.path), 'owner': self.owner, 'group': self.group,
                'mode': '0%o' % self.mode, 'dir_mode': '0%o' % self.dir_mode,
                'keep_previous': self.keep_previous, 'health': self.health}


class Politique:
    """Politique du magasin (côté mesh) : références -> destinations enrôlées."""

    def __init__(self, document: dict):
        _exact(document, _KEYS['policy'], 'policy')
        if document['version'] != 1:
            raise Refused('policy_version')
        self.host = document['host']
        if not isinstance(self.host, str) or not self.host:
            raise Refused('policy_host')
        _exact(document['store'], _KEYS['store'], 'store')
        self.store_root = Path(document['store']['root'])
        self.max_bytes = document['store']['max_bytes']
        if type(self.max_bytes) is not int or not 0 < self.max_bytes <= MAX_BYTES:
            raise Refused('policy_bytes')
        if not self.store_root.is_absolute() or '..' in self.store_root.parts:
            raise Refused('policy_store')
        if not isinstance(document['references'], dict) or not document['references']:
            raise Refused('policy_references')
        self.references = {}
        for reference, entry in document['references'].items():
            parse_reference(reference)
            _exact(entry, _KEYS['reference'], 'reference')
            if not isinstance(entry['destinations'], list) or not entry['destinations']:
                raise Refused('reference_destinations')
            self.references[reference] = [Destination(item)
                                          for item in entry['destinations']]

    def destination(self, reference: str, host: str, account: str) -> Destination:
        for destination in self.references.get(reference, ()):
            if destination.host == host and destination.account == account:
                return destination
        raise Refused('destination_unknown')


class Lecteur:
    """Lecteur étroit du magasin : référence allowlistée, fichier protégé."""

    def __init__(self, politique: Politique, *, uid: int | None = None):
        self.politique = politique
        self.uid = os.geteuid() if uid is None else uid
        _trusted_store(politique.store_root, uid=self.uid)

    def read(self, reference: str) -> bytes:
        name, key = parse_reference(reference)
        if reference not in self.politique.references:
            raise Refused('reference_unknown')
        path = self.politique.store_root / name
        data = _read_private(path, owners=(0, self.uid), modes=(0o600, 0o640),
                             limit=self.politique.max_bytes)
        return env_value(data, key)


class Installeur:
    """Côté hôte : installation atomique, pont `0600`, rotation et révocation.

    Le document d'enrôlement (`helper.json`) liste les seules destinations
    admises sur cet hôte. Toute demande qui ne s'y superpose pas exactement
    est refusée avant le moindre effet.
    """

    def __init__(self, document: dict, *, read_as=None, now=time.time,
                 uid: int | None = None, gid: int | None = None,
                 isolate: bool | None = None):
        _exact(document, _KEYS['helper'], 'helper')
        if document['version'] != 1:
            raise Refused('helper_version')
        if not isinstance(document['host'], str) or not document['host']:
            raise Refused('helper_host')
        if (type(document['max_bytes']) is not int
                or not 0 < document['max_bytes'] <= MAX_BYTES):
            raise Refused('helper_bytes')
        self.host = document['host']
        self.max_bytes = document['max_bytes']
        self.uid = os.geteuid() if uid is None else uid
        self.gid = os.getegid() if gid is None else gid
        self.read_as = read_as
        self.now = now
        # root n'agit jamais par chemin dans un répertoire tenu par un compte :
        # la transaction d'un `account_file` s'y déroule aux droits du compte.
        self.isolate = self.uid == 0 if isolate is None else isolate
        self.destinations = {}
        for item in document['destinations'] or ():
            destination = Destination(item)
            if destination.host != self.host:
                raise Refused('helper_destination_host')
            self.destinations[(destination.kind, str(destination.path))] = destination

    # -- point d'entrée du helper -----------------------------------------
    def handle(self, payload: bytes) -> bytes:
        """Traite une trame et rend une ligne de reçu (un refus est un reçu `ok:false`)."""
        request, value = unframe(payload)
        try:
            destination = self.find(request)
            receipt = self.perform(request, destination, value)
        except Refused as exc:
            receipt = self._refusal(request, exc.category)
        return json.dumps(receipt, sort_keys=True,
                          separators=(',', ':')).encode() + b'\n'

    def _refusal(self, request: dict, category: str) -> dict:
        destination = request.get('destination')
        return {'protocol': PROTOCOL, 'operation': request.get('operation', ''),
                'reference': request.get('reference', ''),
                'version': request.get('version', ''), 'ok': False,
                'category': category, 'replayed': False,
                'destination': destination if isinstance(destination, dict) else {},
                'previous': None}

    def find(self, request: dict) -> Destination:
        parse_reference(request.get('reference', ''))
        if not isinstance(request.get('version'), str) or not VERSION.match(request['version']):
            raise Refused('version')
        _exact(request['destination'], _KEYS['destination'], 'destination')
        wanted = Destination(request['destination'])
        if wanted.host != self.host:
            raise Refused('destination_host')
        enrolled = self.destinations.get((wanted.kind, str(wanted.path)))
        if enrolled is None or enrolled.document() != wanted.document():
            raise Refused('destination_unknown')
        return enrolled

    def perform(self, request: dict, destination: Destination, value: bytes) -> dict:
        """Une opération, sous verrou par destination, réconciliation incluse.

        Le verrou couvre la lecture du record, la santé, la publication et le
        retour arrière : deux processus du helper ne peuvent pas entrelacer la
        valeur, la version et la précédente d'une même destination.
        """
        if destination.kind == 'account_file' and self.isolate:
            return self._as_account(destination, request, value)
        if request['operation'] == 'revoke':
            if value:
                # une révocation ne transporte aucune valeur.
                raise Refused('value_unexpected')
            with self._locked(destination):
                return self.revoke(destination, request)
        if len(value) > self.max_bytes:
            raise Refused('value_size')
        with self._locked(destination):
            return self.install(destination, request, value)

    def _as_account(self, destination: Destination, request: dict, value: bytes) -> dict:
        """Mène l'opération d'un `account_file` dans un fils aux droits du compte.

        Le répertoire appartient au compte : il peut en échanger les entrées à
        tout instant (lien symbolique, lien dur, renommage). Contrôler puis agir
        par chemin en root lui donnerait une écriture root ailleurs. Le fils
        abandonne root (groupes, gid, uid réels, effectifs et sauvegardés) avant
        tout accès au répertoire : un échange ne lui ouvre que ce que le compte
        peut déjà faire. Le contrôle de santé y devient la lecture réelle du
        fichier par le compte. Seul le reçu (ou la catégorie d'erreur) revient.
        """
        owner, group = self._owner_ids(destination)
        if owner == 0 or group == 0:
            # le fils ne doit garder ni l'uid ni le gid de root.
            raise Refused('destination_bridge')
        reader, writer = os.pipe()
        pid = os.fork()
        if pid == 0:  # pragma: no cover - mesuré par le reçu du parent
            status = 1
            try:
                os.close(reader)
                if os.geteuid() == 0:
                    os.setgroups([])
                os.setresgid(group, group, group)
                os.setresuid(owner, owner, owner)
                if os.getresuid() != (owner,) * 3 or os.getresgid() != (group,) * 3:
                    raise OSError('privileges kept')
                child = Installeur.__new__(Installeur)
                child.__dict__.update(self.__dict__)
                child.uid, child.gid, child.isolate = owner, group, False
                child.read_as = _read_as_self
                try:
                    outcome = {'receipt': child.perform(request, destination, value)}
                except Refused as exc:
                    outcome = {'refused': exc.category}
                except Uncertain as exc:
                    outcome = {'uncertain': exc.category}
                data = json.dumps(outcome, sort_keys=True).encode()
                while data:
                    data = data[os.write(writer, data):]
                status = 0
            except BaseException:
                status = 1
            finally:
                os._exit(status)
        os.close(writer)
        chunks, size = [], 0
        try:
            while True:
                part = os.read(reader, 4096)
                if not part:
                    break
                size += len(part)
                if size > MAX_BYTES:
                    break
                chunks.append(part)
        finally:
            os.close(reader)
            _, code = os.waitpid(pid, 0)
        if code != 0 or size > MAX_BYTES:
            # le fils a pu agir avant de mourir : l'issue n'est pas connue.
            raise Uncertain('account_transaction')
        try:
            outcome = json.loads(b''.join(chunks))
        except ValueError:
            raise Uncertain('account_transaction') from None
        if not isinstance(outcome, dict) or len(outcome) != 1:
            raise Uncertain('account_transaction')
        (kind, item), = outcome.items()
        if kind == 'refused' and isinstance(item, str):
            raise Refused(item)
        if kind == 'uncertain' and isinstance(item, str):
            raise Uncertain(item)
        if kind != 'receipt' or not isinstance(item, dict):
            raise Uncertain('account_transaction')
        return item

    # -- transaction d'une destination ------------------------------------
    def _backup_path(self, destination: Destination) -> Path:
        return destination.path.with_name(destination.path.name + '.previous')

    def _pending_path(self, destination: Destination) -> Path:
        return destination.path.with_name(destination.path.name + '.pending')

    def _rollback_path(self, destination: Destination) -> Path:
        return destination.path.with_name(destination.path.name + '.rollback')

    def _lock_path(self, destination: Destination) -> Path:
        return destination.path.with_name(destination.path.name + '.lock')

    def _lock(self, destination: Destination, owner: int, group: int) -> int:
        """Verrou exclusif inter-processus sur la destination.

        `flock` sur un fichier privé du répertoire de la destination : le
        verrou tombe avec le processus, l'attente est bornée (dix secondes) et
        un fichier de verrou étranger est refusé.
        """
        path = self._lock_path(destination)
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
        try:
            fd = os.open(path, flags, 0o600)
        except OSError:
            raise Refused('lock') from None
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (owner, 0)
                    or stat.S_IMODE(info.st_mode) & 0o077):
                raise Refused('lock_mode')
            if self.uid == 0 and (info.st_uid, info.st_gid) != (owner, group):
                os.fchown(fd, owner, group)
            os.fchmod(fd, 0o600)
            deadline = time.monotonic() + 10.0
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return fd
                except OSError:
                    if time.monotonic() >= deadline:
                        raise Refused('busy') from None
                    time.sleep(0.05)
        except BaseException:
            os.close(fd)
            raise

    @contextlib.contextmanager
    def _locked(self, destination: Destination):
        owner = self._prepare_parent(destination)
        _, group = self._owner_ids(destination)
        fd = self._lock(destination, owner, group)
        try:
            self._sweep_temporaries(destination, owner)
            yield
        finally:
            os.close(fd)

    def _sweep_temporaries(self, destination: Destination, owner: int) -> None:
        """Retire les fichiers temporaires laissés par un arrêt brutal.

        `_atomic_write` nettoie son temporaire sur erreur, pas sur mort du
        processus : sous le verrou, les temporaires de cette destination (et de
        ses sidecars) sont des restes sans propriétaire.
        """
        parent = destination.path.parent
        prefixes = tuple('.' + destination.path.name + suffix + '.tmp-'
                         for suffix in ('', '.version', '.previous',
                                        '.rollback', '.pending'))
        try:
            entries = list(parent.iterdir())
        except OSError:
            raise Refused('directory_missing') from None
        for entry in entries:
            if not entry.name.startswith(prefixes):
                continue
            try:
                info = entry.lstat()
            except OSError:
                continue
            if (stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)
                    and info.st_uid in (owner, 0)):
                try:
                    entry.unlink()
                except OSError:
                    pass

    def _pending(self, destination: Destination, owner: int) -> dict | None:
        """Intention d'installation en cours, ou None."""
        path = self._pending_path(destination)
        if not path.exists():
            return None
        try:
            data = _read_private(path, owners=(owner, 0), modes=(0o600,),
                                 limit=4096)
            record = json.loads(data)
        except (Refused, ValueError):
            raise Refused('pending_record') from None
        if not isinstance(record, dict) or set(record) != _KEYS['pending']:
            raise Refused('pending_record')
        if not isinstance(record['version'], str) or not VERSION.match(record['version']):
            raise Refused('pending_record')
        if record['previous'] is not None and (not isinstance(record['previous'], str)
                                               or not VERSION.match(record['previous'])):
            raise Refused('pending_record')
        if type(record['backup']) is not bool or type(record['keep']) is not bool:
            raise Refused('pending_record')
        restore = record['restore']
        if not isinstance(restore, dict) or set(restore) - {'version', 'previous'}:
            raise Refused('pending_record')
        for item in restore.values():
            if not isinstance(item, str) or not VERSION.match(item):
                raise Refused('pending_record')
        if restore and (restore.get('version') != record['previous']):
            # l'état à restaurer doit être celui d'avant l'intention.
            raise Refused('pending_record')
        return record

    def _fsync_dir(self, directory: Path) -> None:
        """Rend durable une transition de répertoire (création, rename, unlink)."""
        fd = os.open(directory, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _replace(self, source: Path, target: Path) -> None:
        """Renomme, et rend le renommage durable."""
        try:
            os.replace(source, target)
        except OSError:
            raise Uncertain('write') from None
        try:
            self._fsync_dir(source.parent)
        except OSError:
            raise Uncertain('write') from None

    def _unlink(self, path: Path) -> None:
        """Retire un fichier de ménage, et rend le retrait durable."""
        try:
            path.unlink()
        except FileNotFoundError:
            return
        except OSError:
            raise Uncertain('write') from None
        try:
            self._fsync_dir(path.parent)
        except OSError:
            raise Uncertain('write') from None

    def _finish(self, destination: Destination, keep: bool) -> None:
        """Après commit : la copie de retour devient la précédente, ou part."""
        rollback = self._rollback_path(destination)
        if not rollback.exists():
            return
        if keep:
            self._replace(rollback, self._backup_path(destination))
        else:
            self._unlink(rollback)

    def _discard_rollback(self, destination: Destination) -> None:
        """Retire une copie de retour devenue inerte (retrait durable)."""
        self._unlink(self._rollback_path(destination))

    def _unlink_pending(self, destination: Destination) -> None:
        self._unlink(self._pending_path(destination))

    def _recover(self, destination: Destination, owner: int, group: int) -> None:
        """Termine ou défait une installation interrompue.

        L'intention (`.pending`) et la copie de retour (`.rollback`) rendent la
        reprise décidable sans condensat : si le sidecar porte déjà l'état
        visé, le commit a eu lieu et on termine le ménage ; sinon l'état
        d'avant est restauré à l'identique (valeur, précédente, sidecar) avant
        tout nouvel effet. La copie de retour n'est retirée qu'une fois la
        valeur et le sidecar restaurés durablement puis l'intention effacée
        durablement : une reprise interrompue entre ces frontières se rejoue à
        l'identique au lieu de manquer sa copie.
        """
        pending = self._pending(destination, owner)
        if pending is None:
            # copie de retour sans intention : reste inerte d'une reprise
            # menée à son terme, retiré sous le verrou.
            self._discard_rollback(destination)
            return
        version, previous = pending['version'], pending['previous']
        keep = pending['keep']
        target = {'version': version}
        if keep and previous is not None:
            target['previous'] = previous
        current = self._sidecar(destination, owner)
        rollback = self._rollback_path(destination)
        if current == target:
            self._finish(destination, keep and previous is not None)
            self._unlink_pending(destination)
            return
        if pending['backup']:
            if not rollback.exists():
                # la copie de retour manque : on ne devine pas, l'état reste
                # gelé sous intention tant qu'un opérateur n'a pas tranché.
                raise Refused('rollback_missing')
            preserved = _read_private(rollback, owners=(owner, 0),
                                      modes=(destination.mode,),
                                      limit=self.max_bytes)
            self._atomic_write(destination.path, preserved, destination,
                               mode=destination.mode, owner=owner, group=group)
        else:
            path = destination.path
            if path.exists():
                _protected_regular(path, owners=(owner, 0),
                                   modes=(destination.mode,))
                self._unlink(path)
        if pending['restore']:
            self._write_sidecar(destination, pending['restore'], owner, group)
        else:
            self._unlink(self._sidecar_path(destination))
        # l'intention s'efface durablement avant la copie de retour : une
        # seconde interruption ici laisse la reprise encore décidable.
        self._unlink_pending(destination)
        self._discard_rollback(destination)

    def _owner_ids(self, destination: Destination) -> tuple[int, int]:
        try:
            owner = pwd.getpwnam(destination.owner).pw_uid
            group = grp.getgrnam(destination.group).gr_gid
        except KeyError:
            raise Refused('account_unknown') from None
        if self.uid and owner != self.uid:
            raise Refused('owner_not_caller')
        return owner, group

    def _prepare_parent(self, destination: Destination) -> int:
        owner, group = self._owner_ids(destination)
        expected = owner if destination.kind == 'account_file' else 0
        if self.uid and expected != self.uid:
            raise Refused('owner_not_caller')
        _trusted_dir(destination.path.parent, owner=expected,
                     mode=destination.dir_mode)
        return owner

    def _sidecar_path(self, destination: Destination) -> Path:
        return destination.path.with_name(destination.path.name + '.version')

    def _sidecar(self, destination: Destination, owner: int) -> dict:
        path = self._sidecar_path(destination)
        if not path.exists():
            return {}
        try:
            data = _read_private(path, owners=(owner, 0), modes=(0o600, 0o640),
                                 limit=4096)
            record = json.loads(data)
        except (Refused, ValueError):
            raise Refused('version_record') from None
        if not isinstance(record, dict) or set(record) - {'version', 'previous'}:
            raise Refused('version_record')
        for item in record.values():
            if not isinstance(item, str) or not VERSION.match(item):
                raise Refused('version_record')
        return record

    def _write_sidecar(self, destination: Destination, record: dict,
                       owner: int, group: int) -> None:
        data = json.dumps(record, sort_keys=True, separators=(',', ':')).encode()
        self._atomic_write(self._sidecar_path(destination), data, destination,
                           mode=0o600, owner=owner, group=group)

    def _atomic_write(self, path: Path, data: bytes, destination: Destination, *,
                      mode: int, owner: int, group: int) -> None:
        parent = path.parent
        temporary = parent / ('.%s.tmp-%s' % (path.name, secrets.token_hex(8)))
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        fd = os.open(temporary, flags, mode)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
                if self.uid == 0:
                    os.fchown(stream.fileno(), owner, group)
                os.fchmod(stream.fileno(), mode)
            readback = _read_private(temporary, owners=(owner, 0), modes=(mode,),
                                     limit=len(data) + 1)
            if readback != data:
                raise Uncertain('write_verify')
            os.replace(temporary, path)
            self._fsync_dir(parent)
        except SecretError:
            raise
        except OSError:
            raise Uncertain('write') from None
        finally:
            if temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass

    def install(self, destination: Destination, request: dict, value: bytes) -> dict:
        if destination.health == 'account_read' and self.read_as is None:
            # pas de contrôle enrôlé : refus avant tout effet, jamais de pont
            # déclaré sain sans que le compte l'ait ouvert.
            raise Refused('health_unavailable')
        owner = self._prepare_parent(destination)
        _, group = self._owner_ids(destination)
        version = request['version']
        path = destination.path
        # une intention non commitée se défait (ou se termine) avant tout
        # nouvel effet : l'état lu est un état stable.
        self._recover(destination, owner, group)
        record = self._sidecar(destination, owner)
        if path.exists():
            _protected_regular(path, owners=(owner, 0), modes=(destination.mode,))
            if not record:
                # fichier non géré : on ne l'adopte pas et on ne l'écrase pas.
                raise Refused('unmanaged_file')
        if record.get('version') == version:
            # rejeu d'un reçu perdu : contenu comparé en mémoire, jamais
            # journalisé ni haché ; rien n'est réécrit.
            if path.exists() and _read_private(path, owners=(owner, 0),
                                               modes=(destination.mode,),
                                               limit=self.max_bytes) == value:
                return self._receipt(request, destination,
                                     record.get('previous'), replayed=True)
            raise Uncertain('replay_mismatch')
        previous = record.get('version')
        if previous is not None and request['operation'] != 'rotate':
            # `install` n'a pas autorité pour remplacer une valeur gérée : le
            # nom d'opération choisi par l'appelant ne contourne pas la
            # frontière de tour, tenue par le chemin `rotate` côté mesh.
            raise Refused('rotation_required')
        if previous is not None and not path.exists():
            raise Refused('managed_missing')
        # transaction durable : copie de retour, intention, valeur, contrôle de
        # santé, puis commit du sidecar. Chaque frontière est décidable après
        # un arrêt (voir _recover) ; aucun condensat n'est écrit.
        rollback = self._rollback_path(destination)
        backup = path.exists()
        if backup:
            preserved = _read_private(path, owners=(owner, 0),
                                      modes=(destination.mode,),
                                      limit=self.max_bytes)
            self._atomic_write(rollback, preserved, destination,
                               mode=destination.mode, owner=owner, group=group)
        pending = {'version': version, 'previous': previous, 'backup': backup,
                   'keep': bool(destination.keep_previous), 'restore': record}
        self._atomic_write(self._pending_path(destination),
                           json.dumps(pending, sort_keys=True,
                                      separators=(',', ':')).encode(),
                           destination, mode=0o600, owner=owner, group=group)
        self._atomic_write(path, value, destination, mode=destination.mode,
                           owner=owner, group=group)
        if not self._health(destination):
            self._recover(destination, owner, group)
            return {'protocol': PROTOCOL, 'operation': request['operation'],
                    'reference': request['reference'], 'version': version,
                    'ok': False, 'category': 'health', 'replayed': False,
                    'destination': destination.document(),
                    'previous': previous}
        committed = {'version': version}
        if previous is not None and destination.keep_previous:
            committed['previous'] = previous
        self._write_sidecar(destination, committed, owner, group)
        self._finish(destination, bool(destination.keep_previous)
                     and previous is not None)
        self._unlink_pending(destination)
        return self._receipt(request, destination, previous)

    def revoke(self, destination: Destination, request: dict) -> dict:
        owner = self._prepare_parent(destination)
        group = self._owner_ids(destination)[1]
        self._recover(destination, owner, group)
        record = self._sidecar(destination, owner)
        version = request['version']
        current = record.get('version')
        previous = record.get('previous')
        if version == current:
            raise Refused('current_version')
        if version != previous:
            raise Refused('version_unknown')
        stale = self._backup_path(destination)
        if stale.exists():
            self._unlink(stale)
        self._write_sidecar(destination, {'version': current}, owner, group)
        return {'protocol': PROTOCOL, 'operation': 'revoke',
                'reference': request['reference'], 'version': version,
                'ok': True, 'category': 'revoked', 'replayed': False,
                'destination': destination.document(), 'previous': current}

    def _health(self, destination: Destination) -> bool:
        if destination.health == 'root_read':
            try:
                _read_private(destination.path, owners=(0, self.uid),
                              modes=(destination.mode,), limit=self.max_bytes)
            except Refused:
                return False
            return True
        try:
            return bool(self.read_as(destination.account, destination.path))
        except Exception:
            # appelé après l'écriture : un refus du contrôle (même
            # `health_unavailable`) est un échec de santé, et la transaction
            # se défait au lieu de rendre un reçu « sans effet » sur une valeur
            # écrite.
            return False

    def _receipt(self, request: dict, destination: Destination,
                 previous: str | None, replayed: bool = False) -> dict:
        return {'protocol': PROTOCOL, 'operation': request['operation'],
                'reference': request['reference'], 'version': request['version'],
                'ok': True, 'category': 'installed', 'replayed': replayed,
                'destination': destination.document(), 'previous': previous}


class Journal:
    """Journal de rotation : versions et catégories, jamais de valeur ni d'empreinte."""

    def __init__(self, root: Path, *, uid: int | None = None, now=time.time):
        self.root = Path(root)
        self.uid = os.geteuid() if uid is None else uid
        self.now = now

    def _ensure(self) -> Path:
        if not self.root.exists():
            self.root.mkdir(mode=0o700, parents=True)
        try:
            info = self.root.lstat()
        except OSError:
            raise Refused('journal_root') from None
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != self.uid or stat.S_IMODE(info.st_mode) & 0o077):
            raise Refused('journal_root')
        return self.root / 'rotations.jsonl'

    def record(self, *, reference: str, host: str, account: str, operation: str,
               version: str, category: str, now=None) -> None:
        parse_reference(reference)
        if not VERSION.match(version) or operation not in _OPERATIONS:
            raise Refused('journal_fields')
        entry = {'ts': round(float(self.now() if now is None else now), 3),
                 'reference': reference, 'host': host, 'account': account,
                 'operation': operation, 'version': version, 'category': category}
        _exact(entry, _KEYS['journal'], 'journal')
        path = self._ensure()
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW
        try:
            fd = os.open(path, flags, 0o600)
        except OSError:
            raise Uncertain('journal') from None
        try:
            with os.fdopen(fd, 'ab') as stream:
                stream.write(json.dumps(entry, sort_keys=True,
                                        separators=(',', ':')).encode() + b'\n')
                stream.flush()
                os.fsync(stream.fileno())
        except OSError:
            raise Uncertain('journal') from None


class Intentions:
    """Livraisons en cours côté mesh : la version tirée survit à un échec ambigu.

    Une intention (référence, hôte, compte, opération, version, destination)
    est écrite durablement avant l'envoi et retirée au premier reçu décidé
    (`ok` vrai ou faux). Après un échec ambigu (`Uncertain`), la livraison
    suivante du même triplet rejoue la même version : le helper la reconnaît
    (rejeu sans réécriture) ou l'installe, au lieu de voir une autre version
    sur une destination gérée (`rotation_required`). Aucune valeur ni
    empreinte n'y est écrite.
    """

    def __init__(self, root: Path, *, uid: int | None = None):
        self.root = Path(root)
        self.uid = os.geteuid() if uid is None else uid

    def _ensure(self) -> Path:
        # le parent (racine du journal) naît privé lui aussi : `mkdir -p`
        # laisserait le masque décider de son mode.
        for directory in (self.root.parent, self.root):
            if not directory.exists():
                directory.mkdir(mode=0o700)
        try:
            info = self.root.lstat()
        except OSError:
            raise Refused('intent_root') from None
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != self.uid or stat.S_IMODE(info.st_mode) & 0o077):
            raise Refused('intent_root')
        return self.root

    @staticmethod
    def _key(reference: str, host: str, account: str) -> dict:
        return {'reference': reference, 'host': host, 'account': account}

    def _path(self, key: dict) -> Path:
        # nom dérivé des seuls identifiants publics du triplet, jamais de la valeur.
        name = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()
        return self._ensure() / (name + '.json')

    def get(self, reference: str, host: str, account: str) -> dict | None:
        key = self._key(reference, host, account)
        path = self._path(key)
        if not path.exists():
            return None
        try:
            entry = json.loads(_read_private(path, owners=(self.uid,), modes=(0o600,),
                                             limit=8192))
        except (Refused, ValueError):
            raise Refused('intent_record') from None
        if (not isinstance(entry, dict)
                or set(entry) != {'reference', 'host', 'account', 'operation',
                                  'version', 'destination'}
                or {name: entry[name] for name in key} != key
                or entry['operation'] not in ('install', 'rotate')
                or not isinstance(entry['version'], str) or not VERSION.match(entry['version'])
                or not isinstance(entry['destination'], dict)):
            raise Refused('intent_record')
        return entry

    def put(self, entry: dict) -> None:
        path = self._path(self._key(entry['reference'], entry['host'], entry['account']))
        temporary = path.with_name('.%s.tmp-%s' % (path.name, secrets.token_hex(8)))
        data = json.dumps(entry, sort_keys=True, separators=(',', ':')).encode()
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            _fsync_directory(path.parent)
        except OSError:
            raise Uncertain('intent') from None
        finally:
            if temporary.exists():
                with contextlib.suppress(OSError):
                    temporary.unlink()

    def clear(self, reference: str, host: str, account: str) -> None:
        path = self._path(self._key(reference, host, account))
        try:
            path.unlink()
            _fsync_directory(path.parent)
        except FileNotFoundError:
            return
        except OSError:
            raise Uncertain('intent') from None


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Livraison:
    """Côté mesh : résout une référence allowlistée et la livre par transport.

    Le transport (SSH de L232b) reçoit la trame complète et rend le reçu du
    helper. La rotation exige une frontière de tour injectée : on ne change
    pas la clé d'un compte sous un tour vivant. Le helper refuse par ailleurs
    tout `install` qui remplacerait une valeur déjà gérée
    (`rotation_required`) : le nom d'opération ne contourne pas la frontière.
    """

    def __init__(self, politique: Politique, transport, journal: Journal, *,
                 lecteur: Lecteur | None = None, boundary=None, timeout: float = 30.0,
                 intentions: Intentions | None = None):
        self.politique = politique
        self.transport = transport
        self.journal = journal
        self.lecteur = lecteur or Lecteur(politique)
        self.boundary = boundary
        self.timeout = timeout
        self.intentions = intentions or Intentions(journal.root / 'intents',
                                                   uid=journal.uid)

    def deliver(self, reference: str, host: str, account: str, *,
                operation: str = 'install') -> dict:
        if operation not in ('install', 'rotate'):
            raise Refused('operation')
        destination = self.politique.destination(reference, host, account)
        pending = self.intentions.get(reference, host, account)
        if pending is not None and (pending['operation'] != operation
                                    or pending['destination'] != destination.document()):
            # une livraison incertaine se rejoue à l'identique ou se tranche :
            # une autre opération ou une autre destination ne la remplace pas.
            raise Refused('pending_delivery')
        if operation == 'rotate':
            # la frontière est tenue à chaque tentative, rejeu compris.
            if self.boundary is None:
                raise Refused('boundary_unavailable')
            if not self.boundary(host, account):
                raise Refused('busy')
        value = self.lecteur.read(reference)
        version = pending['version'] if pending else secrets.token_hex(16)
        request = {'protocol': PROTOCOL, 'operation': operation,
                   'reference': reference, 'version': version, 'bytes': len(value),
                   'destination': destination.document()}
        payload = frame(request, value)
        if pending is None:
            self.intentions.put({'reference': reference, 'host': host, 'account': account,
                                 'operation': operation, 'version': version,
                                 'destination': destination.document()})
        self.journal.record(reference=reference, host=host, account=account,
                            operation=operation, version=version,
                            category='retry' if pending else 'intent')
        try:
            answer = self.transport.exchange(payload, self.timeout)
        except SecretError:
            raise
        except Exception:
            self.journal.record(reference=reference, host=host, account=account,
                                operation=operation, version=version,
                                category='transport')
            raise Uncertain('transport') from None
        receipt = self._receipt(answer, request)
        # reçu décidé : l'intention est soldée ; un nouvel essai tire une
        # nouvelle version (et un refus `rotation_required` est alors juste).
        self.intentions.clear(reference, host, account)
        self.journal.record(reference=reference, host=host, account=account,
                            operation=operation, version=version,
                            category=receipt['category'])
        return receipt

    def _receipt(self, answer, request: dict) -> dict:
        if not isinstance(answer, (bytes, bytearray)) or len(answer) > 65536:
            raise Uncertain('receipt')
        try:
            receipt = json.loads(bytes(answer))
        except ValueError:
            raise Uncertain('receipt') from None
        try:
            _exact(receipt, _KEYS['receipt'], 'receipt')
        except Refused:
            # un reçu illisible est ambigu : l'effet a peut-être eu lieu.
            raise Uncertain('receipt') from None
        if receipt.get('protocol') != PROTOCOL:
            raise Uncertain('receipt')
        for field in ('operation', 'reference', 'version'):
            if receipt.get(field) != request[field]:
                raise Uncertain('receipt')
        if receipt.get('ok') not in (True, False) or not isinstance(receipt.get('replayed'), bool):
            raise Uncertain('receipt')
        if receipt.get('destination') != request['destination']:
            raise Uncertain('mismatch')
        return receipt


def handle(payload: bytes, enrollment: dict, *, read_as=None) -> bytes:
    """Point d'entrée du helper d'installation : une trame, un reçu."""
    return Installeur(enrollment, read_as=read_as).handle(payload)


def _read_as_self(account: str, path: Path) -> bool:
    """Santé dans le fils du compte : l'ouverture réelle par le compte lui-même."""
    if pwd.getpwuid(os.geteuid()).pw_name != account:
        return False
    try:
        _read_private(path, owners=(os.geteuid(),), modes=(0o600,), limit=MAX_BYTES)
    except Refused:
        return False
    return True


def default_read_as(account: str, path: Path) -> bool:
    """Lecture bornée par le compte lui-même (root seulement).

    C'est le vrai contrôle du pont : le fichier est ouvert par l'utilisateur
    qui devra s'en servir, pas par root. Hors root, le contrôle n'est pas
    disponible et l'installation est refusée.
    """
    if os.geteuid() != 0:
        raise Refused('health_unavailable')
    for tool in ('/usr/sbin/runuser', '/sbin/runuser'):
        if os.path.exists(tool):
            break
    else:
        raise Refused('health_unavailable')
    try:
        done = subprocess.run([tool, '-u', account, '--', '/bin/cat', str(path)],
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=5,
                              env={'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C'})
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def helper_main(enrollment_path: Path | None = None, *, stdin=None, stdout=None,
                stderr=None) -> int:
    """Entrée du helper root : document enrôlé fixe, trame sur stdin, reçu sur stdout.

    Aucun chemin, aucune valeur et aucun identifiant ne vient de la ligne de
    commande ; seul le document enrôlé (ou le chemin injecté par un test) est lu.
    """
    stream_in = getattr(sys.stdin, 'buffer', sys.stdin)
    stream_out = getattr(sys.stdout, 'buffer', sys.stdout)
    stream_err = sys.stderr
    fin = stream_in if stdin is None else stdin
    fout = stream_out if stdout is None else stdout
    ferr = stream_err if stderr is None else stderr
    path = Path(HELPER_PATH if enrollment_path is None else enrollment_path)
    try:
        document = json.loads(_read_private(path, owners=(0, os.geteuid()),
                                            modes=(0o600, 0o640), limit=MAX_BYTES))
    except Refused as exc:
        ferr.write('refused: %s\n' % exc.category)
        return 2
    except (OSError, ValueError):
        ferr.write('refused: enrollment\n')
        return 2
    try:
        payload = fin.read(MAX_BYTES + 4096)
        fout.write(Installeur(document, read_as=default_read_as).handle(payload))
        fout.flush()
        return 0
    except Refused as exc:
        ferr.write('refused: %s\n' % exc.category)
        return 2
    except Uncertain as exc:
        ferr.write('uncertain: %s\n' % exc.category)
        return 3
    except (OSError, ValueError):
        ferr.write('refused: request\n')
        return 2


if __name__ == '__main__':
    raise SystemExit(helper_main())
