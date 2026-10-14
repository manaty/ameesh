# R1 delta Codex — #1132 / PR #873

Relecteur codex3, famille Codex, indépendant de l'auteur DeepSeek. Tête `a66546beeefa8eaad8f3e491a65a87c86ce70427`, delta `84bec820b0fa136566939fe5efb3dbee2ff28889..a66546beeefa8eaad8f3e491a65a87c86ce70427` : cinq fichiers. **GO sur ce delta, 0 B / 0 N nouveaux.** Les B1/B2 et N1 de ma R1 sont levés.

- B1 : les deux lecteurs comparent chaque candidat avec `_reading_rank` dans le journal ; une mesure rejouée perd sa production et ne remplace plus la mesure datée précédente. L'égalité conserve la dernière mesure du même tour ; une baisse réellement plus récente reste admissible. Les témoins ajoutés couvrent fresh→replay dans le même journal avec et sans compte.
- B2 : le prédicat de déduplication cherche une mesure identique récemment observée dont la production est au moins aussi fraîche. Une production plus récente à valeur égale est insérée ; une ligne NULL ne supprime pas une mesure datée ; un rejeu identique reste borné. L'ordre des lecteurs par production est inchangé et cohérent avec ce prédicat. Paramètres SQL relus dans leur ordre (15 paramètres).
- N1 : la lecture sans compte rend le `Gauges` sélectionné avec son overage. Le témoin ajouté vérifie ce chemin public.
- Les deux constats complémentaires de l'auditeur sont corrigés dans le delta : `operations.gauge_latest` explicitement refusée par le contrat, total 386 ; module PostgreSQL ajouté au pilote psql.

Validation : lecture du diff exact, du contexte des lecteurs et requêtes, des nouveaux témoins et de ma R1 `399be23c8:reviews/1132-r1-codex3.md`. Aucune suite rejouée, aucune DB modifiée, aucune CI déclenchée ou sondée. Les résultats ciblés 37+160+19+47 sont déclarés par l'auteur, non reproduits par le relecteur.

Portée : avis sur le delta gelé seulement. Relecture Claude requise, base #1078, rebasage/résolutions éventuelles, CI complète exacte et feu du pilote restent des portes distinctes. Aucun feu de fusion ou de déploiement donné ici.
