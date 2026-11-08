"""#5495 C3 v2.1 — jugement de la preuve après C3 (avis impl-11 B1, impl-03 N), sur BG, sans accès
réseau : lit sur l'entrée standard le JSON de `lecteur-c3.py apres`, plus les attentes en argument.
Imprime une ligne OK/KO par critère ; rc 1 au premier KO compté. Testable seul (temoin-apres.sh).
  python3 -I evaluer-c3.py --sha S --depuis EPOCH --canon C --candidats h1,h2 \
      --empreintes '<json expected_sha256 du porteur>' [--marge 120]
Critères : UNE seule exécution (run) de la demande de C3 pour ce SHA depuis l'activation ; son état
terminal = completed ; porteur ∈ candidats ; canon_ref = canon des agents relevé à l'activation ;
empreintes = expected_sha256 ; attestation staging.attested du SHA ; dans la fenêtre du run, au
moins un drain paused=true par ameesh:staging au principal = owner, chaque agent drainé levé
ensuite par ameesh:staging, aucun drain de déploiement humain."""
import argparse
import json
import sys

DRAIN = "drain de déploiement"


def juger(data, sha, depuis, canon, candidats, empreintes, marge=120.0):
    ko, lignes = 0, []

    def case(nom, ok, detail=""):
        nonlocal ko
        lignes.append(("OK  " if ok else "KO  ") + nom + (" : " + detail if detail else ""))
        ko += 0 if ok else 1

    recs = [r for r in data.get("records", []) if r.get("sha") == sha and (r.get("_at") or 0) >= depuis]
    runs = sorted({r.get("run") for r in recs if r.get("run")})
    case("une seule exécution de la demande C3 pour ce SHA", len(runs) == 1, "runs=%s" % runs)
    run = runs[0] if len(runs) == 1 else None
    du_run = sorted((r for r in recs if r.get("run") == run), key=lambda r: r.get("_at") or 0)
    final = du_run[-1] if du_run else {}
    case("état terminal réussi (completed)", final.get("state") == "completed", "state=%s" % final.get("state"))
    case("porteur candidat", final.get("host") in candidats, "host=%s" % final.get("host"))
    debut = final.get("started")
    case("démarrage après l'activation", isinstance(debut, (int, float)) and debut >= depuis, "started=%s" % debut)
    case("canon_ref = canon des agents à l'activation", final.get("canon_ref") == canon, "canon_ref=%s" % final.get("canon_ref"))
    fp = final.get("fingerprints") or {}
    case("empreintes = expected_sha256 du porteur", bool(empreintes) and fp == empreintes,
         "écarts=%s" % sorted(k for k in set(fp) | set(empreintes) if fp.get(k) != empreintes.get(k)))
    att = [a for a in data.get("attestations", []) if a.get("sha") == sha]
    case("attestation staging.attested du SHA", bool(att))
    fin = (final.get("_at") or 0) + marge
    owner = data.get("owner")
    fen = [d for d in data.get("drains", []) if isinstance(debut, (int, float)) and debut - marge <= (d.get("at") or 0) <= fin]
    poses = [d for d in fen if d.get("actor") == "ameesh:staging" and d.get("paused") is True]
    case("au moins un drain paused=true par ameesh:staging", bool(poses), "n=%d" % len(poses))
    case("principal des drains = owner (%s)" % owner, bool(poses) and all(d.get("principal") == owner for d in poses))
    case("motif de drain de déploiement", bool(poses) and all(str(d.get("reason", "")).startswith(DRAIN) for d in poses))
    non_leves = []
    for d in poses:
        if not any(l.get("agent") == d.get("agent") and l.get("actor") == "ameesh:staging" and l.get("paused") is False
                   and (l.get("at") or 0) >= (d.get("at") or 0) for l in fen):
            non_leves.append(d.get("agent"))
    case("chaque drain levé par ameesh:staging", bool(poses) and not non_leves, "non levés=%s" % non_leves)
    humains = [d for d in fen if str(d.get("actor", "")).startswith("human:") and str(d.get("reason", "")).startswith(DRAIN)]
    case("aucun drain de déploiement humain dans la fenêtre", not humains, "n=%d" % len(humains))
    return ko, lignes


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--sha", required=True)
    p.add_argument("--depuis", type=float, required=True)
    p.add_argument("--canon", required=True)
    p.add_argument("--candidats", required=True)
    p.add_argument("--empreintes", required=True)
    p.add_argument("--marge", type=float, default=120.0)
    a = p.parse_args()
    ko, lignes = juger(json.load(sys.stdin), a.sha, a.depuis, a.canon, a.candidats.split(","),
                       json.loads(a.empreintes), a.marge)
    print("\n".join(lignes))
    sys.exit(1 if ko else 0)


if __name__ == "__main__":
    main()
