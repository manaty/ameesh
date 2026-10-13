# R2 delta #1022 / PR #785 — codex3 (Codex), 2026-10-13

**GO — 0 B / 0 N nouveau / 0 n** sur `b6eaa0c89a7d3e4cdfb51352f7102529e4988f03`. Auteur mty-amh-impl-05 (DeepSeek). Delta relu `20c900d0..b6eaa0c8` : deux fichiers, 0420 et test_1022_creneau_landed. Aucun rejeu, migration appliquée ou geste prod ; diff --check propre.

La règle #1022 est conservée : migration NULL ne modifie que le créneau default encore reserved ; merged/promoted le lande, les autres états le libèrent. Une fusion suivie de NULL garde le landed posé par la première instruction. Les quatre témoins précédents demeurent inchangés.

La branche non-NULL porte désormais la règle #879 : elle UPDATE la ligne existante au lieu de tenter un INSERT reserved avant ON CONFLICT. Le statut landed est préservé si le numéro est identique et que le statut calculé serait reserved. Un numéro différent suit le statut calculé ; il n'est donc pas gelé par le landed de l'ancien numéro. Sans ligne existante, INSERT suit le statut calculé. Les contraintes et gardes de réservation de 0419 demeurent en place ; 0419 n'est pas modifiée par ce delta.

Les deux témoins ajoutés sont discriminants : merged puis blocked avec numéro inchangé doit garder landed ; merged puis blocked avec numéro 401 doit donner reserved. Ils s'ajoutent aux quatre témoins fusion/NULL/libération. Aucune preuve d'exécution n'est produite par cette relecture statique.

L'arbitrage mesh-design du 13/10 21:35:23, fil #879, place la définition union dans 0420 et retire la modification de 0419 de #879, avec ordre #711 -> #1022 -> #879. Le delta réalise la partie #1022 de cet arbitrage ; il ne statue pas sur les corrections Python de #879. Le N1/P3 préexistant « close après merged->blocked libère un landed », déjà relevé par l'auditeur dans le fil #879 le 13/10 21:19:50, demeure hors du delta demandé et ne devient pas un bloquant ni un nouveau constat du présent avis.

Avis de code sur cette tête uniquement. Parent #711, autre avis Claude indépendant, CI exacte verte sans part annulée et feu du pilote restent requis avant fusion. Aucun sondage de CI.
