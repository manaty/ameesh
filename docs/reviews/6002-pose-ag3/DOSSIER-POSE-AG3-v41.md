# #6002 — pose AG3 v4.1 : delta minimal de v4

Bureau (BG), 08/11 ~13:00Z. Branche `review/bureau-6002-tunnel`. Les versions v3 et v4 sont
conservées. **AUCUN GESTE** : relevés en lecture et essais à blanc uniquement.

Ce delta intègre :
- les décisions du pilote (#200777, partie (1)) ;
- l'avis B d'impl-03 (#200802) ;
- sans toucher à ce qu'impl-11 a validé (#200781).

Les fichiers v4 restent tels quels. v4.1 ajoute des fichiers suffixés `-v41`, diffables avec v4.
Aucun autre fichier de v4 n'est modifié.

## 1. Ce qui change (et seulement cela)

| Source | Point | v4.1 |
|---|---|---|
| Pilote O1 | Accès de vm-operator au venv | Le paquet `acl` est installé s'il manque. Puis `setfacl -m u:vm-operator:--x` (traversée seule, sans `r`, **sans ACL par défaut**) sur `/home/ameesh`, `.local`, `.local/share` et `.local/share/ameesh`. L'option « aucun » disparaît : l'ACL est le seul chemin, la pose la fait toujours (impl-03 : défaut). États avant (getfacl, modes) gardés dans les reçus. **Preuves dans la pose** : `ls /home/ameesh` refusé pour vm-operator ; `venv-courant/bin/python -I` exécuté par vm-operator |
| Pilote O1 | Un nouveau venv reste lisible | Test B : sous ameesh, umask 022, création d'un `venv-essai6002-<pid>` en 755. Il doit être lisible et traversable par vm-operator sans autre geste, puis il est retiré. Les ACL portent sur les 4 parents seulement ; un nouveau `venv-<sha>` 755 est lisible par « autres » |
| Pilote O1 | Retour | Retire **exactement** `u:vm-operator` des 4 parents. Exige ensuite `getfacl` = état avant et modes = état avant. Retire le paquet `acl` seulement si la pose l'a installé |
| Pilote O4 | Carte producteur figée | `verifier-carte-producteur-AG3.sh`, en lecture seule, compare la carte aux unités runners actives. En cas d'écart : « **ROUGE carte producteur à mettre à jour, geste root** (fiche AG3) ». Il est appelé par le test B. La procédure est rappelée en tête du script et ci-dessous (§3) pour la fiche AG3. Une carte dynamique, d'après le placement en base, part au panier (pilote) |
| Pilote O5, impl-03 B2 | `quiesce` et `runners` | **Correction de v4.** À M, `Native.control` refuse (rc **1**, pas 75) toute porte qui n'est pas une vague fermée `draining`/`returning`, et AG3 n'a **aucune** ligne `host_lifecycle`. Le test B, sans changer le code de M, atteste donc trois choses : `quiesce` et `runners` rendent rc 1 ; l'absence de ligne est lue sous vm_probe (`porte_ag3\|0`) ; `probe-receipt` d'un nonce inconnu rend **75**, ce qui prouve la connexion vm_probe et la lecture des cartes. Un 0 ou un 75 ne viendra qu'avec la première vague (consigné) |
| impl-03 B1 | Argv de santé = lien | `vm-remote.py` (M) refuse un argv dont un élément est un lien, et `/usr/bin/python3` est un lien 0777 : `version-mode` et `doctor` auraient rendu 1. `remote.json` inscrit donc `/usr/bin/python3.12`, dont la pose vérifie qu'il est un fichier régulier root 0755 (relevé AG3 du 12:58Z : régulier, 755). **Écart nommé avec le modèle de M** (`enrollment.example.json` donne `/usr/bin/python3`, que son propre contrôle refuse) : à signaler pour un lot produit |
| Pilote O6, lot #6321 | Périmètre | **Pose inactive et rôle vm_probe seulement.** L'activation attend #6321 : clé cloud de l'adaptateur, inscription de la cible côté contrôleur (`cible-ameesh-agents-3.v2.json` reste non installée) et cycles réels. Motif : ameesh dans docker rootful sur AG3 peut lire les secrets de vm-operator |
| impl-03 N | Pièces résiduelles | Plusieurs corrections : `trap` sur mesh-1 et AG3 pour qu'aucun vérificateur ne reste en cas d'échec avant `shred` (`pose-DB-vm_probe-v41.sh`) ; écriture complète du passfile (`secret-vm-probe-v41.py`, boucle `os.write`) ; `env` d'ameesh exigé en `ameesh:600` ; `set -u` respecté et reçu illisible = échec (tests) ; retour qui arrête et retire **séparément** service et minuteur de santé |
| impl-03 N | Pas de journal pour `health.py` | Non traité : `StandardOutput/StandardError=null` vient de l'unité de M (#6296). Un drop-in opérateur contredirait le modèle relu ; c'est au pilote de décider (lot produit) |

## 2. Ordre de pose (inchangé, scripts v4.1)

1. `pose-AG3-vm-operator-v41.sh --apply` (sans option).
2. `pose-DB-vm_probe-v41.sh --apply <TS>`.
3. `tests-AG3-vm-operator-v41.sh B`.

Pour le retour : `retour-AG3-vm-operator-v41.sh --apply <TS>`, puis `retour-DB-vm_probe.sh --apply` (v4, inchangé).

Feu de pose inactive : après les avis d'impl-11 et d'impl-03 sur v4.1 (pilote).

## 3. Texte pour la fiche AG3 (O4, procédure courte)

> **Reçu de santé #6296 rouge : « carte producteur à mettre à jour, geste root ».** La carte
> `/etc/ameesh-vm-adapter/health-producer.json` (root:root 0644) liste exhaustivement les unités
> runners permanentes d'ameesh sur AG3. Après tout changement de placement, root la réécrit, et ne
> change que la liste `units` (valeur = `systemctl --user -M ameesh@ list-units
> 'ameesh-runner-agent@*' --state=active`). On attend ensuite un reçu frais (< 70 s), puis
> `verifier-carte-producteur-AG3.sh` doit être OK. Aucun autre fichier n'est touché.

## 4. Pièces v4.1 (`SHA256SUMS-v41`)

`DOSSIER-POSE-AG3-v41.md`, `pose-AG3-vm-operator-v41.sh`, `pose-DB-vm_probe-v41.sh`,
`secret-vm-probe-v41.py`, `tests-AG3-vm-operator-v41.sh`, `retour-AG3-vm-operator-v41.sh`,
`verifier-carte-producteur-AG3.sh`, `releve-v41-blancs-*.txt`.

Blancs du 12:58Z : préalables AG3 OK (`python3.12` régulier, `env` en ameesh:600), préalables de
la base OK, retour à blanc.
