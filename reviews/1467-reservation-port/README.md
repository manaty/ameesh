# Port de réservation pendant la CI

Contrat fournisseur #1680 : `a68f54790f7c3bdb1ed5088544bcf7c54e221b7e`
(module applicatif inchangé depuis `5fa41c7c`, correction de faux bail de test
acceptée par codex3 le 15 octobre à 16:44). Clé commune #1374 :
`86ebebedb87f9e0634fd7d2b3439edbd261a0a60`, fonction `land.lock_key`.

Le probe charge ces objets Git exacts, sans fusionner les branches. Il charge
le module `packet_reservation`, la méthode PostgreSQL `land_lock` du fournisseur
et la fonction `lock_key` du raccord #1374. Les autres méthodes du stockage
restent celles de #1467, notamment la persistance transactionnelle du train.
Seuls les témoins propres à ce consommateur sont exécutés.

Les six témoins initiaux de frontière ont passé avec psycopg et psql
(97,5 s et 74,9 s). Le probe exécute désormais les huit scénarios du raccord
réel `Passage`/`Service` ; `--boundary` ajoute les six anciens témoins.
Les suites du fournisseur ne sont pas rejouées.

Validation du raccord : **8/8 psycopg (21,2 s), 8/8 psql (49,7 s)** sous
`ameesh heavy`, et **12/12** témoins unitaires du service. Git/SQL sont réels ;
CI, provenance, autorité et échéance sont des fixtures explicites.

```sh
PYTHONPATH=src:. AMEESH_DRIVER=psycopg ameesh heavy -- .venv/bin/python \
  reviews/1467-reservation-port/probe.py --supplier . \
  --commit a68f54790f7c3bdb1ed5088544bcf7c54e221b7e \
  --land-commit 86ebebedb87f9e0634fd7d2b3439edbd261a0a60
```

Pour le pilote psql, remplacer le pilote par `AMEESH_DRIVER=psql` et le binaire
par `python3`. Le banc utilise son schéma SQL jetable, des dépôts Git locaux et
une horloge de décision injectée. CI, provenance et autorité du pilote sont des
fixtures explicites. Aucune API distante, installation ni configuration active.

Le raccord fournit `train_reservation.Reservation(provider, deadline=callback)`
explicitement à `Passage`. Le callback reçoit le manifeste et l'instant courant,
et doit fournir une échéance absolue future qualifiée. Aucune durée ni aucun
fournisseur n'est choisi par défaut. L'assemblage du service dans le runner
refuse un passage sans ce port ; le moteur autonome reste utilisable pour les
témoins de préparation antérieurs.

`ReservedLedger.save` adopte et conserve le ticket avant la première publication.
La reprise relit sa clé, son propriétaire, son époque, sa branche, sa base, sa
tête et son contrat CI ; elle renouvelle l'époque persistée et ne reprend pas
un bail expiré avec `epoch=None`. L'échéance absolue reste celle du ticket.
`Runtime.publish` exige aussi ce ticket persistant avant l'effet Git.

`Passage.maintain(db, now)` construit seulement le lecteur SQL du ticket.
`Service` lui fournit une connexion fraîche, fermée après l'appel ; son réveil
de renouvellement est distinct des coupures et des événements du moteur.
Un renouvellement ne construit pas de runtime et ne consulte ni Git, ni canon,
ni CI. Si événement et renouvellement sont dus ensemble, un seul passage est
exécuté. Une erreur de maintien est signalée et ne lance pas de CI.

Les six témoins couvrent :

- adoption du bail avant publication, persistance du ticket et reprise Git/SQL
  sur une connexion fraîche sans changement d'époque ;
- maintien sur connexion fraîche, sans passage du moteur ni lecture CI ;
- vert complet conservant la cible jusqu'à la promotion et sa preuve réelles ;
- expiration de l'époque ou limite absolue refusant une nouvelle publication ;
- exclusion commune aux quatre alias de cible et refus d'une substitution de
  l'ancien espace de clés ;
- acquisition sans époque par le même propriétaire invalidant le ticket : elle
  ne peut servir de renouvellement ou de reprise d'un ticket vivant.

Les huit nouveaux scénarios exécutent le raccord : ticket présent avant push,
nouveau `Passage` sur connexion fraîche au même epoch, maintien via `Service`
sans runtime/CI, vert conservé jusqu'à la promotion Git réelle, absence ou
invalidité de limite, ticket d'un autre propriétaire/contrat, expiration sans
réacquisition et nouvelle époque du même propriétaire refusée. Les douze
témoins unitaires du service couvrent aussi ses connexions et ses réveils.

La limite absolue du probe et son TTL sont des valeurs de test. Une source
qualifiée doit fournir la limite d'exploitation ; aucune durée métier n'est
déduite du TTL SQL. Le changement de clé #1374 demande aussi le drainage
explicite des anciens tickets et baux, selon le protocole de déploiement
ci-dessous. Ces deux entrées ont été demandées au pilote le 15 octobre à
16:41. Mesh-design a confirmé à 16:45 l'absence de plafond dans le canon
`579c64d` et la mécanique
candidate `430e65921`, et demandé les sources au bureau et à l'auteur de #1374.
Le pilote autorise le 15 octobre à 18:14 le port injecté avec refus si la valeur
manque. Le fournisseur impl07 confirme à 18:18 qu'aucune source adoptée ni
aucun champ compilé de réservation n'existe ; sa proposition de deux heures
attend une décision de l'owner et n'est pas consommée ici. Aucun profil actif,
champ canon, migration de ticket ou source de durée n'est installé.

## Contrat de déploiement : transition des anciennes clés

Sources : note de l'auteur impl08 dans #1374 du **15 octobre 2026 à 16:53**,
transmise par mesh-design à 16:58 ; qualification de préparation au journal
#1467 par mesh-design à **19:05**, confirmée par le courrier **#61892**.
La qualification porte sur `land.lock_key` à
`86ebebedb87f9e0634fd7d2b3439edbd261a0a60` et `packet_reservation` à
`a68f54790f7c3bdb1ed5088544bcf7c54e221b7e`.

| Cible réelle | Clés historiques à inventorier | Clé canonique |
| --- | --- | --- |
| ameesh/main | `ameesh:origin/main`, `ameesh:refs/heads/main`, `ameesh:refs/remotes/origin/main` | `ameesh:main` |
| nexlink/develop | `nexlink:origin/develop`, `nexlink:refs/heads/develop`, `nexlink:refs/remotes/origin/develop` | `nexlink:develop` |

L'inventaire inclut aussi la clé canonique et toute autre forme observée
pour la même cible réelle. `_remote_branch` retire un seul préfixe :
`refs/heads/origin/main` devient `origin/main`, pas `main`. La table ne ferme
donc pas à elle seule l'union des clés à contrôler.

`begin` recalcule la clé, mais `renew` et `observe` conservent le `ticket.key`
persisté. Un ancien ticket vivant peut donc renouveler une ligne brute pendant
qu'un écrivain nouveau acquiert la ligne canonique. Une table vide à un relevé
antérieur ne prouve pas l'exclusion au déploiement.

L'opérateur du déploiement applique le protocole sur chaque cible concernée :

1. Inventorier l'union des clés historiques, canoniques et observées dans les
   lignes `merge_locks`, les entrées `merge_queue` et les tickets JSON persistés,
   notamment `integration_trains.document.reservation`. Rapprocher les tickets
   de leur ligne par `(key, owner, epoch)` et relever les échéances ; inventorier
   également les anciennes versions encore capables d'acquérir ou renouveler.
2. Barrer les nouvelles acquisitions sur cette union pendant le drainage.
   Les anciens titulaires terminent ou expirent sur leur clé d'origine ;
   une sortie terminale qualifiée d'`observe` libère par l'appel exact
   `land_unlock(key, owner, epoch)`. Un vert CI conserve la réservation.
   Le TTL renouvelable n'est pas un délai maximal garanti de drainage.
3. Juste avant la bascule, sous cette même barrière, refaire l'inventaire et
   vérifier ensemble l'absence d'ancien ticket/bail vivant et l'absence de
   tout ancien écrivain capable de réacquérir. Une ligne ancienne avec
   `owner IS NOT NULL` et `expires_at > clock_timestamp()` reste vivante.
   Un ticket ou une version non rapprochés ne prouvent pas le drainage.
4. Basculer seulement lorsque ces deux conditions sont établies, en conservant
   la barrière jusqu'à ce que les écrivains utilisent la clé canonique.
   Consigner cible, instant du contrôle, versions des écrivains, inventaire
   clés/tickets et preuves des deux conditions avec le geste de déploiement.

Les identités `(key, owner, epoch)` des tickets vivants restent immuables :
aucune purge, aucun renommage et aucun namespace parallèle ne découlent de ce
contrat. Le relevé vide de l'auteur vers **14:5xZ le 15 octobre** est historique ;
il ne vaut pas preuve au futur geste et doit être repris par l'opérateur.

Cette condition concerne la transition de version #1374/#1467. La qualification
de préparation permet de documenter ce protocole ; elle n'atteste ni son
exécution, ni une activation. Les préparations indépendantes continuent.
