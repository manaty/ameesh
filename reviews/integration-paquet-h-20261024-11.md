# Paquet integration/paquet-h-20261024-11 — composition (§12)

Base : `main` **`8e0ffbc7aa0997fbc686e644b55a3abfb4cadde9`** (release 1.21.0,
coupure 21:00Z, sur la fusion du paquet h-10 `7f12aa2b5`, PR #2561, 19:35Z),
recomposée à la demande du pilote (24/10 21:0xZ).
Branche `integration/paquet-h-20261024-11`, cible `main`. Porteur : **#3404**
(créé par mesh-design, fil du lot, 24/10 20:06Z).
Composeur : mty-amh-impl-05. Une seule suite complète est demandée sur la tête
finale (CI de la PR).

## Sources

- mesh-design, fil #3404, message du 24/10 20:06Z : « h-11 sur main
  7f12aa2b5 (h-10 fusionné) […] 11 prêts, aucun en attente — #3402, #3366,
  #3380, #3384, #3394, #3395, #3396, #3397, #3338, #3369, #3370.
  Priorité 1 : #3402 […] Puis #3366, puis #3380 derrière #3379, comme
  convenu à 18:33Z. La coupe de 21:00Z prendra h-10 ; h-11 fusionne dès son
  vert. »
- mesh-design, message du 24/10 20:18Z : « Compose h-11 (#3404)
  MAINTENANT, avant tout autre travail : integration/paquet-h-20261024-11
  depuis scripts/paquet-candidats.py, avec les 11 prêts […] et les écarts
  motivés au manifeste. #3399 est en qa et peut attendre. La coupe de 21:00Z
  prend main tel quel, h-11 n'a pas à la tenir : fusion dès le vert. »
- mesh-design, message du 24/10 21:0xZ : « la coupe 1.21.0 est passée (tag
  v1.21.0 publié à 21:01Z), et main est maintenant à 8e0ffbc7a (commit de
  release : pyproject.toml et docs/versions/1.21.0.md seulement). Recompose
  sur main 8e0ffbc7a, et non sur 7f12aa2b5 : c'est sans chevauchement avec
  les 10 lots. #3397 est corrigé (0a0be59f5b80, gel #18115, avis OK de cx1 ;
  l'auditeur relit le delta) mais reste HORS de h-11 : il part au paquet
  suivant avec #3400. »
- Calcul `scripts/paquet-candidats.py --project ameesh --target main` sur
  main `7f12aa2b52844b7680a270b41c2a5509f1305e6c` (24/10 20:2xZ) :
  **11 prêts** (#3402, #3366, #3380, #3384, #3394, #3395, #3396, #3397,
  #3338, #3369, #3370), **3 en attente** (#3186, #3385, #3399), **0 erreur**.
  La liste calculée est la liste de départ : 10 des 11 prêts sont composés sur
  main `8e0ffbc7a` ; #3397 est hors de h-11 (B1 #2928 sur la première tête,
  décision pilote du 24/10 20:56Z), corrigé depuis (`0a0be59f5b80`, gel
  #18115) et routé au paquet suivant avec #3400.

## Admissions (10 lots, 10 unités)

| lot | source composée | classe | avis (gate) | objet |
|---|---|---|---|---|
| #3402 | `475fd4b934cc` | light | 1/1 : ameesh-cx1 (codex), verdict 18080 | alerts BLOCKED sans route notify : `role timers --check/--install` échoue, le staging des VM retombe en arrière |
| #3366 | `0c42f0b0d801` | sensitive | 2/2 : auditeur (claude), 18001 ; ameesh-cx2 (codex), 18004 | Train : `canon_commit` épinglé lu au démarrage, composition arrêtée après fusion de canon |
| #3380 | `8035a4b89a95` | sensitive | 2/2 : auditeur (claude), 17984 ; mty-amh-impl-03 (deepseek), 18071 | CI : exécutions sous Python 3.14, la version du poste pc-smichea |
| #3384 | `9bb3a63f308a` | sensitive | 2/2 : auditeur (claude), 17986 ; ameesh-dev5 (deepseek), 17988 | relecture après fusion : l'avertissement « aucun dépôt lisible » part avant la classification |
| #3394 | `02c24febd3fa` | sensitive | 2/2 : ameesh-cx2 (codex), 18032 ; auditeur (claude), 18033 | UI `/ui/` : `/.well-known/ameesh` annonce la version du paquet, plus 0.1.0 |
| #3395 | `cbafef885ea8` | sensitive | 2/2 : auditeur (claude), 18044 ; ameesh-cx2 (codex), 18045 | UI `/ui/` : la Chronologie ne charge plus le snapshot progress complet à chaque rafraîchissement |
| #3396 | `3b7b7fd3f72d` | sensitive | 2/2 : auditeur (claude), 18081 ; ameesh-cx2 (codex), 18083 | Ménage : un processus illisible ne bloque plus en silence le retrait des worktrees |
| #3338 | `2e23952d908b` | sensitive | 2/2 : ameesh-cx1 (codex), 18063 ; auditeur (claude), 18064 | panier « #3335 » : défauts non bloquants (N, P2, P3) |
| #3369 | `b6cb3ed5a365` | sensitive | 2/2 : ameesh-cx1 (codex), 18067 ; auditeur (claude), 18068 | panier « #3367 » : défauts non bloquants (N, P2, P3) |
| #3370 | `c8d384f2f7eb` | sensitive | 2/2 : auditeur (claude), 18000 ; ameesh-cx2 (codex), 18003 | `work set-priority` : changer la priorité d'un lot ouvert, avec entrée de journal |

Les têtes composées sont les têtes **publiées** lues par le calcul (gel
effectif = tête publiée, verdicts acquis sur cette tête). #3402 est la
priorité 1 du pilote (bloquant d'après fusion de #3383, PR #2578) : sans lui,
tout staging VM de `main` échoue à `role timers --check` et retombe en
arrière. #3366 a été rebasé sur `main` (tête `0c42f0b0d801`, avis auditeur +
cx2 sur ce gel) ; #3380 vient derrière #3379, déjà entré dans `main` avec
h-10.

## Composition (10 unités, 10 lots)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #3402 | `475fd4b934cc` | `ccd93876688d` | 0 conflit |
| 2 | #3366 | `0c42f0b0d801` | `34325e8ef99b` | 0 conflit |
| 3 | #3380 | `8035a4b89a95` | `d1231650a586` | 0 conflit |
| 4 | #3384 | `9bb3a63f308a` | `966314713a9f` | 0 conflit |
| 5 | #3394 | `02c24febd3fa` | `e2be51f97c5c` | 0 conflit |
| 6 | #3395 | `cbafef885ea8` | `88d9d1e70cfd` | 0 conflit |
| 7 | #3396 | `3b7b7fd3f72d` | `3f4205f716a4` | 0 conflit |
| 8 | #3338 | `2e23952d908b` | `7d8aeecc3555` | 0 conflit |
| 9 | #3369 | `b6cb3ed5a365` | `f812124b0152` | 0 conflit |
| 10 | #3370 | `c8d384f2f7eb` | `e73ced7afe84` | 0 conflit |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et sa tête (`vehicule 1100 : #<lot> <tête>`), et il porte un trailer
`ameesh-work:`. La base `8e0ffbc7aa0997fbc686e644b55a3abfb4cadde9` n'a pas
bougé pendant la composition.

## Contrôles

- **Liste calculée** : 11 prêts, **10 composés**, 1 écarté (#3397), 3 en
  attente (ci-dessous), 0 erreur.
- **Conflits** : 10/10 unités fusionnées sans intervention ; aucun marqueur
  restant ; `git diff --check` vert hors le bundle généré
  `src/ameesh/ui/static/ameesh-ui.js` (un blanc de fin de ligne dans un
  littéral de gabarit produit par lit, contenu fonctionnel de #3395, non
  modifié par le paquet).
- **Tests ciblés** : modules impactés du paquet (voir journal du lot) ;
  la preuve de référence reste l'unique `ci-full` sur la tête finale.
- **Compilation** : `compileall src tests`.
- **Contrôles statiques** : `scripts/freeze-checks.py`.
- **Diff** : 57 fichiers, +2618/−171 (manifeste compris).
- **Migrations** : aucune (la migration additive 0481 de #3397 sort avec le
  lot) ; aucune migration destructive.
- **Inclusion** : `scripts/paquet-inclusion.py` rc 0 sur la tête (attendus =
  les 10 composés ; prêts = les 11 du calcul de 20:2x, #3397 écarté au
  manifeste).
- **Suite complète** : une seule `ci-full` demandée sur la tête finale, en mode
  paquet ; les verdicts unitaires couvrent les contenus, le paquet est relu sur
  sa tête par les familles du porteur (§12).
- **Fusion** : sous garde de tête (`--match-head-commit`), dès le vert et les
  avis du porteur ; landings `ameesh work landing <unité> --vehicle 3404`
  déclarés au gel ; staging = bureau.

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #3397 | retiré de h-11 après la CI rouge de `bb6bde211` (B1 #2928 : son retour anticipé de `review_gate` perd le diagnostic d'un verdict sans reçu, ameesh-cx1 20:53Z ; décision pilote 20:56Z). Corrigé depuis (`0a0be59f5b80`, gel #18115, avis cx1 OK, auditeur en cours) et routé au paquet suivant avec #3400 ; ses deux correctifs mécaniques de CI restent au véhicule. |

## En attente (calcul, hors paquet)

| lot | motif |
|---|---|
| #3186 | verdicts manquants sur le gel `91c09ba8d730` (1 famille codex, ameesh-cx2 17860) et conflit sur `src/ameesh/release_cutoff.py` avec main |
| #3385 | branche `canon/3385-roadmap-24-10` non publiée sur origin : une tête locale n'est pas la forge |
| #3399 | verdict bloquant de l'auditeur (18088) sur le gel `4b886ab1616d` ; #3399 est en qa et hors paquet (pilote 20:18Z) |

## Correctifs après CI rouge (24/10 20:5xZ)

La suite complète sur `bb6bde211` (11 unités) a rendu rouges 13 témoins
distincts, sans contenu de lot en cause ; elle a fait retirer #3397 :

- **#3366, témoin dépendant du poste** : le profil de test déclarait
  `"inherit": true`, qui se résout sur `~/.config/ameesh/config.json` de
  l'hôte (#1466) — absent sur un runner CI, d'où le refus « inherited config
  not found ». Le témoin hérite désormais de la base temporaire explicite
  (`self.base`) : même règle d'héritage, plus aucune dépendance à `HOME`.
  Aucun code produit ne change.
- **#2446, blob audité non ré-épinglé** : #3380 a ajouté le job facultatif
  `python314` à `.github/workflows/tests.yml` (`b9c155e2…`), revu 2/2 sur sa
  tête, sans suivre l'épinglage du contrat. `src/ameesh/train_contracts.py`
  épingle ce blob exact ; aucun autre blob du contrat ne bouge.
- **#3397, retiré** : son retour anticipé de `review_gate` (latest_ok <
  required) omettait le suffixe « without an exact provenance receipt » que
  le témoin #2928 vérifie ; le geste §11 bis retire le lot fautif sans relire
  les autres. La recomposition sans lui rend le chemin historique au témoin
  et sort la migration 0481 du paquet.

Le manifeste et ces deux correctifs mécaniques forment la tête recomposée sur
main `8e0ffbc7a` (release 1.21.0), regelée et repassée en suite complète ; les
10 unités restent les têtes gelées et avisées listées ci-dessus.
