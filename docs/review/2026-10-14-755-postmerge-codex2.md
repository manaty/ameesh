# #755 — revue légère après fusion : GO, 0 B / 1 N

codex2, famille Codex, 2026-10-14. Mandat mesh-design du 14/10 10:41 CEST.
Auteur impl04, famille DeepSeek selon mandat et lot755 ; revue exécutée sous Codex.
Merge exact : bca5be19a9012463c59f77f88362cfbad73c7b73, PR576,
tête981f1443b98a85174873b07c60a01cd3ce103592.

Diff du merge contre premier parent : ajout unique de
 tests/test_l755_workflow_timeouts.py (+115). Scanner/limites/fixtures lus.
Le scanner distingue bien le timeout de job (indentation4) de celui d'étape,
exige sa présence et contrôle <=60. La fixture vérifie absence, présence,
commentaire et nom cité contenant deux-points. Les quatre workflows du merge
sont .yml ; lecture du scanner exact extrait leurs 13 jobs, tous avec un
timeout numérique <=60 (maximum45). Aucune suite auteur ou CI rejouée.
CI20/20 run38495542028 : preuve de l'auteur dans755, non réexécutée.

N1/P2 non bloquant, suite #1279 assignée à impl04 : couverture YAML incomplète.
workflows() ne sélectionne que *.yml ; les fichiers .yaml sont invisibles.
_JOB reconnaît seulement une clé suivie de commentaire ou fin de ligne.
Sonde indépendante par extraction AST du scanner exact, sans lancer unittest :
 jobs:
  a:
    timeout-minutes: 10
  b: {runs-on: self-hosted}
renvoie {'a': 10} ; b sans timeout échappe donc au garde-fou.
Le sous-ensemble est documenté mais doit détecter/refuser les nouvelles formes
non supportées ou les prendre en charge. Aucun défaut sur les jobs actuels.

Verdict GO après fusion, 0B/1N. Parent755 conserve auteur et état merged.
Ce verdict ne vaut pas preuve de déploiement staging.
