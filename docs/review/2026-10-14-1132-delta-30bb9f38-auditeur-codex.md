# #1132 / PR873 — delta contrat superviseur, auditeur Codex, 14 octobre 2026

**GO du delta, 0 B / 0 N nouveaux**, sur
`30bb9f38961bc11a4a7bfe4ac8af6eb5cf73bd60` depuis
`35fa99950463029afbbd419216d2f70e749d4093`. Mandat : impl05, courrier
09:15 CEST adressé au pilote, avis delta Codex demandé à codex3/auditeur.
Auteur DeepSeek, avis rendu sous harnais réel Codex. Les avis Codex du binôme
ne sont pas deux familles ; la R2 Claude sur le gel final reste distincte.

Le seul fichier changé est `deploy/sql/role-superviseur.sql` : les colonnes
`produced_at` et `source` de `quota_gauge_readings` entrent dans le contrat de
lecture. La migration0434 les créait déjà ; le test de classement exige que
chaque colonne soit admise ou exclue. produced_at est un instant de production,
source est le nom de l'agent qui a produit le relevé (`cost.py`), donc des
métadonnées d'état comme observed_at/account/harness déjà admis. Aucun contenu
du flux de quota ni reçu ou secret n'entre dans le contrat.

Comparaison indépendante : hors de cette entrée, les bytes du contrat sont
identiques ; `_contrat-superviseur.sql` est inchangé et continue d'accorder
SELECT sur les seules colonnes nommées, sans privilège d'écriture, puis de
vérifier les droits effectifs en transaction. Le lecteur, le choix de compte,
la datation/déduplication et les gardes de budget du gel35fa9995 sont constants.
Le JSON voisin contient la tête, le blob et ces contrôles.

SuperviseurRoleTest15 et test_113222 sont les preuves **auteur** annoncées ;
aucun test ni SQL rejoué par l'auditeur. PR873 OPEN sur la tête exacte vérifiée
à réception. Ce GO complète la R1 sur ce delta d'une ligne ; il ne certifie pas
la CI de cette tête ni une composition future après1078/rebase. R2 Claude
indépendante, intégration1078, qualification de la tête finale, CI exacte et
feu pilote restent nécessaires. Aucune application du script de droits,
aucune fusion ou activation par cette revue.
