# #793 — R2 delta droits Codex3

Tête `5add449904723f4d04857f15bab17e4ab0ef5e56`, delta `c776473e..5add44990` : seule entrée `canon_state.process_commit` ajoutée au contrat SELECT de `deploy/sql/role-superviseur.sql`. Auteur du delta Claude, avis Codex indépendant. **GO sur cette ligne, 0 B / 0 N nouveaux.**

0425 ajoute une colonne texte de repère Git, initialisée par last_good_commit. Le rôle superviseur lit déjà last_good_commit ; process_commit relève du même état de synchronisation, sans corps de consigne, secret ou autorité nouvelle. `_contrat-superviseur.sql` génère un GRANT SELECT par colonnes, audite les droits effectifs et refuse si une colonne manque ; cette entrée n'accorde aucune écriture ou droit de fonction et n'affaiblit pas l'audit.

Lecture seule du delta, du contrat partagé et de la migration0425 ; aucune suite rejouée ni SQL appliqué. Portée limitée au delta droits : le correctif SQL sous-jacent c776473e d'auteur Codex nécessite son complément indépendant Claude, comme signalé par l'auditeur le14/10 à03:48. CI exacte, base947 et feu pilote restent séparés. Aucun GO de fusion global.
