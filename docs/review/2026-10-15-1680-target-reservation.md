# #1680 — réservation de la cible pendant la CI de paquet

Source : demande orchestrateur du 15/10 15:46, sous-lot 2 de #1607 ; propriétaire codex3. Code ameesh, rattachement fonctionnel Nexlink conservé. Préparation sur `review/codex3-1680-packet-reservation`, base main `7dea502cd9093690befdd4e7ea176588f2e63f34`. Aucun profil ni train activé.

Le train #1467 conserve le bail `merge_locks` pendant la CI mais ne le renouvelle qu'aux passages. Son auteur a confirmé le 15/10 15:52 sur f63d1b37 : TTL 1800 s, aucune maintenance entre événements, aucune réservation de CI extérieure au train. #1374 reprend ce même bail pour le feu direct ; `merge_pr` n'en prenait aucun. Aucun second ordonnanceur ou table de réservation n'est ajouté.

## Port fourni à #1467

`packet_reservation.begin(db, project, target, *, owner, epoch, branch, base, head, ci_contract, deadline, now, ttl_s=None)` adopte l'époque VIVANTE déjà acquise avant composition, conserve son propriétaire `train:...` et la renouvelle avant publication. La publication peut déclencher une CI push : ne pas attendre le dispatch. Le ticket contient la branche integration, base, tête, empreinte du contrat CI, clé/propriétaire/époque et limite absolue explicite du fournisseur qualifié.

`Ticket.to_json/from_json` permet de conserver le ticket dans l'état JSON `train_save` existant ; aucun schéma SQL nouveau. `next_renew(ticket)` donne une échéance de maintenance, à traiter sur une connexion ScopedDb fraîche : elle ne doit appeler ni Engine.step ni le lecteur CI/verdict. `renew(db, ticket, now=...)` ne fait qu'une mise à jour SQL de bail, sans acquisition ni repli sur une nouvelle époque. Le TTL est borné par la limite absolue ; un run mort ou une réponse dispatch perdue ne réserve donc pas indéfiniment la cible.

`observe(db, ticket, now=..., read=...)` ne prend pas un payload d'événement comme preuve : le port `read(Ticket)` doit relire le contrat CI complet, les dernières tentatives exactes ou le reçu d'atterrissage et sa relecture distante. Il renvoie `branch`, `base`, `head`, `ci_contract`, `kind` (pending/passed/failed/landed), `complete=True` pour les trois derniers. Une preuve partielle ou d'une autre tête/base/branche/contrat ne libère rien. Pending et passed conservent le ticket ; failed et landed libèrent l'époque exacte. Une CI verte garde la cible jusqu'au push/reconcile prouvé.

Raccord convenu avec ameesh-cx3, courriers 15:56 et 16:01 : il prend le ticket et la cadence de maintien dans Ledger/Service ; ce lot ne modifie aucun train*. Le fournisseur #1467 n'a actuellement aucune limite absolue qualifiée : elle reste une entrée obligatoire, sans durée métier choisie par ce lot. Une intention de dispatch incertaine garde le bail jusqu'à réconciliation ou limite, sans second dispatch automatique.

## Époque expirée et écrivain merge-pr

`land_lock(epoch=...)` refuse désormais une époque expirée dans la même instruction SQL. L'ancien UPDATE pouvait la ranimer. La reprise sans époque reste une opération distincte et rend une nouvelle époque ; une maintenance ne s'y replie jamais.

`merge_pr.direct_merge` prend le bail de la cible admise avant la construction locale, le renouvelle immédiatement avant le push et le rend en finally. Les contrôles existants d'admission, de tête et de compare-and-swap distant restent exécutés. Un bail de paquet tenu refuse cette voie avant fetch/construction ; une perte d'époque refuse le push. Le projet provient de la configuration radar, ou à défaut de l'app explicitement liée au lot admis ; aucune clé arbitraire de dépôt n'est inventée.

## Composition et limites d'intégration

Dans main avant #1374, `land.lock_key` utilise la cible brute ; #1374 c975eb0c / tête 86ebebed l'unifie par alias. On appelle la fonction installée avec la cible exacte de l'admission et on conserve la clé réelle dans le ticket ; on ne change pas silencieusement une clé déjà tenue. La composition #1374/#1467/#1680 doit qualifier les alias et réconcilier les tickets d'un ancien espace de clés avant changement en exploitation. Source : impl-08, courrier 15/10 15:56. Aucun déploiement donné comme acquis.

Les CI manuelles doivent acquérir le même bail avant leur publication/dispatch et utiliser ce port. Les producteurs externes et le raccord Service restent à brancher avec #1467 : ce module seul ne revendique pas leur activation.

Un `gh pr merge` brut ou un git push indépendant ne consulte pas la base ameesh ; aucun contrôle serveur n'est prétendu. Source impl-08 15:56 : voie d'urgence hors réservation à documenter ; portée et voie imposée remontées au pilote. Le feu direct #1374, qui exécute gh sous le bail commun, est un consommateur couvert à la composition, distinct de ce binaire indépendant.

## Validation

Tests ciblés via `ameesh heavy -- scripts/test.sh tests.test_1680_packet_reservation tests.test_1003_merge_pr` : **62 tests verts avec chacun des deux pilotes psql et psycopg** (124 exécutions, PostgreSQL réel pour les dix témoins de réservation). Les témoins de réservation utilisent PostgreSQL réel, horloge de décision injectée et expiration SQL explicite, sans sommeil ni CI distante. Le banc purge la table indépendante merge_locks entre scénarios dans son seul schéma jetable. Round-trip du ticket JSON et refus des champs ajoutés/schéma manquant vérifiés séparément (trois cas). Vérification syntaxique et diff-check propres.

Relecture indépendante requise avant fusion (concurrence et garde SQL de bail). Aucun run distant, fusion, profil système, CI de lot ou activation effectué par codex3.
