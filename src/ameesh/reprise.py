# SPDX-License-Identifier: AGPL-3.0-only
"""Adoption et reprise d'agents (lot L39, décision 0030 point 3).

  ameesh adopt <agent> --session ID --harness claude|codex|deepseek
               [--account COMPTE] [--cwd D] [--brief FICHIER|-] [--chantier C]
               [--force] [--json]
        fait passer une session interactive EXISTANTE (fermée) sous
        l'exécuteur : mode `execute`, session et compte d'origine en registre,
        consigne de reprise en attente, trace d'audit dans le fil.
  ameesh resume <agent> [--brief FICHIER|-] [--fresh] [--json]
        relance un agent arrêté, mort ou au repos : sur sa session quand le
        prochain compte peut la reprendre, sinon (ou avec --fresh) sur une
        session neuve ouverte par un brief de reprise DÉTERMINISTE.

Adoption et reprise sont des opérations d'ameesh, jamais des prompts : on ne
demande plus à une session de « reprendre la session X » en fouillant
`~/.codex*/sessions`. Les écritures de base passent par
`storage.of(db).operations` (`adopt`, `resume`) ; aucun SQL ici.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

from . import accounts, account_turn, fil, identity, mail, platform, registry, stagnation, storage
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
DEFAULT_ADOPT = ("Reprise sous l'exécuteur : lis ton courrier, reprends l'état de ton "
                 "travail, termine chaque tour par un état clair.")
#: consigne posée par `resume` sans --brief
DEFAULT_RESUME = ("Reprise (ameesh resume) : lis ton courrier, reprends l'état de ton "
                  "travail, termine chaque tour par un état clair.")

#: bornes du brief de reprise déterministe (octets UTF-8)
BRIEF_MAX_BYTES = 16000
FIL_ENTRIES = 30
FIL_MAX_BYTES = 8000
FIL_ENTRY_CHARS = 400
LOTS_MAX = 40


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


def find_session(harness: str, home: str, session_id: str) -> str | None:
    """Le fichier (ou dossier, DeepSeek) de la session sous `home`, ou None.

    * Codex : `<CODEX_HOME>/sessions/AAAA/MM/JJ/rollout-<date>-<id>.jsonl` ;
    * Claude Code : `<CLAUDE_CONFIG_DIR>/projects/<dossier encodé>/<id>.jsonl` ;
    * DeepSeek Harness : `<DSH_HOME>/sessions/<dossier encodé>/session-<id>/`.
    """
    if not SESSION_RE.match(session_id or ""):
        return None
    root = session_root(harness, home)
    if not os.path.isdir(root):
        return None
    if harness == "codex":
        suffix = "-%s.jsonl" % session_id
        found = []
        for base, _dirs, files in os.walk(root):
            for name in files:
                if name.startswith("rollout-") and name.endswith(suffix):
                    found.append(os.path.join(base, name))
        return sorted(found)[-1] if found else None
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return None
    for entry in entries:
        if harness == "claude":
            candidate = os.path.join(root, entry, session_id + ".jsonl")
            if os.path.isfile(candidate):
                return candidate
        elif harness == "deepseek":
            candidate = os.path.join(root, entry, "session-" + session_id)
            if os.path.isdir(candidate):
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
                "compte %s inconnu pour %s (déclarés sur cet hôte : %s)"
                % (account, harness, ", ".join(p.name for p in items) or "aucun"))
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


def holders(path: str, session_id: str) -> list[dict]:
    """Les processus qui tiennent la session : fichier ouvert (ou dossier
    ouvert ou courant), ou id de session sur la ligne de commande (`codex
    resume <id>`, `claude --resume <id>`) — hors ce processus et ses
    ascendants, dont la ligne de commande porte l'id passé à `ameesh adopt`.
    Par la couche plateforme (L63) ; les processus illisibles (autre
    utilisateur, disparus) sont ignorés.

    Lève `platform.NotAvailable` si l'OS ne permet pas de le vérifier :
    jamais de liste vide qui voudrait dire « personne » sans contrôle.

    Limite connue : Claude Code n'ouvre son journal que le temps d'une
    écriture ; une session interactive démarrée sans `--resume` n'est donc vue
    que si elle écrit au moment du contrôle.
    """
    return platform.holders(path, session_id, exclude=platform.ancestry())


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


def _clip(text: str, limit: int) -> str:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[: max(0, limit - 4)].decode("utf-8", "ignore") + " […]"


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
            out.append("- %s (canon) : %s" % (key, fil.excerpt(value, 300)))
    if agent.capabilities:
        out.append("- capacités (canon) : %s" % ", ".join(map(str, agent.capabilities)))
    return out


def _lots(db, name: str) -> list[str]:
    rows = [r for r in work_mod.list_items(db, assignee=name, limit=200)
            if (r.get("state") or "") not in stagnation.DONE_STATES]
    if not rows:
        return ["Aucun lot ouvert ne t'est assigné."]
    try:
        described = stagnation.describe(db, rows)
    except Exception:
        described = {}
    out = []
    for row in rows[:LOTS_MAX]:
        attente = ((described.get(int(row["id"])) or {}).get("waiting") or {}).get("label")
        out.append("- #%s [%s] %s%s" % (row["id"], row.get("state") or "?",
                                        fil.excerpt(row.get("title") or "", 120),
                                        " — attend : %s" % attente if attente else ""))
    if len(rows) > LOTS_MAX:
        out.append("- … et %d autre(s) : `ameesh work list --assignee %s`"
                   % (len(rows) - LOTS_MAX, name))
    return out


def _fil(cfg: Config, project: str) -> tuple[list[str], int]:
    """Les derniers échanges du fil du projet, bornés en nombre et en octets."""
    try:
        transport = fil.transport_for(cfg)
        thread = fil.ThreadRef(project)
        entries = transport.read(thread) if transport.exists(thread) else []
    except Exception:
        return ["(fil du projet illisible)"], 0
    total = len(entries)
    lines: list[str] = []
    budget = FIL_MAX_BYTES
    for entry in reversed(entries[-FIL_ENTRIES:]):
        line = "- %s %s → %s : %s" % (entry.when, entry.author, entry.recipient or "—",
                                      fil.excerpt(entry.text, FIL_ENTRY_CHARS))
        if _octets(line) + 1 > budget:
            break
        budget -= _octets(line) + 1
        lines.append(line)
    lines.reverse()
    return lines or ["(fil vide)"], total


def deterministic_brief(cfg: Config, db, row: dict | None, *, session: str | None = None,
                        account=None, reason: str = "", consigne: str = "") -> str:
    """Le brief de reprise d'un agent, construit par ameesh SANS appel de modèle
    (L39, décision 0030) : identité et rôle, lots assignés et leur attente,
    derniers échanges du fil du projet, courrier non lu, transcript de
    l'ancienne session (lecture seule), puis la consigne. Même entrée, même
    brief ; borné à `BRIEF_MAX_BYTES`.

    `account` : le profil (ou le nom) du compte de l'ancienne session, pour
    retrouver son transcript.
    """
    row = dict(row or {})
    name = row.get("name") or "?"
    harness = row.get("harness") or "?"
    project = fil.project_for(cfg, row.get("team") or row.get("chantier"))
    head = ["# Brief de reprise de %s (ameesh, déterministe)" % name, "",
            "Construit par ameesh sans appel de modèle%s. Ta session précédente n'est "
            "pas reprise : ce brief te donne l'état connu du mesh ; vérifie-le avant "
            "d'agir." % (" — %s" % reason if reason else ""), ""]
    ident = ["## Identité et rôle",
             "- agent : %s (harnais %s%s), hôte %s" % (
                 name, harness, ", modèle %s" % row["model"] if row.get("model") else "",
                 row.get("host") or "?"),
             "- dossier de travail : %s" % (row.get("cwd") or "non renseigné"),
             "- projet : %s%s" % (project, " (équipe %s)" % row["team"]
                                  if row.get("team") else ""),
             "- responsable humain : %s" % (row.get("responsible") or "aucun")]
    if row.get("capabilities"):
        caps = row["capabilities"]
        ident.append("- capacités : %s" % (", ".join(map(str, caps))
                                           if isinstance(caps, (list, tuple)) else caps))
    ident += _role(cfg, name)
    try:
        lots = _lots(db, name)
    except Exception as exc:
        lots = ["(lots illisibles : %s)" % fil.excerpt(str(exc), 120)]
    try:
        non_lus = len(mail.unread(db, name, limit=1000))
    except Exception:
        non_lus = -1
    courrier = ["## Courrier",
                ("%d message(s) non lu(s) : ils te sont remis par l'exécuteur à tes "
                 "prochains tours (`ameesh mail inbox %s` pour les relire)." % (non_lus, name))
                if non_lus > 0 else ("Aucun message non lu." if non_lus == 0
                                     else "(boîte illisible)")]
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
        ancienne = ["## Ancienne session (lecture seule)",
                    "- id : %s (harnais %s%s)" % (session, harness,
                                                  ", compte %s" % compte if compte else ""),
                    "- transcript : %s" % (chemin or "introuvable sur cet hôte"),
                    "Tu peux LIRE ce transcript pour retrouver un détail ; ne le reprends "
                    "pas et ne l'écris pas.", ""]
    fin = ["## Consigne", (consigne or DEFAULT_RESUME).strip()]
    lots_sec = ["## Lots qui te sont assignés"] + lots + [""]
    fixe = "\n".join(head + ident + [""] + lots_sec + courrier + [""] + ancienne + fin)
    reste = BRIEF_MAX_BYTES - _octets(fixe) - 200
    fil_lines, total = _fil(cfg, project)
    fil_sec = ["## Derniers échanges du fil %s (%d sur %d)" % (
        project, sum(1 for l in fil_lines if l.startswith("- ")), total)]
    for line in fil_lines:
        if _octets(line) + 1 > reste:
            break
        reste -= _octets(line) + 1
        fil_sec.append(line)
    text = "\n".join(head + ident + [""] + lots_sec + fil_sec + [""] + courrier + [""]
                     + ancienne + fin)
    return _clip(text.replace("\x00", ""), BRIEF_MAX_BYTES)


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


def adopt(cfg: Config, db, name: str, *, session_id: str, harness: str,
          account: str | None = None, cwd: str | None = None, brief: str = "",
          chantier: str | None = None, force: bool = False, actor: str = "",
          warn=None) -> dict:
    """Fait passer une session interactive existante sous l'exécuteur.

    Refus (`RepriseError`) : nom, harnais ou id invalide ; agent épinglé à un
    autre hôte ; bail vivant ; session introuvable sous les comptes de l'hôte ;
    session encore ouverte (sauf `force`, journalisé).
    """
    warn = warn or (lambda text: None)
    if not NAME_RE.match(name or ""):
        raise RepriseError("nom d'agent invalide : %r" % (name,))
    if harness not in HARNESSES:
        raise RepriseError("harnais %r : adopt sait reprendre %s" % (harness, ", ".join(HARNESSES)))
    if not SESSION_RE.match(session_id or ""):
        raise RepriseError("identifiant de session invalide : %r" % (session_id,))
    row = registry.get(db, name)
    if row and row.get("host") and row["host"] != cfg.host:
        raise RepriseError("%s est épinglé à l'hôte %s (ici : %s) : les sessions sont des "
                           "fichiers locaux, lancez « ameesh adopt » sur %s"
                           % (name, row["host"], cfg.host, row["host"]))
    if _lease_live(row):
        raise RepriseError("%s détient un bail vivant (%s) : un exécuteur ou une session "
                           "attachée le mène déjà ; « agent-runner stop %s » puis attendez la "
                           "fin du bail, ou utilisez « ameesh resume »"
                           % (name, row.get("lease_owner"), name))
    try:
        compte, path, searched = locate(cfg, harness, session_id, account)
    except accounts.AccountError as exc:
        raise RepriseError("configuration des comptes invalide : %s" % exc)
    constats: list[str] = []
    if path is None:
        ailleurs = None
        if account:
            try:
                ailleurs = locate(cfg, harness, session_id, None)
            except (accounts.AccountError, RepriseError):
                ailleurs = None
        if ailleurs and ailleurs[1]:
            raise RepriseError("session %s introuvable sous le compte %s (%s) ; elle est sous "
                               "le compte %s (%s) : relancez avec --account %s"
                               % (session_id, account, ", ".join(searched), ailleurs[0],
                                  ailleurs[1], ailleurs[0]))
        if harness == "deepseek" and not any(os.path.isdir(r) for r in searched):
            # dsh sans dossier de sessions connu : on ne peut pas vérifier
            constats.append("dossier de sessions dsh absent (%s) : session acceptée sans "
                            "contrôle de fichier" % ", ".join(searched))
            compte = account or next((n for n, _h in search_homes(cfg, harness, None)
                                      if n), None)
        else:
            declares = [p.name for p in accounts.profiles(cfg, harness)]
            raise RepriseError(
                "session %s introuvable pour %s : cherchée dans %s%s"
                % (session_id, harness, ", ".join(searched),
                   " (comptes déclarés : %s ; --account COMPTE pour viser l'un d'eux)"
                   % ", ".join(declares) if declares else
                   " (aucun compte déclaré : ajoutez le dossier du harnais dans `accounts` "
                   "de la configuration de l'hôte, ou posez sa variable CODEX_HOME / "
                   "CLAUDE_CONFIG_DIR / DSH_HOME)"))
    # L63 : si l'OS ne permet pas de vérifier que la session est fermée,
    # refus (fail-closed), sauf --force — journalisé comme un forçage
    unverified = ""
    try:
        tenants = holders(path, session_id) if path else []
    except platform.NotAvailable as exc:
        tenants, unverified = [], str(exc)
    if unverified and not force:
        raise RepriseError(
            "impossible de vérifier que la session %s est fermée sur cet hôte (%s) : "
            "adoption refusée (ou --force, à vos risques : si elle est encore ouverte, "
            "deux harnais écriraient la même session)" % (session_id, unverified))
    if unverified:
        avert = ("--force : session %s adoptée sans vérifier qu'elle est fermée (%s)"
                 % (session_id, unverified))
        warn(avert)
        constats.append(avert)
    if tenants and not force:
        raise RepriseError(
            "la session %s est encore ouverte (%s) : fermez d'abord la session interactive "
            "(ou --force, à vos risques : deux harnais écriraient la même session)"
            % (session_id, "; ".join("pid %d, %s : %s" % (t["pid"], t["why"], t["cmd"])
                                     for t in tenants)))
    if tenants:
        avert = ("--force : session %s adoptée alors qu'elle est encore ouverte (%s)"
                 % (session_id, ", ".join("pid %d" % t["pid"] for t in tenants)))
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
        constats.append("dossier de travail %s absent : l'exécuteur attendra qu'il existe"
                        % effectif)
    if harness == "claude" and path and effectif \
            and os.path.basename(os.path.dirname(path)) != claude_project_key(effectif):
        constats.append("Claude Code range cette session sous %s : lancé dans %s, il ne la "
                        "retrouvera pas (--cwd %s ?)"
                        % (os.path.dirname(path), effectif, noted or "<dossier d'origine>"))
    for constat in constats:
        warn(constat)
    prompt = (brief or "").strip() or DEFAULT_ADOPT
    if row is None:
        registry.upsert(db, name, chantier=chantier, harness=harness, host=cfg.host,
                        cwd=cwd, mode="execute")
    elif chantier:
        registry.upsert(db, name, chantier=chantier)
    status_text = "adopté : session %s (%s%s)" % (session_id, harness,
                                                  ", compte %s" % compte if compte else "")
    result = storage.of(db).operations.adopt(
        name, harness=harness, host=cfg.host, cwd=cwd, session_id=session_id,
        session_account=compte, prompt=prompt, status_text=status_text)
    if result is None:
        raise RepriseError("%s : un bail a été pris entre-temps ; réessayez" % name)
    _forget_local(cfg, name)
    # L46 : l'agent adopté est mené par l'exécuteur — ses liaisons de session
    # externes (L41) sont révoquées, sinon une session externe lui volerait
    # son courrier entre deux tours
    try:
        from . import session_bindings as sb
        revoked = sb.revoke_agent(cfg, db, name, by=actor or "ameesh", why="adoption")
    except Exception as exc:  # table absente (0034 non appliquée), base en panne
        revoked = []
        warn("liaisons de session de %s non révoquées : %s"
             % (name, " ".join(str(exc).split())[:160]))
    if revoked:
        constats.append("liaison(s) de session révoquée(s) : %s" % ", ".join(
            "%s:%s" % (r["harness"], r["session_id"]) for r in revoked))
    if cwd:
        try:
            from .runner import write_worktree_marker
            write_worktree_marker(cfg, name, cwd)
        except Exception:
            pass
    ok, why = registry.wakeable(db, cfg, name)
    if not ok:
        warn("adopté, mais pas encore réveillable : %s" % why)
        constats.append("pas encore réveillable : %s" % why)
    after = registry.get(db, name) or {}
    fil.record(cfg, db, sender=actor or "ameesh", recipients=[name],
               text="Adoption de %s (L39) : la session %s %s%s passe sous l'exécuteur "
                    "(mode execute%s). Consigne de reprise de %d caractère(s) en attente.%s"
                    % (name, harness, session_id,
                       " du compte %s" % compte if compte else "",
                       ", ancien mode %s" % result.get("previous_mode")
                       if result.get("previous_mode") not in (None, "execute") else "",
                       len(prompt),
                       "".join("\n- %s" % c for c in constats)),
               project=_project(cfg, after),
               meta={"audit": "adopt", "session": session_id, "account": compte or "",
                     "harness": harness, "forced": bool(tenants or unverified),
                     "previous_session": result.get("previous_session") or ""})
    return {"agent": name, "session": session_id, "harness": harness, "account": compte,
            "path": path, "cwd": effectif, "forced": bool(tenants or unverified),
            "wakeable": ok,
            "previous_session": result.get("previous_session"),
            "previous_mode": result.get("previous_mode"), "notes": constats}


# --------------------------------------------------------------------------
# resume
# --------------------------------------------------------------------------

def _local_session(cfg: Config, row: dict) -> str | None:
    """La session à reprendre, même règle que l'exécuteur (`current_session`) :
    le registre, sinon le fichier local s'il est postérieur au dernier oubli."""
    if row.get("session_id"):
        return row["session_id"]
    if (row.get("host") or "") != (cfg.host or ""):
        return None
    path = os.path.join(cfg.agent_dir(row["name"]), "session")
    try:
        with open(path, encoding="utf-8") as fh:
            session = fh.read().strip() or None
        reset = row.get("session_reset_ts")
        if session and reset and os.stat(path).st_mtime <= float(reset):
            return None
        return session
    except OSError:
        return None


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
        out["why"] = "--fresh : session oubliée, brief de reprise déterministe"
        out["previous"] = account_turn.recorded_session_account(cfg, row) if session else None
        return out
    if not session:
        out["why"] = "aucune session enregistrée : session neuve sur un brief déterministe"
        return out
    try:
        items = accounts.profiles(cfg, harness)
    except accounts.AccountError as exc:
        raise RepriseError("configuration des comptes invalide : %s" % exc)
    if not items:
        out.update(keep=True, deterministic=False,
                   why="aucun compte déclaré pour %s : la session %s est reprise"
                   % (harness or "?", session))
        return out
    previous = account_turn.recorded_session_account(cfg, row)
    book = cost_mod.CostBook(state_dir=cfg.state_dir, db=db, tools={row["name"]: harness})
    # L74 (0034 §4) : la session garde son compte d'origine tant qu'il est sous
    # son seuil
    origine = previous or next((p.name for p in items if p.home() == accounts.Profile(
        harness=harness, name="(défaut)").home()), None)
    choice = accounts.choose(db, cfg.host, harness, items, book, agent=row["name"],
                             simulate=True, session_account=origine)
    nxt = choice.profile or choice.active or items[0]
    if choice.profile is None:
        out["warnings"].append("tous les comptes %s sont au seuil (%s) : l'agent reprendra "
                               "à la remise à zéro" % (harness, choice.reason))
    old = accounts.by_name(items, previous) if previous else \
        accounts.Profile(harness=harness, name="(défaut)")
    out.update(previous=previous, next=nxt.name, old=old)
    if previous == nxt.name or (previous is None and old.home() == nxt.home()):
        out.update(keep=True, deterministic=False,
                   why="la session %s est reprise sous son compte %s" % (session, nxt.name))
        return out
    if accounts.session_portable(harness, old, nxt):
        out.update(keep=True, deterministic=False,
                   why="la session %s (compte %s) est portable vers le compte %s : reprise"
                   % (session, previous or "(défaut)", nxt.name))
        return out
    usable, raison = account_turn.summary_possible(old, book, items)
    if usable:
        out.update(keep=True, deterministic=False,
                   why="la session %s (compte %s) n'est pas portable vers %s ; l'ancien "
                       "compte reste utilisable : l'exécuteur la résumera sous %s puis "
                       "tournera (rotation 0027)" % (session, previous or "(défaut)",
                                                     nxt.name, previous or "(défaut)"))
        return out
    out["why"] = ("compte d'origine %s inutilisable (%s) et session %s non portable vers %s : "
                  "session oubliée, brief de reprise déterministe"
                  % (previous or "(retiré)", raison, session, nxt.name))
    return out


def resume(cfg: Config, db, name: str, *, brief: str = "", fresh: bool = False,
           actor: str = "", warn=None) -> dict:
    """Relance un agent arrêté, mort ou au repos (voir `plan`)."""
    warn = warn or (lambda text: None)
    if not NAME_RE.match(name or ""):
        raise RepriseError("nom d'agent invalide : %r" % (name,))
    row = registry.get(db, name)
    if row is None:
        raise RepriseError("agent inconnu : %s" % name)
    if (row.get("mode") or "execute") == "externe":
        raise RepriseError("%s est un agent externe (session humaine) : ameesh ne le relance "
                           "pas ; utilisez « ameesh adopt %s --session <id> --harness <h> » "
                           "pour faire passer sa session sous l'exécuteur" % (name, name))
    ok, why = registry.wakeable(db, cfg, name)
    if not ok:
        raise RepriseError("%s n'est pas réveillable : %s" % (name, why))
    if registry.turn_in_progress(db, name):
        raise RepriseError("%s a un tour en cours : rien à reprendre (« ameesh restart » "
                           "pour une session neuve)" % name)
    live = _lease_live(row)
    if live and row.get("status") == "stopped":
        raise RepriseError("%s est arrêté mais son bail vit encore (l'exécuteur ne l'a pas "
                           "encore rendu) : réessayez dans quelques secondes" % name)
    decision = plan(cfg, db, row, fresh=fresh)
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
            raise RepriseError("%s : état changé entre-temps ; réessayez" % name)
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
                            status_text="reprise : %s" % ("session neuve sur brief"
                                                          if forget or decision["deterministic"]
                                                          else "même session"))
        if result is None:
            raise RepriseError("%s : état changé entre-temps (bail pris, tour lancé ou arrêt) ; "
                               "réessayez" % name)
    if forget and applied and (row.get("host") or "") == (cfg.host or ""):
        _forget_local(cfg, name)
    fil.record(cfg, db, sender=actor or "ameesh", recipients=[name],
               text="Reprise de %s (L39, ameesh resume) depuis l'état %s : %s. Consigne de "
                    "%d caractère(s) en attente%s."
                    % (name, row.get("status") or "?", decision["why"], len(prompt),
                       "" if applied else " (oubli de session appliqué par l'exécuteur qui "
                       "tient le bail)"),
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
            "applied": applied, "notified": notified, "prompt_bytes": _octets(prompt)}


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
        print("ameesh %s : attention : %s" % (command, text), file=sys.stderr)
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
        print("ameesh adopt : brief illisible : %s" % exc, file=sys.stderr)
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
            print("ameesh adopt : %s" % exc, file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(out, ensure_ascii=False))
        else:
            print("%s adopté : session %s %s%s (%s) sous l'exécuteur, consigne de reprise en "
                  "attente%s" % (out["agent"], out["harness"], out["session"],
                                 ", compte %s" % out["account"] if out["account"] else "",
                                 out["path"] or "fichier non vérifié",
                                 "" if out["wakeable"] else " — pas encore réveillable"))
            print("pour y revenir à la main : ameesh attach %s" % out["agent"])
        return 0
    finally:
        db.close()


def cmd_resume(cfg: Config, args) -> int:
    try:
        brief = _read_brief(args.brief)
    except OSError as exc:
        print("ameesh resume : brief illisible : %s" % exc, file=sys.stderr)
        return 1
    db = db_mod.connect(cfg)
    try:
        db_mod.require_schema(db)
        try:
            out = resume(cfg, db, args.agent, brief=brief, fresh=args.fresh,
                         actor=_actor(cfg, db), warn=_warn("resume"))
        except RepriseError as exc:
            print("ameesh resume : %s" % exc, file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(out, ensure_ascii=False))
        else:
            print("%s : %s" % (out["agent"], out["why"]))
            print("%s repris (était %s) : consigne de %d octet(s) en attente%s"
                  % (out["agent"], out["previous_status"], out["prompt_bytes"],
                     "" if out["applied"] else " ; oubli de session appliqué par "
                     "l'exécuteur qui tient le bail"))
        return 0
    finally:
        db.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ameesh", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")
    p_ad = sub.add_parser("adopt", help="session interactive existante → exécuteur (L39)")
    p_ad.add_argument("agent")
    p_ad.add_argument("--session", required=True, help="identifiant de session du harnais")
    p_ad.add_argument("--harness", required=True, choices=HARNESSES)
    p_ad.add_argument("--account", default=None,
                      help="compte (déclaré dans `accounts`) sous lequel chercher la session ; "
                           "défaut : tous les comptes du harnais, dans l'ordre")
    p_ad.add_argument("--cwd", default=None,
                      help="dossier de travail ; défaut : celui du journal de session")
    p_ad.add_argument("--brief", default=None, metavar="FICHIER|-",
                      help="première consigne sous l'exécuteur (défaut : reprise standard)")
    p_ad.add_argument("--chantier", default=None, help="chantier (agent nouveau)")
    p_ad.add_argument("--force", action="store_true",
                      help="adopter même si un processus tient encore la session, ou si "
                           "cet hôte ne permet pas de le vérifier (journalisé)")
    p_ad.add_argument("--json", action="store_true")
    p_ad.set_defaults(func=cmd_adopt)

    p_re = sub.add_parser("resume", help="relancer un agent arrêté, mort ou au repos (L39)")
    p_re.add_argument("agent")
    p_re.add_argument("--brief", default=None, metavar="FICHIER|-",
                      help="consigne de reprise (défaut : reprise standard)")
    p_re.add_argument("--fresh", action="store_true",
                      help="oublier la session : session neuve sur un brief déterministe")
    p_re.add_argument("--json", action="store_true")
    p_re.set_defaults(func=cmd_resume)
    return parser


COMMANDS = ("adopt", "resume")


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
        print("erreur : %s" % exc, file=sys.stderr)
        return 1
    except db_mod.Unavailable as exc:
        print("erreur : base injoignable : %s" % exc, file=sys.stderr)
        return 1
    except db_mod.DbError as exc:
        print("erreur : %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
