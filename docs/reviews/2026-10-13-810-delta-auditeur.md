# #810 — R1 delta, Codex

Demande mty-amh-impl-07, 13/10/2026 18:05 Paris, PR #663.
Tête `d70c8b12f984`, delta depuis `000799a3`.

GO, 0 B / 0 N sur le delta. Seul deploy/sql/role-superviseur.sql change : douze colonnes ajoutées au contrat SELECT de agent_mesh_overview. Comparaison automatisée des listes et lecture de leurs définitions : les douze sont toutes déjà dans le contrat agent_registry du parent, aucune colonne existante retirée, aucun contenu de consigne/courrier ajouté. 0428 expose r.* et les champs correspondants ; le correctif qualifie cette exposition avec les mêmes droits que la table.

Le mécanisme _contrat-superviseur.sql reste inchangé : droits remis à zéro, SELECT par colonne, audit des droits effectifs/absence d'écriture. Aucune autorisation de table entière ni fonction modifiée. Le B de contrat CI annoncé est fermé dans le code ; les preuves psql/psycopg sont celles de l'auteur, sans rejeu de suite (canon §3). CI exacte et nouveau feu mesh-design, après deuxième avis d'autre famille, restent nécessaires.
