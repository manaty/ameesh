# #499 — R1 Codex sur l’union 02d39819, 14 octobre 2026

Auteur du candidat : mty-amh-impl-08 (DeepSeek). Relecteur : auditeur, harnais Codex.
PR : https://github.com/manaty/ameesh/pull/666
Tête relue : 02d39819cf0bafb946ad63dec5971610aa18fc5a
Base : main 046a106a5c8d2a81a5bf717401f8a68e8dd2d4ac

**GO — 0 B/P1, 0 P2 nouveau, 0 N nouveau sur l’intégration.**

La demande porte sur les deux fusions de main depuis 89c4255a. La qualification repose sur la lecture actuelle du delta, des composants L361b constants (fil, interface, pilote Postgres, contrat, témoins et documentation), de la composition runner et du dispatcher/contrôle de portée de main. Elle ne fabrique pas un ancien verdict : le seul artefact ancien retrouvé, analyses/499-review-report.md, est marqué PARTIAL sur 59132c25. Aucun ancien GO Codex sur 89c4255a n’est utilisé comme preuve de ce GO courant.

## Conservation et composition

- Les 12 blobs hors runner sont identiques entre 89c4255a et 02d39819. Le patch complet des 13 fichiers garde le patch-id stable 32d8d4b7bd4baae44b6439150ae9cbf762130c75 ; aucune migration.
- Les parents sont exactement 3eb38d3f et main 046a106a. Leur merge-tree automatique reproduit tout l’arbre d48f1fec60df60f080422476f0e30a137f42fe65 ; aucune résolution manuelle cachée.
- Les AST de expire_threads_once, _start_thread_expiry_notice, start_housekeeping_poll et housekeeping_once sont identiques au candidat 89c4255a : connexion d’exécuteur réutilisée, réveil médié conservé même sans ménage local, throttle quotidien monotone conservé.
- Les AST de run, shutdown, _stale_restart_ready, sweep et canon_sync_once sont identiques à main 046a106a : gardes de code périmé, fin des tours avant remplacement, remise des baux et arrêt ne sont pas modifiés par le lot.

## Lecture du chemin L361b

L’hôte réécrit son fichier, puis transmet le census à threads.recount. Le dispatcher courant invoque l’opération sous la transaction de sa connexion ; le pilote prend le verrou de ligne et applique le census sans écraser un counted_until plus récent. Le host est forcé par le contrôle de portée H ; la nouvelle opération ne touche pas aux règles de main ni aux opérations lock/expired/indexed refusées. Les témoins distinguent connexion absente, dry-run, recompte perdu et refus d’un census ancien ; le témoin du throttle garde une horloge explicite. Pas de changement de ces comportements dans les deux unions relues.

Les observations déjà portées par la revue Claude historique (3589/3590 : schéma last libre, commentaire d’index trop absolu, connexion ouverte par le runner, commentaires) gardent leur portée et leur suivi ; ce contrôle d’union ne les retire pas et ne les présente pas comme nouvelles.

## Validation et limites

Diff-check propre. Vérifications indépendantes Git/AST consignées dans proof.json ; aucun test auteur ni suite complète rejoué, aucun dispatch CI, aucune modification du candidat. Les 71+71 tests sont la preuve annoncée par l’auteur, non une exécution du relecteur. Le GO ne certifie ni la CI, ni la famille ou la portée finale du second avis Claude, ni le feu de fusion. Cible PR encore 02d39819 lors du contrôle ; toute nouvelle tête demande qualification de son delta.
