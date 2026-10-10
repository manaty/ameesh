# SPDX-License-Identifier: AGPL-3.0-only
"""Fil lisible (spec §6, C5, R12) : chaque message passé par ameesh, en clair.

La boîte Postgres (`agent_mailbox`) n'est que la file de distribution (hooks,
réveil des exécuteurs) ; le **fil** est la référence lisible et auditable par
les humains, y compris pour les échanges d'agent à agent (décision 0006).

* `ThreadRef` = (projet, lot) ; sans lot, c'est le fil du projet.
* `Entry` = auteur (`human:`/`agent:`), horodatage, destinataire, texte en
  langage naturel (autosuffisant) et métadonnées structurées **en plus**.
* `Transport` : `post(thread, entry) -> id externe`, `read(thread, since)`.
  Transport v1 : `file`, un fichier Markdown par fil sous `AMEESH_THREADS`
  (défaut `<état>/fils/<projet>/<lot ou _projet>.md`), en ajout seulement,
  une entrée complète écrite d'un bloc sous verrou `flock`. La racine est de
  confiance ; sous elle, chaque dossier et chaque fichier est ouvert par un
  descripteur ancré sur son parent, sans jamais suivre de lien symbolique.

Une écriture de fil qui échoue ne fait JAMAIS perdre le message : `record()`
ne lève pas, il le dit sur stderr, et le message reste dans la boîte.

Format d'une entrée : le texte du message, tel quel, dans un bloc de code
clôturé dont la barrière (```) est plus longue que toute suite de ` du texte.
Aucune ligne du corps ne peut donc fermer le bloc : rien n'y est interprété
comme du Markdown (titre à toute indentation, setext, HTML, commentaire, autre
barrière), et la relecture rend le texte exact.

    ### 2026-10-04T10:15:30+02:00 — agent:alpha → agent:beta

    ```
    Le texte du message, tel quel.
    ```

    <!-- ameesh {"host": "laptop", "ids": [12]} -->

  ameesh fil list                               les fils connus (index + disque)
  ameesh fil show <projet> [<lot>] [--last N]   lire un fil
  ameesh fil tail <projet> [<lot>] [--last N]   suivre un fil (Ctrl-C pour sortir)

L'index des fils (`thread_index`) est écrit et lu par le stockage
(`storage.of(db).threads`, spec §10).
"""
from __future__ import annotations

import argparse
import datetime
import errno
import fcntl
import json
import os
import re
import stat
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Protocol, Sequence

from . import storage
from .config import Config

DEFAULT_PROJECT = "default"
#: nom de fichier du fil d'un projet (un lot assaini ne commence jamais par `_`)
PROJECT_THREAD = "_projet"
#: au-delà, un bloc sans espace en alphabet base64/hex est une donnée encodée
MAX_BLOB = 200
#: un bloc encodé replié l'est à largeur fixe (64 en PEM, 76 en MIME, 60 pour
#: `xxd -p`) ; les mots d'un texte n'atteignent presque jamais 16 caractères
FOLD_MIN = 16
SEGMENT_MAX = 64

_HEADING_RE = re.compile(r"^### (\S+) — (.+?) → (.*)$")
_META_RE = re.compile(r"^<!-- ameesh (\{.*\}) -->$")
_BACKTICKS_RE = re.compile(r"`+")
# lecture d'un fil : sur les octets, pour sauter les corps sans les décoder
_HEADING_B = re.compile(_HEADING_RE.pattern.encode("utf-8"), re.M)
_FENCE_B = re.compile(rb"`{3,}")
_CLOSING_B = re.compile(rb" {0,3}`{3,}[ \t]*")
_BLANK_LINES_B = re.compile(rb"(?:[ \t\r\f\v]*\n)*")
_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
_BLOB_RE = re.compile(r"[A-Za-z0-9+/=_-]{%d,}" % (MAX_BLOB + 1))
_ALPHABET_RE = re.compile(r"[A-Za-z0-9+/=_-]+")
_HEX_RUN_RE = re.compile(r"[0-9A-Fa-f]{%d,}" % (MAX_BLOB + 1))
_WORD_RE = re.compile(r"[A-Z]?[a-z]{2,}")
_ALLOW_HINT = "(--allow-structured est réservé aux tests et aux outils)"


class UnreadableBody(ValueError):
    """Corps de message refusé : il n'est pas du texte lisible par un humain."""


class ThreadError(RuntimeError):
    """Fil inaccessible (emplacement hors de la racine, identifiant étranger…)."""


# --------------------------------------------------------------------------
# modèle
# --------------------------------------------------------------------------

def safe_segment(name) -> str:
    """Nom de projet ou de lot utilisable comme segment de chemin.

    Translittère les accents, remplace tout le reste par `-`, retire points,
    tirets et soulignés de bord : jamais de `/`, de `..`, de fichier caché ni
    de nom qui commence par `_` (réservé au fil du projet).
    """
    if name is None:
        return ""
    text = unicodedata.normalize("NFKD", str(name))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = _UNSAFE_RE.sub("-", text).strip("._-")
    return text[:SEGMENT_MAX].strip("._-")


@dataclass(frozen=True)
class ThreadRef:
    """Un fil : celui d'un lot, ou celui du projet (`lot` absent)."""

    project: str
    lot: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "project", safe_segment(self.project) or DEFAULT_PROJECT)
        lot = safe_segment(self.lot) if self.lot not in (None, "") else ""
        object.__setattr__(self, "lot", lot or None)

    @property
    def stem(self) -> str:
        return self.lot or PROJECT_THREAD

    @property
    def key(self) -> str:
        return "%s/%s" % (self.project, self.stem)

    def describe(self) -> str:
        return ("%s / lot %s" % (self.project, self.lot)) if self.lot \
            else "%s (fil du projet)" % self.project


@dataclass
class Entry:
    """Une entrée de fil : un message lisible, et ses métadonnées en plus."""

    author: str                     # human:<id> | agent:<id>
    recipient: str                  # agent:<id>[, agent:<id>…] | tous
    text: str
    ts: float = field(default_factory=time.time)
    meta: dict = field(default_factory=dict)
    id: str | None = None           # id externe (renseigné par post/read)

    @property
    def when(self) -> str:
        return iso_local(self.ts)


class Transport(Protocol):
    name: str

    def post(self, thread: ThreadRef, entry: Entry) -> str: ...

    def read(self, thread: ThreadRef, since: str | None = None) -> list[Entry]: ...


# --------------------------------------------------------------------------
# texte : lisibilité, nettoyage, échappement
# --------------------------------------------------------------------------

def unreadable_reason(body) -> str | None:
    """Pourquoi ce corps n'est pas du texte lisible (None s'il l'est)."""
    if body is None or not str(body).strip():
        return "corps vide : un message doit contenir du texte lisible"
    body = str(body)
    for ch in body:
        if ch in "\n\t\r":
            continue
        if unicodedata.category(ch) in ("Cc", "Cs"):
            return ("caractère de contrôle ou binaire U+%04X : le fil n'accepte que du "
                    "texte (retirez les couleurs de terminal et les données binaires)"
                    % ord(ch))
    stripped = body.strip()
    if stripped[0] in "{[":
        try:
            value = json.loads(stripped)
        except ValueError:
            value = None
        if isinstance(value, (dict, list)):
            return ("le corps n'est que du JSON : écrivez en phrases ce que le destinataire "
                    "doit savoir ; les données structurées accompagnent le texte, elles ne "
                    "le remplacent pas")
    for blob, folded in _blobs(body):
        if re.search(r"[A-Za-z0-9]", blob) and not _word_like(blob):
            return ("bloc encodé de %d caractères %s (base64 ou hexadécimal) : "
                    "décrivez-le en clair et transmettez la donnée par un autre moyen"
                    % (len(blob), "replié sur plusieurs lignes" if folded else "sans espace"))
    # L'hexadécimal, replié ou espacé (« de ad be ef … »), se voit aussi dans le
    # texte sans aucun blanc : aucun texte n'aligne 200 caractères de [0-9a-f]
    # mêlant lettres et chiffres (une liste de nombres n'a pas de lettres).
    for match in _HEX_RUN_RE.finditer("".join(body.split())):
        run = match.group(0)
        letters = sum(ch.isalpha() for ch in run)
        if 0.05 * len(run) <= letters <= 0.95 * len(run):
            return ("bloc encodé de %d caractères (hexadécimal replié ou espacé) : "
                    "décrivez-le en clair et transmettez la donnée par un autre moyen"
                    % len(run))
    return None


def _blobs(body: str) -> Iterator[tuple[str, bool]]:
    """Les blocs de l'alphabet base64/base64url/hex de plus de MAX_BLOB
    caractères, avec un drapeau « replié ».

    D'un seul tenant, ou repliés en lignes (et indentés, en CRLF…) : une suite
    de mots de l'alphabet d'au moins FOLD_MIN caractères chacun, plus la
    dernière ligne du bloc (plus courte), est recollée. Les mots d'une phrase
    sont trop courts pour être recollés ainsi.
    """
    for match in _BLOB_RE.finditer(body):
        yield match.group(0), False
    run: list[str] = []
    for token in body.split() + [""]:
        alphabet = _ALPHABET_RE.fullmatch(token) is not None
        if alphabet and len(token) >= FOLD_MIN:
            run.append(token)
            continue
        if alphabet and run:
            run.append(token)
        joined = "".join(run)
        if len(run) > 1 and len(joined) > MAX_BLOB:
            yield joined, True
        run = []


def _word_like(run: str) -> bool:
    """Un long chemin, des identifiants ou des noms collés : pas un bloc encodé.

    Mesuré sur 6 000 base64, base64url et hexadécimaux (données aléatoires,
    texte, JSON, zéros ; 200 à 1 200 caractères) : seul `_path_like` en laisse
    passer, moins de 0,3 %.
    """
    return _path_like(run) or _identifiers_like(run) or _camel_like(run)


def _path_like(run: str) -> bool:
    """Un long chemin (ou chemin d'URL) n'est pas un bloc encodé.

    Un chemin a des segments courts séparés par `/` ; un base64 n'a un `/` que
    tous les 64 caractères en moyenne, un hexadécimal jamais. Seuils mesurés :
    moins de 0,1 % des base64 aléatoires de plus de 200 caractères passent.
    """
    if "/" not in run:
        return False
    longueurs = [len(s) for s in run.split("/")]
    return max(longueurs) <= 48 and sum(longueurs) / len(longueurs) <= 40


def _identifiers_like(run: str) -> bool:
    """Des mots séparés par `-`, `_` ou `/` (noms de tests, UUID…) : segments courts.

    Un base64url n'a un `-` ou un `_` que tous les 32 caractères en moyenne.
    """
    parts = [len(p) for p in re.split(r"[-_/]+", run) if p]
    return len(parts) > 1 and max(parts) <= 32 and sum(parts) / len(parts) <= 12


def _camel_like(run: str) -> bool:
    """Des noms collés (CamelCase) : presque toutes les lettres dans des mots
    (une majuscule puis des minuscules), presque pas de chiffres. Un base64 a
    des chiffres partout et des majuscules au hasard."""
    letters = sum(ch.isalpha() for ch in run)
    in_words = sum(map(len, _WORD_RE.findall(run)))
    digits = sum(ch.isdigit() for ch in run)
    return letters > 0 and in_words >= 0.85 * letters and digits <= 0.05 * len(run)


def ensure_readable(body, allow_structured: bool = False) -> None:
    """Lève `UnreadableBody` si le corps n'est pas lisible (sauf outils et tests)."""
    if allow_structured:
        return
    reason = unreadable_reason(body)
    if reason:
        raise UnreadableBody("message refusé — %s %s" % (reason, _ALLOW_HINT))


def clean_text(text) -> str:
    """Texte prêt pour le fil : fins de ligne normalisées, contrôles neutralisés.

    Rien d'autre n'est touché (ni espaces ni lignes vides de bord) : un texte
    sans `\\r` ni caractère de contrôle (hors `\\n` et `\\t`) est relu exact.
    """
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    out = []
    for ch in text:
        if ch not in "\n\t" and unicodedata.category(ch) in ("Cc", "Cs"):
            out.append("�")
        else:
            out.append(ch)
    return "".join(out)


def _one_line(text) -> str:
    return " ".join(clean_text(text).split())


def excerpt(text, limit: int = 120) -> str:
    line = _one_line(text)
    return line if len(line) <= limit else line[: limit - 1] + "…"


def iso_local(ts: float) -> str:
    """Date ISO 8601 locale, à la seconde, avec le décalage (2026-10-04T10:15:30+02:00)."""
    return datetime.datetime.fromtimestamp(float(ts)).astimezone().isoformat(timespec="seconds")


def _parse_iso(text: str) -> float:
    try:
        return datetime.datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


def _meta_line(meta: dict) -> str:
    # `>` et `<` n'apparaissent que dans des chaînes JSON : leur forme \u…
    # garde le commentaire HTML fermé au bon endroit.
    data = json.dumps(meta, ensure_ascii=False, sort_keys=True, default=str)
    return "<!-- ameesh %s -->" % data.replace("<", "\\u003c").replace(">", "\\u003e")


def fence_for(text: str) -> str:
    """Barrière du bloc de code d'un corps : plus longue que toute suite de ` du corps.

    En CommonMark, un bloc ouvert par N ` ne se ferme que sur une ligne de N `
    ou plus (indentée de 0 à 3 espaces) ; une barrière de ~ ne le ferme pas.
    """
    longest = max(map(len, _BACKTICKS_RE.findall(text)), default=0)
    return "`" * max(3, longest + 1)


def format_entry(entry: Entry, *, meta: bool = True) -> str:
    """Le Markdown d'une entrée, terminé par une ligne vide.

    Le corps est copié tel quel, ligne à ligne, entre deux barrières
    `fence_for(corps)` : il est inerte (aucune ligne ne ferme le bloc) et
    `parse` le relit à l'identique. Corps vide : bloc sans ligne.
    """
    body = clean_text(entry.text)
    fence = fence_for(body)
    lines = ["### %s — %s → %s" % (iso_local(entry.ts), _one_line(entry.author) or "?",
                                   _one_line(entry.recipient) or "?"), "", fence]
    if body:
        lines += body.split("\n")
    lines += [fence, ""]
    if meta and entry.meta:
        lines += [_meta_line(entry.meta), ""]
    return "\n".join(lines) + "\n"


def header(thread: ThreadRef) -> str:
    return (
        "# Fil — %s\n\n"
        "Fil lisible tenu par ameesh : une entrée par message, en ajout seulement.\n"
        "Le texte de chaque message est reproduit tel quel dans un bloc de code ;\n"
        "ses métadonnées sont dans un commentaire `ameesh` discret.\n\n"
        % thread.describe()
    )


def _meta_of(line: str) -> dict | None:
    found = _META_RE.match(line)
    if not found:
        return None
    try:
        meta = json.loads(found.group(1))
    except ValueError:
        return None
    return meta if isinstance(meta, dict) else None


def _line_end(data: bytes, pos: int) -> int:
    end = data.find(b"\n", pos)
    return len(data) if end < 0 else end


def _closing_line(data: bytes, fence: bytes, pos: int) -> int:
    """Début de la ligne qui ferme le bloc ouvert par `fence`, à partir de la
    ligne `pos` ; -1 si le bloc n'est jamais fermé (entrée coupée).

    Règle CommonMark : une ligne de len(fence) ` ou plus, 0 à 3 espaces avant,
    des blancs après. Aucun corps n'a de suite de ` aussi longue : seule la
    vraie fermeture convient, et la lecture s'accorde avec tout rendu Markdown.
    """
    i = data.find(fence, pos)
    while i >= 0:
        start = data.rfind(b"\n", pos, i) + 1 or pos
        end = _line_end(data, i)
        if _CLOSING_B.fullmatch(data, start, end):
            return start
        i = data.find(fence, end)
    return -1


def _walk(data: bytes) -> tuple[list[tuple], bytes | None]:
    """Découpe un fil en entrées brutes, sans rien décoder.

    Seules les lignes HORS des blocs de code sont structurelles : titre
    d'entrée, barrière d'ouverture, commentaire `ameesh` qui suit le bloc. Un
    corps est sauté d'un coup jusqu'à sa barrière de fermeture.

    Renvoie [(début, titre, corps, hors_bloc, après, coupé)] — `corps` et
    `hors_bloc` sont des tranches (début, fin) ou None — et la barrière restée
    ouverte en fin de données (entrée coupée en plein corps), sinon None.
    """
    found: list[tuple] = []
    size = len(data)
    heading = _HEADING_B.search(data)
    while heading:
        start = heading.start()
        pos = _BLANK_LINES_B.match(data, min(heading.end() + 1, size)).end()
        end = _line_end(data, pos)
        if not _FENCE_B.fullmatch(data, pos, end):
            # Pas de bloc de code : entrée écrite à la main, ou coupée avant son
            # corps. Ses lignes vont jusqu'au titre suivant.
            heading_next = _HEADING_B.search(data, pos)
            found.append((start, heading, None,
                          (pos, heading_next.start() if heading_next else size), None, False))
            heading = heading_next
            continue
        fence, body = data[pos:end], min(end + 1, size)
        close = _closing_line(data, fence, body)
        if close < 0:
            found.append((start, heading, (body, size), None, None, True))
            return found, fence
        pos = _BLANK_LINES_B.match(data, min(_line_end(data, close) + 1, size)).end()
        found.append((start, heading, (body, max(body, close - 1)), None,
                      (pos, _line_end(data, pos)), False))
        heading = _HEADING_B.search(data, pos)
    return found, None


def _loose_text(lines: list[str]) -> tuple[str, dict | None]:
    """Texte et métadonnées d'une entrée sans bloc de code (lignes vides de bord ôtées)."""
    while lines and not lines[-1].strip():
        lines.pop()
    meta = _meta_of(lines[-1]) if lines else None
    if meta is not None:
        lines.pop()
    while lines and not lines[-1].strip():
        lines.pop()
    while lines and not lines[0].strip():
        lines.pop(0)
    return "\n".join(lines), meta


def parse(data: bytes, *, base: int = 0, key: str = "") -> list[Entry]:
    """Les entrées d'un fil (ou d'un morceau de fil commençant à l'octet `base`).

    Le corps est relu à l'identique ; une entrée coupée en plein corps rend ce
    qui en a été écrit (sans sa dernière fin de ligne).
    """
    entries: list[Entry] = []
    for start, heading, body, loose, after, torn in _walk(data)[0]:
        meta = None
        if body is not None:
            raw = data[body[0]:body[1]]
            if torn and raw.endswith(b"\n"):
                raw = raw[:-1]
            text = raw.decode("utf-8", "replace")
            if after is not None:
                meta = _meta_of(data[after[0]:after[1]].decode("utf-8", "replace"))
        else:
            text, meta = _loose_text(
                data[loose[0]:loose[1]].decode("utf-8", "replace").split("\n"))
        when, author, recipient = (g.decode("utf-8", "replace") for g in heading.groups())
        offset = base + start
        entries.append(Entry(author=author, recipient=recipient, text=text,
                             ts=_parse_iso(when), meta=meta or {},
                             id="%s#%d" % (key, offset) if key else str(offset)))
    return entries


def _repair(data: bytes) -> bytes:
    """Ce qu'il faut écrire avant une nouvelle entrée si la précédente a été
    interrompue (arrêt brutal en pleine écriture) : finir la ligne, fermer le
    bloc de code ou le commentaire resté ouvert. Sans cela, un lecteur
    Markdown avalerait la suite du fil dans le bloc ouvert."""
    fence = _walk(data)[1]
    tail = data[data.rfind(b"\n") + 1:]
    newline = b"\n" if tail else b""
    if fence is not None:
        return newline + fence + b"\n\n"
    if tail.startswith(b"<!--") and b"-->" not in tail:
        return b" -->\n\n"
    return newline


# --------------------------------------------------------------------------
# transport v1 : fichiers Markdown
# --------------------------------------------------------------------------

def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _pread_all(fd: int, size: int) -> bytes:
    chunks, offset = [], 0
    while offset < size:
        chunk = os.pread(fd, size - offset, offset)
        if not chunk:
            break
        chunks.append(chunk)
        offset += len(chunk)
    return b"".join(chunks)


#: un lien symbolique ouvert avec O_NOFOLLOW (ELOOP), ou un lien / un fichier là
#: où un dossier est attendu avec O_DIRECTORY (ENOTDIR)
_REFUSED = (errno.ELOOP, errno.ENOTDIR)


def _open_at(parent_fd: int, name: str, flags: int, shown: str) -> int:
    """`os.open(name)` relatif au dossier `parent_fd`, sans suivre de lien.

    `name` est un seul composant (assaini : ni `/`, ni `..`) : O_NOFOLLOW porte
    donc sur tout ce qui est résolu. Un lien symbolique, même posé entre deux
    appels, fait échouer l'ouverture au lieu de sortir de la racine.
    """
    try:
        return os.open(name, flags | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno in _REFUSED:
            raise ThreadError("%s : lien symbolique ou type de fichier inattendu, refusé"
                              % shown) from exc
        raise


class FileTransport:
    """Un fichier Markdown par fil, ajout seulement, verrou `flock` par fichier.

    La racine (`AMEESH_THREADS`, défaut `<état>/fils`) est **de confiance** :
    choisie par l'utilisateur, elle est ouverte telle quelle, une fois par
    opération (elle peut elle-même être un lien). Tout ce qui est SOUS elle est
    ouvert par descripteurs ancrés : le dossier du projet relativement au
    descripteur de la racine, le fichier du fil relativement à celui du dossier,
    chacun avec O_NOFOLLOW. Aucun chemin n'est vérifié puis rouvert : remplacer
    un dossier par un lien entre deux appels fait échouer l'opération
    (ThreadError), jamais écrire ni lire hors de la racine.
    """

    name = "file"

    def __init__(self, root: str):
        self.root = os.path.abspath(os.path.expanduser(root))

    def location(self, thread: ThreadRef) -> str:
        """Chemin du fil, pour l'affichage et l'index.

        Purement lexical (segments assainis : toujours sous la racine) ; ce
        chemin n'est jamais rouvert tel quel, la sécurité est dans `_open`.
        """
        return os.path.join(self.root, thread.project, thread.stem + ".md")

    def _root_fd(self, create: bool = False) -> int:
        if create:
            os.makedirs(self.root, mode=0o700, exist_ok=True)
        return os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)

    def _dir_fd(self, root_fd: int, project: str, create: bool = False) -> int:
        """Le dossier d'un projet, ouvert sous la racine sans suivre de lien."""
        flags = os.O_RDONLY | os.O_DIRECTORY
        shown = os.path.join(self.root, project)
        try:
            return _open_at(root_fd, project, flags, shown)
        except FileNotFoundError:
            if not create:
                raise
        try:
            os.mkdir(project, 0o700, dir_fd=root_fd)
        except FileExistsError:
            pass  # créé entre-temps : rouvert ci-dessous, toujours sans suivre de lien
        return _open_at(root_fd, project, flags, shown)

    def _open(self, thread: ThreadRef, write: bool) -> int:
        """Descripteur du fichier du fil : un fichier ordinaire sous la racine.

        Écriture : dossier et fichier créés au besoin (0700 / 0600), ouvert en
        ajout. Lecture : FileNotFoundError si le fil n'existe pas ; O_NONBLOCK
        pour qu'un tube nommé posé à sa place ne bloque pas (il est refusé).
        """
        root_fd = self._root_fd(create=write)
        try:
            dir_fd = self._dir_fd(root_fd, thread.project, create=write)
        finally:
            os.close(root_fd)
        flags = (os.O_RDWR | os.O_APPEND | os.O_CREAT) if write \
            else (os.O_RDONLY | os.O_NONBLOCK)
        try:
            fd = _open_at(dir_fd, thread.stem + ".md", flags, self.location(thread))
        finally:
            os.close(dir_fd)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ThreadError("%s : pas un fichier ordinaire, refusé" % self.location(thread))
        except BaseException:
            os.close(fd)
            raise
        return fd

    def exists(self, thread: ThreadRef) -> bool:
        try:
            os.close(self._open(thread, write=False))
        except FileNotFoundError:
            return False
        return True

    def post(self, thread: ThreadRef, entry: Entry) -> str:
        data = format_entry(entry).encode("utf-8")
        fd = self._open(thread, write=True)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            size = os.fstat(fd).st_size
            if size == 0:
                prefix = header(thread).encode("utf-8")
            else:
                # une écriture précédente interrompue (arrêt brutal) : la clore
                prefix = _repair(_pread_all(fd, size))
            try:
                _write_all(fd, prefix + data)
                os.fsync(fd)
            except BaseException:
                # tout ou rien : on ne laisse pas une entrée à moitié écrite
                try:
                    os.ftruncate(fd, size)
                except OSError:
                    pass
                raise
        finally:
            os.close(fd)  # libère le verrou
        entry.id = "%s#%d" % (thread.key, size + len(prefix))
        return entry.id

    def _offset(self, thread: ThreadRef, since: str) -> int:
        key, sep, offset = str(since).rpartition("#")
        if not sep or key != thread.key or not offset.isdigit():
            raise ThreadError("id d'entrée %r étranger au fil %s" % (since, thread.key))
        return int(offset)

    def read(self, thread: ThreadRef, since: str | None = None) -> list[Entry]:
        """Les entrées du fil, ou celles postérieures à l'id `since`."""
        start = self._offset(thread, since) if since else 0
        try:
            fd = self._open(thread, write=False)
        except FileNotFoundError:
            return []
        with open(fd, "rb") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_SH)  # jamais une entrée en cours d'écriture
            fh.seek(start)
            data = fh.read()
        entries = parse(data, base=start, key=thread.key)
        if since:
            entries = [e for e in entries if int(e.id.rpartition("#")[2]) > start]
        return entries

    def threads(self) -> list[ThreadRef]:
        """Les fils présents sur disque (liens symboliques ignorés, jamais suivis)."""
        found: list[ThreadRef] = []
        try:
            root_fd = self._root_fd()
        except OSError:
            return found
        try:
            with os.scandir(root_fd) as it:
                projects = sorted(e.name for e in it if e.is_dir(follow_symlinks=False)
                                  and safe_segment(e.name) == e.name)
            for project in projects:
                try:
                    dir_fd = self._dir_fd(root_fd, project)
                except (OSError, ThreadError):
                    continue  # disparu, ou remplacé par un lien entre-temps
                try:
                    with os.scandir(dir_fd) as it:
                        stems = sorted(e.name[:-3] for e in it if e.name.endswith(".md")
                                       and e.is_file(follow_symlinks=False))
                except OSError:
                    continue
                finally:
                    os.close(dir_fd)
                for stem in stems:
                    thread = ThreadRef(project, None if stem == PROJECT_THREAD else stem)
                    if thread.stem == stem:
                        found.append(thread)
        except OSError:
            pass
        finally:
            os.close(root_fd)
        return found


def transport_for(cfg: Config) -> FileTransport:
    """Le transport des fils (v1 : `file` seulement)."""
    return FileTransport(cfg.threads_root)


# --------------------------------------------------------------------------
# écriture d'un message dans son fil (appelée par mail.send et les backends)
# --------------------------------------------------------------------------

def member(cfg: Config, name: str) -> str:
    """`human:<nom>` pour un humain déclaré (AMEESH_HUMANS), `agent:<nom>` sinon.

    Un nom déjà qualifié (`human:`, `agent:`, ou `ameesh:<acteur>` pour un
    outil qui n'est ni l'un ni l'autre) reste tel quel : un nom d'agent ne
    contient jamais `:`."""
    if name == "all":
        return "tous"
    if ":" in name:
        return name
    return ("human:%s" if name in cfg.human_names else "agent:%s") % name


def project_for(cfg: Config, *projects) -> str:
    """Projet d'un message : celui de l'expéditeur, du destinataire (`agent_project`),
    AMEESH_PROJECT, default."""
    for project in projects:
        if project and str(project).strip():
            return str(project).strip()
    return cfg.project or DEFAULT_PROJECT


def agent_project(row: dict | None) -> str:
    """Projet du fil d'un agent (ligne du registre, avec `canon_governed`) :
    son équipe (`team` du canon) s'il est gouverné par le canon, sinon son
    chantier ; vide si aucun."""
    if not row:
        return ""
    team = str(row.get("team") or "").strip()
    if row.get("canon_governed") and team:
        return team
    return str(row.get("chantier") or "").strip()


def _warn(text: str) -> None:
    try:
        print("ameesh fil : %s" % text, file=sys.stderr)
    except Exception:
        pass


def warn(text: str) -> None:
    """Dit un problème de fil sur stderr ; ne lève jamais."""
    _warn(text)


def record(cfg: Config, db, *, sender: str, recipients: Sequence[str], text: str,
           ts: float | None = None, project: str | None = None, lot=None,
           mailbox_ids: Sequence[int] = (), meta: dict | None = None,
           transport: Transport | None = None) -> str | None:
    """Écrit un message déjà déposé dans son fil, et met l'index à jour.

    Ne lève JAMAIS : le message est déjà dans la boîte (ou dans le repli
    fichier) ; un fil inaccessible se dit sur stderr, il ne perd rien.
    """
    ids: list[int] = []
    try:
        ids = [int(i) for i in mailbox_ids]
        transport = transport or transport_for(cfg)
        thread = ThreadRef(project or project_for(cfg), lot)
        entry_meta: dict = {}
        if ids:
            entry_meta["ids"] = ids
        if lot not in (None, ""):
            entry_meta["lot"] = str(lot)
        entry_meta.update({k: v for k, v in (meta or {}).items() if v not in (None, "")})
        entry = Entry(author=member(cfg, sender),
                      recipient=", ".join(member(cfg, r) for r in recipients),
                      text=text, ts=float(ts) if ts else time.time(), meta=entry_meta)
        external_id = transport.post(thread, entry)
    except Exception as exc:
        _warn("écriture du fil impossible (%s) — message conservé %s" % (
            " ".join(str(exc).split())[:200],
            "dans la boîte (id %s)" % ", ".join(map(str, ids)) if ids
            else "dans la boîte fichier"))
        return None
    if db is not None:
        try:
            index(db, cfg, transport, thread, entry, external_id, ids)
        except Exception as exc:
            from . import db as db_mod
            _warn("index des fils non mis à jour (%s) — le fil et le message sont intacts"
                  % db_mod.explain(exc, 200))
    return external_id


def index(db, cfg: Config, transport, thread: ThreadRef, entry: Entry, external_id: str,
          mailbox_ids: Sequence[int] = ()) -> None:
    """Met à jour `thread_index` et note l'id externe dans `agent_mailbox.meta`
    (une seule écriture : `storage.of(db).threads.index`)."""
    host = cfg.host if transport.name == "file" else ""
    storage.of(db).threads.index(
        project=thread.project, lot=thread.lot or "", transport=transport.name, host=host,
        location=(transport.location(thread) if hasattr(transport, "location")
                  else thread.key),
        entry_id=external_id, mailbox_ids=mailbox_ids, author=entry.author,
        excerpt=excerpt(entry.text), ts=float(entry.ts),
        trace={"fil": {"transport": transport.name, "id": external_id, "host": host}})


def v0_recorded(cfg: Config) -> set[str]:
    """Messages du repli fichier déjà écrits dans un fil (`<dest>/<fichier v0>`).

    `import-v0` s'en sert pour ne pas écrire deux fois le même message.
    """
    transport = transport_for(cfg)
    seen: set[str] = set()
    for thread in transport.threads():
        try:
            entries = transport.read(thread)
        except (OSError, ThreadError):
            continue
        for entry in entries:
            for item in entry.meta.get("v0") or ():
                seen.add(str(item))
    return seen


# --------------------------------------------------------------------------
# CLI : ameesh fil list | show | tail
# --------------------------------------------------------------------------

def _moment(ts) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "—"


def _write(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def cmd_list(cfg: Config, _args: argparse.Namespace) -> int:
    from . import db as db_mod
    rows: list[dict] = []
    try:
        db = db_mod.connect(cfg)
        try:
            rows = storage.of(db).threads.indexed()
        finally:
            db.close()
    except db_mod.DbError as exc:
        print("index des fils indisponible (%s) : fils trouvés sur disque seulement"
              % " ".join(str(exc).split())[:200], file=sys.stderr)
    # Les fils écrits en repli fichier (base absente) ne sont pas indexés.
    transport = transport_for(cfg)
    indexed = {(row["project"], row["lot"] or "") for row in rows
               if row["transport"] == transport.name and row["host"] == cfg.host}
    for thread in transport.threads():
        if (thread.project, thread.lot or "") in indexed:
            continue
        try:
            entries = transport.read(thread)
        except (OSError, ThreadError):
            continue  # remplacé par un lien, ou disparu, depuis le listage
        last = entries[-1] if entries else None
        rows.append({
            "project": thread.project, "lot": thread.lot or "", "transport": transport.name,
            "host": cfg.host, "location": transport.location(thread) + "  (hors index)",
            "entries": len(entries), "last_ts": last.ts if last else None,
            "last_author": last.author if last else "",
        })
    if not rows:
        print("aucun fil")
        return 0
    print("%-20s %-12s %7s  %-16s %-20s %s" % (
        "PROJET", "LOT", "ENTRÉES", "DERNIÈRE", "AUTEUR", "EMPLACEMENT"))
    for row in rows:
        location = row.get("location") or ""
        if row.get("transport") != transport.name:
            location = "%s:%s" % (row["transport"], location)
        elif row.get("host") and row["host"] != cfg.host:
            location = "%s:%s" % (row["host"], location)
        print("%-20s %-12s %7d  %-16s %-20s %s" % (
            row["project"][:20], (row.get("lot") or "—")[:12], int(row.get("entries") or 0),
            _moment(row.get("last_ts")), (row.get("last_author") or "—")[:20], location))
    return 0


def _thread_from(args: argparse.Namespace) -> ThreadRef:
    return ThreadRef(args.project, args.lot)


def cmd_show(cfg: Config, args: argparse.Namespace) -> int:
    thread = _thread_from(args)
    transport = transport_for(cfg)
    if not transport.exists(thread):
        print("aucun fil %s (%s)" % (thread.describe(), transport.location(thread)),
              file=sys.stderr)
        return 1
    entries = transport.read(thread)
    if args.last is not None:
        entries = entries[-args.last:] if args.last > 0 else []
    for entry in entries:
        _write(format_entry(entry, meta=args.meta))
    return 0


def cmd_tail(cfg: Config, args: argparse.Namespace) -> int:
    thread = _thread_from(args)
    transport = transport_for(cfg)
    try:
        entries = transport.read(thread)
        for entry in (entries[-args.last:] if args.last > 0 else []):
            _write(format_entry(entry, meta=args.meta))
        cursor = entries[-1].id if entries else None
        print("(suivi de %s — Ctrl-C pour sortir)" % thread.describe(), file=sys.stderr)
        while True:
            time.sleep(args.interval)
            for entry in transport.read(thread, since=cursor):
                _write(format_entry(entry, meta=args.meta))
                cursor = entry.id
    except KeyboardInterrupt:
        return 0


def _positive(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("entier attendu : %r" % text)
    if value < 0:
        raise argparse.ArgumentTypeError("entier positif attendu : %r" % text)
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ameesh fil", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")
    p_list = sub.add_parser("list", help="les fils connus")
    p_list.set_defaults(func=cmd_list)
    for name, func, last, aide in (
            ("show", cmd_show, None, "lire un fil"),
            ("tail", cmd_tail, 10, "suivre un fil (Ctrl-C pour sortir)")):
        p_cmd = sub.add_parser(name, help=aide)
        p_cmd.add_argument("project", metavar="projet")
        p_cmd.add_argument("lot", nargs="?", default=None)
        p_cmd.add_argument("--last", type=_positive, default=last,
                           help="seulement les N dernières entrées")
        p_cmd.add_argument("--meta", action="store_true",
                           help="afficher aussi les métadonnées de chaque entrée")
        if name == "tail":
            p_cmd.add_argument("--interval", type=float, default=0.5,
                               help="période de relecture en secondes (défaut 0,5)")
        p_cmd.set_defaults(func=func)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    from . import config as config_mod
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if not argv:
        parser.print_help()
        return 0
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    cfg = config_mod.load()
    try:
        return args.func(cfg, args)
    except (ThreadError, OSError) as exc:
        print("erreur : %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
