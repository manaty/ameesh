# SPDX-License-Identifier: AGPL-3.0-only
"""#8395 — fragments de révision du contrat module v1, pliés au paquet.

La révision du contrat public se décidait dans chaque lot qui le touchait :
deux lots qui ajoutaient une révision se suivaient donc en série (conflit
d'en-tête, rebases et renumérotations ; #8340 a été regelé cinq fois le
27/11/2026). Ce module porte la convention qui l'en empêche :

* un lot qui change ``docs/api/module-contract-v1.md`` écrit un fragment
  ``docs/api/module-contract-v1.d/<lot>.md``, sans numéro. Son premier
  paragraphe commence par ``adds:`` ; les sections qu'il ajoute suivent après
  une ligne vide. Il ne touche ni l'en-tête, ni la liste des révisions, ni la
  surface, ni ``CONTRACT_VERSION`` ;
* à la composition du paquet, le composeur lit les fragments des lots
  composés dans l'ordre du manifeste, attribue les révisions suivantes,
  plie les lignes et les sections dans le contrat, aligne l'en-tête, la
  surface (``"contract"``) et ``CONTRACT_VERSION``, puis retire les fragments.
  Le commit correspondant est cité au manifeste du paquet ;
* un lot en vol qui a écrit son numéro à l'ancienne manière reste accepté :
  le composeur retrouve le paragraphe ajouté par l'unité et le renumérote
  mécaniquement, sans regel (canon §2).

Le pliage est une fonction pure : :func:`plan_fold` reçoit le texte de la
base, l'arbre courant, les fragments et les paragraphes anciens, et rend le
nouveau contrat, la nouvelle surface et la nouvelle version. Le composeur
écrit le résultat et le commite ; les tests de contrat, eux, acceptent un
arbre de lot qui porte encore ses fragments.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date as _date
from pathlib import Path

#: répertoire des fragments, un fichier ``<lot>.md`` par lot
DIR_REL = "docs/api/module-contract-v1.d"
#: le contrat public et ses deux satellites que le pliage aligne
CONTRACT_REL = "docs/api/module-contract-v1.md"
SURFACE_REL = "docs/api/module-contract-v1.surface.json"
VERSION_REL = "src/ameesh/public/__init__.py"
#: ancre où les sections des fragments sont insérées (avant « Consumer
#: qualification and delivery ») ; elle reste dans l'arbre plié
ANCHOR = "<!-- contract-revision-sections:"

_FRAGMENT_NAME = re.compile(r"^([1-9][0-9]*)\.md$")
_HEADER = re.compile(
    r"(?m)^Contract revision: \*\*(?P<version>\d+\.\d+), [^*\n]+\*\*")
#: la même ligne, avec une parenthèse de résumé éventuellement écrite par un
#: lot à l'ancienne manière : le pliage la remplace par son propre en-tête
_HEADER_LINE = re.compile(
    r"(?m)^Contract revision: \*\*\d+\.\d+, [^*\n]+\*\*(?: ?\([^\n]*\))?")
_REVISION_ITEM = re.compile(r"(?m)^Revision \d+\.\d+ adds\b")
_SURFACE = re.compile(r'("contract"\s*:\s*")(\d+\.\d+)(")')
_VERSION = re.compile(r'(?m)^CONTRACT_VERSION = "(\d+\.\d+)"$')
_ADDITIVE = re.compile(r"\badditive to (?:revision )?\d+\.\d+\b")
_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")


class FragmentError(ValueError):
    """Un fragment, ou l'arbre qui le porte, ne permet pas un pliage sûr.

    Le message nomme toujours le lot quand il est connu : le composeur refuse
    la composition plutôt que de plier un fragment douteux.
    """

    def __init__(self, message: str, lot: int | None = None, path: str = "") -> None:
        self.lot = None if lot is None else int(lot)
        self.path = str(path or "")
        where = ""
        if self.lot is not None:
            where = "lot #%d" % self.lot
        elif self.path:
            where = self.path
        super().__init__("%s : %s" % (where, message) if where else message)


@dataclass(frozen=True)
class Fragment:
    """Un fragment de révision : sa ligne ``adds:`` et ses sections."""

    lot: int
    adds: str
    sections: str = ""
    path: str = ""


@dataclass(frozen=True)
class Revision:
    """Une révision attribuée par le composeur."""

    number: str
    lot: int
    #: ``"fragment"`` (nouvelle manière) ou ``"legacy"`` (numéro écrit)
    kind: str


@dataclass(frozen=True)
class Fold:
    """Le résultat pur du pliage : trois textes et la trace des révisions."""

    contract: str
    surface: str
    version: str
    revisions: tuple[Revision, ...] = ()
    removed: tuple[str, ...] = ()
    header_before: str = ""
    header_after: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.revisions)


def header_revision(text: str) -> str:
    """La révision ``1.N`` de l'en-tête du contrat."""
    match = _HEADER.search(str(text))
    if not match:
        raise FragmentError(
            "en-tête « Contract revision: **1.N, date** » introuvable")
    return match.group("version")


def english_date(value: str) -> str:
    """``2026-11-28`` → ``28 November 2026`` (forme de l'en-tête)."""
    try:
        parsed = _date.fromisoformat(str(value))
    except ValueError as error:
        raise FragmentError("date de composition illisible : %r" % (value,)) from error
    return "%d %s %d" % (parsed.day, _MONTHS[parsed.month - 1], parsed.year)


def _strip_leading_comments(text: str, lot: int, path: str) -> str:
    """Retire les commentaires HTML d'en-tête, sans toucher au contenu."""
    rest = text.lstrip("\n")
    while rest.startswith("<!--"):
        end = rest.find("-->")
        if end < 0:
            raise FragmentError("commentaire HTML non fermé", lot=lot, path=path)
        rest = rest[end + 3:].lstrip("\n")
    return rest


def parse_fragment(lot: int, text: str, path: str = "") -> Fragment:
    """Valide le fragment du lot et rend sa ligne ``adds:`` et ses sections.

    Format : commentaires HTML éventuels, puis un premier paragraphe dont la
    première ligne commence par ``adds:`` ; il se termine à la première ligne
    vide. Le reste, s'il existe, est inséré avant l'ancre du contrat.
    """
    lot = int(lot)
    if lot <= 0:
        raise FragmentError("identifiant de lot invalide", path=path)
    if "\x00" in str(text):
        raise FragmentError("fragment binaire (octet nul)", lot=lot, path=path)
    body = _strip_leading_comments(
        str(text).replace("\r\n", "\n").replace("\r", "\n"), lot, path)
    lines = body.split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        raise FragmentError("fragment vide", lot=lot, path=path)
    first = lines[0]
    if not first.startswith("adds:"):
        raise FragmentError(
            "le premier paragraphe doit commencer par « adds: »", lot=lot,
            path=path)
    adds = first[len("adds:"):].strip()
    if not adds:
        raise FragmentError("ligne « adds: » vide", lot=lot, path=path)
    index = 1
    while index < len(lines) and lines[index].strip():
        adds += "\n" + lines[index].rstrip()
        index += 1
    if _REVISION_ITEM.match(adds):
        raise FragmentError(
            "le fragment porte un numéro de révision ; il ne doit pas "
            "l'écrire (le composeur l'attribue)", lot=lot, path=path)
    sections = "\n".join(lines[index:]).strip("\n")
    return Fragment(lot=lot, adds=adds, sections=sections, path=str(path or ""))


def fragment_files(root: Path | str) -> dict[int, Path]:
    """Les fragments ``<lot>.md`` du répertoire, par lot.

    Un fichier ``.md`` dont le nom n'est pas ``<lot>.md`` est une faute : il
    serait silencieusement oublié au pliage. Les autres fichiers (le README
    sans extension) sont ignorés.
    """
    directory = Path(root) / DIR_REL
    if not directory.is_dir():
        return {}
    found: dict[int, Path] = {}
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix != ".md":
            continue
        match = _FRAGMENT_NAME.match(path.name)
        if not match:
            raise FragmentError(
                "nom de fragment inattendu : attendu <lot>.md",
                path=str(path))
        lot = int(match.group(1))
        if lot in found:
            raise FragmentError("deux fragments pour le même lot", lot=lot,
                                path=str(path))
        found[lot] = path
    return found


def read_fragments(root: Path | str) -> dict[int, Fragment]:
    """Lit et valide tous les fragments de l'arbre."""
    fragments: dict[int, Fragment] = {}
    for lot, path in fragment_files(root).items():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise FragmentError("fragment illisible : %s" % error, lot=lot,
                                path=str(path)) from error
        fragments[lot] = parse_fragment(lot, text, str(path))
    return fragments


def added_revision_paragraphs(diff: str) -> tuple[str, ...]:
    """Les paragraphes « Revision 1.N adds … » ajoutés par un diff git.

    Sert à attribuer une révision écrite à l'ancienne manière au lot dont
    l'unité l'a introduite ; le diff est celui de l'unité de fusion contre sa
    première parente, sans contexte (``-U0``).
    """
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in str(diff).splitlines():
        if line.startswith("+++ ") or line.startswith("--- "):
            continue
        if line.startswith("+"):
            current.append(line[1:])
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    paragraphs: list[str] = []
    for block in blocks:
        paragraph: list[str] = []
        for line in block:
            if not line.strip():
                if paragraph:
                    paragraphs.append("\n".join(paragraph))
                    paragraph = []
            else:
                paragraph.append(line)
        if paragraph:
            paragraphs.append("\n".join(paragraph))
    return tuple(paragraph for paragraph in paragraphs
                 if _REVISION_ITEM.match(paragraph))


def _norm(text: str) -> str:
    return "\n".join(line.rstrip()
                     for line in str(text).strip("\n").splitlines()).strip()


def _render_adds(adds: str, previous: str) -> str:
    """Substitue ``{previous}``, puis la référence finale « additive to 1.N ».

    Seule la **dernière** mention est réécrite (N2) : un fragment peut citer
    une autre révision plus tôt dans sa phrase sans que le pliage la change.
    """
    text = str(adds).replace("{previous}", previous)
    return _rewrite_last_additive(text, previous)


def _rewrite_last_additive(text: str, previous: str) -> str:
    """Réécrit la dernière mention « additive to 1.N » seulement."""
    matches = list(_ADDITIVE.finditer(text))
    if not matches:
        return text
    match = matches[-1]
    return text[:match.start()] + "additive to " + previous + text[match.end():]


def _renumber(paragraph: str, number: str, previous: str) -> str:
    """Renumérote un paragraphe de l'ancienne manière, sans le relire."""
    text = _REVISION_ITEM.sub("Revision %s adds" % number, paragraph, count=1)
    return _rewrite_last_additive(text, previous)


def plan_fold(base_contract: str, contract: str, surface: str, version: str,
              fragments: dict[int, Fragment],
              legacy: dict[int, tuple[str, ...]], lots, date: str) -> Fold:
    """Attribue les révisions et rend les trois textes pliés, sans écrire.

    ``base_contract`` est le contrat de la base exacte du paquet : c'est lui
    qui donne la révision de départ, même si un lot à l'ancienne manière a
    déjà changé l'en-tête de l'arbre courant. ``legacy`` porte, par lot, les
    paragraphes que son unité a ajoutés (voir
    :func:`added_revision_paragraphs`).
    """
    ordered = [int(lot) for lot in lots]
    if len(set(ordered)) != len(ordered):
        raise FragmentError("un lot est composé deux fois dans l'ordre du manifeste")
    if any(lot <= 0 for lot in ordered):
        raise FragmentError("identifiant de lot invalide dans l'ordre du manifeste")
    for lot in fragments:
        if int(lot) not in ordered:
            raise FragmentError(
                "fragment présent mais lot absent de la composition (fusionné "
                "hors paquet ?) : geste = replier le fragment dans la cible "
                "avec ce lot dans l'ordre, ou retirer le fragment",
                lot=lot, path=fragments[lot].path)
    for lot, paragraphs in legacy.items():
        if int(lot) not in ordered:
            raise FragmentError(
                "révision écrite présente mais lot absent de la composition",
                lot=lot)
        if int(lot) in fragments:
            raise FragmentError(
                "porte à la fois un fragment et une révision écrite à "
                "l'ancienne manière : ambigu", lot=lot)
        if not paragraphs:
            raise FragmentError("révision écrite sans paragraphe", lot=lot)

    base_version = header_revision(base_contract)
    current = header_revision(contract)
    starts = list(_REVISION_ITEM.finditer(contract))
    if not starts:
        raise FragmentError("liste des révisions introuvable dans le contrat")
    revision_start = starts[0].start()
    # La liste des révisions est un bloc de paragraphes consécutifs : elle
    # s'arrête à la première ligne vide. Aucun nom de client n'est écrit ici
    # (#4259) : ce qui suit le bloc (mapping IMA, sections) est du texte libre.
    blank = contract.find("\n\n", revision_start)
    revision_end = len(contract) if blank < 0 else blank
    tail = contract[revision_end:]
    if not tail and contract.endswith("\n"):
        tail = "\n"
    items: list[str] = []
    for index, match in enumerate(starts):
        if match.start() >= revision_end:
            break
        following = (starts[index + 1].start()
                     if index + 1 < len(starts)
                     and starts[index + 1].start() < revision_end
                     else revision_end)
        items.append(contract[match.start():following].rstrip("\n"))
    if not items:
        raise FragmentError("liste des révisions vide dans le contrat")

    remaining = list(items)
    attributed: dict[int, list[str]] = {}
    for lot, paragraphs in legacy.items():
        for paragraph in paragraphs:
            wanted = _norm(paragraph)
            for index, item in enumerate(remaining):
                if _norm(item) == wanted:
                    attributed.setdefault(int(lot), []).append(remaining.pop(index))
                    break
            else:
                raise FragmentError(
                    "paragraphe de révision ajouté par l'unité introuvable "
                    "dans le contrat (modifié depuis ?)", lot=lot)
    base_minor = int(base_version.split(".")[1])
    for item in remaining:
        match = _REVISION_ITEM.match(item)
        if match and int(item.split()[1].split(".")[1]) > base_minor:
            number = item.split()[1]
            raise FragmentError(
                "révision %s présente dans le contrat mais attribuée à aucun "
                "lot de la composition" % number)

    major = int(base_version.split(".")[0])
    minor = base_minor
    previous = base_version
    revisions: list[Revision] = []
    folded: list[str] = []
    for lot in ordered:
        if lot in fragments:
            minor += 1
            number = "%d.%d" % (major, minor)
            folded.append("Revision %s adds %s"
                          % (number, _render_adds(fragments[lot].adds, previous)))
            revisions.append(Revision(number, lot, "fragment"))
            previous = number
        elif lot in attributed:
            for paragraph in attributed[lot]:
                minor += 1
                number = "%d.%d" % (major, minor)
                folded.append(_renumber(paragraph, number, previous))
                revisions.append(Revision(number, lot, "legacy"))
                previous = number

    header_match = _HEADER_LINE.search(contract)
    if not header_match:
        raise FragmentError(
            "en-tête « Contract revision: **1.N, date** » introuvable")
    if not revisions:
        return Fold(contract=contract, surface=surface, version=version,
                    header_before=current, header_after=current)
    header = "Contract revision: **%s, %s**" % (previous, english_date(date))
    rebuilt = (contract[:header_match.start()] + header
               + contract[header_match.end():revision_start]
               + "\n".join(remaining + folded)
               + tail)

    section_lots = [lot for lot in ordered
                    if lot in fragments and fragments[lot].sections.strip()]
    if section_lots:
        index = rebuilt.find(ANCHOR)
        if index < 0:
            raise FragmentError(
                "ancre « %s » absente du contrat : les sections du fragment "
                "ne peuvent pas être pliées" % ANCHOR, lot=section_lots[0],
                path=fragments[section_lots[0]].path)
        before = rebuilt[:index].rstrip("\n")
        sections = [fragments[lot].sections.strip("\n") for lot in section_lots]
        rebuilt = (before + "\n\n" + "\n\n".join(sections) + "\n\n"
                   + rebuilt[index:])

    surface_match = _SURFACE.search(surface)
    if not surface_match:
        raise FragmentError(
            "champ « \"contract\" » introuvable dans %s" % SURFACE_REL)
    new_surface = (surface[:surface_match.start()] + surface_match.group(1)
                   + previous + surface_match.group(3)
                   + surface[surface_match.end():])
    version_match = _VERSION.search(version)
    if not version_match:
        raise FragmentError("ligne CONTRACT_VERSION introuvable dans %s"
                            % VERSION_REL)
    new_version = (version[:version_match.start()]
                   + 'CONTRACT_VERSION = "%s"' % previous
                   + version[version_match.end():])
    removed = tuple("%s/%d.md" % (DIR_REL, lot)
                    for lot in ordered if lot in fragments)
    return Fold(contract=rebuilt, surface=new_surface, version=new_version,
                revisions=tuple(revisions), removed=removed,
                header_before=current, header_after=previous)


def apply_fold(root: Path | str, fold: Fold) -> list[str]:
    """Écrit le pliage dans l'arbre et retire les fragments.

    Rend les chemins relatifs modifiés (contrat, surface, version et fragments
    supprimés), dans un ordre stable : le composeur les passe à ``git add``.
    """
    if not fold.changed:
        return []
    base = Path(root)
    changed: list[str] = []
    for rel, text in ((CONTRACT_REL, fold.contract),
                      (SURFACE_REL, fold.surface),
                      (VERSION_REL, fold.version)):
        path = base / rel
        try:
            current = path.read_text(encoding="utf-8")
        except OSError as error:
            raise FragmentError("fichier du pliage illisible : %s" % error,
                                path=rel) from error
        if current != text:
            path.write_text(text, encoding="utf-8")
            changed.append(rel)
    for rel in fold.removed:
        path = base / rel
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise FragmentError("fragment non retiré : %s" % error,
                                path=rel) from error
        changed.append(rel)
    return sorted(changed)
