"""#5495 C3 v2.1 — lecteur en LECTURE SEULE de la base partagée, lancé sous ameesh (AG2) par
sonde-c3-v21.sh : `python - <mode> [args]` avec ce fichier sur l'entrée standard.
Connexion writer=False, transaction en lecture seule ; aucune écriture, aucun fichier local
(read_attestation n'est PAS appelé : il réécrit le fichier local). Sortie : un objet JSON.
  go [<release_sha>]              détenteur, in_flight, dernière demande, owner, attestation
  apres <sha> <depuis epoch>      enregistrements de la demande, drains, attestations, owner
"""
import json
import sys
from datetime import datetime

from ameesh import config, db as db_mod, role_run, serve, staging_executor, staging_watch, storage
from ameesh import release_cutoff

KEY = "mesh-staging"


def epoch(value):
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, str):
        return datetime.fromisoformat(value).timestamp()
    return value


def plain(value):
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


def stamp(row):
    """Horodatage d'un événement partagé : colonne `ts` (ameesh_events), sinon `created_at`."""
    return epoch(row.get("ts") if row.get("ts") is not None else row.get("created_at"))


def attested(events, sha):
    # created_at/ts CONSERVÉ (avis impl-11 B1 sur v2.1) : le juge exige une attestation postérieure
    # au démarrage du C3, pas une ancienne attestation du même SHA.
    return [dict(plain(r.get("data") or {}), _at=stamp(r)) for r in events.recent_type(
        release_cutoff.STAGING_EVENT, 2, 200) if (r.get("data") or {}).get("sha") == sha]


def main(argv):
    cfg = config.load()
    db = db_mod.connect(cfg, writer=False)
    events = storage.of(db).events
    mode = argv[0]
    out = {"owner": serve.owner(cfg, None)}
    if mode == "go":
        elected = role_run.live(db, "mesh-design")
        rows = events.latest_type(staging_watch.EVENT, 60, staging_watch.LIMIT)
        done = events.latest_type(release_cutoff.STAGING_EVENT, 60, staging_watch.LIMIT)
        latest = staging_executor.latest(db, KEY) or {}
        out.update(holder=elected.get("holder") if elected.get("held") else None,
                   in_flight=plain(staging_watch.in_flight(rows, attested=done)),
                   history_truncated=len(rows) >= staging_watch.LIMIT,
                   request_state=latest.get("state"))
        if len(argv) > 1:
            out["release_attested"] = attested(events, argv[1])
    elif mode == "apres":
        sha, since = argv[1], float(argv[2])
        recs = [dict(plain(r.get("data") or {}), _at=stamp(r))
                for r in events.recent_type(staging_executor.EVENT, 2, 500, object="staging-request:" + KEY)]
        out["records"] = [r for r in recs if (r.get("_at") or 0) >= since]
        out["attestations"] = attested(events, sha)
        out["drains"] = [plain({"agent": r["agent"], "paused": r["paused"], "reason": r["reason"],
                                "actor": r["actor"], "principal": r["principal"], "at": epoch(r["created_at"])})
                         for r in db.query("SELECT agent, paused, reason, actor, principal, created_at "
                                           "FROM operator_pause_events WHERE created_at >= to_timestamp(%s) "
                                           "ORDER BY id", (since,))]
    else:
        raise SystemExit("mode inconnu")
    print(json.dumps(out, sort_keys=True))


if __name__ == "__main__":
    main(sys.argv[1:])
