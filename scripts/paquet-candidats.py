#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Liste calculée des lots prêts pour le paquet horaire (#2404).

    scripts/paquet-candidats.py [--repo .] [--project ameesh] [--target main] \\
        [--json] [--ready-out "$TMPDIR/prets-0800.txt"]

La composition d'un paquet part de cette liste, **jamais** des seuls lots
cités par les courriers : un lot prêt que personne n'a cité doit apparaître
soit comme unité du paquet, soit comme ligne « écarté » motivée du manifeste.
Le calcul est celui de `ameesh.packet_candidates` : état `qa` (sans filtre
d'assigné), gel effectif égal à la **tête publiée** de la branche (lecture
fraîche d'`origin`), verdicts requis acquis sur cette tête par la porte de
fusion, essai de fusion propre contre la cible. La cible est `--target`, sinon
celle du projet, sinon `origin/HEAD` ; un `origin/HEAD` qui nomme `release/*`
sans cible déclarée est une erreur de lecture (rc 1), jamais une classe : un
clone radar figé sur une release périmée écartait en silence tous les lots sans
`branch_target` (#2464).

Sortie lisible par défaut : les lots prêts d'abord, puis les lots en attente
avec leur motif (`freeze-not-head`, `verdicts-missing`, `conflict`, …), puis
les lectures impossibles. `--json` rend le schéma
`ameesh-packet-candidates/1`. `--ready-out` écrit les ids prêts puis les lots
en erreur (`!<lot> : <motif>`), dans le format de
`scripts/paquet-inclusion.py --ready-file` :

    scripts/paquet-candidats.py --repo . --ready-out "$TMPDIR/prets-0800.txt"
    scripts/paquet-inclusion.py --manifest reviews/integration-paquet-h-…md \\
        --head origin/integration/paquet-h-… --ready-file "$TMPDIR/prets-0800.txt" \\
        --expected-file "$TMPDIR/admissions-0800.txt"

#3375 : un lot sous **hold de pilote** actif (`planned_waits`, la lecture de la
stagnation) n'est pas prêt ; son motif est `pilot-hold` et son détail commence
par « hold pilote », ce que le composeur écrit dans la table « Écartés » du
manifeste. Le composeur peut passer outre, explicitement et pour une raison :

    scripts/paquet-candidats.py --hold-override 3231 \\
        --hold-reason "premier paquet post-T0 (décision mesh-design 16:5xZ)"

La surcharge ne lève que le hold : un autre refus (gel, avis, conflit) garde
le lot en attente, et le geste est alors refusé sans rien journaliser. Sinon
la raison est écrite au journal du lot (`hold-override: …`), et le lot entre
dans la liste prête pour ce calcul. Le hold du pilote, lui, reste en place :
la liste suivante l'écarte de nouveau, et seule une note `unhold:` (ou un
changement d'état) le lève.

#3782 : le calcul n'est valide que si le code exécuté est la référence
attendue. Le script rend `code_sha` (le commit du dépôt qui fournit `src`),
`code_ref` et `code_ref_source` à côté de `target_head`, et refuse quand ce
code n'est pas la référence, ne la contient pas, ou quand son arbre porte des
modifications suivies ou des fichiers non suivis sous `src` (un paquet non
suivi masque un module). Pour le projet ameesh, la référence est la tête de la
cible ; pour un autre projet, c'est `origin/main` du dépôt du code (la cible du
projet n'est pas comparable). Un calcul lancé depuis un vieux checkout ne peut
donc plus passer pour un calcul sur la cible. `--allow-code-mismatch` assume
explicitement le décalage : il est alors dit dans la sortie lisible et le JSON.

Codes de retour : 0 si la liste est complète (même sans lot prêt), 3 si la
liste reste utilisable mais qu'un ou plusieurs lots sont en erreur (lecture de
leur gel ou de leur porte impossible : ils sont nommés, à écarter), 1 si une
lecture globale a échoué (base, cible, base de données, inventaire) et 2 si
l'entrée est invalide (dépôt, projet, base, surcharge de hold sans objet, code
exécuté qui n'est pas la tête de la cible). En cas d'échec global, `--ready-out`
est retiré — y compris un export d'une exécution précédente — pour qu'aucune
liste partielle ne serve de base au contrôle d'inclusion. En `3`, l'export
reste écrit et nomme explicitement les lots en erreur :

    # paquet-candidats main f444955b2eeb code 6d89c0aeca21
    # prêts
    #3782
    # erreurs
    !3506 : <motif sur une ligne>

`scripts/paquet-inclusion.py` lit ce format : un lot en erreur qui est composé
au manifeste est un défaut bloquant ; un lot en erreur absent du manifeste est
consigné comme écarté avec son motif, sans recopie manuelle (#3813).

Lecture seule, sauf le geste explicite `--hold-override` : la base n'est
modifiée que pour y journaliser la raison de la surcharge sur le lot ; les
branches sont lues dans le clone privé du radar ou dans un clone jetable,
jamais dans le dépôt des agents.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

CODE_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_REPO / "src"))

from ameesh import (config, db, identity, merge_queue, packet_candidates,  # noqa: E402
                    plan_git, work)

HoldOverrideError = packet_candidates.HoldOverrideError


def resolve_project(cfg, repo: str, name: str | None):
    """Le projet déclaré (`radar.projects`), sinon une identité de lecture.

    Rend `(project, declared)` : `declared` dit si le clone privé du radar peut
    servir (un projet seulement deviné ne doit pas écrire dans ce clone).
    """
    if name:
        projects = merge_queue.declared_projects(cfg)
        project = projects.get(name)
        if project is None:
            raise packet_candidates.CandidateError(
                "project %r is not declared in radar.projects (declared: %s)"
                % (name, ", ".join(sorted(projects)) or "none"))
        return project, True
    try:
        project = merge_queue.radar_project(cfg, repo)
    except merge_queue.QueueError as exc:
        raise packet_candidates.CandidateError(str(exc)) from exc
    if project is not None:
        return project, True
    return SimpleNamespace(name="", target="", repo=repo), False


def parse_hold_overrides(lots, reason) -> dict[int, str]:
    """#3375 : `--hold-override` répétable + une `--hold-reason` obligatoire.

    La raison est le motif journalisé sur le lot : sans elle, la surcharge
    n'est pas auditable et le geste est refusé. Une raison seule (sans
    surcharge) est une erreur d'entrée, pas un commentaire.
    """
    ids = list(dict.fromkeys(int(lot) for lot in (lots or ())))
    if any(lot <= 0 for lot in ids):
        raise HoldOverrideError("--hold-override: positive lot ids are required")
    text = " ".join(str(reason or "").split())
    if ids and not text:
        raise HoldOverrideError(
            "--hold-override requires --hold-reason: the overtaking and its reason "
            "are journaled on the lot")
    if text and not ids:
        raise HoldOverrideError("--hold-reason given without --hold-override")
    return {lot: text for lot in ids}


def hold_actor(cfg, conn) -> str:
    """Le nom de la session qui surcharge, pour le journal du lot ; "" si
    l'identité n'est pas lisible (la note reste écrite, l'auteur est vide)."""
    try:
        binding = identity.resolve_binding(cfg, conn)
    except Exception:  # noqa: BLE001 - identité illisible : note sans auteur
        return ""
    return binding.name if getattr(binding, "ok", False) else ""


RELATION_TEXT = {
    "same": "la référence exacte",
    "ahead": "en avance sur la référence",
    "behind": "antérieur à la référence",
    "diverged": "histoire sans rapport avec la référence",
    "absent": "référence absente du dépôt",
    "dirty": "modifié non commité",
    "unknown": "inconnu",
}


def _origin_url(repo) -> str:
    """L'URL d'origine d'un dépôt, normalisée pour comparer deux clones."""
    url = plan_git._out(str(repo), "remote", "get-url", "origin")
    if not url:
        return ""
    value = url.strip().rstrip("/")
    if value.endswith(".git"):
        value = value[:-4]
    return value


def code_reference(code_repo, project_repo, target_head: str) -> tuple[str, str]:
    """#3782 : la référence que le code exécuté doit être.

    Le dépôt du code (`src` importé) peut être celui du projet composé
    (`--repo ameesh`) : la référence est alors la tête de la cible. Pour un
    autre projet (Nexlink), la cible est un commit du projet, jamais comparable
    au SHA du dépôt ameesh : la référence de l'outillage est `origin/main` du
    dépôt du code. Rend `(ref, source)` avec source `project-target`,
    `tooling-origin-main` ou `unknown`.
    """
    target = str(target_head or "").strip()
    code_url = _origin_url(code_repo)
    project_url = _origin_url(project_repo)
    if target and code_url and project_url and code_url == project_url:
        return target, "project-target"
    main = plan_git._out(str(code_repo), "rev-parse", "--verify", "origin/main")
    if main:
        return main, "tooling-origin-main"
    return "", "unknown"


def code_provenance(code_repo, code_ref: str) -> dict:
    """#3782 : le commit du code qui tourne (le `src` importé), face à la référence.

    Le code exécuté est le `src` du dépôt qui porte ce script, jamais le dépôt
    `--repo` (qui peut être un clone jetable). `code_relation` vaut `same` (le
    code est la référence), `ahead` (il la contient mais n'est pas cette
    référence), `behind` (il lui est antérieur), `diverged` (ni l'un ni
    l'autre), `absent` (la référence n'est pas dans ce dépôt), `dirty` (l'arbre
    a des modifications suivies ou des fichiers non suivis sous `src`) ou
    `unknown` (HEAD illisible). Les **non suivis** comptent : un paquet ou
    module non suivi sous `src` peut masquer un module suivi et changer
    l'import (relecture cx2 B/P2, #3782). Lecture stricte : une preuve
    illisible n'est pas une preuve d'à-propos.
    """
    out = {"code_sha": "", "code_repo": str(code_repo),
           "code_relation": "unknown", "code_contains_ref": False,
           "code_dirty": False}
    try:
        head = plan_git._out(str(code_repo), "rev-parse", "HEAD")
        if not head:
            return out
        out["code_sha"] = head
        status = plan_git._out(str(code_repo), "status", "--porcelain",
                               "--untracked-files=all", "--", "src",
                               "scripts/paquet-candidats.py")
        # #3782 : un statut illisible est un doute, pas une preuve de propreté ;
        # les non suivis de `src` peuvent masquer un module à l'import.
        out["code_dirty"] = status is None or bool(status)
        ref = str(code_ref or "").strip()
        if not ref:
            return out
        pinned = plan_git._commit(str(code_repo), ref.lower())
        if not pinned:
            out["code_relation"] = "absent"
            return out
        out["code_relation"] = "same" if pinned.lower() == head.lower() else ""
        if plan_git._out(str(code_repo), "merge-base", "--is-ancestor",
                         pinned, head) is not None:
            out["code_contains_ref"] = True
            if out["code_relation"] != "same":
                out["code_relation"] = "ahead"
        elif plan_git._out(str(code_repo), "merge-base", "--is-ancestor",
                           head, pinned) is not None:
            out["code_relation"] = "behind"
        else:
            out["code_relation"] = "diverged"
        if out["code_relation"] == "same" and out["code_dirty"]:
            out["code_relation"] = "dirty"
    except Exception:  # noqa: BLE001 - lecture git impossible : un doute, pas un accord
        out["code_relation"] = "unknown"
        out["code_contains_ref"] = False
    return out


def code_refusal(provenance: dict, code_ref: str,
                 ref_source: str = "project-target") -> str:
    """#3782 : le message de refus, ou "" si le code est la référence.

    `--allow-code-mismatch` ne passe pas par ici : l'appelant assume alors le
    décalage, et le dit dans la sortie (`code_override`).
    """
    relation = str(provenance.get("code_relation") or "unknown")
    if relation == "same":
        return ""
    label = ("la tête de la cible" if ref_source == "project-target"
             else "la référence de l'outillage (origin/main du code)")
    return ("le code exécuté n'est pas %s : %s (code %s, référence %s) ; "
            "relancer depuis un worktree à cette référence exacte, ou "
            "--allow-code-mismatch pour assumer le décalage"
            % (label, RELATION_TEXT.get(relation, relation),
               (provenance.get("code_sha") or "?")[:12],
               str(code_ref or "?")[:12]))


def journal_hold_overrides(conn, payload: dict, overrides: dict[int, str], *,
                           actor: str = "") -> list[dict]:
    """#3375 : journalise chaque surcharge effective, et rien d'autre.

    Toute la validation passe avant la première écriture : une surcharge qui
    ne lève pas le hold (lot absent, sans hold actif, ou refusé par un autre
    motif) n'écrit aucune note. Une surcharge effective écrit sur le lot la
    ligne stable `hold-override: … raison …` (cible, base et source citées) :
    le hold du pilote reste en place, la liste suivante écarte de nouveau le
    lot.
    """
    entries = {int(entry["work_item"]): entry for entry in payload["candidates"]}
    planned: list[tuple[int, str]] = []
    for lot in sorted(overrides):
        reason = overrides[lot]
        entry = entries.get(lot)
        if entry is None:
            raise HoldOverrideError(
                "hold override #%d: lot absent from the calculated list" % lot)
        if not entry.get("hold_override"):
            raise HoldOverrideError(
                "hold override #%d: no active pilot hold to override" % lot)
        if not entry.get("ready"):
            causes = ", ".join(cause.get("code") or "?" for cause in entry["causes"])
            raise HoldOverrideError(
                "hold override #%d refused: the lot is not otherwise ready (%s); "
                "the override lifts the pilot hold only" % (lot, causes or "unknown cause"))
        hold = entry.get("pilot_hold") or {}
        source = str(entry.get("source") or entry.get("published_head") or "")
        planned.append((lot, (
            "hold-override: composition malgré le hold pilote de %s — %s "
            "(cible %s, base %s, source %s)"
            % (hold.get("by") or "auteur inconnu", reason,
               payload.get("target") or "?",
               str(payload.get("base") or "")[:12], source[:12]))))
    for lot, text in planned:
        work.note(conn, lot, text, actor=actor or "", once=True)
    return [{"lot": lot, "note": text} for lot, text in planned]


def render(payload: dict) -> str:
    lines = [
        "paquet-candidats : projet %s, cible %s (%s), base %s (%s)"
        % (payload["project"] or "?", payload["target"],
           payload.get("target_source") or "?",
           payload["base"][:12],
           "tête de la cible lue de origin" if payload["base_source"] == "target"
           else "commit imposé par --base"),
        "%d lot(s) prêt(s), %d en attente, %d erreur(s)"
        % (len(payload["ready"]), len(payload["waiting"]), len(payload["errors"])),
    ]
    if payload.get("code_sha"):
        relation = str(payload.get("code_relation") or "unknown")
        ref = ("réf %s : %s" % (payload["code_ref"][:12],
                                payload.get("code_ref_source") or "?")
               if payload.get("code_ref") else "réf inconnue")
        lines.insert(1, "code %s (%s ; %s)%s" % (
            payload["code_sha"][:12], ref, RELATION_TEXT.get(relation, relation),
            " — DÉCALAGE ASSUMÉ (--allow-code-mismatch)"
            if payload.get("code_override") and relation != "same" else ""))
    by_id = {entry["work_item"]: entry for entry in payload["candidates"]}
    if payload["ready"]:
        lines += ["", "PRÊTS (à composer, ou à écarter avec un motif au manifeste)"]
        for lot in payload["ready"]:
            entry = by_id[lot]
            klass = (entry.get("review_class") or {}).get("class") or "?"
            remise = entry.get("remise") or {}
            lines.append("  #%-5d %s  %-14s %-9s %s%s%s"
                         % (lot, (entry.get("source") or entry["published_head"])[:12],
                            entry.get("assignee") or "-", klass, entry["branch"],
                            "  (remise %s, %s)" % (remise["ref"], remise["cover"])
                            if remise else "",
                            "  (hold pilote surchargé : %s)" % entry["hold_override"]
                            if entry.get("hold_override") else ""))
    if payload["waiting"]:
        lines += ["", "EN ATTENTE (motif calculé)"]
        for lot in payload["waiting"]:
            entry = by_id[lot]
            reasons = "; ".join(
                "%s%s" % (cause["code"],
                          ": " + cause["detail"] if cause.get("detail") else "")
                for cause in entry["causes"])
            lines.append("  #%-5d %s  %s"
                         % (lot, (entry.get("published_head") or "-")[:12], reasons))
    if payload["errors"]:
        lines += ["", "ERREURS (lots à écarter ; la liste reste utilisable)"]
        lines += ["  %s" % error for error in payload["errors"]]
    return "\n".join(lines)


def _one_line(text) -> str:
    """Un motif d'erreur tient sur une ligne : l'export reste lisible."""
    return " ".join(str(text or "").split())


def error_lots(payload: dict) -> dict[int, str] | None:
    """#3813 : les lots en erreur attribués, ou None si l'erreur est globale.

    Une erreur propre à un lot (gel, porte, essai de fusion) laisse la liste
    utilisable : rc 3, et l'export nomme les lots à écarter. Une erreur qui
    n'est pas attribuable à un lot (base, cible, inventaire, `errors` sans
    `errors_by_lot`) rend la liste inutilisable : rc 1, aucun export
    (#2404, relecture cx2 B3).
    """
    messages = [str(message) for message in payload.get("errors") or ()]
    if not messages:
        return {}
    listed = payload.get("errors_by_lot")
    if not isinstance(listed, list) or len(listed) != len(messages):
        return None
    out: dict[int, str] = {}
    for item in listed:
        if not isinstance(item, dict):
            return None
        lot = item.get("work_item")
        motive = _one_line(item.get("error"))
        if (not isinstance(lot, int) or isinstance(lot, bool) or lot <= 0
                or not motive or lot in out):
            return None
        out[lot] = motive
    if {_one_line(message) for message in messages} != {
            "#%d : %s" % (lot, motive) for lot, motive in out.items()}:
        return None
    try:
        ready = {int(lot) for lot in payload.get("ready") or ()}
    except (TypeError, ValueError):
        return None
    if ready & set(out):
        return None
    return out


def clear_ready_out(path: Path) -> None:
    """Retire un export précédent : une liste périmée ne doit jamais servir."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def write_ready_out(path: Path, payload: dict) -> dict[int, str] | None:
    """Écrit la liste prête et la section des lots en erreur (#3813).

    Rend les erreurs attribuées quand l'export est utilisable (`{}` si la
    liste est complète), `None` quand elle ne l'est pas : le fichier est alors
    retiré, y compris un export d'une exécution précédente, pour qu'aucune
    liste partielle ne serve de base au contrôle d'inclusion (#2404 B3). Le
    fichier dit toujours explicitement quels lots sont en erreur.
    """
    errors = error_lots(payload)
    if errors is None:
        clear_ready_out(path)
        return None
    code = (("code %s" % payload["code_sha"][:12])
            if payload.get("code_sha") else "code ?")
    content = "\n".join([
        "# paquet-candidats %s %s %s"
        % (payload["target"], payload["base"][:12], code),
        "# prêts",
        "\n".join("#%d" % lot for lot in payload["ready"]),
        "# erreurs",
        "\n".join("!%d : %s" % (lot, motive)
                  for lot, motive in sorted(errors.items())),
    ]) + "\n"
    temporary = path.with_name("%s.%d.tmp" % (path.name, os.getpid()))
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Liste calculée des lots prêts pour le paquet horaire (#2404).")
    parser.add_argument("--repo", default=".",
                        help="dépôt git du projet (défaut : .)")
    parser.add_argument("--project", default=None,
                        help="nom du projet radar.projects (défaut : celui du dépôt)")
    parser.add_argument("--app", default=None,
                        help="nom du projet dans les lots (défaut : nom du projet)")
    parser.add_argument("--target", default=None,
                        help="branche cible (défaut : cible du projet, sinon origin/HEAD)")
    parser.add_argument("--base", default=None,
                        help="commit de base de l'essai de fusion "
                             "(défaut : tête fraîche de la cible)")
    parser.add_argument("--json", action="store_true", help="sortie JSON")
    parser.add_argument("--ready-out", default=None, metavar="FILE",
                        help="écrit les ids prêts puis les lots en erreur "
                             "(!lot : motif), pour --ready-file")
    parser.add_argument("--allow-code-mismatch", action="store_true",
                        help="#3782 : calculer même si le code exécuté n'est pas la "
                             "tête de la cible ; le décalage est dit dans la sortie")
    parser.add_argument("--hold-override", type=int, action="append", default=None,
                        metavar="LOT",
                        help="#3375 : passer outre le hold pilote actif de ce lot "
                             "(répétable), la raison étant journalisée sur le lot")
    parser.add_argument("--hold-reason", default=None,
                        help="la raison de --hold-override, écrite au journal du lot")
    args = parser.parse_args(argv)

    ready_out = Path(args.ready_out) if args.ready_out else None
    if ready_out is not None:
        try:
            clear_ready_out(ready_out)
        except OSError as exc:
            print("paquet-candidats: cannot clear %s: %s" % (ready_out, exc),
                  file=sys.stderr)
            return 2

    try:
        hold_overrides = parse_hold_overrides(args.hold_override, args.hold_reason)
    except HoldOverrideError as exc:
        print("paquet-candidats: %s" % exc, file=sys.stderr)
        return 2

    repo = os.path.realpath(args.repo)
    try:
        cfg = config.load()
    except Exception as exc:  # noqa: BLE001 - configuration illisible
        print("paquet-candidats: cannot load the configuration: %s" % exc, file=sys.stderr)
        return 2
    try:
        project, declared = resolve_project(cfg, repo, args.project)
    except packet_candidates.CandidateError as exc:
        print("paquet-candidats: %s" % exc, file=sys.stderr)
        return 2
    app = args.app or getattr(project, "name", "") or packet_candidates.default_app(repo)
    conn = None
    scratch = None
    try:
        conn = db.connect(cfg)
        clone, scratch = merge_queue.working_clone(cfg, repo, project if declared else None)
        payload = packet_candidates.inventory(
            conn, project, repo=repo, clone=clone, target=args.target, app=app,
            base=args.base, cfg=cfg, hold_overrides=hold_overrides or None)
        # #3782 : avant toute écriture (journal d'une surcharge) et tout export,
        # le code exécuté doit être la référence attendue (tête de la cible
        # pour ce projet, origin/main de l'outillage pour un autre projet). Le
        # calcul est refusé sinon, même si la liste est par ailleurs complète.
        ref, ref_source = code_reference(CODE_REPO, repo,
                                         payload.get("target_head") or "")
        code = code_provenance(CODE_REPO, ref)
        payload.update(code)
        payload["code_ref"] = ref
        payload["code_ref_source"] = ref_source
        payload["code_override"] = bool(args.allow_code_mismatch)
        refusal = code_refusal(code, ref, ref_source)
        if refusal:
            if not args.allow_code_mismatch:
                print("paquet-candidats: %s" % refusal, file=sys.stderr)
                return 2
            print("paquet-candidats: AVERTISSEMENT: code %s décalé de la référence %s "
                  "(%s ; %s), assumé par --allow-code-mismatch"
                  % ((code.get("code_sha") or "?")[:12], (ref or "?")[:12],
                     RELATION_TEXT.get(code.get("code_relation"),
                                       code.get("code_relation")),
                     ref_source),
                  file=sys.stderr)
        if hold_overrides:
            # #3375 : la raison est écrite avant l'export de la liste prête ;
            # une surcharge sans effet réel (autre refus) est refusée ici.
            # #3813 : une erreur sur un autre lot ne retire pas la liste, donc
            # elle ne doit pas faire taire la journalisation d'une surcharge.
            journal_hold_overrides(conn, payload, hold_overrides,
                                   actor=hold_actor(cfg, conn))
        error_lots_by_id = error_lots(payload)
        if error_lots_by_id is None:
            # #3813 : une erreur qui n'est pas attribuable à un lot est
            # globale ; aucune liste partielle ne doit sortir.
            if ready_out is not None:
                clear_ready_out(ready_out)
            print("paquet-candidats: erreur(s) non attribuable(s) à un lot : "
                  "la liste est inutilisable, aucun export", file=sys.stderr)
            return 1
        if ready_out is not None:
            write_ready_out(ready_out, payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str)
              if args.json else render(payload))
        return 3 if error_lots_by_id else 0
    except HoldOverrideError as exc:
        print("paquet-candidats: %s" % exc, file=sys.stderr)
        return 2
    except packet_candidates.CandidateError as exc:
        print("paquet-candidats: %s" % exc, file=sys.stderr)
        return 1
    except (db.DbError, work.WorkError, OSError, ValueError) as exc:
        print("paquet-candidats: %s" % exc, file=sys.stderr)
        return 1
    finally:
        if conn is not None:
            conn.close()
        if scratch:
            import shutil
            shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
