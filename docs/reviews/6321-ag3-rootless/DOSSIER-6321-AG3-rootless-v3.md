# #6321 — AG3 rootless, dossier v3 : delta de v2 (avis B d'impl-11 #200929)

Bureau (BG), 08/11 ~14:10Z. **AUCUN GESTE et aucune commande sur une VM.** Les témoins sont locaux,
avec un Docker simulé. Les versions v1 et v2 sont conservées.

## B1 — reçu de retour incomplet aux points d'échec de la phase A

- **Défaut v2.**
  - Après l'arrêt d'une source, `sources_stopped` n'était persisté qu'au point de progression
    suivant, c'est-à-dire après la copie, le manifeste, la création, la santé et la référence.
  - L'identifiant de la cible n'était persisté qu'après la santé.
  - Conséquence : en cas d'échec pendant la copie, le retour ne relançait pas la source ; en cas
    d'échec après création, il refusait la cible comme « charge inconnue ».
- **v3.** Chaque intention est écrite sur disque **avant** la mutation correspondante, avec son
  identité, dans `intents` :
  - `volume_create` (nom du volume) ;
  - `stop_source` (nom et ID du conteneur rootful) ;
  - `create_target` (nom et étiquette).

  L'ID de la cible est persisté dès `docker run`, avant l'attente de santé. Les cibles portent
  aussi `ameesh.operator.source=<nom>`.
- **Retour : réconciliation de l'état réel.**
  - Tout conteneur rootless doit porter `ameesh.operator=6321` (cibles, même sans santé, et
    conteneurs de copie). Les autres sont refusés comme charge inconnue, sans y toucher.
  - Les sources à relancer viennent des intentions `stop_source`. L'ID du conteneur rootful doit
    égaler l'ID de l'intention, sinon STOP.

## B2 — garde de fenêtre

- **Phase C `--apply`** : `outside_cutoffs()` est appelé juste après l'autorité, **avant toute
  mutation** (témoin, dossier, unités). La fenêtre est le 09/11 de 13:00Z à 15:30Z, et les coupures
  restent exclues.
- **Retour** : il est soumis à la même garde, sauf **secours explicite et documenté**. L'autorité doit
  porter `secours_hors_fenetre: true` et `secours_mail`, l'identifiant du courrier de mesh-design.
  Les deux valeurs sont consignées dans `retour-progress.json`. Sans courrier, le secours est refusé.

## N

- **Régulateur des suites lourdes.** La suite SQL de la phase C passe par
  `ameesh heavy -- /bin/bash …/test-parallele.sh -n 1 -p psql --liste …`.
- **Garde d'invités.** La preuve couvre le **texte** de `/etc/ameesh-guest/firewall.nft` **et** le
  jeu de règles **chargé** (`nft list ruleset`, lecture). Si l'un des 4 ports manque à l'un ou à
  l'autre, c'est un refus.

## Témoins (`releve-6321-v3-temoins.txt`)

**`temoin-6321-v3.py`** (Docker simulé, octets du contrôleur v3) : 8/8.
- Échec pendant la copie : intention `stop_source` consignée avant l'arrêt, puis source relancée par le retour.
- Échec après création, avant santé : ID consigné, puis cible reconnue par son étiquette et arrêtée.
- Conteneur sans étiquette : retour refusé, sans y toucher.
- Phase C hors fenêtre : refus avant toute mutation.
- Retour hors fenêtre : refusé sans secours, accepté avec secours et courrier, et consigné.
- Lancé contre la v2, ce témoin échoue faute d'intentions (`KeyError: 'intents'`) : il reproduit bien le défaut B1.

**`temoin-6321-v3-fonctions.py`** : 11/11. Ce sont les 10 témoins de v2, plus « jeu chargé sans
55442 alors que le texte est conforme → refus ».

## Pièces

- `ag3-rootless-6321-v3.py` ;
- `ag3-rootless-6321-v2-v3.diff` (diff explicite) ;
- les deux témoins ;
- le relevé ;
- `SHA256SUMS` mis à jour.

Le feu `--apply` de chaque phase reste à mesh-design, le 09/11.
