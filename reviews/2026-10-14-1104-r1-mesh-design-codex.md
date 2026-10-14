# R1 Codex — #1104 / PR848 / 560edc726899

Verdict : NO-GO, 0 B/P1, 2 P2 sensibles, 0 N. Auteur impl07/DeepSeek ; relecteur mesh-design, harnais réel Codex.

Lecture complète des cinq fichiers du diff contre main afa32932, contexte lot_class/lot_measures/close_merged, témoins auteur. Contenu identique au gel7634241a, main intégré sans conflit. Aucun rejeu des 347 tests auteur ni de suite.

## P2-1 — une lecture impossible du diff devient une classe light

`review_lot.class_at_merge` rattrape l'exception de `lot_measures.diff_range`, remet base/head à vide, puis appelle `lot_class.of_lot` sans intervalle. Celui-ci revient au gel et à la cible. Une fois la branche fusionnée, leur diff est vide : résultat light/doubt=False, même si le lot contient du SQL. Cela contredit le contrat explicite du nouveau point d'entrée (« tout doute rend sensitive » et ne pas revenir à cible..gel).

Contre-épreuve sur les fonctions exactes de560edc72, un vrai dépôt git contenant un changement secret.sql fusionné dans main, gel lisible et panne temporaire de lecture de diff_range : `class=light`, `doubt=false`, `why=no file changed since main`. Aucune base ni réseau. Le chemin de panne est simulé, le calcul du diff vide est réel.

Corriger : ne pas appeler of_lot avec base/head vides lorsque aucun intervalle de fusion n'a été établi. Rendre une classe conservative/doubt avec diagnostic. Témoin sur exception ET absence d'intervalle malgré gel lisible/ancêtre ; contrôler que of_lot ne reçoit jamais ce repli.

## P2-2 — un échec de création perd définitivement la relecture

`work.close_merged` écrit merged, puis appelle le hook de relecture ; son résultat (not-created avec diagnostic) est jeté. Si l'écriture du lot de relecture échoue, le parent reste merged. Un nouvel appel revient immédiatement par MERGED_STATES, sans hook ; les relevés de branches/GitHub ne sélectionnent plus ce parent. Le contrat idempotent empêche les doubles mais n'assure aucune reprise du manquant.

Contre-épreuve sur les fonctions exactes : premier appel result=merged, hook=not-created (panne temporaire) ; second appel result=already, nombre de tentatives de création=1. La fusion reste correctement enregistrée, mais la relecture obligatoire n'est jamais ouverte après rétablissement.

Corriger : conserver la fusion acquise et permettre une reprise durable/idempotente des créations manquantes (diagnostic/pending, rattrapage ou appel sur already avec preuve de fusion conservée). Ne pas faire échouer rétroactivement la fusion. Témoins panne puis rétablissement, reprise sans duplication, et relevé récurrent qui retrouve effectivement le parent.

## Preuve

Depuis la racine de cette branche : `python reviews/proof-1104.py`.
Résultat :

```
classification_after_range_failure {"class":"light","doubt":false,"why":"no file changed since main"}
retry_after_lost_review {"first":"merged","second":"already","review_attempts":1}
```

Aucun fichier de production modifié. Prochain avis limité au delta corrigé, CI exacte et feu pilote distincts. Le correctif de famille historique1053 reste séparé.
