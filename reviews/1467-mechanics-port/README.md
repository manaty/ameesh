# Raccord #1467 / #1616 / #1757

Contrat fournisseur livré par impl07 le 15 octobre 2026 à 21:09, corrigé par
dev2 à 21:22 puis gelé à 21:25 après le regard borné de mesh-design : code
[`7ccf16d8c46cb827bb6db435fdf751b7b8d22803`](https://github.com/manaty/ameesh/commit/7ccf16d8c46cb827bb6db435fdf751b7b8d22803),
canon [home#96](https://github.com/manaty/home/pull/96) au commit exact
`21d6653922bac554d2d6cd0570e7f6497b215c37`. Le module est injecté
explicitement dans `Passage` ; aucune copie n'est ajoutée au produit.

Le même objet compilé fournit le calendrier, la classification et le nombre
d'avis, les familles autorisées, leur indépendance et l'autorité de fusion.
`merge.named_fire: false` sélectionne le port automatique ; la tête verte
est promue sans appel au pilote. L'intention `promoting` conserve le plan et
le commit du canon sous le bail cible. Après une réponse perdue, la reprise
observe la cible exacte sans rejouer le push. Le mode nominatif est exercé
seulement lorsque le canon de fixture le demande.

Le passage exige `packet_cutoffs` non vide et les ports `last_packet` et
`next_packet`. Il n'appelle jamais les ports release `cutoff`/`next_cutoff`.
L'instant du paquet doit être fini et inférieur ou égal à l'instant du
passage ; le prochain réveil doit être fini et strictement futur.
Un document absent/invalide, un autre sujet, une CI autre que complète ou
le mode historique par intervalle refuse avant réservation.

## Deux frontières de preuve

Avec `--canon` et `--canon-commit`, le fournisseur lit les objets réels du
canon exact, sans checkout. Le probe passe par `train_policy.Compiled` pour
les comptes ameesh et Nexlink : CI/infra/staging à un avis ; identité, SQL,
paiement et chemin de prod déclaré à deux. Il vérifie aussi le mode de
fusion automatique. Pour chaque projet, `train.Policy.cutoff` consomme le
calendrier réel : 24 créneaux ordinaires, 23 le 29 mars 2026 et 25 le
25 octobre 2026, triés, sans doublon d'epoch. Les bornes incluses/exclues,
le passage de minuit, l'heure absente de mars et les deux occurrences de
02 h en octobre sont contrôlés ; midi est un paquet, la dernière coupure
release restant 11 h. Ces vérifications ne sélectionnent aucun profil actif.

Les seize scénarios Git/SQL chargent le seul blob `mechanics.py` de la tête
fournisseur exacte, sur des canons de **fixture** versionnés. Ils couvrent :

- changement du calendrier, du fuseau, des familles et du nombre d'avis ;
- refus du document ou du calendrier paquet absent, sans repli release ;
- publication puis promotion sur connexion fraîche, exclusion d'un autre
  runner et refus d'une provenance inconnue ;
- réveil du service et fusion automatique ou nominative selon le contrat ;
- maintien d'un paquet horaire en CI après reprise, sans deuxième publication
  ni deuxième tentative CI ;
- retrait pendant la CI : phase `draining`, réservation conservée jusqu'à
  une conclusion complète et aucune promotion de la tête retirée ;
- CI à travers les changements d'heure de mars et d'octobre : même manifeste
  sur connexion SQL et `Passage` neufs, un seul lancement CI simulé et
  promotion de sa tête exacte. Ces scénarios utilisent le calendrier du
  fournisseur exact sur un commit unique de canon horaire de fixture.

Git, Postgres, les verrous et la promotion locale sont réels et jetables.
La CI distante et la provenance #1465 sont simulées. Aucun calendrier simulé
n'est utilisé dans ce probe ; les tests unitaires du runner gardent leurs
epochs explicites.

Qualification initiale sur le code `735651aa` et le même canon : 16/16 scénarios verts
avec psycopg (156,2 s) et 16/16 avec psql (174,6 s), sous `ameesh heavy`,
avec les contrôles du canon réel pour les deux projets dans chaque exécution.

Le delta `735651aa..7ccf16d8` étend la recherche à deux jours pour traverser
le jour sans 02:00. Un témoin consommateur supplémentaire choisit cette
cadence dans un canon de fixture, reprend sur connexion fraîche avant le
créneau du 30 mars et vérifie l'absence de publication prématurée, puis une
seule tentative CI simulée et la promotion Git exacte. Il échoue sur
`735651aa` à `next_packet` ; il passe sur `7ccf16d8` avec psycopg (4,7 s)
et psql (13,2 s). Les contrôles du canon réel 24/23/25 sont également verts
sur `7ccf16d8`. Les seize scénarios initiaux et la suite fournisseur ne sont
pas rejoués pour ce delta.

## Reproduction

Rendre les objets code et canon ci-dessus disponibles dans les dépôts
indiqués, puis exécuter depuis la branche consommateur #1467 :

```sh
ameesh heavy -- env PYTHONPATH=src:. AMEESH_DRIVER=psycopg .venv/bin/python \
  reviews/1467-mechanics-port/probe.py --supplier . \
  --commit 7ccf16d8c46cb827bb6db435fdf751b7b8d22803 \
  --canon "$DEPOT_CANON" \
  --canon-commit 21d6653922bac554d2d6cd0570e7f6497b215c37
# Même commande avec AMEESH_DRIVER=psql pour le second pilote.
```

Pour limiter l'exécution aux deux reprises DST, ajouter :

```sh
--test test_pending_packet_survives_repeated_hour_on_fresh_connection \
--test test_pending_packet_survives_spring_gap_on_fresh_connection
```

Pour le seul témoin de la cadence réduite à 02:00, utiliser :

```sh
--test test_sparse_calendar_resumes_after_a_day_without_a_packet
```

Pas de suite du fournisseur rejouée, ni CI distante lancée. Cette preuve de
port ne remplace pas ses avis indépendants, ne choisit pas un commit canon
de production et ne constitue pas une activation du train.
