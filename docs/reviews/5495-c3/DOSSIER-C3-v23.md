# #5495 — dossier C3 v2.3 : deux N d'impl-03 seulement (décision mesh-design #200888)

Bureau (BG), 08/11. **Rien d'autre ne change** par rapport à v2.2 (quorum OK, gelée). AUCUN GESTE.

## N1 — retour : état gardé de la minuterie release vide ou inconnu (`retour-c3-v23.sh`)

**Défaut v2.2 (l. 66).** `wc -l = 2` acceptait une 2e ligne vide. La restauration échouait alors
**après** l'arrêt de la minuterie release, qui restait arrêtée : la coupure suivante était manquée.

**v2.3, choix le plus sûr.** L'état à restaurer est décidé pour **tous** les hôtes modifiés **avant
le moindre geste** du retour, y compris avant la désactivation de la minuterie L2.
- État gardé `active` ou `inactive` : il est suivi. Si `active` : `start`, puis `ActiveState=active` exigé.
- État gardé absent, vide ou inconnu, et `UnitFileState=enabled` : **relance explicite vérifiée**.
  Une minuterie activée est l'état nominal d'un candidat ; la laisser arrêtée ferait manquer la
  coupure, la relancer ne contredit aucune décision.
- État gardé inconnu, et `UnitFileState` autre qu'`enabled` ou illisible : **STOP avant tout
  arrêt**, avec un diagnostic et les commandes. Une minuterie désactivée ou masquée peut l'être
  volontairement ; le retour ne la démarre jamais, et ne la laisse pas non plus arrêtée par sa faute.

**Témoin** `temoin-v23.sh` (faux ssh), 5/5 :
- vide + enabled → relance vérifiée ;
- vide + disabled → STOP sans aucun GESTE ;
- inconnu + illisible → STOP sans geste ;
- `inactive` → suivi ;
- `active` → relance.

Diff : `retour-c3-v22-v23.diff`.

## N2 — installateur : `.p6.tmp` résiduel (`p6/update-l2-runtime-v32.py`)

**Défaut v3.1.** Les temporaires `staging-executor.json.p6.tmp` (980) et
`staging-submit.json.p6.tmp` (root) sont créés en `O_EXCL`. Un résidu d'un échec antérieur
bloquait donc `--apply` **et** `--retour`.

**v3.2.** `clear_stale(tmp, uid)` est appelé juste avant chaque création. Il retire le résidu
**seulement** s'il remplit toutes ces conditions :
- c'est un fichier régulier (pas un lien) ;
- `nlink` vaut 1 ;
- il appartient au propriétaire attendu (980 ou root) ;
- son nom exact se termine en `.p6.tmp`.

Sinon : refus nommé, avec la commande d'examen (`stat …`), et rien n'est touché.

**Témoin** `p6/temoin-installateur-v32.py` (fonction chargée depuis les octets du fichier), 5/5 :
- absent → rien ;
- résidu conforme → retiré ;
- mauvais propriétaire → refus, fichier gardé ;
- lien symbolique → refus, rien suivi ;
- liens multiples → refus.

Diff : `p6/update-l2-runtime-v31-v32.diff` ; sha `e6526d40…`.

Les avis OK portent sur v3.1 (`f838ee6b`). v3.2 est ce delta de 47 lignes, à confirmer.
