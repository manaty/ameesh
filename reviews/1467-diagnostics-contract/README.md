# Port factuel des diagnostics de paquet

Source : qualification mesh-design du 15 octobre 2026 à 16:09, portée du
mandat #1606(1) dans #1467. #1616 fournit la règle de processus ; #1681
consomme le diagnostic après atterrissage. Le même contrat factuel sert aux
deux consommateurs, sans second moteur ni nouvelle CI diagnostique.

`train_diagnosis.unresolved(plan, ci)` rend `ameesh-train-diagnosis/1` pour
une CI complète rouge sur le commit et l'arbre exacts. Runtime conserve ce
rapport dans la preuve CI ; Engine le conserve dans son état avant de
signaler le rouge. En construction/bêta, aucun lot n'est accusé ni retiré, aucune CI n'est relancée
et aucun vert n'est déduit. Les deux résultats sont actuellement `unknown`.

Le sujet du rapport porte `source`, `target`, `base`, `head`, `tree`, puis
chaque workflow, run, tentative et job rouge observé. Un producteur factuel
qualifié doit compléter les données suivantes avant toute autre décision :

| Donnée | Liaison et preuve requises |
| --- | --- |
| Échecs de tests | Liste complète des tests en échec, avec workflow, run, tentative, job, module, identifiant du test et commande réellement exécutée ; aucun agrégat ne remplace cette liste. |
| Impact | Source versionnée de la carte, commit couvert, complétude du périmètre et liens test→code/données/tables ; les noms de fichiers semblables ne prouvent aucun lien. |
| Membres | `work_item_id`, génération, gel et `frozen_sha` du manifeste ; diff du membre depuis sa base de divergence, pour éviter d'attribuer le delta de la cible à ce lot. |
| Vert local | Reçu d'exécution produit par une source authentifiée, sur le même commit, arbre, test et commande que le rouge, avec environnement, résultat et provenance de l'exécution. Une note ou un champ libre n'est pas un reçu. |
| Culpabilité | Preuve causale explicite sur les lots et prérequis concernés. Un candidat unique dans une carte d'impact reste un candidat ; plusieurs candidats ne désignent aucun auteur fautif. |

Sources déjà présentes : `train_ci.GitHub.read` fournit les runs/tentatives
et jobs exacts ; `storage.ci.failures(repo, run_id)` fournit le flux des tests,
mais son seul identifiant de run ne prouve pas la tentative ; `scripts/map.py`
décrit la carte mesurée ameesh (`ameesh-carte/1`, blocs, données, dossiers,
tables et commit). Aucune carte Nexlink complète ni reçu local de paquet
n'est sélectionné ici. Leur absence reste indiquée dans `missing`.

Le commit final du train est multiparent : premier parent `plan.base`, puis
les têtes `frozen_sha` ordonnées. Il porte un trailer par lot, mais ses
merges intermédiaires ne sont pas publiés dans son ascendance. Le lecteur
après atterrissage doit utiliser le manifeste dont `plan.head` correspond
au commit observé et dont `source` correspond au dépôt. La clé de lecture
est `land.lock_key(project, target)` ; aucun consommateur ne reconstruit un
alias de cible. L'auteur vient de la provenance historique, pas du committer
du train. Les preuves membres sont sous `plan.proof.members`.

Quand ces preuves deviennent disponibles, le consommateur applique les
champs compilés `ci.environment_red`, `ci.faulty_lot` et
`merge.post_merge_red` du même canon exact. Le rapport courant `unknown`
n'autorise aucune exemption environnementale, exclusion ou attribution.
