# #1110 / PR845 — R1 Codex du delta d’union

Auditeur, harnais réel Codex, indépendant de l’auteur ameesh-dev5 (DeepSeek). Demande mesh-design du 14/10/2026, courrier 34629 et note du lot 15775 ; artefact auteur annoncé par courrier 12:54, note 15813. Canon relu à `manaty/home@ccaca9e42596e87703a8abc413f62945ffe2587b`, §§2, 3, 11 et 12.

**GO delta, 0 B / 0 P2 / 0 N nouveau**, sur l’union `ff4b5d4041958b34d2f7d9fac24d0bb8e74b044b`, arbre `4976b8f07429d85d61e0b218386f878f3d67bb94`. Parents exacts : gel PR845 `c927fec5af65fa1ea6d8bdf46c876b3ae662cac0` et main `6eeecc8e914efb391c9e4be67058ae83519d8bd7`.

La qualification porte sur les trois fichiers combinés et sur la conservation des neuf autres blobs du lot. Les R1 antérieures 3341 et 3409 gardent leur portée ; leurs preuves ne sont pas réexécutées.

- `deploy/sql/role-superviseur.sql` : depuis le gel, seul l’ajout main de `agents.roles` est intégré ; depuis main, seul l’ajout du lot `ci_runs.workflow` subsiste. Les tableaux de colonnes restent valides ; aucune colonne de contenu, écriture, délégation ou logique du contrat transactionnel n’est ajoutée par cette combinaison. L’appel à `_contrat-superviseur.sql` et le COMMIT sont conservés. La migration 0433 est identique au gel.
- `tests/parts/pilote-psql.txt` : union exacte des ensembles des deux parents, 45 modules, tri alphabétique et unicité vérifiés. `test_l161c_failures_realtime` et `test_1037_bascule_harnais` sont présents chacun une fois, et leurs fichiers existent dans l’union. Aucun retrait de module.
- `docs/EXPLOITATION.md` : les passages du lot sur la sélection des workflows, le rattrapage borné, les verdicts, le rouge hérité par workflow et la migration 0433 sont conservés dans la documentation de main. L’insertion main « Plafond du job (#899) » précède le passage du suivi CI sans en modifier le sens. Les autres apports documentaires de main restent présents. Le diff depuis main dans ce fichier contient seulement les passages du lot.

Vérifications indépendantes : `merge-tree --write-tree` rc0 et arbre identique ; compte des 12 blobs (9 constants / 3 combinés) ; les six SHA256 du paquet auteur concordent ; `git diff --check` propre. Les fichiers de collecte, stockage, exploitation, contrat JSON et tests du lot, hors manifeste, sont identiques au gel c927. Aucune suite, commande SQL, CI ou branche produit modifiée.

Ce verdict qualifie le contenu de l’union, pas son exécution. La CI 38489198233 attempt2 sur l’arbre testé d331 a été arbitrée par le pilote (note 15775) ; ce rapport ne la transforme pas en preuve d’exécution de 4976b8f. La R2 Claude réelle doit couvrir ce contexte final, puis le pilote doit donner son feu nominatif avec les vérifications de fraîcheur et la qualification CI applicables. La PR reste sur c927 ; aucun feu de fusion, déploiement ou activation n’est donné ici.
