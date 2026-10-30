# SPDX-License-Identifier: AGPL-3.0-only
"""Readable thread (spec §6, C5, R12): every message that goes through ameesh, in plain text.

The Postgres mailbox (`agent_mailbox`) is only the delivery queue (hooks,
waking runners); the **thread** is the readable, auditable reference for
humans, including agent-to-agent exchanges (decision 0006).

* `ThreadRef` = (project, lot); without a lot, it is the project thread.
* `Entry` = author (`human:`/`agent:`), timestamp, recipient, natural
  language text (self-contained) and structured metadata **on top**.
* `Transport`: `post(thread, entry) -> external id`, `read(thread, since)`.
  Transport v1: `file`, one Markdown file per thread under `AMEESH_THREADS`
  (default `<state>/fils/<project>/<lot or _projet>.md`), append-only, each
  entry written whole in one block under a `flock` lock. The root is
  trusted; below it, every directory and file is opened through a
  descriptor anchored on its parent, never following a symbolic link.

Retention (L361, 0033 §9): entries older than one year
(`thread_retention_days`, 0 = never) are removed by `expire`, run daily by
the runner's housekeeping and by `ameesh fil expire`. Only the oldest run
of complete ameesh entries goes (the file stays in append order; text that
cannot be dated stops it, and stays); the file is rewritten
whole under the thread's exclusive lock, then renamed over the old one. A
writer or reader that locked the replaced file sees it is no longer the
named one and reopens: no append is lost. Entry ids stay valid: they are
logical offsets, and the header keeps the shift between logical and
physical offsets in its `expired` note, with the cut-off date and the
number of entries removed so far (the audit trace of the expiry). An id
that has expired reads as "from the oldest kept entry".

A failed thread write NEVER loses the message: `record()` does not raise,
it reports on stderr, and the message stays in the mailbox.

Entry format: the message text, verbatim, in a fenced code block whose
fence (```) is longer than any run of ` in the text. No body line can close
the block: nothing in it is read as Markdown (heading at any indentation,
setext, HTML, comment, other fence), and reading it back gives the exact
text.

    ### 2026-10-04T10:15:30+02:00 — agent:alpha → agent:beta

    ```
    The message text, verbatim.
    ```

    <!-- ameesh {"host": "laptop", "ids": [12]} -->

  ameesh fil list                                known threads (index + disk)
  ameesh fil show <project> [<lot>] [--last N]   read a thread
  ameesh fil tail <project> [<lot>] [--last N]   follow a thread (Ctrl-C to quit)
  ameesh fil expire [--days N] [--dry-run]       remove entries past retention

The thread index (`thread_index`) is written and read by storage
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
from .storage.interface import ThreadCensus

DEFAULT_PROJECT = "default"
#: nom de fichier du fil d'un projet (un lot assaini ne commence jamais par `_`)
PROJECT_THREAD = "_projet"
#: au-delà, un bloc sans espace en alphabet base64/hex est une donnée encodée
MAX_BLOB = 200
#: un bloc encodé replié l'est à largeur fixe (64 en PEM, 76 en MIME, 60 pour
#: `xxd -p`) ; les mots d'un texte n'atteignent presque jamais 16 caractères
FOLD_MIN = 16
SEGMENT_MAX = 64
#: rétention par défaut des fils (0033 §9), en jours
THREAD_DAYS = 365
#: la note d'expiration est dans l'en-tête, avant la première entrée
HEADER_SCAN = 8192
#: réouvertures d'un fil remplacé par une expiration pendant qu'on l'ouvrait
_REOPEN_MAX = 8

_HEADING_RE = re.compile(r"^### (\S+) — (.+?) → (.*)$")
_META_RE = re.compile(r"^<!-- ameesh (\{.*\}) -->$")
_EXPIRED_B = re.compile(rb"^<!-- ameesh (\{\"expired\": .*\}) *-->$", re.M)
# la note d'expiration d'un en-tête, phrase lisible comprise, pour la remplacer
_EXPIRY_BLOCK_B = re.compile(
    rb"(?:^Entries before [^\n]*\n\n)?^<!-- ameesh \{\"expired\": [^\n]*-->\n(?:\n)?", re.M)
#: largeur réservée au décalage dans la note : l'en-tête a la même longueur
#: quel que soit le décalage qu'il porte (sinon point fixe parfois introuvable)
_SHIFT_WIDTH = 24
# dans l'en-tête d'un fil, un titre de section ou une barrière de code n'est
# pas d'ameesh : du texte qu'on ne sait pas dater, l'expiration s'arrête
_FOREIGN_B = re.compile(rb"^ {0,3}(?:#{2,6}[ \t]|`{3,}|~{3,})", re.M)
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
_ALLOW_HINT = "(--allow-structured is reserved for tests and tools)"


class UnreadableBody(ValueError):
    """Corps de message refusé : il n'est pas du texte lisible par un humain."""


class ThreadError(RuntimeError):
    """Fil inaccessible (emplacement hors de la racine, identifiant étranger…)."""


class ExpiryUnavailable(ThreadError):
    """Pas d'expiration sans index : ni transaction Postgres, ni connexion
    médiée pour l'opération serveur L361b (`threads.recount`), qui fait
    verrou, recomptage et expiration en une seule transaction."""


EXPIRY_UNAVAILABLE = ("thread expiry needs the index: no database or mediated"
                      " connection, nothing expired")


def expiry_unavailable(cfg: Config, db=None) -> bool:
    """L'expiration a-t-elle de quoi tenir son index ?

    Une connexion Postgres tient le verrou de la ligne d'index pendant la
    réécriture du fichier (L361). Un exécuteur médié n'a pas de transaction :
    `threads.recount` (L361b) verrouille, recompte et expire en UNE opération
    serveur. Sans connexion, un hôte médié (`backend: mediated`, ou
    `host_mediated` dont les fils restent locaux, r8) refuse : il ne peut pas
    tenir son index — la connexion est justement ce qui manque."""
    if storage.is_remote(db):
        return False
    if db is not None:
        return False
    return (getattr(cfg, "backend", "") == "mediated" or bool(getattr(cfg, "host_mediated", False)))


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
            else "%s (project thread)" % self.project


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

    def post(self, thread: ThreadRef, entry: Entry, *,
             with_census: bool = True) -> str: ...

    def read(self, thread: ThreadRef, since: str | None = None) -> list[Entry]: ...


# --------------------------------------------------------------------------
# texte : lisibilité, nettoyage, échappement
# --------------------------------------------------------------------------

def unreadable_reason(body) -> str | None:
    """Pourquoi ce corps n'est pas du texte lisible (None s'il l'est)."""
    if body is None or not str(body).strip():
        return "empty body: a message must contain readable text"
    body = str(body)
    for ch in body:
        if ch in "\n\t\r":
            continue
        if unicodedata.category(ch) in ("Cc", "Cs"):
            return ("control or binary character U+%04X: the thread only accepts "
                    "text (remove terminal colors and binary data)"
                    % ord(ch))
    stripped = body.strip()
    if stripped[0] in "{[":
        try:
            value = json.loads(stripped)
        except ValueError:
            value = None
        if isinstance(value, (dict, list)):
            return ("the body is only JSON: write in sentences what the recipient "
                    "needs to know; structured data goes along with the text, it does not "
                    "replace it")
    for blob, folded in _blobs(body):
        if re.search(r"[A-Za-z0-9]", blob) and not _word_like(blob):
            return ("encoded block of %d characters %s (base64 or hexadecimal): "
                    "describe it in plain words and send the data another way"
                    % (len(blob), "folded over several lines" if folded else "without spaces"))
    # L'hexadécimal, replié ou espacé (« de ad be ef … »), se voit aussi dans le
    # texte sans aucun blanc : aucun texte n'aligne 200 caractères de [0-9a-f]
    # mêlant lettres et chiffres (une liste de nombres n'a pas de lettres).
    for match in _HEX_RUN_RE.finditer("".join(body.split())):
        run = match.group(0)
        letters = sum(ch.isalpha() for ch in run)
        if 0.05 * len(run) <= letters <= 0.95 * len(run):
            return ("encoded block of %d characters (folded or spaced hexadecimal): "
                    "describe it in plain words and send the data another way"
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
        raise UnreadableBody("message refused — %s %s" % (reason, _ALLOW_HINT))


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
        "# Thread — %s\n\n"
        "Readable thread kept by ameesh: one entry per message, append-only.\n"
        "Each message text is copied verbatim into a code block;\n"
        "its metadata is in an unobtrusive `ameesh` comment.\n\n"
        % thread.describe()
    )


def _expiry_of(head: bytes) -> tuple[int, dict]:
    """La note d'expiration d'un en-tête de fil : (décalage, note).

    Le décalage est ajouté à la position physique d'une entrée pour donner son
    id (position logique, stable d'une expiration à l'autre). Sans note, ou
    note illisible : 0, le fil n'a jamais expiré.
    """
    first = _HEADING_B.search(head)
    found = _EXPIRED_B.search(head, 0, first.start() if first else len(head))
    if not found:
        return 0, {}
    try:
        note = json.loads(found.group(1))["expired"]
    except (ValueError, KeyError, TypeError):
        return 0, {}
    shift = note.get("shift") if isinstance(note, dict) else None
    if not isinstance(shift, int) or isinstance(shift, bool):
        return 0, {}
    return shift, note


def _expiry_block(note: dict) -> bytes:
    """La note d'expiration de l'en-tête : la date de coupure et le nombre
    d'entrées retirées, lisibles, puis la note pour les outils, complétée
    d'espaces pour que sa longueur ne dépende pas du décalage."""
    comment = json.dumps({"expired": note}, ensure_ascii=False)
    width = len(json.dumps({"expired": dict(note, shift=0)}, ensure_ascii=False)) + _SHIFT_WIDTH
    return ("Entries before %s have expired (retention of %d days): %d removed so far,"
            " last expiry on %s.\n\n<!-- ameesh %s -->\n\n"
            % (note["before"], note["days"], note["entries"], note["at"],
               comment.ljust(width))).encode("utf-8")


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


def _latest_entry(data: bytes, found: list, base: int, key: str) -> Entry | None:
    """L'entrée la plus récente d'une marche déjà faite (`_walk`) : la plus
    grande date, la dernière écrite à égalité de date, comme l'index la cite.

    Seule cette entrée est relue (`parse` de sa tranche), là où le census
    complet les décode toutes : l'appelant qui vient de marcher le fil pour le
    réparer en profite (L361c)."""
    if not found:
        return None
    at = max(reversed(range(len(found))),
             key=lambda i: _parse_iso(found[i][1].group(1).decode("utf-8", "replace")))
    start = found[at][0]
    end = found[at + 1][0] if at + 1 < len(found) else len(data)
    entries = parse(data[start:end], base=base + start, key=key)
    return entries[0] if entries else None


def _repair(data: bytes, fence: bytes | None) -> bytes:
    """Ce qu'il faut écrire avant une nouvelle entrée si la précédente a été
    interrompue (arrêt brutal en pleine écriture) : finir la ligne, fermer le
    bloc de code ou le commentaire resté ouvert. Sans cela, un lecteur
    Markdown avalerait la suite du fil dans le bloc ouvert.

    `fence` : la barrière restée ouverte (`_walk`), pour ne pas refaire la
    marche du fil quand l'appelant l'a déjà faite."""
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
            raise ThreadError("%s: symbolic link or unexpected file type, refused"
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
        #: census laissé par le dernier `post` de cette instance, par fil :
        #: (périphérique, inode, taille, mtime_ns, ctime_ns, entrées, dernière
        #: ligne, fin logique). `census_after_post` ne le rend que si le fichier
        #: est resté exactement celui-là.
        self._posted: dict[str, tuple] = {}

    #: #1764 N5 : borne du cache — il ne sert qu'au `post` qui précède
    #: l'indexation, un processus de longue vie ne garde pas un census par fil.
    _POSTED_MAX = 256

    def _remember(self, key: str, census: tuple) -> None:
        """Retient le census d'un fil, en oubliant le plus ancien au-delà de
        `_POSTED_MAX` (#1764 N5 : `_posted` ne grandit pas sans borne)."""
        self._posted.pop(key, None)     # un fil reposté passe en dernier
        self._posted[key] = census
        while len(self._posted) > self._POSTED_MAX:
            self._posted.pop(next(iter(self._posted)))

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
        try:
            return self._open_file(dir_fd, thread, write)
        finally:
            os.close(dir_fd)

    def _open_file(self, dir_fd: int, thread: ThreadRef, write: bool) -> int:
        """Le fichier du fil dans le dossier `dir_fd` (voir `_open`)."""
        flags = (os.O_RDWR | os.O_APPEND | os.O_CREAT) if write \
            else (os.O_RDONLY | os.O_NONBLOCK)
        fd = _open_at(dir_fd, thread.stem + ".md", flags, self.location(thread))
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ThreadError("%s: not a regular file, refused" % self.location(thread))
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _locked(self, thread: ThreadRef, mode: int, *, write: bool,
                keep_dir: bool = False) -> tuple[int, int | None]:
        """`_open` puis `flock(mode)`, sur le fichier qui porte ENCORE le nom du fil.

        `expire` remplace le fichier par renommage sous son verrou exclusif :
        un descripteur pris sur l'ancien fichier avant le renommage obtient le
        verrou ensuite, sur un fichier détaché. On compare donc le fichier
        verrouillé au nom (même dossier, sans suivre de lien) et on rouvre s'il
        a changé : aucune entrée n'est écrite dans un fichier remplacé.
        Rend (fichier, dossier) ; le dossier n'est gardé ouvert qu'avec
        `keep_dir` (sinon None).
        """
        name = thread.stem + ".md"
        for _ in range(_REOPEN_MAX):
            root_fd = self._root_fd(create=write)
            try:
                dir_fd = self._dir_fd(root_fd, thread.project, create=write)
            finally:
                os.close(root_fd)
            fd = -1
            try:
                fd = self._open_file(dir_fd, thread, write)
                fcntl.flock(fd, mode)
                held = os.fstat(fd)
                try:
                    named = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                except FileNotFoundError:
                    named = None
                if named is not None and (named.st_dev, named.st_ino) \
                        == (held.st_dev, held.st_ino):
                    found, fd = fd, -1
                    if keep_dir:
                        found_dir, dir_fd = dir_fd, -1
                        return found, found_dir
                    return found, None
            finally:
                if fd >= 0:
                    os.close(fd)
                if dir_fd >= 0:
                    os.close(dir_fd)
        raise ThreadError("%s: replaced again and again while opening, refused"
                          % self.location(thread))

    def exists(self, thread: ThreadRef) -> bool:
        try:
            os.close(self._open(thread, write=False))
        except FileNotFoundError:
            return False
        return True

    def post(self, thread: ThreadRef, entry: Entry, *,
             with_census: bool = True) -> str:
        """Écrit l'entrée à la fin du fil et retient son census (L361c).

        La marche faite pour réparer une écriture interrompue sert aussi à
        déduire le census du fil APRÈS l'ajout (`census_after_post`) : sur un
        exécuteur médié, l'indexation n'a plus à relire tout le fil. Le census
        n'est retenu que si le fil était neuf ou bien formé (rien à réparer) ;
        tout doute (réparation, autre écrivain, expiration) le fait écarter.
        `with_census=False` (#1764 N3) : l'indexation locale ne demande jamais
        ce census — la marche de réparation reste faite, mais la plus récente
        entrée n'est pas relue pour un census que personne ne demandera.
        """
        data = format_entry(entry).encode("utf-8")
        fd, _ = self._locked(thread, fcntl.LOCK_EX, write=True)
        census = None
        self._posted.pop(thread.key, None)  # un post raté n'a pas de census
        try:
            size = os.fstat(fd).st_size
            shift = 0
            found: list = []
            current = b""
            if size == 0:
                prefix = header(thread).encode("utf-8")
            else:
                # une écriture précédente interrompue (arrêt brutal) : la clore
                current = _pread_all(fd, size)
                found, fence = _walk(current)
                prefix = _repair(current, fence)
                shift = _expiry_of(current[:HEADER_SCAN])[0]
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
            if with_census and (size == 0 or not prefix):
                try:
                    census = self._census_after(thread, data, found, current,
                                                size + len(prefix), shift, os.fstat(fd))
                except Exception:
                    # #1764 N2 : le fil est écrit ; un census illisible ne fait
                    # pas échouer le post, l'appelant relira le fil entier.
                    census = None
        finally:
            os.close(fd)  # libère le verrou
        entry.id = "%s#%d" % (thread.key, size + len(prefix) + shift)
        if census is None:
            self._posted.pop(thread.key, None)
        else:
            self._remember(thread.key, census)
        return entry.id

    def _census_after(self, thread: ThreadRef, data: bytes, found: list,
                      current: bytes, start: int, shift: int,
                      st: os.stat_result) -> tuple:
        """Le census du fil après l'ajout : entrées gardées, la plus récente
        (ligne d'index), fin logique — et l'identité du fichier écrit.

        `found`/`current` : la marche déjà faite du fichier d'avant, bien formé ;
        `data` : l'entrée écrite, à la position logique `start + shift` (relue,
        comme le ferait un census complet).
        """
        new_entries = parse(data, base=start + shift, key=thread.key)
        new = new_entries[0] if new_entries else None
        before = _latest_entry(current, found, shift, thread.key)
        last = new if new is not None and (before is None or new.ts >= before.ts) else before
        # #1764 N4 : le compte vient des entrées réellement lues, pas d'un « +1 »
        # aveugle qui compterait une entrée que la relecture complète ignore.
        # #1764 N1 : mtime/ctime_ns entrent dans l'identité — une réécriture de
        # même taille (ou une réutilisation d'inode) les change, pas la taille.
        return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns,
                len(found) + len(new_entries), _index_row(last), st.st_size + shift)

    def _offset(self, thread: ThreadRef, since: str) -> int:
        key, sep, offset = str(since).rpartition("#")
        if not sep or key != thread.key or not offset.isdigit():
            raise ThreadError("entry id %r does not belong to thread %s" % (since, thread.key))
        return int(offset)

    def read(self, thread: ThreadRef, since: str | None = None) -> list[Entry]:
        """Les entrées du fil, ou celles postérieures à l'id `since`."""
        start = self._offset(thread, since) if since else 0
        try:
            # verrou partagé : jamais une entrée en cours d'écriture
            fd, _ = self._locked(thread, fcntl.LOCK_SH, write=False)
        except FileNotFoundError:
            return []
        with open(fd, "rb") as fh:
            head = fh.read(HEADER_SCAN)
            shift = _expiry_of(head)[0]
            # `since` logique ; avant la première entrée gardée (expirée) :
            # tout le fil, l'en-tête n'a pas d'entrée
            first = _HEADING_B.search(head)
            physical = start - shift
            if physical < (first.start() if first else len(head)):
                physical = 0
            fh.seek(physical)
            data = fh.read()
        entries = parse(data, base=physical + shift, key=thread.key)
        if since:
            entries = [e for e in entries if int(e.id.rpartition("#")[2]) > start]
        return entries

    def census(self, thread: ThreadRef) -> tuple[list[Entry], int]:
        """Toutes les entrées du fil et sa fin logique, lues sous un même
        verrou : une entrée ajoutée ensuite est au-delà de cette fin."""
        try:
            fd, _ = self._locked(thread, fcntl.LOCK_SH, write=False)
        except FileNotFoundError:
            return [], 0
        with open(fd, "rb") as fh:
            data = fh.read()
        shift = _expiry_of(data[:HEADER_SCAN])[0]
        return parse(data, base=shift, key=thread.key), len(data) + shift

    def bounds(self, thread: ThreadRef) -> tuple[int, int] | None:
        """Après une expiration : (première position logique gardée, fin
        logique du fil à l'expiration) ; None si le fil n'a jamais expiré.

        `index` s'en sert : une entrée d'avant la première gardée a expiré
        avant d'être indexée. Ce qui est déjà compté, seule la base le sait
        (`counted_until`) ; une base en retard sur la fin (recomptage perdu)
        recompte depuis le fichier (`_recount_if_behind`).
        """
        try:
            fd, _ = self._locked(thread, fcntl.LOCK_SH, write=False)
        except FileNotFoundError:
            return None
        try:
            note = _expiry_of(os.pread(fd, HEADER_SCAN, 0))[1]
        finally:
            os.close(fd)
        kept, end = note.get("from"), note.get("end")
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in (kept, end)):
            return None
        return kept, end

    def census_after_post(self, thread: ThreadRef) -> tuple[int, dict | None, int] | None:
        """Le census du fil tel que le dernier `post` de cette instance l'a
        laissé : entrées gardées, la plus récente (ligne d'index), fin logique.

        Le census complet relit et décode tout le fil ; ici il est déjà déduit
        de la marche que `post` fait pour réparer une écriture interrompue
        (L361c). Il n'est rendu que si le fichier est resté exactement celui
        écrit — même fichier (périphérique, inode), même taille et mêmes
        horodatages (une réécriture de même taille change mtime/ctime_ns,
        #1764 N1) : un autre écrivain, une expiration ou un remplacement le
        font écarter, et l'appelant relit le fil (`census`)."""
        state = self._posted.get(thread.key)
        if state is None:
            return None
        dev, ino, size, mtime_ns, ctime_ns, kept, last, end = state
        try:
            fd = self._open(thread, write=False)
        except (OSError, ThreadError):
            return None
        try:
            st = os.fstat(fd)
        finally:
            os.close(fd)
        if (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns) != (
                dev, ino, size, mtime_ns, ctime_ns):
            return None
        return kept, last, end

    def expire(self, thread: ThreadRef, before_ts: float, *, days: int,
               now: float | None = None, dry_run: bool = False) -> dict:
        """Retire du fil la plus longue suite d'entrées, depuis la première,
        écrites avant `before_ts` (L361).

        S'arrête à la première entrée plus récente ou sans date lisible, et à
        la première qui n'est pas une entrée d'ameesh complète (titre, bloc de
        code fermé, commentaire `ameesh` facultatif, puis rien que des lignes
        vides) : un texte qu'on ne sait pas dater n'est jamais retiré. Le fil
        reste dans l'ordre d'ajout ; une entrée arrivée en retard avec une
        date ancienne (file R12) part avec ses voisines. Le reste, en-tête
        compris, est recopié à l'identique dans un fichier neuf du même
        dossier (nom imprévisible, créé exclusivement), synchronisé, puis
        renommé sur l'ancien, le tout sous le verrou exclusif du fil : un arrêt
        brutal laisse l'ancien fichier ou le nouveau, jamais un mélange.
        Rend {"dropped", "kept", "shift", "last"} (`last` : l'entrée gardée la
        plus récente, pour l'index) ; un fil absent ou à jour n'est pas touché.
        """
        report = {"thread": thread.key, "dropped": 0, "kept": 0, "shift": 0, "last": None}
        try:
            fd, dir_fd = self._locked(thread, fcntl.LOCK_EX, write=False, keep_dir=True)
        except FileNotFoundError:
            return report
        tmp = None
        try:
            data = _pread_all(fd, os.fstat(fd).st_size)
            shift, note = _expiry_of(data[:HEADER_SCAN])
            found = _walk(data)[0]
            dropped, cut = 0, found[0][0] if found else len(data)
            if _FOREIGN_B.search(_EXPIRY_BLOCK_B.sub(b"", data[:cut])):
                report.update(kept=len(found), shift=shift,
                              held="unrecognised text before the first entry")
                return report
            for n, (start, heading, body, _loose, after, torn) in enumerate(found):
                ts = _parse_iso(heading.group(1).decode("utf-8", "replace"))
                if not 0 < ts < before_ts or body is None or torn or after is None:
                    break
                end = found[n + 1][0] if n + 1 < len(found) else len(data)
                rest = after[0]
                if _meta_of(data[after[0]:after[1]].decode("utf-8", "replace")) is not None:
                    rest = after[1]
                dropped += 1
                cut = end
                foreign = data[rest:end]
                if foreign.strip():
                    # du texte hors entrée (titre non reconnu…) : on ne sait
                    # pas le dater, il reste, et tout ce qui le suit
                    cut = rest + len(foreign) - len(foreign.lstrip())
                    break
            # #4809 : la marche connaît déjà le nombre d'entrées. Décoder
            # tous leurs corps gonfle inutilement le RSS du ménage quotidien,
            # même quand aucune entrée n'expire. Seul l'index a besoin du
            # contenu de l'entrée la plus récente, après une expiration.
            kept = found[dropped:]
            report.update(kept=len(kept), shift=shift)
            if not dropped:
                return report
            total = note.get("entries") if isinstance(note.get("entries"), int) else 0
            note = {"before": iso_local(before_ts),
                    "at": iso_local(time.time() if now is None else now),
                    "days": int(days), "entries": total + dropped,
                    "from": cut + shift, "end": len(data) + shift, "shift": 0}
            # l'en-tête d'avant (première entrée exclue) reste, sa note remplacée
            preamble = _EXPIRY_BLOCK_B.sub(b"", data[:found[0][0]])
            if preamble and not preamble.endswith(b"\n\n"):
                preamble = preamble.rstrip(b"\n") + b"\n\n"
            # l'id d'une entrée gardée ne change pas : sa position logique
            # (cut + shift) devient la fin du nouvel en-tête plus le décalage ;
            # la longueur de la note ne dépend pas du décalage
            head_len = len(preamble) + len(_expiry_block(note))
            note["shift"] = cut + shift - head_len
            head = preamble + _expiry_block(note)
            if len(head) != head_len:
                raise ThreadError("%s: expiry note width changed, refused" % thread.key)
            # à égalité d'horodatage, la dernière écrite (comme l'index)
            report.update(dropped=dropped, shift=note["shift"], before=note["before"],
                          end=note["end"],
                          last=_latest_entry(data, kept, shift, thread.key))
            if dry_run:
                return report
            self._sweep_temporaries(dir_fd, thread)
            tmp = ".%s.expire.%s.tmp" % (thread.stem, os.urandom(8).hex())
            out = _open_at(dir_fd, tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                           os.path.join(self.root, thread.project, tmp))
            try:
                _write_all(out, head + data[cut:])
                os.fsync(out)
            finally:
                os.close(out)
            os.rename(tmp, thread.stem + ".md", src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            tmp = None
            os.fsync(dir_fd)
            self._posted.pop(thread.key, None)   # fichier réécrit : census caduc
            return report
        except BaseException:
            if tmp is not None:  # seulement le fichier que cet appel a créé
                try:
                    os.unlink(tmp, dir_fd=dir_fd)
                except OSError:
                    pass
            raise
        finally:
            os.close(fd)  # libère le verrou, après le renommage
            os.close(dir_fd)

    @staticmethod
    def _sweep_temporaries(dir_fd: int, thread: ThreadRef) -> None:
        """Les restes d'une expiration interrompue (arrêt brutal) de ce fil :
        retirer le NOM suffit (un lien n'est jamais suivi, ni sa cible touchée)."""
        prefix, suffix = ".%s.expire." % thread.stem, ".tmp"
        with os.scandir(dir_fd) as it:
            names = [e.name for e in it if e.name.startswith(prefix) and e.name.endswith(suffix)]
        for name in names:
            try:
                os.unlink(name, dir_fd=dir_fd)
            except FileNotFoundError:
                pass

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
        return "all"
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


def lot_project(db, lot) -> str:
    """Projet du fil d'un lot (`work_items.app`, L118c) : vide si le lot est
    inconnu, sans projet, si `lot` est une étiquette libre (pas un numéro), ou
    si la base ne répond pas — l'appelant garde alors la règle historique
    (expéditeur, destinataire, défaut). Un message lié à un lot se lit donc
    dans le fil du projet de ce lot, pas dans celui de son expéditeur ; ne
    lève jamais (l'écriture du fil ne doit rien perdre)."""
    if db is None or lot in (None, ""):
        return ""
    try:
        item_id = int(lot)
    except (TypeError, ValueError):
        return ""  # étiquette libre du fil : aucun lot à consulter
    try:
        item = storage.of(db).work.get(item_id)
    except Exception as exc:
        _warn("lot %s: project unreadable (%s)" % (lot, exc))
        return ""
    return str((item or {}).get("app") or "").strip()


def _warn(text: str) -> None:
    try:
        print("ameesh fil: %s" % text, file=sys.stderr)
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
        from .agent_names import resolve
        sender = resolve(sender, db=db, cfg=cfg, operation="thread.sender")
        recipients = [resolve(r, db=db, cfg=cfg, operation="thread.recipient") for r in recipients]
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
        # #1764 N3 : le census n'est calculé que pour un index médié, seul
        # lecteur de `census_after_post` ; une indexation locale ne le demande
        # jamais (le calcul relit la plus récente entrée à chaque post).
        external_id = transport.post(
            thread, entry, with_census=db is not None and storage.is_remote(db))
    except Exception as exc:
        _warn("thread write failed (%s) — message kept %s" % (
            " ".join(str(exc).split())[:200],
            "in the mailbox (id %s)" % ", ".join(map(str, ids)) if ids
            else "in the file mailbox"))
        return None
    if db is not None:
        try:
            index(db, cfg, transport, thread, entry, external_id, ids)
        except Exception as exc:
            from . import db as db_mod
            _warn("thread index not updated (%s) — the thread and the message are intact"
                  % db_mod.explain(exc, 200))
    return external_id


def index(db, cfg: Config, transport, thread: ThreadRef, entry: Entry, external_id: str,
          mailbox_ids: Sequence[int] = ()) -> None:
    """Met à jour `thread_index` et note l'id externe dans `agent_mailbox.meta`.

    Avec une connexion Postgres, une seule écriture (`threads.index`) : L361,
    sous le verrou de la ligne du fil (`threads.lock`), que `expire` prend
    aussi. Une entrée expirée entre son écriture et son indexation n'est ni
    comptée ni citée ; une entrée déjà dans le fil quand la base l'a recompté
    (`counted_until`, écrit dans la transaction du recomptage) n'est pas
    comptée deux fois. Le fichier ne prouve pas ce recomptage : après une
    expiration sans base, ou dont le recomptage n'a pas été validé (échec,
    arrêt brutal après le renommage), la base est en retard sur le fichier et
    recompte d'abord depuis lui (`_recount_if_behind`).

    Sur une connexion médiée, L361c : l'indexation est le census absolu du
    fichier (`threads.recount`, portée H), la même opération serveur que
    l'expiration — un `+1` local ne peut plus compter en double une entrée
    déjà reprise par un recomptage, ni être écrasé par un recomptage plus
    court. Un census de même fin logique qu'un recomptage plus court est
    périmé (le recount ne reprend pas un census qui rallonge la ligne) :
    la trace de la boîte reste écrite par `threads.index` (`count` faux),
    portée B et auteur membre du fil, quand l'entrée porte des messages ;
    elle ne touche ni le compte ni `last_*`, donc une entrée expirée
    entre-temps n'est jamais ressuscitée par sa trace. Le census vient du
    `post` qui précède quand le fichier n'a pas bougé depuis
    (`census_after_post` : la marche de la réparation est réutilisée, pas de
    seconde lecture du fil) ; sinon il est relu en entier.
    """
    host = cfg.host if transport.name == "file" else ""
    key = dict(project=thread.project, lot=thread.lot or "", transport=transport.name,
               host=host, location=(transport.location(thread)
                                    if hasattr(transport, "location") else thread.key))
    if storage.is_remote(db):
        # exécuteur médié : un verrou ne tiendrait pas d'une requête à
        # l'autre. L361c : le compte vient du census du fichier (à suivre ou
        # à venir), par la même opération serveur que l'expiration
        # (`threads.recount`, portée H), et non plus d'un `+1` local en
        # course avec un recomptage. La trace de la boîte reste une écriture
        # à part, sans comptage ni citation (`count` faux : ni `entries` ni
        # `last_*`).
        census = None
        if hasattr(transport, "census"):
            # Le post qui précède vient de marcher le fil : son census est
            # rendu tel quel s'il n'a pas bougé depuis (`census_after_post`),
            # sinon on relit le fil entier (L361c).
            after_post = getattr(transport, "census_after_post", None)
            census = after_post(thread) if after_post is not None else None
            if census is None:
                census = _census(transport, thread)
        if census is None or mailbox_ids:
            storage.of(db).threads.index(
                **key, entry_id=external_id, mailbox_ids=mailbox_ids, author=entry.author,
                excerpt=excerpt(entry.text), ts=float(entry.ts),
                trace={"fil": {"transport": transport.name, "id": external_id, "host": host}},
                count=census is None)
        if census is not None:
            kept, last, end = census
            storage.of(db).threads.recount(**key, kept=kept, last=last, end=end)
        return
    with db.transaction() as tx:
        threads = storage.of(tx).threads
        counted = threads.lock(**key)
        bounds = transport.bounds(thread) if hasattr(transport, "bounds") else None
        counted = _recount_if_behind(threads, key, transport, thread, counted, bounds)
        offset = _offset_of(external_id)
        if bounds is not None and offset is not None and offset < bounds[0]:
            return
        threads.index(
            **key, entry_id=external_id, mailbox_ids=mailbox_ids, author=entry.author,
            excerpt=excerpt(entry.text), ts=float(entry.ts),
            trace={"fil": {"transport": transport.name, "id": external_id, "host": host}},
            count=not (counted is not None and offset is not None and offset < counted))


def _recount_if_behind(threads, key: dict, transport, thread: ThreadRef,
                       counted: int | None, bounds: tuple[int, int] | None) -> int | None:
    """Sous le verrou de la ligne : si la dernière expiration du fichier
    (`bounds`) n'a pas de recomptage validé en base (`counted` absent ou
    avant sa fin), l'index reprend le fichier tel qu'il est (nombre
    d'entrées, la plus récente, fin logique). Rend le `counted_until` à jour.
    """
    if bounds is None or (counted is not None and counted >= bounds[1]):
        return counted
    kept, last, end = _census(transport, thread)
    threads.expired(**{k: v for k, v in key.items() if k != "location"},
                    kept=kept, last=last, end=end)
    return end


def _census(transport, thread: ThreadRef) -> tuple[int, dict | None, int]:
    """Le census du fil, prêt pour l'index : entrées gardées, la plus récente
    (ligne d'index), fin logique. Lu une seule fois, sous le verrou partagé du
    fichier (`FileTransport.census`)."""
    entries, end = transport.census(thread)
    latest = max(reversed(entries), key=lambda e: e.ts) if entries else None
    return len(entries), _index_row(latest), end


def _offset_of(entry_id: str) -> int | None:
    offset = str(entry_id).rpartition("#")[2]
    return int(offset) if offset.isascii() and offset.isdigit() else None


def expire(cfg: Config, db, *, days: int | None = None, now: float | None = None,
           dry_run: bool = False, transport: FileTransport | None = None) -> list[dict]:
    """Expiration de tous les fils de cet hôte (L361, 0033 §9).

    `days` : `thread_retention_days` par défaut ; 0 = rien. Avec une base,
    chaque fil expire sous le verrou de sa ligne d'index (pris AVANT le
    verrou du fichier, dans le même ordre que l'écriture différée des fils
    R12) ; l'index reprend alors le nombre d'entrées gardées et la plus
    récente, et un fil vidé ne cite plus d'entrée expirée. Un fil en erreur
    est signalé dans son rapport (`error`), les autres expirent quand même.

    Sur un exécuteur médié (`storage.is_remote(db)`), le verrou de la ligne
    ne peut pas être tenu d'une requête à l'autre (L361b) : le fichier est
    réécrit sous son propre verrou, puis `threads.recount` fait verrou,
    recomptage et expiration en une seule opération serveur. Un hôte médié
    sans connexion refuse (`ExpiryUnavailable`) : sans index, rien n'expire.
    """
    days = cfg.thread_retention_days if days is None else days
    if not isinstance(days, int) or isinstance(days, bool) or days < 0:
        raise ValueError("thread retention must be a whole number of days, >= 0")
    if days == 0:
        return []
    if not dry_run and expiry_unavailable(cfg, db):
        raise ExpiryUnavailable(EXPIRY_UNAVAILABLE)
    now = time.time() if now is None else float(now)
    transport = transport or transport_for(cfg)
    host = cfg.host if transport.name == "file" else ""
    reports = []
    for thread in transport.threads():
        run = lambda: transport.expire(thread, now - days * 86400, days=days, now=now,
                                       dry_run=dry_run)
        try:
            if db is None or dry_run:
                report = run()
            else:
                key = dict(project=thread.project, lot=thread.lot or "",
                           transport=transport.name, host=host)
                location = transport.location(thread)
                if storage.is_remote(db):
                    # L361b : le fichier d'abord (verrou local), puis UNE
                    # opération serveur (verrou de la ligne, recomptage,
                    # expiration). Un recomptage perdu est rattrapé à
                    # l'expiration suivante, depuis le fichier : l'index ne
                    # reste pas en arrière.
                    report = run()
                    _recount_remote(db, transport, thread, key, location, report)
                else:
                    with db.transaction() as tx:
                        threads = storage.of(tx).threads
                        counted = threads.lock(**key, location=location)
                        report = run()
                        if report["dropped"]:
                            threads.expired(**key, kept=report["kept"],
                                            last=_index_row(report["last"]), end=report["end"])
                        else:
                            # une expiration d'avant dont le recomptage a été perdu
                            _recount_if_behind(threads, dict(key, location=None), transport,
                                               thread, counted, transport.bounds(thread))
        except (OSError, ThreadError) as exc:
            reports.append({"thread": thread.key, "dropped": 0,
                            "error": " ".join(str(exc).split())[:200]})
            continue
        except Exception as exc:
            from . import db as db_mod
            reports.append({"thread": thread.key, "dropped": 0,
                            "error": "thread index: %s" % db_mod.explain(exc, 200)})
            continue
        reports.append(report)
    return reports


def _recount_remote(db, transport, thread: ThreadRef, key: dict, location: str,
                    report: dict) -> None:
    """L361b : l'index d'un fil d'hôte médié, en une seule opération serveur.

    Le fichier vient d'être réécrit sous son verrou (ou n'a jamais expiré) :
    `threads.recount` verrouille la ligne d'index, reprend le census du
    fichier (`kept`, `last`, `end`) et l'expiration dans la même transaction.
    Un recomptage perdu (échec ou arrêt brutal d'une expiration précédente)
    est refait de la même façon que `_recount_if_behind` côté Postgres.
    """
    bounds = transport.bounds(thread) if hasattr(transport, "bounds") else None
    if not report["dropped"] and bounds is None:
        return          # jamais expiré : l'index d'avant L361 suffit
    kept, last, end = _census(transport, thread)
    storage.of(db).threads.recount(**key, location=location, kept=kept, last=last, end=end)


def _index_row(entry: Entry | None) -> ThreadCensus | None:
    """La plus récente entrée gardée, telle que l'index la cite (L361b, la
    forme déclarée au contrat : `entry_id`, `author`, `excerpt`, `ts`,
    `mailbox_id`)."""
    if entry is None:
        return None
    ids = [i for i in entry.meta.get("ids") or () if isinstance(i, int) and not isinstance(i, bool)]
    return {"entry_id": entry.id, "author": entry.author, "excerpt": excerpt(entry.text),
            "ts": entry.ts, "mailbox_id": max(ids) if ids else None}


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
        print("thread index unavailable (%s): showing threads found on disk only"
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
            "host": cfg.host, "location": transport.location(thread) + "  (not indexed)",
            "entries": len(entries), "last_ts": last.ts if last else None,
            "last_author": last.author if last else "",
        })
    if not rows:
        print("no threads")
        return 0
    print("%-20s %-12s %7s  %-16s %-20s %s" % (
        "PROJECT", "LOT", "ENTRIES", "LAST", "AUTHOR", "LOCATION"))
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
        print("no thread %s (%s)" % (thread.describe(), transport.location(thread)),
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
        print("(following %s — Ctrl-C to quit)" % thread.describe(), file=sys.stderr)
        while True:
            time.sleep(args.interval)
            for entry in transport.read(thread, since=cursor):
                _write(format_entry(entry, meta=args.meta))
                cursor = entry.id
    except KeyboardInterrupt:
        return 0


def cmd_expire(cfg: Config, args: argparse.Namespace) -> int:
    from . import db as db_mod
    days = cfg.thread_retention_days if args.days is None else args.days
    if not days:
        print("thread retention is 0: nothing expires")
        return 0
    db = None
    if not args.dry_run:
        # L361b: a mediated host expires through the server operation (lock,
        # recount, expiry); the connection says whether it exists. Without it,
        # `expire` refuses and nothing is touched.
        try:
            db = db_mod.connect(cfg)
        except db_mod.DbError as exc:
            message = " ".join(str(exc).split())[:200]
            if expiry_unavailable(cfg, None):
                # L361b: without an index, a mediated host does not expire; the
                # refusal starts here, before any thread rewrite
                raise ExpiryUnavailable("%s (index unavailable: %s)" % (EXPIRY_UNAVAILABLE,
                                                                        message))
            print("thread index unavailable (%s): threads expire, the index is not updated"
                  % message, file=sys.stderr)
    try:
        reports = expire(cfg, db, days=days, dry_run=args.dry_run)
    finally:
        if db is not None:
            db.close()
    failed = 0
    for report in reports:
        if report.get("error"):
            failed += 1
            print("%s: %s" % (report["thread"], report["error"]), file=sys.stderr)
        if report.get("held"):
            print("%s: not expired, %s" % (report["thread"], report["held"]), file=sys.stderr)
        if report["dropped"]:
            print("%s: %d entr%s %s before %s, %d kept" % (
                report["thread"], report["dropped"], "y" if report["dropped"] == 1 else "ies",
                "would expire" if args.dry_run else "expired", report["before"],
                report["kept"]))
    if not any(r["dropped"] for r in reports):
        print("no entry older than %d days in %d thread(s)" % (days, len(reports)))
    return 1 if failed else 0


def _positive(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("integer expected: %r" % text)
    if value < 0:
        raise argparse.ArgumentTypeError("positive integer expected: %r" % text)
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ameesh fil", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")
    p_list = sub.add_parser("list", help="known threads")
    p_list.set_defaults(func=cmd_list)
    for name, func, last, aide in (
            ("show", cmd_show, None, "read a thread"),
            ("tail", cmd_tail, 10, "follow a thread (Ctrl-C to quit)")):
        p_cmd = sub.add_parser(name, help=aide)
        p_cmd.add_argument("project", metavar="project")
        p_cmd.add_argument("lot", nargs="?", default=None)
        p_cmd.add_argument("--last", type=_positive, default=last,
                           help="only the last N entries")
        p_cmd.add_argument("--meta", action="store_true",
                           help="also show each entry's metadata")
        if name == "tail":
            p_cmd.add_argument("--interval", type=float, default=0.5,
                               help="polling period in seconds (default 0.5)")
        p_cmd.set_defaults(func=func)
    p_expire = sub.add_parser("expire", help="remove entries past retention (L361)")
    p_expire.add_argument("--days", type=_positive, default=None,
                          help="retention in days (default: thread_retention_days, 365;"
                               " 0 = nothing expires)")
    p_expire.add_argument("--dry-run", action="store_true",
                          help="say what would expire, change nothing")
    p_expire.set_defaults(func=cmd_expire)
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
        print("error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
