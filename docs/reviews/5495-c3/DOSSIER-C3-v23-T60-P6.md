# #5495 C3 v2.3 — T-60 : autorité de l'installateur P6 v3.2 (note d'impl-03 #200930)

Bureau (BG), 08/11. Ce document **ne touche à aucun script gelé**. Il prépare la commande exacte à
lancer à T-60 (09/11 ~09:00Z), sur le feu C3 de mesh-design. Rien n'est exécuté maintenant.

## Ce que l'installateur exige (`p6/update-l2-runtime-v32.py`, octets gelés)

| Exigence | Valeur |
|---|---|
| sha256 de l'installateur v3.2 | `e6526d402a43ce68117cc24393b6b392bd3bc7d3e1f5d56c791923f51e0b01c6` |
| sha256 du kit `L2-review-97285a07-20261108.tar.gz` | `4143455e87bb07f32398efd5df49df3ca274c3a35e78e51b504f49fcf6784a57` |
| Dossier de reçus | `/root/ameesh-operator-5495-P6-<hôte>/`, root **0700** |
| Fichiers attendus dans ce dossier | `payload.tar.gz` (le kit), `authority-runtime-p6.json` et l'installateur, tous root **0600** |
| `scope` | `L2 runtime alignment P6` (pour le retour : `L2 runtime alignment P6 return`) |
| `host` | `ameesh-agents-1` ou `ameesh-agents-2` (nodename) |
| `verified_epoch` | Moins de **600 s** au lancement |
| `expires_epoch` | Postérieur au lancement |
| `controller_sha256` | sha256 **du fichier installateur posé sur l'hôte**, soit `e6526d40…` ; c'est la valeur à régénérer |
| `owner_decision` | `184823` |
| `pilot_go_mail` | Identifiant du courrier du feu C3 à T-60 (mesh-design), **à reporter le 09/11** |
| `review_mails` | `{"codex": 200824, "deepseek": 200930}` : OK d'impl-11 sur v3.1 et OK delta d'impl-03 sur v3.2 (décision #200888 : avis delta d'impl-03 seul) |
| `auditor_requalification_mail` | `200778` (P6 (b) CONFORME) |

## Commande exacte, par hôte (depuis BG, à T-60, sur feu C3)

Elle est à lancer d'abord pour AG1, puis pour AG2 :
- variables de AG1 : `IP=78.232.56.22`, `HOTE=ameesh-agents-1` ;
- variables de AG2 : `IP=78.232.60.1`, `HOTE=ameesh-agents-2`.

Pour chaque hôte, il faut aussi renseigner l'identifiant du feu C3 (`GO`). Le script s'arrête au premier écart.

```sh
set -euo pipefail
S=/tmp/claude-1001/-home-agent/526a0a74-f00e-4639-b4d1-d11066d51f99/scratchpad/p6
IP=78.232.56.22 HOTE=ameesh-agents-1 GO=<identifiant du courrier de feu C3>
INST=update-l2-runtime-v32.py; R=/root/ameesh-operator-5495-P6-$HOTE
[ "$(sha256sum < $S/$INST | cut -c1-64)" = e6526d402a43ce68117cc24393b6b392bd3bc7d3e1f5d56c791923f51e0b01c6 ]
[ "$(sha256sum < $S/L2-review-97285a07-20261108.tar.gz | cut -c1-64)" = 4143455e87bb07f32398efd5df49df3ca274c3a35e78e51b504f49fcf6784a57 ]
ssh root@$IP "test ! -e $R && install -d -m 700 $R"
ssh root@$IP "umask 077; cat > $R/payload.tar.gz" < $S/L2-review-97285a07-20261108.tar.gz
ssh root@$IP "umask 077; cat > $R/$INST" < $S/$INST
# controller_sha256 régénéré SUR L'HÔTE à partir du fichier posé, puis comparé à e6526d40… :
CS=$(ssh root@$IP "sha256sum < $R/$INST" | cut -c1-64)
[ "$CS" = e6526d402a43ce68117cc24393b6b392bd3bc7d3e1f5d56c791923f51e0b01c6 ]
[ "$(ssh root@$IP "sha256sum < $R/payload.tar.gz" | cut -c1-64)" = 4143455e87bb07f32398efd5df49df3ca274c3a35e78e51b504f49fcf6784a57 ]
NOW=$(date +%s)
python3 -I -c 'import json,sys; n=int(sys.argv[1]); print(json.dumps({"scope":"L2 runtime alignment P6","host":sys.argv[2],
  "verified_epoch":n,"expires_epoch":n+600,"controller_sha256":sys.argv[3],"owner_decision":184823,
  "pilot_go_mail":int(sys.argv[4]),"review_mails":{"codex":200824,"deepseek":200930},
  "auditor_requalification_mail":200778}))' "$NOW" "$HOTE" "$CS" "$GO" \
  | ssh root@$IP "umask 077; cat > $R/authority-runtime-p6.json && stat -c '%a %U %n' $R/*"
# À lancer dans les 600 s : L2 doit être disabled/inactive (l'installateur le revérifie).
ssh root@$IP "cd $R && python3.12 -I $INST --apply"
```

Le reçu `runtime-aligned.json` donne `config_after_sha256`. On le passe ensuite à :

```sh
pose-c3-v22.sh prealables <AG1|AG2> … --config-avant <config_after_sha256>
```

**Retour P6**, si nécessaire : même dossier, autorité avec le `scope` `L2 runtime alignment P6 return`,
puis `python3.12 -I update-l2-runtime-v32.py --apply --retour`.

## N résiduels d'impl-03 (#200930), sans blocage, dans la check-list de T-60

- `pose-c3-v22.sh` (l. 169) garde le contrôle `wc -l = 2` du fichier d'état, et ses messages
  renvoient à `retour-c3-v22.sh`. **À T-60, utiliser `retour-c3-v23.sh`** pour tout retour.
  L'en-tête de ce script affiche encore « v2.2 », sans effet.
- `CANON_REMIS` n'est pas mis à 1 dans la branche « canon absent » : seul le message final est
  inexact ; le contrôle après restauration fait foi.
- Un hôte NONMOD est vérifié sans son service L2 : le relire à la main en cas de retour partiel.
- P6 : `clear_stale` est appelé dans `back()` **après** `bascule.py retour`. Un `.p6.tmp` étranger
  ferait refuser après le retour du venv : dans ce cas, appliquer la commande `stat` donnée par le refus.
