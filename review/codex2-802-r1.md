# #802 / PR #618 — R1 codex2 : NO GO, 1 B / 0 N

2026-10-13, 11:09 Europe/Paris. Tête exacte relue :
`47a306f1acd7ee50009b8b8b9a6c365fa58f575d`, `lot/802-canon-sync-concurrent`.
Deux fichiers : verrou dans canon_sync.py et témoin concurrency.
Aucune suite rejouée ; analyse des deux pilotes, des écrivains et du test.

## B1 — témoin de délai non portable au pilote psql, absent de sa part CI

`tests/test_802_canon_sync_concurrency.py:66` pose statement_timeout=6000
avec db.execute avant sync_packages. `PsqlDriver.execute` (db.py:573)
lance un sous-processus/une connexion qui se ferme après l'instruction ;
transaction() ouvre ensuite un autre psql persistant. Le SET de session ne
s'y applique pas : le délai effectif vient de Config.statement_timeout_ms
(30000 ms par défaut). Le témoin annoncé à 6 s ne démontre donc pas ce
contrat avec psql, même si son assertion d'un seul écrivain peut détecter
la course autrement. Psycopg conserve, lui, la connexion et le SET.

Le module est également absent de tests/parts/pilote-psql.txt. Le
CONTRIBUTING.md de cette tête exige qu'un test de couche base ou propre
à un pilote soit ajouté à cette part, afin de tourner sur les deux pilotes
sur PR. Il ne faut pas attendre la suite main pour découvrir cette divergence.

Correction attendue : configurer le délai sur la connexion réellement
utilisée (Config remplacée avant connect, ou SET LOCAL dans la transaction
qui contient sync_packages), attester SHOW statement_timeout dans cette
session pour les deux pilotes ; inscrire le module dans la part psql.
Le défaut porte sur la validation du correctif SQL/concurrence ; sur ameesh,
un P2 sensible est encore bloquant (canon processus/lots.md §2).

## Portée réelle de la clé

Le verrou est pris avant la lecture de before, dans la transaction qui fait
upsert/retire et refresh_package_parents. Dans les deux pilotes, ses requêtes
utilisent la même session jusqu'au commit/rollback. Schéma + ident effectif
sérialise les sync_packages de ce canon entre hôtes également : pas de host
ni commit dans la clé, donc aucun contournement pour la course ciblée.

**Il ne couvre pas tous les écrivains de work_packages.**
- storage/postgres/canon.py:152 et :163 : rebase_default réétiquette canon,
  appelé depuis adopt_default, sous verrou authentificateurs/canon_state,
  sans le nouveau verrou.
- storage/postgres/roadmap.py:141 : plan_package écrit les planned_* sous
  verrou de ligne SQL seulement ; sync_packages ne réécrit pas ces colonnes.
- Deux canons d'identités distinctes ont des verrous distincts alors que
  les identifiants de paquet sont globaux (ON CONFLICT id). Leur course
  d'attribution/reprise n'est pas résolue par #802 ; elle préexiste.

Ce sont les limites du correctif ciblé, pas de nouveaux blocages ajoutés
à ce lot. Ne pas présenter le verrou comme une exclusion générale de table.
Aucun nouveau défaut du verrou identifié dans la course même canon/schéma.

## Preuves et portes

Le 13/10 au contrôle API : aucun check-run/status publié sur 47a306f1
(PR #618, status pending sans statuses). La CI n'est pas prouvée verte.
Les 129 tests ciblés et le rouge/vert annoncés restent les preuves de
l'auteur ; aucun résultat personnel d'exécution revendiqué. Diff-check propre.
Deux relectures d'une autre famille et CI verte restent nécessaires.
Aucune fusion ni déploiement effectué.
