# SPDX-License-Identifier: AGPL-3.0-only
"""Commandes du journal d'observations du mesh client (#4425).

* ``ameesh process-observations refresh`` : sur bureau-goal, collecte les
  attestations de staging et les acceptations de la projection #4006, ecrit
  le journal append-only puis la projection locale en lecture seule.
* ``ameesh process-observations check`` : sur un hote, charge la projection
  et dit si elle est fraiche, avec les compteurs et les empreintes.
* ``ameesh process-observations show`` : lit la projection locale (aucun
  reseau), par identifiant ou par filtre de lot / projet / genre /
  environnement / revision.
* ``ameesh process-observations revoke`` : sur bureau-goal, revoque un
  enregistrement du journal (raison obligatoire) et republie la projection.
  La revocation est terminale : le journal n'est jamais reecrit.

Configuration : cle ``observations`` du fichier ``ameesh`` (ou d'un fichier
JSON passe a ``--config``) ::

    {
      "observations": {
        "authority": "<autorite>",
        "journal": "/var/lib/ameesh/observations/<autorite>.jsonl",
        "projection": "/var/lib/ameesh/observations-public/<autorite>.json",
        "max_age_s": 300,
        "owner_uid": 0,
        "source": {
          "journal_id": "<identite-du-journal>",
          "repos": ["<org>/<depot>"],
          "issues": ["<org>/<depot>#<issue>"],
          "staging": {
            "state_dir": "/home/agent/.local/state/ameesh",
            "app": "<projet>",
            "hosts": ["<hote>", "..."],
            "reportable_hosts": ["<hote-reportable>"]
          },
          "acceptances": true,
          "ttl_s": 300
        }
      }
    }

Les hotes n'ont besoin que de ``authority``, ``projection``,
``max_age_s``, ``owner_uid`` et du bloc d'identite ``source``
(``journal_id``, ``repos``, ``issues``). Aucun secret n'est lu ni ecrit.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Mapping

from . import config as config_mod
from . import process_observations as observations


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
        value = config.get("observations") or {}
    else:
        value = getattr(config, "observations", None) or {}
    if not isinstance(value, Mapping):
        raise observations.ProjectionError("projection-source",
                                           "observations: JSON object expected")
    return dict(value)


def _emit(summary: Mapping, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return
    counts = summary["counts"]
    print("projection %s: %s (%s) %s..%s, %d observations "
          "(%d current, %d superseded, %d revoked, %d expired), "
          "%d revocations, %d refusals, digest %s"
          % (summary["projection"], summary["schema"],
             summary["source"].get("journal_id"),
             summary["generated_at"], summary["expires_at"],
             counts["observations"], counts["current"],
             counts["superseded"], counts["revoked"], counts["expired"],
             counts["revocations"], counts["refusals"],
             str(summary["digest"])[:12]))


def _cmd_refresh(args: argparse.Namespace) -> int:
    config = _load_config(args.config)
    source = observations.source_from_config(_section(config))
    projection = observations.refresh(source, dry_run=args.dry_run)
    written = "dry-run"
    if not args.dry_run:
        written = observations.write_projection(projection, source.projection)
    _emit(observations.summary(projection, written=written), as_json=args.json)
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    config = _load_config(args.config)
    projection = observations.local_projection(config)
    _emit(observations.summary(projection), as_json=args.json)
    return 0


def _record(projection: observations.Projection, ident: str) -> Mapping:
    record = projection.by_id(ident)
    if record is None:
        raise observations.ObservationRefusal(
            "observation-missing", "observation %s is unknown" % ident)
    return record.as_dict()


def _cmd_show(args: argparse.Namespace) -> int:
    config = _load_config(args.config)
    projection = observations.local_projection(config)
    if args.id:
        records = [_record(projection, args.id)]
    else:
        rows = projection.observations if args.all else projection.current()
        records = []
        for observation in rows:
            if args.lot is not None and observation.work_item_id != args.lot:
                continue
            if args.app and observation.app != args.app:
                continue
            if args.kind and observation.kind != args.kind:
                continue
            if args.environment \
                    and observation.environment != args.environment:
                continue
            if args.artifact \
                    and not observation.artifact.startswith(args.artifact.lower()):
                continue
            records.append(observation.as_dict())
    payload = {
        "projection": observations.summary(projection),
        "observations": records,
        "revocations": [dict(item) for item in projection.revocations],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    for record in records:
        print("%s  %s/%s  lot=%s app=%s %s %s  %s"
              % (record["id"], record["kind"], record["environment"],
                 record.get("work_item_id") or "-", record.get("app") or "-",
                 record["artifact"][:12], record["state"],
                 record["observed_at"]))
    if not records:
        print("no observation")
    return 0


def _cmd_revoke(args: argparse.Namespace) -> int:
    config = _load_config(args.config)
    source = observations.source_from_config(_section(config))
    journal = observations.Journal(source.journal)
    with journal.lock():
        journal.reload()
        journal.revoke(args.id, reason=args.reason,
                       actor=args.actor
                       or os.environ.get("AGENT_MAIL_NAME", "operator"))
        # La projection est derivee et publiee sous le meme verrou : un
        # collecteur concurrent ne peut pas publier un etat plus ancien apres
        # cette revocation.
        projection = observations.build_projection(source, journal)
        written = observations.write_projection(projection, source.projection)
    _emit(observations.summary(projection, written=written), as_json=args.json)
    if written != "written":
        print("revocation journaled but not published: %s" % written,
              file=sys.stderr)
        return 3
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ameesh process-observations",
        description="Journal d'observations du mesh client (#4425)")
    sub = parser.add_subparsers(dest="command", required=True)

    refresh = sub.add_parser(
        "refresh", help="collecter les sources et ecrire la projection locale")
    refresh.add_argument("--config", help="fichier JSON de configuration")
    refresh.add_argument("--dry-run", action="store_true",
                         help="lire et deriver sans ecrire")
    refresh.add_argument("--json", action="store_true")
    refresh.set_defaults(func=_cmd_refresh)

    check = sub.add_parser("check", help="verifier la projection locale")
    check.add_argument("--config", help="fichier JSON de configuration")
    check.add_argument("--json", action="store_true")
    check.set_defaults(func=_cmd_check)

    show = sub.add_parser("show", help="lire une observation de la projection")
    show.add_argument("--config", help="fichier JSON de configuration")
    show.add_argument("--id", help="identifiant exact d'observation")
    show.add_argument("--lot", type=int, help="lot (work item id)")
    show.add_argument("--app", help="projet")
    show.add_argument("--kind", choices=observations.OBSERVATION_KINDS)
    show.add_argument("--environment",
                      choices=observations.OBSERVATION_ENVIRONMENTS)
    show.add_argument("--artifact", help="prefixe de revision (sha)")
    show.add_argument("--all", action="store_true",
                      help="inclure superseded et revoked")
    show.add_argument("--json", action="store_true")
    show.set_defaults(func=_cmd_show)

    revoke = sub.add_parser(
        "revoke", help="revoquer une observation (bureau-goal)")
    revoke.add_argument("--config", help="fichier JSON de configuration")
    revoke.add_argument("--id", required=True)
    revoke.add_argument("--reason", required=True)
    revoke.add_argument("--actor")
    revoke.add_argument("--json", action="store_true")
    revoke.set_defaults(func=_cmd_revoke)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except observations.ObservationError as exc:
        print("%s: %s" % (exc.code, exc.message), file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
