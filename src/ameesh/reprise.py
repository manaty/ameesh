# SPDX-License-Identifier: AGPL-3.0-only
"""Adopting and resuming agents (work item L39, decision 0030 point 3).

  ameesh adopt <agent> --session ID --harness claude|codex|deepseek
               [--account ACCOUNT] [--cwd D] [--brief FILE|-] [--chantier C]
               [--force] [--json]
        moves an EXISTING (closed) interactive session under the runner:
        `execute` mode, session and original account in the registry,
        resume instruction queued, audit trail in the thread.
  ameesh resume <agent> [--brief FILE|-] [--fresh] [--json]
        restarts a stopped, dead or idle agent: on its session when the
        next account can resume it, otherwise (or with --fresh) on a new
        session opened by a DETERMINISTIC resume brief.

Adopting and resuming are ameesh operations, never prompts: a session is no
longer asked to "resume session X" by digging through
`~/.codex*/sessions`. Database writes go through
`storage.of(db).operations` (`adopt`, `resume`); no SQL here.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import stat
import subprocess
import sys
import time
from typing import Sequence

from . import accounts, account_turn, auth_failure, fil, identity, mail, platform, registry, stagnation, storage
from . import canon as canon_mod
from . import config as config_mod
from . import cost as cost_mod
from . import db as db_mod
from . import work as work_mod
from .config import CHANNEL_MAIL, NAME_RE, Config

#: harnais dont ameesh sait retrouver et reprendre une session
HARNESSES = ("claude", "codex", "deepseek")

#: identifiant de session accepté (jamais un chemin : pas de `/`, pas de `..`)
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")

#: consigne posée par `adopt` sans --brief
DEFAULT_ADOPT = ("Resuming under the runner: read your mail, pick up the state of your "
                 "work, end each turn with a clear status.")
#: consigne posée par `resume` sans --brief
DEFAULT_RESUME = ("Resuming (ameesh resume): read your mail, pick up the state of your "
                  "work, end each turn with a clear status.")

#: bornes du brief de reprise déterministe (octets UTF-8) ; #3867 : le plafond
#: est partagé avec `canon check`, qui mesure le bloc de règles avec lui
BRIEF_MAX_BYTES = canon_mod.BRIEF_MAX_BYTES
FIL_ENTRIES = 30
FIL_MAX_BYTES = 8000
FIL_ENTRY_CHARS = 400
LOTS_MAX = 40

#: #538 : part qu'un texte accompagné de règles garde au plus du plafond. Le
#: bloc de règles est réservé D'ABORD, le texte (résumé, brief préparé) est
#: tronqué ensuite — jamais l'inverse, sinon un résumé plein fait disparaître
#: les règles (verdict codex #510 r3, P2) ; mais les règles ne l'effacent pas
#: pour autant : le texte plus court que cette part reste entier.
#: #3126, #4159 : réserve fixe de 6 000 octets ; relever le plafond du brief
#: donne toute la place supplémentaire aux règles, sans agrandir cette réserve.
BRIEF_TEXT_MIN_BYTES = canon_mod.BRIEF_TEXT_MIN_BYTES

#: #538 : les règles de travail des fiches Process du canon ouvrent le brief
#: (nouvelle session, rotation, reprise), avant le rôle et la consigne.
PROCESS_RULES_HEADER = canon_mod.PROCESS_RULES_HEADER

#: #538 : pied du bloc de règles. Il marque la FIN du bloc : un brief préparé à
#: l'avance (rotation, reprise) se laisse ainsi RELIRE au lancement, et une
#: fiche retirée entre-temps ne reste pas dans le brief (verdict codex #510 r2, P2).
PROCESS_RULES_FOOTER = canon_mod.PROCESS_RULES_FOOTER

#: #538 : constats de lecture qui privent le canon de ses fiches. `readable`
#: reste vrai quand la lecture des fiches échoue (`canon-git-error`) : le brief
#: dit alors les règles indisponibles, jamais un silence trompeur.
PROCESS_RULES_READ_ERRORS = ("canon-git-error", "canon-io-error",
                             "canon-bundle-missing")

#: #538 : canon configuré mais illisible (ou fiches illisibles) : le brief le
#: dit, jamais silencieusement.
PROCESS_RULES_UNAVAILABLE = ("(working rules unavailable: the agent's canon is "
                             "not readable)")


class RepriseError(ValueError):
    """Refus d'adoption ou de reprise : le message dit pourquoi et quoi faire."""


# --------------------------------------------------------------------------
# sessions des harnais sur disque
# --------------------------------------------------------------------------

def _store(harness: str) -> str:
    return accounts.session_store_of(harness) or {
        "claude": "projects", "codex": "sessions", "deepseek": "sessions"}.get(harness, "")


def session_root(harness: str, home: str) -> str:
    """Le dossier des sessions du harnais sous un dossier de configuration."""
    return os.path.join(home, _store(harness))


class SessionSearchIncomplete(OSError):
    """L224 : le stockage de sessions n'a pas pu être parcouru en entier
    (dossier illisible, erreur d'E/S) : « introuvable » n'est pas établi."""


def _present(path: str, directory: bool, strict: bool) -> bool:
    """`path` existe (dossier ou fichier) ; une erreur autre que « absent »
    lève `SessionSearchIncomplete` en mode strict, compte pour absent sinon."""
    try:
        st = os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError as exc:
        if strict:
            raise SessionSearchIncomplete("%s : %s" % (path, exc)) from exc
        return False
    return stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode)


def find_session(harness: str, home: str, session_id: str,
                 strict: bool = False) -> str | None:
    """Le fichier (ou dossier, DeepSeek) de la session sous `home`, ou None.

    * Codex : `<CODEX_HOME>/sessions/AAAA/MM/JJ/rollout-<date>-<id>.jsonl` ;
    * Claude Code : `<CLAUDE_CONFIG_DIR>/projects/<dossier encodé>/<id>.jsonl` ;
    * DeepSeek Harness : `<DSH_HOME>/sessions/<dossier encodé>/session-<id>/`
      (l'identifiant dsh porte déjà le préfixe `session-`).

    `strict` (L224, verdict codex #183) : une recherche incomplète (dossier
    illisible) lève `SessionSearchIncomplete` au lieu de répondre None.
    """
    if not SESSION_RE.match(session_id or ""):
        return None
    root = session_root(harness, home)
    if not _present(root, True, strict):
        return None
    if harness == "codex":
        suffix = "-%s.jsonl" % session_id
        found = []
        # Parcours explicite plutôt que os.walk : os.walk avale aussi les
        # erreurs de `DirEntry.is_dir()` (verdict codex #183, 3e relecture).
        pending = [root]
        while pending:
            base = pending.pop()
            try:
                with os.scandir(base) as it:
                    entries = list(it)
            except OSError as exc:
                if strict:
                    raise SessionSearchIncomplete("%s : %s" % (base, exc)) from exc
                continue
            for entry in entries:
                try:
                    is_dir = entry.is_dir()
                    walk_into = is_dir and not entry.is_symlink()
                except OSError as exc:
                    if strict:
                        raise SessionSearchIncomplete(
                            "%s : %s" % (entry.path, exc)) from exc
                    is_dir = walk_into = False
                if walk_into:
                    pending.append(entry.path)
                elif not is_dir and entry.name.startswith("rollout-") \
                        and entry.name.endswith(suffix):
                    found.append(entry.path)
        return sorted(found)[-1] if found else None
    try:
        entries = sorted(os.listdir(root))
    except OSError as exc:
        if strict:
            raise SessionSearchIncomplete("%s : %s" % (root, exc)) from exc
        return None
    for entry in entries:
        if harness == "claude":
            candidate = os.path.join(root, entry, session_id + ".jsonl")
            if _present(candidate, False, strict):
                return candidate
        elif harness == "deepseek":
            # dsh ids already carry the prefix (`session-b126155c-…`): adding it
            # again looked for `session-session-…`, so no DeepSeek session was
            # ever found and every turn opened a new one on the resume brief
            name = session_id if session_id.startswith("session-") else "session-" + session_id
            candidate = os.path.join(root, entry, name)
            if _present(candidate, True, strict):
                return candidate
    return None


def search_homes(cfg: Config, harness: str, account: str | None = None) -> list[tuple]:
    """`[(compte ou None, dossier du harnais)]` où chercher une session.

    `account` donné : son dossier seulement. Sinon : les comptes déclarés de
    l'hôte, dans leur ordre ; sans compte déclaré, le dossier par défaut du
    harnais (variable héritée, sinon `~/.codex`, `~/.claude`, `~/.dsh`).
    """
    items = accounts.profiles(cfg, harness)
    if account:
        profile = accounts.by_name(items, account)
        if profile is None:
            raise RepriseError(
                "unknown account %s for %s (declared on this host: %s)"
                % (account, harness, ", ".join(p.name for p in items) or "none"))
        return [(profile.name, profile.home())]
    if items:
        return [(p.name, p.home()) for p in items]
    return [(None, accounts.Profile(harness=harness, name="(défaut)").home())]


def locate(cfg: Config, harness: str, session_id: str,
           account: str | None = None) -> tuple[str | None, str | None, list[str]]:
    """`(compte, chemin, dossiers fouillés)` de la session ; chemin None si
    introuvable. Le compte est celui SOUS LEQUEL le fichier a été trouvé."""
    searched = []
    for name, home in search_homes(cfg, harness, account):
        root = session_root(harness, home)
        searched.append(root)
        path = find_session(harness, home, session_id)
        if path:
            return name, path, searched
    return None, None, searched


def holders(path: str | None, session_id: str) -> list[dict]:
    """Les processus qui tiennent la session : fichier ouvert (ou dossier
    ouvert ou courant), ou id de session sur la ligne de commande d'une
    REPRISE (`codex resume <id>`, `claude --resume <id>`, `dsh --session-id
    <id>`) — hors ce processus et ses ascendants, dont la ligne de commande
    porte l'id passé à `ameesh adopt`. L'id simplement CITÉ (brief, message,
    chemin) ne compte pas (#1283). Par la couche plateforme (L63) ; les
    processus illisibles (autre utilisateur, disparus) sont ignorés.

    Lève `platform.NotAvailable` si l'OS ne permet pas de le vérifier :
    jamais de liste vide qui voudrait dire « personne » sans contrôle.

    Limite connue : Claude Code n'ouvre son journal que le temps d'une
    écriture ; une session interactive démarrée sans `--resume` n'est donc vue
    que si elle écrit au moment du contrôle.

    #5029 : l'exécuteur est non-dumpable et ne tient jamais le journal d'une
    session ; son refus de lire ses propres fichiers ne doit pas faire refuser
    un `adopt`/`resume` (le bail vivant, lui, est contrôlé avant).
    """
    return platform.holders(path, session_id, exclude=platform.ancestry(),
                            session=True, allow=platform.EXECUTOR_NAMES)


def recently_modified(path: str | None) -> bool:
    """A recently written external journal may still have a live writer.

    Some harnesses close their journal between writes. Wait a quiet minute
    before adoption or unleased resumption, even when no open fd was found.
    """
    if not path:
        return False
    try:
        return time.time() - os.stat(path).st_mtime < 60
    except FileNotFoundError:
        return False


def recently_modified(path: str | None) -> bool:
    """A recently written external journal may still have a live writer.

    Some harnesses close their journal between writes. Wait a quiet minute
    before adoption or unleased resumption, even when no open fd was found.
    """
    if not path:
        return False
    try:
        return time.time() - os.stat(path).st_mtime < 60
    except FileNotFoundError:
        return False


def session_cwd(harness: str, path: str) -> str | None:
    """Le dossier de travail noté dans le journal de session (Codex :
    `session_meta.payload.cwd` ; Claude : champ `cwd`), ou None."""
    if harness not in ("codex", "claude") or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for index, line in enumerate(fh):
                if index >= 200:
                    break
                if '"cwd"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                if harness == "codex":
                    payload = event.get("payload")
                    if isinstance(payload, dict) and isinstance(payload.get("cwd"), str):
                        return payload["cwd"]
                elif isinstance(event.get("cwd"), str):
                    return event["cwd"]
    except OSError:
        return None
    return None


def claude_project_key(cwd: str) -> str:
    """Nom du dossier `projects/` où Claude Code range les sessions d'un dossier."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


# --------------------------------------------------------------------------
# brief de reprise déterministe
# --------------------------------------------------------------------------

def _octets(text: str) -> int:
    return len(text.encode("utf-8"))


#: #538 : marque de troncature ; sa place est comprise dans la borne de `_clip`.
_CLIP_MARK = " […]"


def _clip(text: str, limit: int) -> str:
    """`text` borné à `limit` octets UTF-8, marque de troncature comprise."""
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    marge = len(_CLIP_MARK.encode("utf-8"))
    if limit <= marge:
        return data[: max(0, limit)].decode("utf-8", "ignore")
    return data[: limit - marge].decode("utf-8", "ignore") + _CLIP_MARK


def _rules_unreadable(charge) -> bool:
    """#538 : ce canon ne peut pas fournir ses règles.

    `readable` ne suffit pas : une erreur de lecture des fiches
    (`canon-git-error`) le laisse vrai alors qu'aucune fiche n'est chargée —
    l'absence de règles est alors DITE, jamais silencieuse (verdict codex
    #510 r2, P3)."""
    from . import canon as canon_mod
    if not getattr(charge, "readable", False):
        return True
    return any(f.severity == canon_mod.ERROR and f.code in PROCESS_RULES_READ_ERRORS
               for f in (getattr(charge, "load_findings", ()) or ()))


def _process_fragment(process) -> tuple[str, str]:
    """#1372 : le texte d'une fiche Process pour le brief, et son diagnostic.

    La projection courte de la carte, quand elle est lue, REMPLACE le texte
    entier : la fin du canon (§12, §14…) n'est plus mangée par le début. Sinon
    le texte entier est gardé (borné plus loin par le budget du bloc) et un
    diagnostic DIT ce qui manque — jamais de coupe silencieuse."""
    from . import canon as canon_mod
    entete = ["### %s" % process.title,
              "- responsible: %s" % process.responsible,
              "- updated: %s" % process.updated]
    if process.expire:
        entete.append("- expire: %s" % process.expire)
    proj = (getattr(process, "projection", "") or "").strip()
    erreur = getattr(process, "projection_error", "") or ""
    corps = (process.body or "").strip()
    diag = ""
    if erreur:
        diag = ('the card "%s" carries an unusable short projection (%s): its '
                "whole text is used instead" % (process.title, erreur))
    elif proj:
        if _octets(proj) > canon_mod.PROCESS_PROJECTION_MAX:
            # #1372 : une projection au-delà de son budget est COUPÉE à ce
            # budget (pas au budget du bloc) : le texte priorisé reste, sa fin
            # est dite, et `canon check` demande de la raccourcir.
            diag = ('the short projection of "%s" is too long (%d > %d bytes): '
                    "cut to its budget" % (process.title, _octets(proj),
                                           canon_mod.PROCESS_PROJECTION_MAX))
            proj = _clip(proj, canon_mod.PROCESS_PROJECTION_MAX)
        corps = proj
    elif _octets(corps) > canon_mod.PROCESS_BODY_MAX:
        # #1624 (N2) : la carte peut tenir entière dans le bloc (jusqu'à
        # ~17 400 o) ; ce diagnostic annonce donc une coupe POSSIBLE, pas une
        # coupe acquise — seul le constat de coupe du bloc est exact.
        diag = ('the card "%s" has no short projection (%d bytes of rules): '
                "the brief may keep only what fits"
                % (process.title, _octets(corps)))
    return "\n".join(entete) + ("\n\n" + corps if corps else ""), diag


def _process_rules(cfg: Config, *, agent: str = "", canon_id: str | None = None,
                   host: str = "") -> tuple[str, list[str]]:
    """#538 : le texte des fiches Process du canon DE L'AGENT, prêt pour un brief,
    et les diagnostics de ce qui n'a pas pu être injecté (#1372).

    Le canon est celui qui **possède la ligne du registre** de l'agent
    (`canon_id`, la colonne `canon` ; NULL = canon par défaut), comme le rôle
    (`_role`) : un homonyme déclaré par un autre canon configuré ne fournit
    jamais ses règles (L42/L43, isolation ; verdict codex #510 r2, P1). Le canon
    doit être **approuvé** (lu depuis git, pas l'arbre de travail) et **validé
    pour son hôte** (`canon sync` : ni erreur globale, ni erreur de sa fiche
    Host) ; une fiche invalide n'est jamais injectée. Un canon illisible, ou
    dont la lecture des fiches a échoué, est dit, pas tu."""
    if not getattr(cfg, "canon", None):
        return "", []
    try:
        from . import canon as canon_mod
        canons = canon_mod.load_configured(cfg)
        # L42 (0031) : seules les lignes de CE canon ; le canon par défaut tient
        # les lignes historiques (colonne `canon` NULL).
        owns = [c for c in canons if c.owns(canon_id)]
        charge = next((c for c in owns if agent and c.agent(agent) is not None), None)
        if charge is None:
            return ((PROCESS_RULES_UNAVAILABLE, [])
                    if any(_rules_unreadable(c) for c in owns) else ("", []))
        if _rules_unreadable(charge):
            return PROCESS_RULES_UNAVAILABLE, []
        if charge.untrusted:
            return "", []
        from . import canon_sync
        if canon_sync.assess(charge, host)[0] != canon_sync.CANON_OK:
            return "", []
    except Exception:
        return "", []
    blocs, diagnostics = [], []
    for process in getattr(charge, "processes", ()) or ():
        if not process.valid:
            continue
        texte, diag = _process_fragment(process)
        if diag:
            diagnostics.append(diag)
        if texte:
            blocs.append(texte)
    return "\n\n".join(blocs).strip(), diagnostics


def _clip_rules(rules: str, place: int) -> str:
    """Les règles coupées pour occuper `place` octets au plus (marque comprise)."""
    return _clip(rules, place) if _octets(rules) > place else rules


#: #1372 : préfixe des diagnostics de règles posés dans le bloc. Ils viennent
#: APRÈS l'en-tête du bloc, donc ils survivent à la coupe qui emporte la fin du
#: texte : la perte de règles est dite, jamais silencieuse.
RULES_DIAG_PREFIX = "> rules: "
#: #1372 : plafond des diagnostics dans le bloc (octets). Des constats nombreux
#: (une carte fautive par ligne) ne doivent jamais chasser le texte des règles
#: ni rendre le bloc vide : au-delà, les lignes sont agrégées (R1 #1372, B2).
RULES_DIAG_MAX = 1200


def _diag_block(diagnostics, cap: int) -> str:
    """Les diagnostics prêts à poser dans le bloc, bornés à `cap` octets.

    Les premières lignes qui tiennent sont gardées ; l'excédent est résumé par
    une dernière ligne `… N more rule diagnostics`. Rend "" si même le résumé
    ne tient pas : l'appelant garde alors le texte des règles."""
    lignes = [RULES_DIAG_PREFIX + str(d).strip()
              for d in (diagnostics or ()) if str(d).strip()]
    if not lignes or cap <= 0:
        return ""
    gardees: list[str] = []
    for i, ligne in enumerate(lignes):
        reste = len(lignes) - i - 1
        suite = ["%s… %d more rule diagnostics (`canon check`)"
                 % (RULES_DIAG_PREFIX, reste)] if reste else []
        if _octets("\n".join(gardees + [ligne] + suite) + "\n") > cap:
            break
        gardees.append(ligne)
    reste = len(lignes) - len(gardees)
    if not reste:
        return "\n".join(gardees) + "\n"
    agregat = ("%s… %d more rule diagnostics (`canon check`)"
               % (RULES_DIAG_PREFIX, reste))
    while gardees:
        texte = "\n".join(gardees + [agregat]) + "\n"
        if _octets(texte) <= cap:
            return texte
        gardees.pop()
    # #1624 (N1) : même la première ligne dépasse le plafond. La couper sans
    # casser l'UTF-8 en réservant son saut de ligne et, s'il reste des lignes à
    # taire, la ligne d'agrégat : le résultat tient dans `cap` (jamais cap+1) et
    # la perte des lignes suivantes reste dite.
    if reste > 1:
        suite = ("%s… %d more rule diagnostics (`canon check`)"
                 % (RULES_DIAG_PREFIX, reste - 1))
        coupe = _clip(lignes[0], max(0, cap - _octets(suite) - 2)).rstrip()
        if coupe:
            return coupe + "\n" + suite + "\n"
        # la première ligne elle-même ne tient pas : le résumé seul, s'il tient
        return suite + "\n" if _octets(suite + "\n") <= cap else ""
    coupe = _clip(lignes[0], max(0, cap - 1)).rstrip()
    return coupe + "\n" if coupe else ""


def _rules_bloc(rules: str, limit: int, diagnostics=()) -> str:
    """#538/#1372 : le bloc de règles (en-tête, diagnostics, règles, pied) borné
    à `limit` octets, ou "" quand l'en-tête et le pied n'y tiennent déjà pas.

    Les diagnostics (#1372) sont posés avant les règles, sous un plafond
    (`RULES_DIAG_MAX`, au plus le quart de la place) : leur nombre ne peut ni
    chasser les règles ni vider le bloc (R1 #1372, B2). Si les règles doivent
    être coupées, une ligne le DIT, et ce constat passe avant les diagnostics
    de cartes."""
    entete, pied = PROCESS_RULES_HEADER + "\n", "\n" + PROCESS_RULES_FOOTER
    place_total = limit - _octets(entete) - _octets(pied)
    if place_total <= 6:
        return ""
    diags = list(diagnostics or ())
    cap = max(0, min(RULES_DIAG_MAX, place_total // 4))
    diag = _diag_block(diags, cap)
    place = place_total - _octets(diag)
    if _octets(rules) > place:
        coupe = ("the rules block is cut: %d bytes of rules, the end is not "
                 "injected" % _octets(rules))
        diag_coupe = _diag_block([coupe], cap)
        diag_reduit = _diag_block(diags, max(0, cap - _octets(diag_coupe)))
        place_reduit = place_total - _octets(diag_reduit)
        if _octets(rules) <= place_reduit:
            # #1624 (N3) : la seconde passe a raccourci les diagnostics ; les
            # règles tiennent alors entières et le constat de coupe serait une
            # sur-déclaration — il n'est pas posé.
            diag, place = diag_reduit, place_reduit
        else:
            diag = diag_reduit + diag_coupe
            place = place_total - _octets(diag)
            if place <= 6:
                # même sans diagnostics, le texte des règles prime
                diag, place = "", place_total
    return entete + diag + _clip_rules(rules, place) + pied


def process_rules_block(cfg: Config, *, agent: str = "", canon_id: str | None = None,
                        host: str = "", limit: int = BRIEF_MAX_BYTES) -> str:
    """#538 : le bloc de règles prêt à ouvrir une session neuve, ou "".

    Le bloc entier (en-tête, diagnostics, règles, pied) tient dans `limit`
    octets : une première session ne part jamais avec un préfixe non borné
    (verdict codex #510 r2, P2). Ne lève jamais : une erreur de mise en forme
    laisse l'appelant sans règles plutôt que d'interrompre l'ouverture de
    session (revue codex #510 r1, P1)."""
    try:
        rules, diagnostics = _process_rules(cfg, agent=agent, canon_id=canon_id,
                                            host=host)
    except Exception:
        return ""
    return _rules_bloc(rules, limit, diagnostics) if rules else ""


def _join_without_rules(avant: str, apres: str) -> str:
    """Deux morceaux de texte recollés après retrait d'un bloc de règles."""
    avant, apres = avant.rstrip("\n"), apres.lstrip("\n")
    return avant + "\n\n" + apres if avant and apres else avant + apres


def _strip_process_rules(text: str) -> str:
    """#538 : le texte sans AUCUN bloc de règles déjà posé (`text` peut l'avoir
    en tête, après une note d'ameesh, ou en porter plusieurs).

    Un bloc délimité par le pied est retiré en entier. Un en-tête sans pied est
    un bloc indélimitable : un texte forgé peut s'y faire passer pour les règles
    du canon, il est donc retiré lui aussi (verdict codex #510 r3, P2) — jusqu'à
    la prochaine section de premier niveau (`\\n## `) ou la fin, pour ne pas
    emporter la consigne qui suit."""
    out = text
    while True:
        debut = out.find(PROCESS_RULES_HEADER)
        if debut < 0:
            return out
        fin = out.find(PROCESS_RULES_FOOTER, debut)
        if fin >= 0:
            coupe = fin + len(PROCESS_RULES_FOOTER)
            out = _join_without_rules(out[:debut], out[coupe:])
            continue
        suite = out.find("\n## ", debut + len(PROCESS_RULES_HEADER))
        out = _join_without_rules(out[:debut], out[suite:] if suite >= 0 else "")


def with_process_rules(cfg: Config, text: str, *, agent: str = "",
                       canon_id: str | None = None, host: str = "") -> str:
    """#538 : ouvre `text` sur les règles du canon de `agent`, sans doubler ni
    couper la consigne. Un bloc déjà posé est RELU (une fiche retirée ne reste
    pas dans un brief préparé).

    La place du bloc est réservée D'ABORD : le texte (résumé de rotation, brief
    préparé) prend ce qui reste et se laisse tronquer, jamais les règles — un
    résumé plein ne fait plus disparaître les règles en silence (verdict codex
    #510 r3, P2). Le texte garde au plus `BRIEF_TEXT_MIN_BYTES` : les règles ne
    l'effacent pas, la consigne d'un brief court reste entière.

    #1372 : la carte peut porter une projection courte, injectée à la place du
    texte entier ; ce qui n'a pas pu être injecté (projection absente, illisible
    ou trop longue, bloc coupé) est DIT en tête du bloc.

    Ne lève jamais (revue codex #510 r1, P1)."""
    try:
        corps = _strip_process_rules(text)
        rules, diagnostics = _process_rules(cfg, agent=agent, canon_id=canon_id,
                                            host=host)
        if not rules:
            return corps
        garde = min(_octets(corps), BRIEF_TEXT_MIN_BYTES)
        bloc = _rules_bloc(rules, BRIEF_MAX_BYTES - 2 - garde, diagnostics)
        if not bloc:
            return corps
        reste = BRIEF_MAX_BYTES - _octets(bloc) - 2
        if _octets(corps) > reste:
            corps = _clip(corps, reste)
        return bloc + "\n\n" + corps if corps else bloc
    except Exception:
        return text


def _role(cfg: Config, name: str) -> list[str]:
    """Rôle tiré du canon (fiche de l'agent) ; rien si aucun canon lisible."""
    if not getattr(cfg, "canon", None):
        return []
    try:
        from . import canon as canon_mod
        # L43 (0031) : la fiche de l'agent est dans SON canon, pas forcément
        # dans le canon par défaut
        agent = next((a for a in (c.agent(name) for c in canon_mod.load_configured(cfg))
                      if a is not None), None)
    except Exception:
        return []
    if agent is None:
        return []
    data = agent.fiche.data or {}
    out = []
    for key in ("role", "description"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            out.append("- %s (canon): %s" % (key, fil.excerpt(value, 300)))
    if agent.capabilities:
        out.append("- capabilities (canon): %s" % ", ".join(map(str, agent.capabilities)))
    return out


def _lots(db, name: str) -> list[str]:
    rows = [r for r in work_mod.list_items(db, assignee=name, limit=200)
            if (r.get("state") or "") not in stagnation.DONE_STATES]
    if not rows:
        return ["No open work item is assigned to you."]
    try:
        described = stagnation.describe(db, rows)
    except Exception:
        described = {}
    out = []
    for row in rows[:LOTS_MAX]:
        attente = ((described.get(int(row["id"])) or {}).get("waiting") or {}).get("label")
        out.append("- #%s [%s] %s%s" % (row["id"], row.get("state") or "?",
                                        fil.excerpt(row.get("title") or "", 120),
                                        " — waiting for: %s" % attente if attente else ""))
    if len(rows) > LOTS_MAX:
        out.append("- … and %d more: `ameesh work list --assignee %s`"
                   % (len(rows) - LOTS_MAX, name))
    return out


#: #3033 : bornes de la liste des avis dus dans un brief ou un tour au repos
REVIEWS_MAX = 20
NO_PENDING_REVIEW = "No review is waiting for your verdict."


def pending_review_lines(db, name: str, *, now: float | None = None) -> list[str]:
    """#3033 : les avis que l'agent doit encore rendre, une ligne par lot.

    La lecture est `review_designation_activation.pending_reviews`, le seul
    calcul d'un avis dû (le même que la charge de la désignation, #3004) : un
    lot ouvert dont le dernier gel le désigne sans verdict de sa part sur
    cette tête. La désignation est un courrier lu une fois ; cette liste est
    ce qui la fait survivre aux rotations. Une lecture illisible le dit, elle
    ne rend jamais une file vide."""
    from . import freeze_gate
    from . import review_designation_activation as activation
    from .sous_utilisation import _duration
    try:
        rows = activation.pending_reviews(db, name)
    except Exception as exc:  # noqa: BLE001 - illisible : dit, jamais « aucun avis »
        return ["(pending reviews unreadable: %s — `ameesh work pending-reviews`)"
                % fil.excerpt(str(exc), 120)]
    if not rows:
        return [NO_PENDING_REVIEW]
    now = time.time() if now is None else float(now)
    out = []
    for row in rows[:REVIEWS_MAX]:
        since = row.get("designated_ts")
        age = (" for %s" % _duration(now - float(since))) if since is not None else ""
        out.append("- #%s [%s] %s — frozen head %s, designated%s: "
                   "`ameesh work milestone %s verdict ok|blocked --sha %s`" % (
                       row["lot"], row.get("state") or "?",
                       fil.excerpt(row.get("title") or "", 100), row["sha"][:12], age,
                       row["lot"],
                       freeze_gate.verdict_sha(db, row["lot"], row["sha"])
                       or "<frozen head>"))
    if len(rows) > REVIEWS_MAX:
        out.append("- … and %d more: `ameesh work pending-reviews %s`"
                   % (len(rows) - REVIEWS_MAX, name))
    return out


#: #3288 : l'en-tête et les bornes de la liste des corrections dues
FIXES_MAX = 20
NO_FIX_OWED = "No blocking verdict waits for your fix."
FIX_OWED_HEADER = "## Fixes you owe (blocking verdict on your frozen head)"


def fixes_owed_lines(db, name: str, *, now: float | None = None) -> list[str]:
    """#3288 : les corrections que l'agent doit encore livrer, une ligne par lot.

    La lecture est `review_designation_activation.due_fixes`, l'autre moitié du
    calcul unique des avis dus (#3033) : un lot ouvert dont le dernier gel porte
    un verdict `blocked` non annulé, sans gel plus récent, dont l'agent est
    l'assigné. C'est le travail qui passe avant tout autre : le blocage d'un
    relecteur n'expire pas parce que son auteur travaille ailleurs. Une lecture
    illisible le dit, elle ne rend jamais une liste vide."""
    from . import review_designation_activation as activation
    from .sous_utilisation import _duration
    try:
        rows = activation.due_fixes(db, name)
    except Exception as exc:  # noqa: BLE001 - illisible : dit, jamais « rien dû »
        return ["(fixes owed unreadable: %s)" % fil.excerpt(str(exc), 120)]
    if not rows:
        return [NO_FIX_OWED]
    now = time.time() if now is None else float(now)
    out = []
    for row in rows[:FIXES_MAX]:
        since = row.get("blocked_ts")
        age = (" %s ago" % _duration(now - float(since))) if since is not None else ""
        by = row.get("blocked_by") or "a reviewer"
        out.append("- #%s [%s] %s — blocking verdict by %s%s on frozen head %s: "
                   "fix, push and freeze the new head "
                   "(`ameesh work milestone %s frozen --sha <new head>`); if you moved "
                   "the lot to build, re-enter qa (`ameesh work move %s qa`)" % (
                       row["lot"], row.get("state") or "?",
                       fil.excerpt(row.get("title") or "", 100), by, age,
                       str(row.get("sha") or "")[:12], row["lot"], row["lot"]))
    if len(rows) > FIXES_MAX:
        out.append("- … and %d more: `ameesh work fixes-owed %s`"
                   % (len(rows) - FIXES_MAX, name))
    return out


def _fil(cfg: Config, project: str) -> tuple[list[str], int]:
    """Les derniers échanges du fil du projet, bornés en nombre et en octets."""
    try:
        transport = fil.transport_for(cfg)
        thread = fil.ThreadRef(project)
        entries = transport.read(thread) if transport.exists(thread) else []
    except Exception:
        return ["(project thread unreadable)"], 0
    total = len(entries)
    lines: list[str] = []
    budget = FIL_MAX_BYTES
    for entry in reversed(entries[-FIL_ENTRIES:]):
        line = "- %s %s → %s: %s" % (entry.when, entry.author, entry.recipient or "—",
                                      fil.excerpt(entry.text, FIL_ENTRY_CHARS))
        if _octets(line) + 1 > budget:
            break
        budget -= _octets(line) + 1
        lines.append(line)
    lines.reverse()
    return lines or ["(empty thread)"], total


def deterministic_brief(cfg: Config, db, row: dict | None, *, session: str | None = None,
                        account=None, reason: str = "", consigne: str = "",
                        election: Sequence[str] | None = None,
                        extra_sections: Sequence[Sequence[str]] = ()) -> str:
    """Le brief de reprise d'un agent, construit par ameesh SANS appel de modèle
    (L39, décision 0030) : identité et rôle, lots assignés et leur attente,
    derniers échanges du fil du projet, courrier non lu, transcript de
    l'ancienne session (lecture seule), puis la consigne. Même entrée, même
    brief ; borné à `BRIEF_MAX_BYTES`.

    `account` : le profil (ou le nom) du compte de l'ancienne session, pour
    retrouver son transcript.

    `election` (L165/EP3, « ## Election ») et `extra_sections` (blocs de
    l'état persisté) : sections supplémentaires d'un brief de relève. Elles
    sont bornées par leurs producteurs et comptent dans la place réservée,
    avant le fil du projet (la partie élastique).
    """
    row = dict(row or {})
    name = row.get("name") or "?"
    harness = row.get("harness") or "?"
    project = fil.project_for(cfg, row.get("team") or row.get("chantier"))
    head = ["# Resume brief for %s (ameesh, deterministic)" % name, "",
            "Built by ameesh without a model call%s. Your previous session is not "
            "resumed: this brief gives you the known state of the mesh; check it before "
            "acting." % (" — %s" % reason if reason else ""), ""]
    ident = ["## Identity and role",
             "- agent: %s (harness %s%s), host %s" % (
                 name, harness, ", model %s" % row["model"] if row.get("model") else "",
                 row.get("host") or "?"),
             "- working directory: %s" % (row.get("cwd") or "not set"),
             "- project: %s%s" % (project, " (team %s)" % row["team"]
                                  if row.get("team") else ""),
             "- human owner: %s" % (row.get("responsible") or "none")]
    if row.get("capabilities"):
        caps = row["capabilities"]
        ident.append("- capabilities: %s" % (", ".join(map(str, caps))
                                           if isinstance(caps, (list, tuple)) else caps))
    ident += _role(cfg, name)
    election_sec = list(election or ())
    extra: list[str] = []
    for block in extra_sections:
        extra.extend(block)
    try:
        lots = _lots(db, name)
    except Exception as exc:
        lots = ["(work items unreadable: %s)" % fil.excerpt(str(exc), 120)]
    try:
        non_lus = len(mail.unread(db, name, limit=1000))
    except Exception:
        non_lus = -1
    courrier = ["## Mail",
                ("%d unread message(s): the runner delivers them to you in your "
                 "next turns (`ameesh mail inbox %s` to reread them)." % (non_lus, name))
                if non_lus > 0 else ("No unread message." if non_lus == 0
                                     else "(mailbox unreadable)")]
    ancienne = []
    if session:
        compte = getattr(account, "name", account)
        chemin = None
        if harness in HARNESSES:
            try:
                homes = ([(account.name, account.home())]
                         if isinstance(account, accounts.Profile)
                         else search_homes(cfg, harness, None))
            except Exception:
                homes = []
            for _nom, home in homes:
                chemin = find_session(harness, home, session)
                if chemin:
                    break
        ancienne = ["## Previous session (read-only)",
                    "- id: %s (harness %s%s)" % (session, harness,
                                                 ", account %s" % compte if compte else ""),
                    "- transcript: %s" % (chemin or "not found on this host"),
                    "You may READ this transcript to recover a detail; do not resume "
                    "it and do not write to it.", ""]
    fin = ["## Instructions", (consigne or DEFAULT_RESUME).strip()]
    # #3288 : une correction due passe avant tout autre travail — section en
    # tête, avant les lots assignés et avant les avis à rendre (#3033).
    fixes_sec = [FIX_OWED_HEADER] + fixes_owed_lines(db, name) + [""]
    lots_sec = ["## Work items assigned to you"] + lots + [""]
    # #3033 : les avis dus survivent à la rotation (la désignation est un
    # courrier déjà lu) ; ils passent avant le fil, qui prend le reste.
    lots_sec += ["## Reviews you owe (designated, no verdict on the frozen head)"] \
        + pending_review_lines(db, name) + [""]
    fixe = "\n".join(head + ident + [""] + fixes_sec + election_sec + [""]
                     + lots_sec + courrier + [""] + extra + ancienne + fin)
    reste = BRIEF_MAX_BYTES - _octets(fixe) - 200
    fil_lines, total = _fil(cfg, project)
    fil_sec = ["## Latest exchanges in thread %s (%d of %d)" % (
        project, sum(1 for l in fil_lines if l.startswith("- ")), total)]
    for line in fil_lines:
        if _octets(line) + 1 > reste:
            break
        reste -= _octets(line) + 1
        fil_sec.append(line)
    text = "\n".join(head + ident + [""] + fixes_sec + election_sec + [""]
                     + lots_sec + fil_sec + [""] + courrier + [""] + extra
                     + ancienne + fin)
    return with_process_rules(cfg, _clip(text.replace("\x00", ""), BRIEF_MAX_BYTES),
                              agent=name, canon_id=row.get("canon"),
                              host=row.get("host") or "")


#: L165 (EP3) : bornes du briefing de relève (chaque section est bornée pour
#: laisser la place aux instructions et aux règles du canon).
ELECTION_DECISIONS_MAX = 5
ELECTION_ALERTS_MAX = 5
ELECTION_GITHUB_MAX = 8
ELECTION_LOTS_MAX = 5
ELECTION_LINE_CHARS = 240


def _age_s(ts, now: float) -> str:
    if not ts:
        return ""
    seconds = max(0, int(now - float(ts)))
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm" % (seconds // 60)
    if seconds < 86400:
        return "%dh" % (seconds // 3600)
    return "%dd" % (seconds // 86400)


def _election_state_sections(cfg: Config, db, row: dict, *,
                             now: float | None = None) -> list[list[str]]:
    """L165 (EP3) : l'état persisté du rôle, pour son briefing de relève.

    Aucune mémoire de session : décisions en attente, alertes en cours, dernier
    relevé GitHub connu par dépôt et lots prenables. Chaque lecture échoue en
    le disant, jamais en silence ; chaque section est bornée.
    """
    now = time.time() if now is None else float(now)
    out: list[list[str]] = []
    # Décisions en attente (les plus anciennes d'abord).
    try:
        pending = storage.of(db).decisions.pending(limit=ELECTION_DECISIONS_MAX)
    except Exception as exc:
        out.append(["## Pending decisions",
                    "(unreadable: %s)" % fil.excerpt(str(exc), ELECTION_LINE_CHARS), ""])
    else:
        lines = ["## Pending decisions (%d shown)" % len(pending)]
        if not pending:
            lines.append("- none.")
        for decision in pending:
            payload = decision.get("payload") or {}
            detail = payload.get("decision") or {}
            question = detail.get("question") or decision.get("body") or ""
            lot = str(decision.get("work_item_id") or "").strip()
            age = _age_s(decision.get("created_ts"), now)
            lines.append("- #%s from %s%s%s: %s" % (
                decision.get("id"), decision.get("sender") or "?",
                " (%s ago)" % age if age else "",
                " — lot #%s" % lot if lot else "",
                fil.excerpt(question, ELECTION_LINE_CHARS)))
        lines.append("")
        out.append(lines)
    # Alertes en cours (`ameesh alerts`, sans la console locale du pool).
    try:
        from . import exploitation
        alerts = exploitation.alerts(cfg, db, local_pool=False, now=now)
    except Exception as exc:
        out.append(["## Alerts",
                    "(unreadable: %s)" % fil.excerpt(str(exc), ELECTION_LINE_CHARS), ""])
    else:
        lines = ["## Alerts in progress (%d)" % len(alerts)]
        for alert in alerts[:ELECTION_ALERTS_MAX]:
            subject = (alert.get("agent") or alert.get("lot") or alert.get("host")
                       or alert.get("repo") or "")
            age = _age_s(alert.get("since"), now)
            lines.append("- %s%s%s: %s" % (
                alert.get("type") or "?", " (%s)" % subject if subject else "",
                " since %s" % age if age else "",
                fil.excerpt(alert.get("detail") or "", ELECTION_LINE_CHARS)))
        if len(alerts) > ELECTION_ALERTS_MAX:
            lines.append("- … and %d more: `ameesh alerts`"
                         % (len(alerts) - ELECTION_ALERTS_MAX))
        if not alerts:
            lines.append("- none.")
        lines.append("")
        out.append(lines)
    # Dernier relevé GitHub complet, par dépôt configuré.
    try:
        from . import plan_github
        settings = plan_github.settings_of(cfg)
    except Exception as exc:
        out.append(["## GitHub observation",
                    "(configuration unreadable: %s)"
                    % fil.excerpt(str(exc), ELECTION_LINE_CHARS), ""])
    else:
        lines = ["## GitHub observation"]
        if not settings.projects:
            lines.append("- no GitHub repository configured on this host.")
        for project, repo in sorted(settings.projects.items())[:ELECTION_GITHUB_MAX]:
            try:
                seen = storage.of(db).github_watch.cursor(project, repo)
            except Exception as exc:
                lines.append("- %s (%s): unreadable (%s)" % (
                    project, repo, fil.excerpt(str(exc), 120)))
                continue
            lines.append("- %s (%s): last complete reading %s" % (
                project, repo, _stamp(seen) if seen is not None else "never recorded"))
        if len(settings.projects) > ELECTION_GITHUB_MAX:
            lines.append("- … and %d more repositories"
                         % (len(settings.projects) - ELECTION_GITHUB_MAX))
        lines.append("")
        out.append(lines)
    # Lots prenables (aucun agent), pour un pilote qui répartit.
    try:
        lots = storage.of(db).work.unassigned_lots(ELECTION_LOTS_MAX)
    except Exception as exc:
        out.append(["## Takeable lots",
                    "(unreadable: %s)" % fil.excerpt(str(exc), ELECTION_LINE_CHARS), ""])
    else:
        lines = ["## Takeable lots (no assignee)"]
        if not lots:
            lines.append("- none.")
        for lot in lots[:ELECTION_LOTS_MAX]:
            lines.append("- #%s [%s] %s" % (
                lot.get("id"), lot.get("state") or "?",
                fil.excerpt(lot.get("title") or "", ELECTION_LINE_CHARS)))
        lines.append("")
        out.append(lines)
    return out


def election_brief(cfg: Config, db, row: dict | None, *, previous_host: str,
                   previous_owner: str | None = None,
                   previous_epoch: int | None = None,
                   previous_session: str | None = None,
                   reason: str = "", consigne: str = "",
                   now: float | None = None) -> str:
    """Le briefing de reprise d'une relève élue (L165/EP3, étude §5).

    Construit par ameesh SANS appel de modèle, à partir du dernier état
    PERSISTÉ du rôle : sa fiche et son canon, ses lots, son courrier non lu,
    les décisions en attente, les alertes en cours, le dernier relevé GitHub
    par dépôt et les lots prenables. Aucune session ne traverse un hôte : le
    brief rappelle l'ancien détenteur et dit que sa session reste là-bas.

    `row` est la ligne du candidat qui a gagné (hôte et dossier de CET hôte).
    Le brief devient le résumé de reprise de la session neuve ; la consigne
    du tour qui l'ouvre suit.
    """
    row = dict(row or {})
    host = row.get("host") or cfg.host
    owner = previous_owner or "unknown owner"
    epoch = " (epoch %s)" % previous_epoch if previous_epoch is not None else ""
    session = ("its session %s stays there" % previous_session
               if previous_session else "it leaves no session here")
    election = [
        "## Election",
        "- you have just been elected on host %s." % host,
        "- previous holder: host %s, %s%s. No session is transferred: %s and is "
        "never resumed or written by this host." % (previous_host, owner, epoch, session),
        "- your state is the mesh database, the canon and GitHub, not a local "
        "worktree. Check it before acting: open lots, mail, pending decisions, "
        "alerts and the GitHub observation below.",
    ]
    consigne = consigne or (
        "You have just been elected: this briefing is the persisted state of the "
        "role. Verify it, then follow this turn's instruction.")
    return deterministic_brief(
        cfg, db, row, session=previous_session,
        reason=reason or ("elected on %s after %s%s on %s"
                          % (host, owner, epoch, previous_host)),
        consigne=consigne, election=election,
        extra_sections=_election_state_sections(cfg, db, row, now=now))


# --------------------------------------------------------------------------
# #794 : le brief MINIMAL d'une session dédiée à un lot
# --------------------------------------------------------------------------

#: Bornes du brief de lot (décision du bureau, 2026-10-13) : plus serré que le
#: brief de reprise, il ne porte que le canon, la fiche du lot, le diff de sa
#: branche et son fil réduit aux décisions, messages non lus et courriers déjà
#: livrés pour ce lot (#2539). Ni transcript d'une session, ni autre lot, ni
#: synthèse de rotation.
LOT_BRIEF_MAX_BYTES = 16000
LOT_NOTES_MAX = 6
LOT_NOTE_CHARS = 600
LOT_MAIL_MAX = 8
LOT_MAIL_CHARS = 300
LOT_DECISIONS_MAX = 5
LOT_DIFF_MAX_LINES = 40
LOT_DIFF_MAX_BYTES = 3000
LOT_DIFF_TIMEOUT_S = 15.0
#: branches candidates pour le merge-base quand le lot n'a pas de cible
LOT_DIFF_BASES = ("origin/HEAD", "origin/main", "main", "master")


def _stamp(value) -> str:
    """Un instant lisible en UTC : epoch, datetime, ou ISO du pilote psql."""
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        ts = float(value)
    elif isinstance(value, str):
        try:
            ts = datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return value[:16]
    else:
        try:
            ts = value.timestamp()
        except AttributeError:
            return str(value)[:16]
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts))
    except (OSError, ValueError, OverflowError):
        return "?"


def _git(cwd: str, args) -> tuple[str | None, str]:
    """`git -C cwd args`, borné en durée ; (sortie, erreur). Jamais d'exception."""
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, *args], capture_output=True, text=True,
            timeout=LOT_DIFF_TIMEOUT_S,
            env=dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_PAGER="cat",
                     LC_ALL="C", GIT_TERMINAL_PROMPT="0"))
    except (OSError, subprocess.SubprocessError) as exc:
        return None, type(exc).__name__
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        return None, detail[0] if detail else "git failed"
    return proc.stdout, ""


def _lot_diff(item: dict, cwd: str) -> list[str]:
    """Le diff de la branche du lot (stat et derniers commits), borné.

    Jamais fatal : sans branche, sans dépôt, sans git ou sans merge-base, la
    section le dit et la session neuve sait où reprendre. N2 (revue Claude du
    2026-10-14) : `branch` et `branch_target` viennent de `work.set_branch` et
    ne sont jamais passés à git comme des options — un nom qui commence par
    `-` est refusé, et `--end-of-options` précède toute référence."""
    section = ["## Diff of the lot's branch"]
    branche = (item.get("branch") or "").strip()
    if not branche:
        return section + ["- no branch yet: the agent opens the lot's worktree and "
                          "branch in the first turns."]
    if branche.startswith("-"):
        return section + ["- branch name %r refused (starts with '-')" % branche]
    if not cwd or not os.path.isdir(cwd):
        return section + ["- branch %s (diff unreadable: no working directory)"
                          % branche]
    tete, err = _git(cwd, ["rev-parse", "--verify", "--quiet", "--end-of-options",
                           branche + "^{commit}"])
    if not tete:
        return section + ["- branch %s (diff unreadable: %s)" % (branche, err or "unknown ref")]
    tete = tete.strip()
    cible = (item.get("branch_target") or "").strip()
    if cible.startswith("-"):
        cible = ""
    racine = base_nom = None
    for candidat in ([cible] if cible else list(LOT_DIFF_BASES)):
        sortie, _ = _git(cwd, ["merge-base", "--end-of-options", candidat, branche])
        if sortie and sortie.strip():
            racine, base_nom = sortie.strip(), candidat
            break
    if racine is None:
        return section + ["- branch %s, head %s (no merge-base with %s: diff unreadable)"
                          % (branche, tete[:12], cible or "/".join(LOT_DIFF_BASES))]
    out = ["- %s: base %s (%s) → head %s" % (branche, base_nom, racine[:12], tete[:12])]
    stat, _ = _git(cwd, ["diff", "--stat", "--no-color", "--end-of-options",
                         racine, branche])
    if stat:
        stat_lignes = [l for l in stat.splitlines() if l.strip()][:LOT_DIFF_MAX_LINES]
        out += ["  - " + fil.excerpt(l, 300) for l in stat_lignes]
        out = _clip_lines(out, LOT_DIFF_MAX_BYTES)
    journal, _ = _git(cwd, ["log", "--oneline", "--no-decorate", "-5",
                            "--end-of-options", "%s..%s" % (racine, branche)])
    if journal:
        out.append("  - last commits:")
        out += ["    - " + fil.excerpt(l, 200) for l in journal.splitlines()[:5]]
    return section + out


def _clip_lines(lines: list[str], limit: int) -> list[str]:
    """`lines` bornées à `limit` octets UTF-8 (la dernière ligne tronquée)."""
    out: list[str] = []
    reste = limit
    for line in lines:
        taille = _octets(line) + 1
        if taille > reste:
            if reste > 1:
                out.append(_clip(line, reste - 1))
            break
        out.append(line)
        reste -= taille
    return out


def _lot_sheet(db, item: dict) -> list[str]:
    """La fiche du lot : identité, état, attente, estimate, branche, notes."""
    lot = item.get("id")
    out = ["## Lot #%s — %s" % (lot, fil.excerpt(item.get("title") or "", 160))]
    champs = ["state: %s" % (item.get("state") or "?")]
    for cle in ("type", "priority", "assignee"):
        if item.get(cle) is not None:
            champs.append("%s: %s" % (cle, item[cle]))
    if not item.get("assignee"):
        champs.append("assignee: none")
    out.append("- " + " · ".join(champs))
    try:
        jalons = work_mod.timeline(db, int(lot)) or {}
    except Exception:
        jalons = {}
    dates = ["%s: %s" % (nom, _stamp(jalons[cle]))
             for nom, cle in (("created", "requested_ts"), ("frozen", "frozen_ts"),
                              ("review", "reviewed_ts"), ("merged", "merged_ts"))
             if jalons.get(cle)]
    if dates:
        out.append("- " + " · ".join(dates))
    attente = None
    try:
        desc = stagnation.describe(db, [item]).get(int(lot)) or {}
        attente = (desc.get("waiting") or {}).get("label")
    except Exception:
        attente = None
    meta = []
    if item.get("estimate_minutes") is not None:
        from . import estimates
        par = ""
        if item.get("estimate_by"):
            par = " (by %s%s)" % (item["estimate_by"],
                                  ", " + _stamp(item["estimate_at"])
                                  if item.get("estimate_at") else "")
        meta.append("estimate: %s%s" % (estimates.label(item["estimate_minutes"]), par))
    if attente:
        meta.append("waiting for: %s" % attente)
    for cle in ("app", "source"):
        if item.get(cle):
            meta.append("%s: %s" % (cle, item[cle]))
    if item.get("issue_ref"):
        meta.append("issue: %s" % item["issue_ref"])
    if meta:
        out.append("- " + " · ".join(meta))
    if item.get("branch"):
        branche = ["branch: %s" % item["branch"]]
        if item.get("branch_target"):
            branche.append("target: %s" % item["branch_target"])
        if item.get("branch_head"):
            branche.append("head: %s" % str(item["branch_head"])[:12])
        out.append("- " + " · ".join(branche))
    try:
        events = work_mod.events(db, int(lot), LOT_NOTES_MAX * 3)
    except Exception:
        events = []
    notes = [e for e in events if (e.get("note") or "").strip()][:LOT_NOTES_MAX]
    if notes:
        out.append("- last notes (oldest first):")
        out += ["  - %s %s: %s" % (_stamp(e.get("created_ts")), e.get("actor") or "?",
                                   fil.excerpt(e.get("note") or "", LOT_NOTE_CHARS))
                for e in reversed(notes)]
    return out


def _lot_thread(db, name: str, lot: str) -> list[str]:
    """Le fil du lot : décisions en attente, non-lus et courriers déjà livrés
    pour ce lot (#2539). Ces derniers ne sont plus ``unread`` : sans eux, une
    session neuve ouverte après une bascule perdrait le verdict reçu dans la
    session précédente."""
    section = ["## Lot thread — decisions, unread and delivered messages"]
    lignes: list[str] = []
    try:
        decisions = storage.of(db).decisions.pending_for_lot(str(lot))
    except Exception:
        decisions = []
    for d in decisions[:LOT_DECISIONS_MAX]:
        question = ((d.get("payload") or {}).get("decision") or {}).get("question") \
            or d.get("body") or ""
        lignes.append("- decision #%s (from %s, %s): %s" % (
            d.get("id"), d.get("sender") or "?", _stamp(d.get("created_ts")),
            fil.excerpt(question, LOT_MAIL_CHARS)))
    try:
        non_lus = [m for m in mail.unread(db, name)
                   if str(m.get("work_item_id") or "").strip() == str(lot)]
    except Exception:
        non_lus = []
    for m in non_lus[:LOT_MAIL_MAX]:
        lignes.append("- unread #%s (from %s, %s): %s" % (
            m.get("id"), m.get("sender") or "?", _stamp(m.get("created_ts")),
            fil.excerpt(m.get("body") or "", LOT_MAIL_CHARS)))
    if len(non_lus) > LOT_MAIL_MAX:
        lignes.append("- … and %d more unread messages for this lot (`ameesh mail inbox %s`)"
                      % (len(non_lus) - LOT_MAIL_MAX, name))
    # #2539 : un tour de courrier peut livrer les messages de plusieurs lots
    # dans la session du lot courant (le lot du tour est alors ambigu). Quand
    # cette session est mise de côté, le brief de la session dédiée au lot ne
    # les voit plus (delivered_at est posé) : on reprend ici les courriers
    # livrés pour CE lot, filtrés côté stockage (destinataire + lot, sans
    # fenêtre de temps), bornés pour le coût du brief.
    try:
        livres = mail.delivered_for_lot(db, name, lot, LOT_MAIL_MAX + 1) or []
    except Exception:
        # Mode médié : la lecture n'est pas admise au contrat (mailbox.
        # delivered_for_lot refusé) ; on garde les non-lus, sans échouer.
        livres = []
    for m in livres[:LOT_MAIL_MAX]:
        lignes.append("- delivered #%s (from %s, %s): %s" % (
            m.get("id"), m.get("sender") or "?", _stamp(m.get("delivered_ts")),
            fil.excerpt(m.get("body") or "", LOT_MAIL_CHARS)))
    if len(livres) > LOT_MAIL_MAX:
        lignes.append("- … and more delivered messages for this lot")
    if not lignes:
        lignes = ["- no pending decision, unread or delivered message for this lot."]
    return section + lignes + [""]


def _not_yours(item: dict, name: str) -> list[str]:
    """#5006 : le brief relit l'assigné courant et le dit quand le lot n'est
    plus à l'agent (réassigné, ou rendu à la file d'équipe) — l'ancien
    assigné réveillé sur ce lot s'arrête au lieu de pousser ou de geler. Un
    relecteur désigné (#3325) ou un lot tenu par un humain : rien."""
    from .work_reviewers import designated
    holder = (item.get("assignee") or "").strip()
    if not name or name == "?" or holder == name or holder.startswith("human:"):
        return []
    if name in designated(item):
        return []  # un relecteur désigné travaille sur le lot d'un autre
    return ["", "**Lot #%s is not assigned to you** (current assignee: %s). Do not "
            "push, freeze or move it; leave a note of your state on the lot "
            "(branch, pushed head, what remains) for %s, then stop."
            % (item.get("id"), holder or "nobody, team queue",
               holder or "the next assignee")]


def lot_brief(cfg: Config, db, row: dict | None, lot, *, consigne: str = "",
              reason: str = "") -> str:
    """Le brief déterministe d'une session NEUVE dédiée à un lot (#794).

    Contenu minimal décidé par le bureau le 2026-10-13 : canon (les règles de
    travail, ajoutées par `with_process_rules`), fiche du lot, diff de sa
    branche, fil du lot réduit aux décisions en attente, messages non lus et
    courriers déjà livrés pour ce lot (#2539).
    Aucun transcript, aucun autre lot, aucune synthèse : une session par lot,
    jamais partagée."""
    row = dict(row or {})
    name = row.get("name") or "?"
    project = fil.project_for(cfg, row.get("team") or row.get("workstream"))
    head = ["# Session dedicated to lot #%s (ameesh, deterministic)" % lot, "",
            "Built by ameesh without a model call%s. This session is dedicated to lot "
            "#%s: work only on it here; the other lots keep their own sessions, put "
            "aside and resumed when their turn comes." % (
                " — %s" % reason if reason else "", lot), ""]
    ident = ["## Identity and role",
             "- agent: %s (harness %s), host %s" % (name, row.get("harness") or "?",
                                                    row.get("host") or "?"),
             "- working directory: %s" % (row.get("cwd") or "not set"),
             "- project: %s%s" % (project, " (team %s)" % row["team"]
                                  if row.get("team") else ""),
             "- human owner: %s" % (row.get("responsible") or "none")]
    if row.get("capabilities"):
        caps = row["capabilities"]
        ident.append("- capabilities: %s" % (", ".join(map(str, caps))
                                             if isinstance(caps, (list, tuple)) else caps))
    ident += _role(cfg, name)
    try:
        item = work_mod.get(db, int(lot)) if str(lot).isdigit() else None
    except Exception:
        item = None
    if item is None:
        fiche = ["## Lot #%s" % lot, "- lot unreadable or unknown to ameesh"]
    else:
        fiche = _lot_sheet(db, item) + _not_yours(item, name)
    diff = _lot_diff(item or {}, row.get("cwd") or "")
    fin = ["## Instructions", (consigne or "").strip() or DEFAULT_RESUME]
    text = "\n".join(head + ident + [""] + fiche + [""] + diff + [""]
                     + _lot_thread(db, name, str(lot)) + fin)
    return with_process_rules(cfg, _clip(text.replace("\x00", ""), LOT_BRIEF_MAX_BYTES),
                              agent=name, canon_id=row.get("canon"),
                              host=row.get("host") or "")


# --------------------------------------------------------------------------
# adopt
# --------------------------------------------------------------------------

def _lease_live(row: dict | None, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return bool(row and row.get("lease_owner") and row.get("lease_expires_ts")
                and float(row["lease_expires_ts"]) > now)


def _project(cfg: Config, row: dict | None) -> str:
    row = row or {}
    return fil.project_for(cfg, row.get("team") or row.get("chantier"))


def _forget_local(cfg: Config, name: str) -> None:
    try:
        os.unlink(os.path.join(cfg.agent_dir(name), "session"))
    except OSError:
        pass


#: #1037 : réglages locaux qui n'ont de sens que sous le harnais déclaré
_HARNESS_SETTINGS = ("model", "effort", "tier")


def _drop_harness_settings(cfg: Config, name: str) -> None:
    """Efface les réglages locaux du harnais quitté (l'exécuteur les relit
    avant les colonnes du registre : les garder ferait lancer le nouveau
    harnais avec le modèle, l'effort ou le tier de l'ancien)."""
    for cle in _HARNESS_SETTINGS:
        try:
            os.unlink(os.path.join(cfg.agent_dir(name), cle))
        except OSError:
            pass


def adopt(cfg: Config, db, name: str, *, session_id: str, harness: str,
          account: str | None = None, cwd: str | None = None, brief: str = "",
          chantier: str | None = None, force: bool = False, actor: str = "",
          warn=None) -> dict:
    """Fait passer une session interactive existante sous l'exécuteur.

    Refus (`RepriseError`) : nom, harnais ou id invalide ; agent épinglé à un
    autre hôte ; bail vivant ; session introuvable sous les comptes de l'hôte ;
    session encore ouverte (sauf `force`, journalisé) ; session dont cet hôte
    ne peut pas vérifier qu'elle est fermée (L63b : même avec `force`).
    """
    warn = warn or (lambda text: None)
    if not NAME_RE.match(name or ""):
        raise RepriseError("invalid agent name: %r" % (name,))
    # L243b (B1) : résoudre à l'entrée, puis écrire sous le primaire — la
    # lecture (`registry.get`) résout déjà, les écritures non : garder l'alias
    # ferait chercher une ligne inexistante et refuser un agent connu.
    from .agent_names import resolve as resolve_agent_name
    name = resolve_agent_name(name, db=db, operation="reprise.adopt")
    if harness not in HARNESSES:
        raise RepriseError("harness %r: adopt can resume %s" % (harness, ", ".join(HARNESSES)))
    if not SESSION_RE.match(session_id or ""):
        raise RepriseError("invalid session id: %r" % (session_id,))
    row = registry.get(db, name)
    if row and row.get("host") and row["host"] != cfg.host:
        raise RepriseError("%s is pinned to host %s (here: %s): sessions are local "
                           "files, run \"ameesh adopt\" on %s"
                           % (name, row["host"], cfg.host, row["host"]))
    if _lease_live(row):
        raise RepriseError("%s holds a live lease (%s): a runner or an attached session "
                           "is already driving it; \"agent-runner stop %s\" then wait for the "
                           "lease to end, or use \"ameesh resume\""
                           % (name, row.get("lease_owner"), name))
    try:
        compte, path, searched = locate(cfg, harness, session_id, account)
    except accounts.AccountError as exc:
        raise RepriseError("invalid account configuration: %s" % exc)
    constats: list[str] = []
    if path is None:
        ailleurs = None
        if account:
            try:
                ailleurs = locate(cfg, harness, session_id, None)
            except (accounts.AccountError, RepriseError):
                ailleurs = None
        if ailleurs and ailleurs[1]:
            raise RepriseError("session %s not found under account %s (%s); it is under "
                               "account %s (%s): rerun with --account %s"
                               % (session_id, account, ", ".join(searched), ailleurs[0],
                                  ailleurs[1], ailleurs[0]))
        if harness == "deepseek" and not any(os.path.isdir(r) for r in searched):
            # dsh sans dossier de sessions connu : on ne peut pas vérifier
            constats.append("dsh sessions directory missing (%s): session accepted without "
                            "a file check" % ", ".join(searched))
            compte = account or next((n for n, _h in search_homes(cfg, harness, None)
                                      if n), None)
        else:
            declares = [p.name for p in accounts.profiles(cfg, harness)]
            raise RepriseError(
                "session %s not found for %s: searched in %s%s"
                % (session_id, harness, ", ".join(searched),
                   " (declared accounts: %s; --account ACCOUNT to target one of them)"
                   % ", ".join(declares) if declares else
                   " (no declared account: add the harness directory to `accounts` "
                   "in the host configuration, or set its CODEX_HOME / "
                   "CLAUDE_CONFIG_DIR / DSH_HOME variable)"))
    # L63 : si l'OS ne permet pas de vérifier que la session est fermée,
    # refus (fail-closed). L63b (owner, decision #9413 b): no bypass, not even
    # --force — the open session is one of the guarantees without one
    try:
        # no session file (dsh without its directory): the command lines are
        # still read, and the host must still be able to read them
        tenants = holders(path, session_id)
    except platform.NotAvailable as exc:
        raise RepriseError(
            "cannot check that session %s is closed on this host (%s): adoption "
            "refused, with or without --force (if it is still open, two harnesses "
            "would write the same session); close the session, then adopt it from "
            "a host that can check it" % (session_id, exc)) from None
    if tenants and not force:
        raise RepriseError(
            "session %s is still open (%s): close the interactive session first "
            "(or --force, at your own risk: two harnesses would write the same session)"
            % (session_id, "; ".join("pid %d, %s: %s" % (t["pid"], t["why"], t["cmd"])
                                     for t in tenants)))
    if tenants:
        avert = ("--force: session %s adopted while it is still open (%s)"
                 % (session_id, ", ".join("pid %d" % t["pid"] for t in tenants)))
        warn(avert)
        constats.append(avert)
    recent = recently_modified(path)
    if recent and not force:
        raise RepriseError("session %s was modified in the last 60 seconds: close the "
                           "interactive session first and wait for a quiet minute" % session_id)
    if recent:
        avert = "--force: session %s adopted with activity in the last 60 seconds" % session_id
        warn(avert)
        constats.append(avert)
    # dossier de travail : --cwd, sinon celui du journal de session, sinon le registre
    noted = session_cwd(harness, path) if path else None
    if cwd:
        cwd = os.path.abspath(os.path.expanduser(cwd))
    elif noted and os.path.isdir(noted):
        cwd = noted
    effectif = cwd or (row or {}).get("cwd")
    if effectif and not os.path.isdir(effectif):
        constats.append("working directory %s missing: the runner will wait until it exists"
                        % effectif)
    if harness == "claude" and path and effectif \
            and os.path.basename(os.path.dirname(path)) != claude_project_key(effectif):
        constats.append("Claude Code stores this session under %s: started in %s, it will "
                        "not find it (--cwd %s?)"
                        % (os.path.dirname(path), effectif, noted or "<original directory>"))
    for constat in constats:
        warn(constat)
    prompt = (brief or "").strip() or DEFAULT_ADOPT
    if row is None:
        registry.upsert(db, name, chantier=chantier, harness=harness, host=cfg.host,
                        cwd=cwd, mode="execute")
    elif chantier:
        registry.upsert(db, name, chantier=chantier)
    status_text = "adopted: session %s (%s%s)" % (session_id, harness,
                                                  ", account %s" % compte if compte else "")
    result = storage.of(db).operations.adopt(
        name, harness=harness, host=cfg.host, cwd=cwd, session_id=session_id,
        session_account=compte, prompt=prompt, status_text=status_text)
    if result is None:
        raise RepriseError("%s: a lease was taken in the meantime; try again" % name)
    # #1037 : le harnais déclaré a changé — les réglages du précédent
    # (modèle, effort, tier ; base et état local) ne s'appliquent plus. Le
    # registre les a effacés dans la même écriture que le harnais ; l'état
    # local est effacé ici (même hôte seulement : ailleurs, il n'existe pas).
    quitte = str(result.get("previous_harness") or "")
    if quitte and quitte != harness:
        _drop_harness_settings(cfg, name)
        constats.append("harness %s → %s: model, effort and tier of the former harness "
                        "dropped (they do not apply to %s)" % (quitte, harness, harness))
    _forget_local(cfg, name)
    # L46 : l'agent adopté est mené par l'exécuteur — ses liaisons de session
    # externes (L41) sont révoquées, sinon une session externe lui volerait
    # son courrier entre deux tours
    try:
        from . import session_bindings as sb
        revoked = sb.revoke_agent(cfg, db, name, by=actor or "ameesh", why="adoption")
    except Exception as exc:  # table absente (0034 non appliquée), base en panne
        revoked = []
        warn("session bindings of %s not revoked: %s"
             % (name, " ".join(str(exc).split())[:160]))
    if revoked:
        constats.append("session binding(s) revoked: %s" % ", ".join(
            "%s:%s" % (r["harness"], r["session_id"]) for r in revoked))
    if cwd:
        try:
            from .runner import write_worktree_marker
            write_worktree_marker(cfg, name, cwd)
        except Exception:
            pass
    ok, why = registry.wakeable(db, cfg, name)
    if not ok:
        warn("adopted, but not wakeable yet: %s" % why)
        constats.append("not wakeable yet: %s" % why)
    after = registry.get(db, name) or {}
    fil.record(cfg, db, sender=actor or "ameesh", recipients=[name],
               text="Adoption of %s (L39): session %s %s%s moves under the runner "
                    "(execute mode%s). Resume instruction of %d character(s) queued.%s"
                    % (name, harness, session_id,
                       " of account %s" % compte if compte else "",
                       ", previous mode %s" % result.get("previous_mode")
                       if result.get("previous_mode") not in (None, "execute") else "",
                       len(prompt),
                       "".join("\n- %s" % c for c in constats)),
               project=_project(cfg, after),
               meta={"audit": "adopt", "session": session_id, "account": compte or "",
                     "harness": harness, "forced": bool(tenants or recent),
                     "previous_session": result.get("previous_session") or ""})
    return {"agent": name, "session": session_id, "harness": harness, "account": compte,
            "path": path, "cwd": effectif, "forced": bool(tenants or recent),
            "wakeable": ok,
            "previous_session": result.get("previous_session"),
            "previous_mode": result.get("previous_mode"), "notes": constats}


# --------------------------------------------------------------------------
# resume
# --------------------------------------------------------------------------

def _local_session(cfg: Config, row: dict) -> str | None:
    """La session à reprendre, même règle que l'exécuteur (`current_session`) :
    le registre, sinon le fichier local s'il est postérieur au dernier oubli,
    à la marge de dérive des horloges près (`SESSION_RESET_SKEW_S`, #1898 N4)."""
    if row.get("session_id"):
        return row["session_id"]
    if (row.get("host") or "") != (cfg.host or ""):
        return None
    path = os.path.join(cfg.agent_dir(row["name"]), "session")
    try:
        with open(path, encoding="utf-8") as fh:
            session = fh.read().strip() or None
        reset = row.get("session_reset_ts")
        if session and reset:
            from .runner import SESSION_RESET_SKEW_S
            if os.stat(path).st_mtime <= float(reset) + SESSION_RESET_SKEW_S:
                return None
        return session
    except OSError:
        return None


def _critical(cfg: Config, row: dict) -> bool:
    """L242c : comme `Runner.is_critical` (`auditor.is_critical`) — déclaré, ou
    rôle critique de sa fiche dans son canon propriétaire ; canon lisible, le
    verdict rafraîchit le cache (#424 r3) ; illisible, le dernier verdict lu,
    sinon critique (#424 r6) ; canon configuré sur aucun canon de l'hôte
    (#424 r7) : non critique."""
    from . import auditor as auditor_mod, canon as canon_mod, sous_utilisation as su
    try:
        canons = canon_mod.load_configured(cfg) if canon_mod.configured(cfg) else []
    except Exception:
        canons = []
    return auditor_mod.is_critical(cfg, None, row["name"], canons,
                                   declared=su.declared_orchestrators(), agent=row)


def plan(cfg: Config, db, row: dict, *, fresh: bool = False) -> dict:
    """Ce que `resume` fera de la session, SANS rien écrire.

    `{session, keep, deterministic, previous, next, old, why}` : `keep` garde
    la session (même compte, session portable, ou rotation de l'exécuteur
    avec résumé sous l'ancien compte encore utilisable) ; sinon elle est
    oubliée et `deterministic` dit qu'un brief déterministe ouvre la suivante.
    """
    harness = row.get("harness") or ""
    session = _local_session(cfg, row)
    out = {"session": session, "keep": False, "deterministic": True, "previous": None,
           "next": None, "old": None, "why": "", "warnings": []}
    if fresh:
        out["why"] = "--fresh: session forgotten, deterministic resume brief"
        out["previous"] = account_turn.recorded_session_account(cfg, row) if session else None
        return out
    if not session:
        out["why"] = "no recorded session: new session on a deterministic brief"
        return out
    try:
        items = accounts.profiles(cfg, harness)
    except accounts.AccountError as exc:
        raise RepriseError("invalid account configuration: %s" % exc)
    if not items:
        out.update(keep=True, deterministic=False,
                   why="no declared account for %s: session %s is resumed"
                   % (harness or "?", session))
        return out
    previous = account_turn.recorded_session_account(cfg, row)
    book = cost_mod.CostBook(state_dir=cfg.state_dir, db=db, tools={row["name"]: harness})
    # L74 (0034 §4) : la session garde son compte d'origine tant qu'il est sous
    # son seuil
    # (session d'avant L30 : le compte déclaré du dossier par défaut, L226b)
    legacy = account_turn.legacy_profile(harness, items)
    origine = previous or (legacy.name if legacy in items else None)
    choice = accounts.choose(db, cfg.host, harness, items, book, agent=row["name"],
                             simulate=True, session_account=origine,
                             critical=_critical(cfg, row),
                             # #1580 (tranche 3) : le compte simulé pour cet
                             # agent tient compte de son modèle
                             model=row.get("model") or book.model_of(row["name"]),
                             cfg=cfg)
    nxt = choice.profile or choice.active or items[0]
    if choice.profile is None:
        out["warnings"].append("all %s accounts are at their threshold (%s): the agent will "
                               "resume at the reset" % (harness, choice.reason))
    old = accounts.by_name(items, previous) if previous else legacy
    out.update(previous=previous, next=nxt.name, old=old)
    if previous == nxt.name or (previous is None and old.home() == nxt.home()):
        out.update(keep=True, deterministic=False,
                   why="session %s is resumed under its account %s" % (session, nxt.name))
        return out
    if accounts.session_portable(harness, old, nxt):
        out.update(keep=True, deterministic=False,
                   why="session %s (account %s) is portable to account %s: resumed"
                   % (session, previous or old.name, nxt.name))
        return out
    try:
        sick = auth_failure.failing(db, cfg.host, harness)
    except db_mod.DbError:
        sick = {}
    usable, raison = account_turn.summary_possible(
        old, book, items, sick=sick,
        model=row.get("model") or book.model_of(row["name"]))
    if usable:
        out.update(keep=True, deterministic=False,
                   why="session %s (account %s) is not portable to %s; the old "
                       "account is still usable: the runner will summarize it under %s then "
                       "rotate (rotation 0027)" % (
                           session, previous or old.name, nxt.name, previous or old.name))
        return out
    out["why"] = ("original account %s unusable (%s) and session %s not portable to %s: "
                  "session forgotten, deterministic resume brief"
                  % (previous or "(removed)", raison, session, nxt.name))
    return out


def resume(cfg: Config, db, name: str, *, brief: str = "", fresh: bool = False,
           actor: str = "", warn=None) -> dict:
    """Relance un agent arrêté, mort ou au repos (voir `plan`)."""
    warn = warn or (lambda text: None)
    if not NAME_RE.match(name or ""):
        raise RepriseError("invalid agent name: %r" % (name,))
    # L243b (B1) : même résolution que la lecture, à l'entrée : tout ce qui
    # suit (bail, NOTIFY, oubli local, fil) écrit sous le nom primaire.
    from .agent_names import resolve as resolve_agent_name
    name = resolve_agent_name(name, db=db, operation="reprise.resume")
    row = registry.get(db, name)
    if row is None:
        raise RepriseError("unknown agent: %s" % name)
    if row.get("operator_paused"):
        raise RepriseError("%s is operator paused: use ameesh resume %s --operator --reason <reason>"
                           % (name, name))
    if (row.get("mode") or "execute") == "externe":
        raise RepriseError("%s is an external agent (human session): ameesh does not restart "
                           "it; use \"ameesh adopt %s --session <id> --harness <h>\" "
                           "to move its session under the runner" % (name, name))
    ok, why = registry.wakeable(db, cfg, name)
    if not ok:
        raise RepriseError("%s is not wakeable: %s" % (name, why))
    if registry.turn_in_progress(db, name):
        raise RepriseError("%s has a turn in progress: nothing to resume (\"ameesh restart\" "
                           "for a new session)" % name)
    live = _lease_live(row)
    if live and row.get("status") == "stopped":
        raise RepriseError("%s is stopped but its lease is still live (the runner has not "
                           "returned it yet): try again in a few seconds" % name)
    runner_seen = False
    if not live and row.get("status") == "stopped":
        # #2625: stopped agents normally have no lease. Only a recent pass
        # for this agent on its current host proves that a runner serves it.
        if (row.get("runner_seen_ts") is not None and row.get("runner_seen_owner")
                and row.get("host") and row.get("runner_seen_host") == row["host"]):
            now = db.query("SELECT extract(epoch from clock_timestamp())::float8 AS ts")[0]["ts"]
            age = now - float(row["runner_seen_ts"])
            runner_seen = 0 <= age <= 2 * max(60.0, cfg.poll, cfg.runner_liveness_interval)
    if not live and not runner_seen:
        # #293: without a lease (or #2625's stopped-agent proof), the queued
        # instruction waits for a runner, including admission/capacity holds.
        warning = ("no runner has reported serving %s on %s recently"
                   if row.get("status") == "stopped"
                   else "no runner holds %s's lease on %s")
        warn((warning + ": the instruction waits until a runner serves it again")
             % (name, row.get("host") or "?"))
    decision = plan(cfg, db, row, fresh=fresh)
    if (not live and decision["keep"] and decision["session"]
            and (row.get("host") or "") == cfg.host):
        session = decision["session"]
        _account, path, _searched = locate(cfg, row.get("harness") or "", session,
                                          decision["previous"])
        try:
            tenants = holders(path or os.path.join(cfg.agent_dir(name), "session"), session)
            recent = recently_modified(path)
        except (platform.NotAvailable, OSError) as exc:
            raise RepriseError("cannot check that session %s is closed: %s" % (session, exc)) from exc
        if tenants or recent:
            raise RepriseError("session %s is still open or was modified in the last 60 seconds: "
                               "close the interactive session first and wait for a quiet minute"
                               % session)
    for text in decision["warnings"]:
        warn(text)
    consigne = (brief or "").strip() or DEFAULT_RESUME
    forget = not decision["keep"] and bool(decision["session"])
    if decision["deterministic"]:
        prompt = deterministic_brief(cfg, db, row, session=decision["session"],
                                     account=decision["old"] or decision["previous"],
                                     reason=decision["why"], consigne=consigne)
    else:
        prompt = consigne
    ops = storage.of(db).operations
    notified = False
    applied = True
    if live and forget:
        # Un exécuteur tient le bail (agent au repos) : l'oubli passe par lui,
        # comme `ameesh restart` (appliqué hors tour, fencé par son bail).
        if ops.request_restart(name, prompt) is None:
            raise RepriseError("%s: state changed in the meantime; try again" % name)
        applied = bool(ops.apply_restart(name))
        if not applied:
            try:
                storage.of(db).wakeups.notify(CHANNEL_MAIL,
                                              json.dumps({"to": name, "restart": True}))
                notified = True
            except db_mod.DbError:
                notified = False
        result = {"previous_session": decision["session"], "previous_status": row.get("status")}
    else:
        result = ops.resume(name, prompt=prompt, forget=forget,
                            status_text="resumed: %s" % ("new session on a brief"
                                                         if forget or decision["deterministic"]
                                                         else "same session"))
        if result is None:
            raise RepriseError("%s: state changed in the meantime (lease taken, turn started or "
                               "stopped); try again" % name)
    if forget and applied and (row.get("host") or "") == (cfg.host or ""):
        _forget_local(cfg, name)
    fil.record(cfg, db, sender=actor or "ameesh", recipients=[name],
               text="Resume of %s (L39, ameesh resume) from status %s: %s. Instruction of "
                    "%d character(s) queued%s."
                    % (name, row.get("status") or "?", decision["why"], len(prompt),
                       "" if applied else " (session forgetting applied by the runner holding "
                       "the lease)"),
               project=_project(cfg, row),
               meta={"audit": "resume", "session": decision["session"] or "",
                     "kept": decision["keep"], "deterministic": decision["deterministic"],
                     "fresh": bool(fresh), "account": decision["previous"] or "",
                     "next_account": decision["next"] or ""})
    return {"agent": name, "previous_status": row.get("status"),
            "session": decision["session"], "kept": decision["keep"],
            "forgotten_session": decision["session"] if forget else None,
            "deterministic": decision["deterministic"], "why": decision["why"],
            "account": decision["previous"], "next_account": decision["next"],
            "applied": applied, "notified": notified, "prompt_bytes": _octets(prompt),
            "runner_live": live}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _read_brief(source: str | None) -> str:
    if not source:
        return ""
    if source == "-":
        return sys.stdin.read()
    with open(os.path.expanduser(source), encoding="utf-8") as fh:
        return fh.read()


def _warn(command: str):
    def say(text: str) -> None:
        print("ameesh %s: warning: %s" % (command, text), file=sys.stderr)
    return say


def _actor(cfg: Config, db) -> str:
    try:
        binding = identity.resolve_binding(cfg, db)
        return binding.name if binding.ok else ""
    except Exception:
        return ""


def cmd_adopt(cfg: Config, args) -> int:
    try:
        brief = _read_brief(args.brief)
    except OSError as exc:
        print("ameesh adopt: unreadable brief: %s" % exc, file=sys.stderr)
        return 1
    db = db_mod.connect(cfg)
    try:
        db_mod.require_schema(db)
        try:
            out = adopt(cfg, db, args.agent, session_id=args.session, harness=args.harness,
                        account=args.account, cwd=args.cwd, brief=brief,
                        chantier=args.chantier, force=args.force, actor=_actor(cfg, db),
                        warn=_warn("adopt"))
        except RepriseError as exc:
            print("ameesh adopt: %s" % exc, file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(out, ensure_ascii=False))
        else:
            print("%s adopted: session %s %s%s (%s) under the runner, resume instruction "
                  "queued%s" % (out["agent"], out["harness"], out["session"],
                                ", account %s" % out["account"] if out["account"] else "",
                                out["path"] or "file not checked",
                                "" if out["wakeable"] else " — not wakeable yet"))
            print("to take it back by hand: ameesh attach %s" % out["agent"])
        return 0
    finally:
        db.close()


def cmd_resume(cfg: Config, args) -> int:
    if getattr(args, "operator", False):
        from . import agent_pause
        if args.brief is not None or args.fresh:
            print("ameesh resume --operator does not replace the session or instruction",
                  file=sys.stderr)
            return 1
        return agent_pause.cmd_cli(cfg, args, paused=False)
    # #2184/N7 : un motif vide explicite n'est pas « absent » ; la présence
    # du drapeau suffit à exiger --operator (même règle qu'agent_pause.py).
    if getattr(args, "expect_reason", None) is not None:
        print("ameesh resume: --expect-reason requires --operator", file=sys.stderr)
        return 1
    try:
        brief = _read_brief(args.brief)
    except OSError as exc:
        print("ameesh resume: unreadable brief: %s" % exc, file=sys.stderr)
        return 1
    db = db_mod.connect(cfg)
    try:
        db_mod.require_schema(db)
        try:
            out = resume(cfg, db, args.agent, brief=brief, fresh=args.fresh,
                         actor=_actor(cfg, db), warn=_warn("resume"))
        except RepriseError as exc:
            print("ameesh resume: %s" % exc, file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(out, ensure_ascii=False))
        else:
            print("%s: %s" % (out["agent"], out["why"]))
            print("%s resumed (was %s): instruction of %d byte(s) queued%s"
                  % (out["agent"], out["previous_status"], out["prompt_bytes"],
                     "" if out["applied"] else "; session forgetting applied by "
                     "the runner holding the lease"))
        return 0
    finally:
        db.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ameesh", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")
    p_pause = sub.add_parser("pause", help="operator pause: finish the turn, start no new one")
    p_pause.add_argument("agent")
    p_pause.add_argument("--reason", required=True)
    p_pause.add_argument("--human", default=None, help="local human (same as ameesh serve)")
    p_pause.add_argument("--not-after", type=float, default=None, metavar="EPOCH",
                         help="deadline (epoch) after which the database refuses "
                              "the pause; set by the drain before a remote call")
    from . import agent_pause, stagnation
    # #3961 : une pause a une échéance ; sans option, DEFAULT_MAX_S (4 h)
    echeance = p_pause.add_mutually_exclusive_group()
    echeance.add_argument("--for", dest="for_s", type=stagnation.parse_duration,
                          default=None, metavar="DURATION",
                          help="pause length (90s, 30m, 2h); lifted by the runner after it")
    echeance.add_argument("--until", type=agent_pause.parse_until, default=None,
                          metavar="WHEN", help="deadline: epoch or ISO 8601 date (UTC "
                          "without zone); lifted by the runner after it")
    echeance.add_argument("--indefinite", action="store_true",
                          help="no deadline (signalled by operator_pause_long); default: "
                               "the pause ends after 4h")
    p_pause.add_argument("--json", action="store_true")
    p_pause.set_defaults(func=lambda cfg, args: agent_pause.cmd_cli(cfg, args, paused=True))
    p_ad = sub.add_parser("adopt", help="existing interactive session → runner (L39)")
    p_ad.add_argument("agent")
    p_ad.add_argument("--session", required=True, help="harness session id")
    p_ad.add_argument("--harness", required=True, choices=HARNESSES)
    p_ad.add_argument("--account", default=None,
                      help="account (declared in `accounts`) under which to look for the session; "
                           "default: all accounts of the harness, in order")
    p_ad.add_argument("--cwd", default=None,
                      help="working directory; default: the one in the session log")
    p_ad.add_argument("--brief", default=None, metavar="FILE|-",
                      help="first instruction under the runner (default: standard resume)")
    p_ad.add_argument("--chantier", default=None, help="workspace (new agent)")
    p_ad.add_argument("--force", action="store_true",
                      help="adopt even if a process still holds the session (logged); "
                           "never when this host cannot check it (L63b)")
    p_ad.add_argument("--json", action="store_true")
    p_ad.set_defaults(func=cmd_adopt)

    p_re = sub.add_parser("resume", help="restart a stopped, dead or idle agent (L39)")
    p_re.add_argument("agent")
    p_re.add_argument("--brief", default=None, metavar="FILE|-",
                      help="resume instruction (default: standard resume)")
    p_re.add_argument("--fresh", action="store_true",
                      help="forget the session: new session on a deterministic brief")
    p_re.add_argument("--json", action="store_true")
    p_re.add_argument("--operator", action="store_true", help="lift only an operator pause")
    p_re.add_argument("--reason", default="", help="required with --operator")
    p_re.add_argument("--expect-reason", default=None, metavar="TEXT",
                      help="with --operator: lift only the pause whose reason is "
                           "exactly TEXT, atomically (#2152)")
    p_re.add_argument("--human", default=None, help="local human for --operator")
    p_re.set_defaults(func=cmd_resume)
    return parser


COMMANDS = ("adopt", "resume", "pause")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    cfg = config_mod.load()
    try:
        return args.func(cfg, args)
    except db_mod.SchemaMissing as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    except db_mod.Unavailable as exc:
        print("error: database unreachable: %s" % exc, file=sys.stderr)
        return 1
    except db_mod.DbError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
