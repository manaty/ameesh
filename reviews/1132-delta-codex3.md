# R1 delta Codex — #1132 / PR #873

Relecteur codex3, famille Codex, indépendant de l'auteur DeepSeek. Tête `a66546beeefa8eaad8f3e491a65a87c86ce70427`, delta `84bec820b0fa136566939fe5efb3dbee2ff28889..a66546beeefa8eaad8f3e491a65a87c86ce70427` : cinq fichiers. **GO initial retiré : BLOQUÉ sur une régression d'égalité de production signalée par l'auditeur le 14/10 à 03:32.** Les témoins B1/B2/N1 initiaux sont corrigés, mais le nouveau B3 empêche le GO de cette tête.

## B3 — égalité de production : 87→67→87 conserve 67 en historique

Plusieurs événements d'un même tour partagent le `produced_at` du marqueur. Pour trois insertions A(87), B(67), C(87) de même production et reset, A et B existent ; C est supprimée par `NOT EXISTS` parce que A porte déjà 87 avec production `>=` C. `gauge_latest` départage A/B par insertion et rend B(67), tandis que le lecteur du journal choisit C(87) à rang égal. La garde distante peut donc être moins prudente que la mesure locale. Confirmation par lecture du SQL exact ; l'auditeur a exécuté sa preuve SQL sur CTE VALUES en lecture seule. Je n'ai pas rejoué cette preuve. L'égalité doit préserver le départage par ordre d'observation ; une ancienne valeur identique à production égale ne doit pas masquer une valeur différente intercalée.

L'avis initial `4ccadfc11` a manqué ce cas ; il est remplacé par ce rapport et le nouveau jalon bloqué. L'auditeur travaille en binôme avec impl05 sur le correctif unique.

- B1 : les deux lecteurs comparent chaque candidat avec `_reading_rank` dans le journal ; une mesure rejouée perd sa production et ne remplace plus la mesure datée précédente. L'égalité conserve la dernière mesure du même tour ; une baisse réellement plus récente reste admissible. Les témoins ajoutés couvrent fresh→replay dans le même journal avec et sans compte.
- B2 : le prédicat de déduplication cherche une mesure identique récemment observée dont la production est au moins aussi fraîche. Une production plus récente à valeur égale est insérée ; une ligne NULL ne supprime pas une mesure datée ; un rejeu identique reste borné. L'ordre des lecteurs par production est inchangé et cohérent avec ce prédicat. Paramètres SQL relus dans leur ordre (15 paramètres).
- N1 : la lecture sans compte rend le `Gauges` sélectionné avec son overage. Le témoin ajouté vérifie ce chemin public.
- Les deux constats complémentaires de l'auditeur sont corrigés dans le delta : `operations.gauge_latest` explicitement refusée par le contrat, total 386 ; module PostgreSQL ajouté au pilote psql.

Validation : lecture du diff exact, du contexte des lecteurs et requêtes, des nouveaux témoins et de ma R1 `399be23c8:reviews/1132-r1-codex3.md`. Aucune suite rejouée, aucune DB modifiée, aucune CI déclenchée ou sondée. Les résultats ciblés 37+160+19+47 sont déclarés par l'auteur, non reproduits par le relecteur.

Portée : avis sur le delta gelé seulement. Relecture Claude requise, base #1078, rebasage/résolutions éventuelles, CI complète exacte et feu du pilote restent des portes distinctes. Aucun feu de fusion ou de déploiement donné ici.
