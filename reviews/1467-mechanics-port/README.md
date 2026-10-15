# Raccord #1467 / #1616 — 15 octobre 2026

Préparation du consommateur, non gelée. Fournisseur exact :
`53cf2e01c90fbc07233f865a68c4d44fab5c096c`, branche
`lot/1616-process-params`. Il est injecté explicitement dans `Passage` ;
aucune copie de son module n'est ajoutée à #1467.

Le même objet compilé fournit le calendrier, la classification et le nombre
d'avis, les familles autorisées, leur indépendance et l'autorité de fusion.
`merge.named_fire: false` sélectionne le port automatique ; la tête verte
est promue sans appel au pilote. L'intention `promoting` existante conserve
le plan et le commit du canon sous le bail cible. Après une réponse perdue,
la reprise observe la cible exacte sans rejouer le push. Le mode nominatif
reste exercé seulement lorsque le canon compilé le demande.
Le passage refuse
un document absent/invalide, un autre sujet, une CI autre que complète et
le mode historique par intervalle. Les validations Git du diff et les
preuves historiques exactes restent exigées. Le fournisseur #1465 et la CI
distante sont simulés ; Git, Postgres, les verrous et la promotion locale
sont réels et jetables.

`probe.py` charge uniquement le blob Python du fournisseur au commit
complet, dans un répertoire temporaire. Il exécute treize scénarios ciblés :
coupure modifiée dans le canon, fuseau modifié, fichier absent, famille
exclue, nombre d'avis modifié, parcours publication/feu/promotion avec
nouvelle connexion, concurrence de runners, provenance inconnue, calendrier
du service, fusion automatique, absence du bloc de fusion, cadence horaire
et retrait pendant la CI.

Avec `--canon` et `--canon-commit`, le même fournisseur lit aussi les objets
réels du candidat home `430e65921f4421831ab87b4c1160d631221fc317`.
Le témoin passe par `train_policy.Compiled` pour les comptes ameesh et
Nexlink : CI/infra/staging à un avis ; identité, SQL, paiement et chemin de
prod déclaré à deux. Cette sélection sert au témoin, pas à un profil actif.

```sh
ameesh heavy -- env PYTHONPATH=src:. AMEESH_DRIVER=psycopg .venv/bin/python \
  reviews/1467-mechanics-port/probe.py --supplier . \
  --commit 53cf2e01c90fbc07233f865a68c4d44fab5c096c \
  --canon /chemin/vers/home \
  --canon-commit 430e65921f4421831ab87b4c1160d631221fc317
# Même commande avec AMEESH_DRIVER=psql pour le second pilote.
```

Pas de suite du fournisseur rejouée, ni CI distante lancée. Cette preuve
de port ne remplace pas ses avis indépendants, ne choisit pas un commit
canon de production et ne constitue pas une activation du train.

Le témoin ajouté pour le service runner change uniquement le canon de 11 h
à 12 h : `scheduled_step` attend 12 h puis compose à 12 h, avec la prochaine
échéance à 23 h fournie par le même objet compilé. Exécution ciblée : ajouter
`--test test_real_next_cutoff_and_engine_use_the_changed_canon` aux commandes
ci-dessus. Les neuf témoins de `tests.test_1467_train_service` couvrent le
cycle de vie et le port d'événement, sans fournisseur ni API réels.

## Préparation de la cadence horaire (home#94)

Source : canon fusionné `937974cfcc705b35caaa4de923c193e5f3d839c4`,
§11 bis, communiqué par mesh-design le 15 octobre à 19:55 (#63085).
Deux nouveaux témoins consommateur utilisent un calendrier horaire dans
un canon de **fixture** :

- `test_real_hourly_calendar_keeps_pending_packet_across_restart` compose à
  midi, reprend le même paquet à 13 h sur une connexion fraîche, puis fusionne
  sa tête verte sans deuxième publication ni deuxième tentative CI ;
- `test_real_withdrawal_keeps_ci_barrier_on_fresh_connection` retire un lot
  après publication. Le paquet passe en `draining`, conserve la réservation
  pendant sa CI et ne promeut jamais la tête retirée. La conclusion complète
  libère ensuite le passage pour les nouvelles admissions.

Le témoin unitaire de `tests.test_1467_train_cutoffs` vérifie aussi qu'un
autre lot prêt au créneau suivant attend cette conclusion et qu'une preuve
CI d'une autre tête ne lève pas la réservation.

Les deux nouveaux témoins passent avec psycopg (6,3 s et 9,4 s) et psql
(2/2 en 62,0 s), sous `ameesh heavy`. Git et SQL sont réels ; CI et
provenance sont des fixtures, sans suite fournisseur rejouée.

Cette fixture exerce l'interface existante ; elle ne qualifie pas le
contrat horaire d'exploitation. Le commit home#94 ne contient aucun
`processus/mecanique.yaml`. L'auteur de #1616 confirme à 19:59 que son
fournisseur gelé `53cf2e01` ne distingue pas paquets et releases. Mesh-design
qualifie à 20:04 le bloc canon `packets.cutoffs`, exposé par `packet_cutoffs`,
`packets_on`, `last_packet` et `next_packet`, avec refus en l'absence de
déclaration. Son fournisseur est confié à impl07 dans #1757 après son P1
#1749 ; les SHA code et canon machine restent à publier. Le train ne
substitue aucune durée locale et ne choisit aucun profil actif.
