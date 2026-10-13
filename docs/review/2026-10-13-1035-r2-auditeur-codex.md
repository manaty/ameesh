# #1035 t2 / PR #817 — delta R1, auditeur Codex

13/10/2026, harnais réellement exécuté : Codex.

**GO contenu, 0 B / 0 N nouveau**, sur
`5b018976942afe33aedf1de15b0f59d96c7b3cc1`.
Les deux B/P2 sensible de la R1 sont fermés. Portée : delta depuis
`ef6a717dd99ea8cbdebaf1cc279b1d95e2e35151`, sans nouvelle revue de t1.

Le nouveau commit a pour parent direct la tête R1. Le delta porte seulement
sur `src/ameesh/notify.py`, `tests/test_789_slo_alerts.py` et
`tests/parts/pilote-psql.txt`. Base t1 et autres blobs t2 inchangés.

## B1 fermé — livraison au propriétaire et urgence

`Notifier.run_pass` (:1036–1038) collecte les alertes portant la cible
`owner` même si leur type n'est pas dans `notify.types`. `Router.resolve`
(:844–851) les adresse à `notify.default_human` avant les responsables de
l'agent ou du lot. Le chemin existant `_raise` → `_attempt` → `Sender`
réalise ensuite la livraison sur les canaux configurés pour cet humain.

`_KEPT` conserve `critical` et `targets` : le rendu urgent et la résolution
ne perdent pas ces champs en passant par l'état. La suppression d'une alerte
owner (:1052–1058) suit aussi le chemin de résolution quand son type est hors
du filtre. `_brief_agents` (:1354–1355) transmet `alert.urgent` au payload,
sans rendre tous les SLO ordinaires urgents.

Les quatre nouveaux témoins lisent les sorties du passage Notifier : P1
hors filtre livré à `human:owner`, sortie du faux binaire `notify-send` avec
`--urgency=critical`, non-P1 filtré puis livré si configuré, payload du courrier
urgent pour P1 et ordinaire sinon, clés différentes aux niveaux 1 et 2. Ils
ne se limitent plus à vérifier les champs de l'alerte ou le texte du brief.
Le contrôle des canaux et l'absence éventuelle de destinataire restent ceux
du Notifier existant.

## B2 fermé — pilote psql

`tests/parts/pilote-psql.txt:19` contient désormais `test_789_slo_alerts`,
avec sa raison et à sa place alphabétique, conformément à CONTRIBUTING.
Le module PgTestCase est donc sélectionné par le pilote psql des PR.

## Validation et porte restante

Lecture du delta, du routage, de la persistance, du rendu et de la livraison,
ainsi que des quatre témoins ; `git diff --check` propre. Aucune suite rejouée
(canon §3). L'auteur annonce 13/13 psql et psycopg, l38 29/29 et l116/l63/l148
70/70 ; ces exécutions ne sont pas celles du relecteur.

Tête PR #817 vérifiée à `5b0189769`. La consultation ponctuelle ne montre
aucun check attaché dans `statusCheckRollup` ; aucun vert CI n'est attesté.
Restent l'avis **Claude** sur cette tête, la CI exacte réellement verte sans
part annulée, la fusion de **PR ameesh #603** (t1) et le feu du pilote.
Le bureau sous Codex ne fournit pas la deuxième famille. Aucun GO
d'activation ou de déploiement par cet avis.
