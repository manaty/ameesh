"""#5495 C3 v2.6 — jugement de la preuve après C3 (v2.1, avis impl-11 B1, impl-03 N ; v2.6, #6763),
sur BG, sans accès réseau : lit sur l'entrée standard le JSON de `lecteur-c3.py apres`, plus les
attentes en argument. Imprime une ligne OK/KO par critère ; rc 1 au premier KO compté. Testable
seul (temoin-apres.sh).
  python3 -I evaluer-c3.py --sha S --depuis EPOCH --canon C --candidats h1,h2 \
      --empreintes '<json expected_sha256 du porteur>' [--marge 120] [--borne-prise 90]
Critères : UNE seule exécution (run) de la demande de C3 pour ce SHA depuis l'activation ; son état
terminal = completed ; porteur ∈ candidats ; canon_ref = canon des agents relevé à l'activation ;
empreintes = expected_sha256 ; attestation staging.attested du SHA horodatée dans la fenêtre du run ; dans la fenêtre du run, au
moins un drain paused=true par ameesh:staging au principal = owner, chaque agent drainé levé
ensuite par ameesh:staging, aucun drain de déploiement humain.
#6763 : la **prise** est la première ligne `pending` portant le lien d'exécution `execution`
(#6520), écrite par le tick L2 après les empreintes et avant la préparation longue (`git fetch`,
CI forge, régressions). La ligne `running` vient après cette préparation : 124 s là ne sont pas
une prise de 124 s. Runtime antérieur à #6520 (pas de champ `execution`) : prise non mesurée,
jamais un écart. La borne par défaut est dérivée de la période réelle du tick L2 (60 s, voir la
constante ci-dessous) : 1,5 période = 90 s."""
import argparse
import json
import sys

DRAIN = "drain de déploiement"
#: #6763 : période réelle du tick L2, lue dans `deploy/systemd/ameesh-staging.timer`
#: (`OnActiveSec=60s`, `OnUnitInactiveSec=60s`, aucun `RandomizedDelaySec`) et dans
#: `docs/DEPLOIEMENT-HOTE-AGENTS.md` (« la minuterie passe chaque minute »). Le témoin
#: `temoin-apres.sh` échoue si la minuterie déployée et cette constante divergent.
TICK_PERIOD_S = 60.0
#: « Au tick suivant » : une période, plus une demi-période pour le démarrage de la passe et
#: l'écriture du lien d'exécution (quelques secondes d'empreintes), soit 90 s.
PICKUP_BOUND_S = 1.5 * TICK_PERIOD_S


def juger(data, sha, depuis, canon, candidats, empreintes, marge=120.0,
          borne_prise=PICKUP_BOUND_S):
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
    # #6763 : la prise est la première ligne `pending` portant le lien d'exécution (#6520),
    # écrite après les empreintes et avant la préparation longue ; `running` vient après et ne
    # mesure pas la prise.
    ident = final.get("request")
    own = [r for r in recs if not ident or r.get("request") == ident]
    pends = sorted((r for r in own if r.get("state") == "pending"),
                   key=lambda r: r.get("_at") or 0)
    submit = next((r for r in pends if not r.get("execution")), None)
    claim = next((r for r in pends if r.get("execution") and (
        submit is None or (r.get("_at") or 0) >= (submit.get("_at") or 0))), None)
    if not ident:
        lignes.append("N/M prise L2 non mesurée : identifiant de demande absent")
    elif submit is None:
        lignes.append("N/M prise L2 non mesurée : soumission absente des enregistrements")
    elif claim is None:
        # Un runtime antérieur à #6520 n'écrit pas le lien d'exécution : la prise n'est pas
        # observable. Ce n'est pas un écart (canon §3 : un non-concluant ne retient pas).
        lignes.append("N/M prise L2 non mesurée : aucune ligne pending avec execution "
                      "(runtime antérieur à #6520, ou prise non tracée) — pas un écart")
    else:
        prise = (claim.get("_at") or 0) - (submit.get("_at") or 0)
        case("prise L2 dans la borne %.0f s (tick %.0f s)"
             % (borne_prise, TICK_PERIOD_S), 0 <= prise <= borne_prise,
             "prise=%.1f s (submit=%.3f, execution=%.3f)"
             % (prise, submit.get("_at") or 0, claim.get("_at") or 0))
    case("état terminal réussi (completed)", final.get("state") == "completed", "state=%s" % final.get("state"))
    case("porteur candidat", final.get("host") in candidats, "host=%s" % final.get("host"))
    debut = final.get("started")
    case("démarrage après l'activation", isinstance(debut, (int, float)) and debut >= depuis, "started=%s" % debut)
    case("canon_ref = canon des agents à l'activation", final.get("canon_ref") == canon, "canon_ref=%s" % final.get("canon_ref"))
    fp = final.get("fingerprints") or {}
    case("empreintes = expected_sha256 du porteur", bool(empreintes) and fp == empreintes,
         "écarts=%s" % sorted(k for k in set(fp) | set(empreintes) if fp.get(k) != empreintes.get(k)))
    fin = (final.get("_at") or 0) + marge
    # Attestation DE CE C3 (avis impl-11 B1 sur v2.1) : même SHA, horodatée dans la fenêtre du run
    # (après son démarrage, au plus tard la marge après son état terminal), et du même run si
    # l'attestation porte ce champ. Une ancienne attestation du même SHA ne suffit jamais.
    att = [a for a in data.get("attestations", []) if a.get("sha") == sha
           and isinstance(debut, (int, float)) and isinstance(a.get("_at"), (int, float))
           and debut <= a["_at"] <= fin and a.get("run", run) == run]
    case("attestation staging.attested du SHA dans la fenêtre du run", bool(att),
         "horodatages=%s" % [a.get("_at") for a in data.get("attestations", []) if a.get("sha") == sha])
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
    p.add_argument("--borne-prise", type=float, default=PICKUP_BOUND_S,
                   help="pickup bound in seconds; default 1.5 x the L2 tick period (90 s)")
    a = p.parse_args()
    ko, lignes = juger(json.load(sys.stdin), a.sha, a.depuis, a.canon, a.candidats.split(","),
                       json.loads(a.empreintes), a.marge, a.borne_prise)
    print("\n".join(lignes))
    sys.exit(1 if ko else 0)


if __name__ == "__main__":
    main()
