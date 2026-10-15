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

Validation du consommateur : **6/6 verts avec psycopg et 6/6 avec psql**,
respectivement 97,5 s et 74,9 s, sous `ameesh heavy`. Les suites du fournisseur
ne sont pas rejouées.

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

La préparation du probe suspend le moteur à son intention de publication,
adopte son époque vivante et conserve le ticket JSON avec `Ledger.save` avant
de reprendre. Elle modélise la frontière à raccorder ; elle n'installe pas ce
comportement dans `Passage` ou `Service`.

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

Le raccord de production doit conserver le ticket avant le premier push, puis
le relire avant toute acquisition de reprise. Un nouveau `Passage` ne peut
remettre son époque à zéro pour contourner une réservation perdue ou expirée.
Le maintien doit utiliser une connexion fraîche, sauvegarder le ticket sous le
même bail et ne jamais déclencher de lecture CI ni de passage du moteur. Le
succès CI garde la réservation jusqu'au dernier geste et à sa réconciliation.

La limite absolue du probe et son TTL sont des valeurs de test. Une source
qualifiée doit fournir la limite d'exploitation ; aucune durée métier n'est
déduite du TTL SQL. Le changement de clé #1374 demande aussi une réconciliation
explicite des anciens tickets et baux, sans substitution silencieuse. Ces deux
entrées ont été demandées au pilote le 15 octobre à 16:41. Mesh-design a
confirmé à 16:45 l'absence de plafond dans le canon `579c64d` et la mécanique
candidate `430e65921`, et demandé les sources au bureau et à l'auteur de #1374.

## Transition des anciennes clés

Source : note de l'auteur impl08 dans #1374 du 15 octobre à 16:53, transmise
par mesh-design à 16:58. Les anciennes formes pour ameesh sont
`ameesh:origin/main`, `ameesh:refs/heads/main` et
`ameesh:refs/remotes/origin/main` ; la forme canonique est `ameesh:main`.
Pour Nexlink, les mêmes formes de `develop` convergent vers
`nexlink:develop`. Les formes observées avec plusieurs préfixes doivent aussi
être inventoriées : `_remote_branch` ne retire qu'un préfixe.

`begin` recalcule la clé, mais `renew` et `observe` conservent le `ticket.key`
persisté. Un ancien ticket vivant peut donc renouveler une ligne brute pendant
qu'un écrivain nouveau acquiert la ligne canonique. Une table vide à un relevé
antérieur ne prouve pas l'exclusion au déploiement.

La bascule doit inventorier les lignes des deux formes et les tickets JSON,
barrer les nouvelles acquisitions sur l'union des formes pendant le drainage,
laisser les anciens titulaires terminer ou expirer, puis vérifier l'absence
d'ancienne ligne vivante **et** d'ancien écrivain capable de réacquérir.
Aucun ticket vivant n'est renommé : clé, propriétaire et époque restent liés.
Le relevé de l'auteur (tables vides vers 16:5x) doit être refait au dernier
geste. Ce contrat n'effectue aucune purge, migration de ticket ni action hôte.
