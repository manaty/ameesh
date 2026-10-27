# SPDX-License-Identifier: AGPL-3.0-only
"""Commandes du pont d'approbation IMA/GitHub (#4006).

* ``ameesh process-approvals refresh`` : sur bureau-goal, lit l'issue IMA
  (#704 par défaut de la configuration), normalise et écrit la projection
  locale en lecture seule. Jamais de valeur de jeton dans la sortie.
* ``ameesh process-approvals check`` : sur un hôte, charge la projection et
  dit si elle est fraîche, avec les compteurs et les empreintes.
* ``ameesh process-approvals show`` : demande l'approbation exacte d'un
  artefact (dépôt, révision complète, chemin) ; refus codé sinon.

La configuration est la clé ``approvals`` du fichier ``ameesh`` (ou d'un
fichier JSON passé à ``--config``) ::

    {
      "approvals": {
        "projection": "/var/lib/ameesh/approvals/ima-704.json",
        "max_age_s": 300,
        "owner_uid": 0,
        "source": {
          "repo": "thepackengers/issue-manager-agent",
          "issue": 704,
          "approvers": ["smichea"],
          "artifact_repos": ["thepackengers/home"],
          "token_file": "/etc/ameesh/secrets/ima-github.token",
          "api_base": "https://api.github.com",
          "ttl_s": 300,
          "timeout_s": 10,
          "total_timeout_s": 30,
          "reader": "bureau-goal"
        }
      }
    }

``token_file`` nomme le secret 0600 ; la valeur n'est jamais lue depuis le
JSON. ``owner_uid`` est optionnel : quand il est posé, la projection et son
dossier doivent appartenir à cet uid (0 pour un fichier synchronisé par root).
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Mapping

from . import config as config_mod
from . import process_approvals as approvals


def _load_config(path: str | None) -> Any:
    if not path:
        return config_mod.load()
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except OSError as exc:
        raise SystemExit("config not readable: %s" % path) from exc
    except ValueError as exc:
        raise SystemExit("config is not JSON: %s" % path) from exc
    if not isinstance(raw, Mapping):
        raise SystemExit("config: JSON object expected")
    return dict(raw)


def _section(config: Any) -> dict:
    if isinstance(config, Mapping):
        value = config.get("approvals") or {}
    else:
        value = getattr(config, "approvals", None) or {}
    if not isinstance(value, Mapping):
        raise approvals.ProjectionError("projection-source",
                                        "approvals: JSON object expected")
    return dict(value)


def _summary(projection: approvals.Projection, *,
             written: str = "") -> dict:
    states = {"current": 0, "superseded": 0, "revoked": 0}
    for approval in projection.approvals:
        states[approval.state] = states.get(approval.state, 0) + 1
    summary = {
        "schema": approvals.SCHEMA,
        "authority": approvals.AUTHORITY,
        "projection": projection.path,
        "generated_at": approvals._iso(projection.generated_at),
        "expires_at": approvals._iso(projection.expires_at),
        "digest": projection.digest,
        "source": {
            "repo": projection.source.get("repo"),
            "issue": projection.source.get("issue"),
            "issue_state": projection.source.get("issue_state"),
            "issue_updated_at": projection.source.get("issue_updated_at"),
            "source_digest": projection.source.get("source_digest"),
        },
        "counts": {
            "approvals": len(projection.approvals),
            "current": states.get("current", 0),
            "superseded": states.get("superseded", 0),
            "revoked": states.get("revoked", 0),
            "revocations": len(projection.revocations),
            "refusals": len(projection.refusals),
        },
        "written": written,
    }
    return summary


def _human(summary: Mapping) -> str:
    counts = summary["counts"]
    return ("projection %s: %s (%s:%s) %s..%s, %d approbations "
            "(%d courantes, %d remplacees, %d revoquees), %d refus, digest %s"
            % (summary["projection"], summary["schema"],
               summary["source"]["repo"], summary["source"]["issue"],
               summary["generated_at"], summary["expires_at"],
               counts["approvals"], counts["current"], counts["superseded"],
               counts["revoked"], counts["refusals"],
               str(summary["digest"])[:12]))


def _emit(summary: Mapping, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    else:
        print(_human(summary))


def _cmd_refresh(args: argparse.Namespace) -> int:
    config = _load_config(args.config)
    section = _section(config)
    source = approvals.source_from_config(section)
    projection = approvals.refresh(source)
    written = "dry-run"
    if not args.dry_run:
        written = approvals.write_projection(projection, source.projection)
    _emit(_summary(projection, written=written), as_json=args.json)
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    config = _load_config(args.config)
    projection = approvals.local_projection(config)
    _emit(_summary(projection), as_json=args.json)
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    config = _load_config(args.config)
    projection = approvals.local_projection(config)
    record = approvals.require_approval(
        projection, repo=args.repo, revision=args.revision, path=args.path,
        source=args.source)
    payload = {
        "approval": record.as_dict(),
        "projection": projection.meta(),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print("%s  %s  %s" % (record.id, record.author, record.url))
        print("artifact %s@%s:%s" % (record.artifact.repo,
                                     record.artifact.revision,
                                     record.artifact.path))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ameesh process-approvals",
        description="Pont d'approbation IMA/GitHub (#4006)")
    sub = parser.add_subparsers(dest="command", required=True)

    refresh = sub.add_parser("refresh",
                             help="lire GitHub et ecrire la projection locale")
    refresh.add_argument("--config", help="fichier JSON de configuration")
    refresh.add_argument("--dry-run", action="store_true",
                         help="lire et normaliser sans ecrire")
    refresh.add_argument("--json", action="store_true")
    refresh.set_defaults(func=_cmd_refresh)

    check = sub.add_parser("check",
                           help="verifier la projection locale")
    check.add_argument("--config", help="fichier JSON de configuration")
    check.add_argument("--json", action="store_true")
    check.set_defaults(func=_cmd_check)

    show = sub.add_parser("show", help="demander une approbation exacte")
    show.add_argument("--config", help="fichier JSON de configuration")
    show.add_argument("--repo", required=True)
    show.add_argument("--revision", required=True,
                      help="SHA complet de l'artefact")
    show.add_argument("--path", required=True)
    show.add_argument("--source", default=approvals.SOURCE_GITHUB,
                      help="github uniquement ; api n'est jamais une autorite")
    show.add_argument("--json", action="store_true")
    show.set_defaults(func=_cmd_show)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except approvals.ApprovalError as exc:
        print("%s: %s" % (exc.code, exc.message), file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
