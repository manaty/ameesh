# #901 — clôture B1 en binôme

Tête vérifiée : `d2a76ba0f1a679520ed2ccda9ebb482f5800f743`.
Delta depuis le gel R2 `642fd308c94eae2dbdc52e5d227a0a8b13d40db7`.
**B1 levé ; GO du delta, 0 B / 0 N.**

La publication marque chaque ligne d'échec avec GITHUB_RUN_ATTEMPT, y compris
le chemin close/création du check. Le lecteur conserve la provenance source,
déduplique par tentative et n'émet que les nouveaux échecs de la tentative
courante. Sans marque, une ligne reste tentative 1. L'upsert ne remplace une
ligne que par une tentative strictement supérieure : un ancien événement ne
peut faire reculer le témoin courant. Le contrat d'interface est aligné.

Le témoin de l'auteur garde désormais le check ancien lors de la relance et
couvre les parts non rejouées par rerun --failed. Preuves annoncées par l'auteur :
31 tests verts par pilote psql et psycopg ; suites non rejouées par le relecteur.

Vérification indépendante ciblée : ancien probe B1 ne reproduit plus ;
`PYTHONPATH=src python reviews/901-b1-delta-probe.py` PASS. Ce dernier appelle
réellement le publisher et `_failures` : une ligne ancienne sans marque reste
attempt=1 sans notification ; un échec courant est publié et relevé attempt=2,
même lorsque les deux checks du même nom coexistent. Diff-check PASS.

CI exacte au constat : run 38420637671, headSha=d2a76ba0f1a679520ed2ccda9ebb482f5800f743,
status=pending, conclusion vide. Aucun vert revendiqué : la fusion attend les
portes de relecture et le vert exact, ainsi que le feu du pilote.

https://github.com/manaty/ameesh/pull/703
https://github.com/manaty/ameesh/actions/runs/38420637671
