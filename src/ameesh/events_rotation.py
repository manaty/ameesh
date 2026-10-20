# SPDX-License-Identifier: AGPL-3.0-only
"""Rotation du flux `events.jsonl` d'un agent, au début d'un tour (#2820).

L'exécuteur ajoute le flux du harnais à `<state>/<agent>/events.jsonl` à
chaque tour. Sans plafond, le fichier grossissait sans fin (≈ 1 Go/h sur un
hôte de huit agents, le 20/10 sur agents-2), et chaque tour en recomptait
toutes les lignes (`events_start`, `CostBook.events`).

Quand le flux dépasse `events_quota` (fiche Host, `policy.housekeeping`), il
devient `events.jsonl.1`, qui remplace la génération précédente. Un flux neuf
prend sa place et **reprend** la fin de l'ancien : ses `carry` derniers
octets en lignes entières (la fenêtre que lisent les jauges,
`cost.TAIL_BYTES`), précédés du dernier marqueur de compte (`ameesh.account`)
et du dernier marqueur de tour (`ameesh.turn`) d'avant la coupe. Pour chaque
ligne reprise, le marqueur de compte et le marqueur de tour qui la précèdent
sont donc ceux de l'ancien flux. `account_at`, `marker_account`,
`turn_marker`, `session_of`, `session_reading` et les jauges (compte, date de
production) rendent au tour suivant ce qu'ils rendaient sans rotation.

Les index de ligne ne survivent pas à une rotation. Elle n'a donc lieu qu'au
point sûr du tour (avant le marqueur de compte), quand aucun marqueur
comptable n'est en attente en base ; c'est à l'appelant de le vérifier
(`AgentWorker._rotation_du_flux`). Le chemin ne disparaît jamais : l'ancien
flux est lié en `.1` avant que le neuf ne le remplace (`os.replace`).
"""
from __future__ import annotations

import json
import os
import stat as stat_mod
from dataclasses import dataclass

from . import cost as cost_mod

#: un marqueur est une ligne courte : au-delà, une ligne qui contient son nom
#: est un contenu (un résultat d'outil qui cite le marqueur), jamais analysée
MARKER_MAX_BYTES = 4096
#: fin reprise : la fenêtre que lisent les jauges (`cost.TAIL_BYTES`), moins
#: la place des deux marqueurs repris devant elle ; jamais plus du quart du quota
CARRY_BYTES = cost_mod.TAIL_BYTES - 2 * MARKER_MAX_BYTES
#: bloc de la lecture à rebours
BLOCK = 1 << 20

PREVIOUS = ".1"
TEMPORARY = ".rotation"


@dataclass(frozen=True)
class Rotation:
    """Ce qu'une rotation a fait : taille du flux tourné, octets repris."""
    size: int
    carried: int


def _marker(line: bytes, kind: str) -> bool:
    """Vrai si `line` est un marqueur `kind` de l'exécuteur (type de premier niveau)."""
    if len(line) > MARKER_MAX_BYTES or kind.encode() not in line:
        return False
    try:
        event = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return False
    return isinstance(event, dict) and event.get("type") == kind


def _lines_reverse(flux, size: int):
    """Les lignes de `flux[0:size]`, de la dernière à la première ; mémoire
    bornée par le bloc et la plus longue ligne (comme `cost._lines_reverse`,
    mais sur le descripteur déjà ouvert et jusqu'à `size`)."""
    pos, morceaux = size, []      # la ligne en cours, de sa fin vers son début
    while pos > 0:
        debut = max(0, pos - BLOCK)
        flux.seek(debut)
        donnees = flux.read(pos - debut)
        if len(donnees) != pos - debut:
            raise OSError("events stream shrank during rotation")
        parties = donnees.split(b"\n")
        morceaux.append(parties[-1])
        if len(parties) > 1:
            yield b"".join(reversed(morceaux))
            yield from reversed(parties[1:-1])
            morceaux = [parties[0]]
        pos = debut
    if morceaux:
        yield b"".join(reversed(morceaux))


def _carried(flux, size: int, carry: int) -> bytes:
    """Ce que le flux neuf reprend de `flux[0:size]` (voir le module)."""
    coupe = size - carry
    if coupe > 0:
        flux.seek(coupe - 1)
        if flux.read(1) != b"\n":
            reste = flux.read(size - coupe)
            fin = reste.find(b"\n")
            coupe = size if fin < 0 else coupe + fin + 1
    tour = compte = None
    coupe = max(0, coupe)
    for ligne in _lines_reverse(flux, coupe):
        if tour is None and _marker(ligne, cost_mod.TURN_MARKER):
            tour = ligne
        elif compte is None and _marker(ligne, cost_mod.ACCOUNT_MARKER):
            compte = ligne
        if tour is not None and compte is not None:
            break                 # rien de plus ancien n'est repris
    tete = b"".join(marqueur + b"\n" for marqueur in (compte, tour) if marqueur is not None)
    flux.seek(coupe)
    corps = flux.read(size - coupe)
    if corps and not corps.endswith(b"\n"):
        corps += b"\n"            # une ligne partielle n'absorbe pas le marqueur suivant
    return tete + corps


def rotate(path: str, quota: int, carry: int = CARRY_BYTES) -> Rotation | None:
    """Tourne le flux `path` s'il dépasse `quota` octets ; None sinon.

    Le flux doit être au repos (aucun écrivain) : l'appelant tient le bail et
    le verrou de session de l'agent. Une écriture inattendue pendant la
    rotation l'annule (le flux est laissé tel quel). Lève OSError si le disque
    refuse ; le flux d'origine reste alors en place."""
    if quota <= 0:
        return None
    try:
        avant = os.stat(path)
    except FileNotFoundError:
        return None
    if avant.st_size <= quota:
        return None
    carry = max(0, min(carry, quota // 4))
    with open(path, "rb") as flux:
        taille = os.fstat(flux.fileno()).st_size
        donnees = _carried(flux, taille, carry)
    temporaire = path + TEMPORARY
    try:
        fd = os.open(temporaire, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                     stat_mod.S_IMODE(avant.st_mode))
        try:
            vue = memoryview(donnees)
            while vue:
                vue = vue[os.write(fd, vue):]
            os.fsync(fd)
        finally:
            os.close(fd)
        maintenant = os.stat(path)
        if (maintenant.st_ino, maintenant.st_size) != (avant.st_ino, taille):
            os.unlink(temporaire)
            return None           # un écrivain est passé : au prochain tour
        precedent = path + PREVIOUS
        try:
            os.unlink(precedent)
        except FileNotFoundError:
            pass
        lie = True
        try:
            os.link(path, precedent)
        except OSError:
            lie = False
            os.replace(path, precedent)   # sans lien physique : bref instant sans flux
        try:
            os.replace(temporaire, path)
        except OSError:
            if not lie:
                os.replace(precedent, path)   # le flux d'origine reprend sa place
            raise
    except OSError:
        try:
            os.unlink(temporaire)
        except OSError:
            pass
        raise
    try:
        dossier = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
        try:
            os.fsync(dossier)
        finally:
            os.close(dossier)
    except OSError:
        pass                      # les renommages sont faits ; leur durabilité suit
    return Rotation(taille, len(donnees))
