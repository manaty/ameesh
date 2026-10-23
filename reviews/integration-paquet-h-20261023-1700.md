# Paquet integration/paquet-h-20261023-1700 — composition (§12)

Base : `main` **`d93b3a468023`** (fusion de la PR #2409, paquet h-1500, 14:52Z).
Branche `integration/paquet-h-20261023-1700`, cible `main`. Porteur : **#3176**.
Composeur : mty-amh-impl-01. Une seule suite complète est demandée sur la tête
finale.

## Sources

- Bureau, message #128153 (14:53Z) : « Composez le paquet suivant (#3126 puis
  #3014/#3134/#3142…) sur cette main. »
- Directive mesh-design 14:18Z : paquet large pour vider la file (> 20 prêts,
  presque tous SLO 2/3). Directive 14:33Z : #3126 en premier, puis l'unité
  `7ade8c387bfc` (#3014+#3134+#3142, avis mesh-design) ; sans nouvelle remise,
  #3007 est écarté et #3014 part quand même.
- Liste calculée `scripts/paquet-candidats.py` à 15:00Z sur `d93b3a468` :
  **27 prêts, 17 en attente, 3 lectures impossibles** (#3123, #2994, #2914).

#3147 (porte de revue : provenance lue par SHA depuis la forge) est composé en
premier, justement pour rendre lisibles les gels absents du clone. Le recalcul
depuis l'arbre du paquet a ensuite été refusé par l'environnement de lecture
(la base du maillage n'a pas encore la colonne `election_brief` de #3012 :
migration non appliquée hors staging, `column "election_brief" does not
exist`) ; la liste de référence reste celle du calcul installé, et les trois
lectures impossibles restent hors composition, comme au paquet h-1500.

## Admissions (28 lots, 26 unités)

| lot | source composée | classe | avis (gate) | objet |
|---|---|---|---|---|
| #3147 | `74cf9bc3c477` | sensitive | 2/2 : auditeur (claude), ameesh-cx1 (codex) | porte de revue : gel antérieur récupéré par SHA depuis la forge |
| #3126 | `214505e23ef8` | sensitive | 2/1 : auditeur (claude), impl-05 (deepseek) | brief : projection et reprise sous budget, §15 de nouveau lisible |
| #3014 | `7ade8c387bfc` (résolution) | sensitive | pilote mesh-design OK sur l'unité (note 14:33Z) ; auditeur 1/2 sur `adc791fb` | EP5 : `role run`, `notify --only`, `train.role` |
| #3134 | `7ade8c387bfc` (résolution) | sensitive | 2/2 sur `43598eda` : impl-01 (deepseek), cx2 (codex) | arrêt du runner : bail train-service libéré, effets fencés |
| #3142 | `7ade8c387bfc` (résolution) | sensitive | 2/2 sur `5068e011` : docs-01 (deepseek), cx2 (codex) | adoption des réservations train par le nouveau porteur |
| #3029 | `29ef7ec39a48` (remise) | sensitive | 2/2 : cx2 (codex), dev2 (claude) | panier #3010 : drain du bail élu |
| #3115 | `8bc04c0e06cc` | sensitive | 2/2 : auditeur (claude), cx2 (codex) | `Gate.hold` ne raccourcit pas un bail d'hôte |
| #3162 | `ef4a02d3cc02` | light | 1/1 : cx2 (codex) | SLO « ready → started » : un lot déjà commencé garde sa mesure |
| #3080 | `d07b160e0dea` | sensitive | 2/2 : dev3 (deepseek), auditeur (claude) | feu + file en atterrissage : le commit de fusion attendu |
| #3138 | `96812a562b53` (résolution) | sensitive | 2/2 sur `85da140c` : cx2 (codex), auditeur (claude) | train : plan persisté non tiré, abandon |
| #3135 | `800a0a11e0da` | sensitive | 2/2 : auditeur (claude), cx2 (codex) | macOS : restes coffre/câblage, `release_health`, bascule |
| #3139 | `f8b53c89d55b` (résolution) | sensitive | 2/2 sur `65b951aa` : auditeur (claude), cx2 (codex) | train : motifs de refus d'enrôlement plus tus |
| #3098 | `f4d50fe7a867` | sensitive | 2/2 : cx2 (codex), dev2 (claude) | train : contrat CI épinglé dans le code installé |
| #3132 | `b2e145c40de1` | light | 1/1 : auditeur (claude) | macOS : tests Linux-only gardés |
| #3131 | `9b8c8d00aee7` | light | 1/1 : auditeur (claude) | macOS : bancs de notification sans `notify-send` |
| #3128 | `532396ecb251` | light | 1/1 : auditeur (claude) | macOS : nom d'hôte long du runner accepté |
| #3133 | `252283d7d780` | light | 1/1 : auditeur (claude) | macOS : oauth/comptes — groupes de processus et reprise |
| #3099 | `7f90951b4182` | sensitive | 2/2 : cx2 (codex), auditeur (claude) | passation : état persisté, reprise après redémarrage |
| #3070 | `7774680dfd33` | sensitive | 1/1 : cx2 (codex) | appareil indisponible : `unavailable_until` |
| #3048 | `4c8bdb26094a` | sensitive | 2/2 : cx2 (codex), impl-01 (deepseek) | panier #3030 : garde |
| #3054 | `074f470c37a6` | light | 1/1 : cx2 (codex) | panier EP8 |
| #3103 | `c99682cc5a95` | sensitive | 2/2 : cx2 (codex), dev5 (deepseek) | docteur : route Docker |
| #3100 | `0d48905c8a77` | light | 1/1 : cx2 (codex) | bail cligné : un bail rendu quelques secondes n'est pas une perte |
| #3111 | `9fcadf970a10` | sensitive | 2/2 : dev5 (deepseek), cx2 (codex) | provenance locale |
| #3096 | `d326f0f85fb3` | light | 2/1 : dev5 (deepseek), impl-08 (deepseek) | relecture #2955 |
| #3066 | `05eae2b0c265` | sensitive | 2/2 : auditeur (claude), dev3 (deepseek) | paquet : checks requis |
| #2993 | `1a5149c0b1c5` | light | 1/1 : dev1 (claude) | candidat non prouvé : test |
| #3057 | `26ee15631120` | light | 1/1 : cx2 (codex) | panier #3036 : grâce de bail, routage des briefs |

#3138 et #3139 sont composés depuis leurs résolutions enregistrées (respectivement
`96812a562b53`, merge de 3138 et 3142, et `f8b53c89d55b`, merge de 3138 et
3139) : les têtes gelées restent celles des avis, la résolution porte le delta
déjà relu par leurs auteurs et testé (76 et 16 témoins ciblés).

## Composition (26 unités, 28 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #3147 | `74cf9bc3c477` | `d598fa8ae` | 0 conflit |
| 2 | #3126 | `214505e23ef8` | `8e965819b` | 0 conflit |
| 3 | #3014, #3134, #3142 | `7ade8c387bfc` | `a961ebb1b` | résolution enregistrée #3014/#3142 ; 0 conflit sur la base |
| 4 | #3029 | `29ef7ec39a48` | `70d56076e` | remise (cover=verdict) ; 0 conflit |
| 5 | #3115 | `8bc04c0e06cc` | `14d074494` | 0 conflit |
| 6 | #3162 | `ef4a02d3cc02` | `fc99ccbaa` | 0 conflit |
| 7 | #3080 | `d07b160e0dea` | `0de700d5f` | 0 conflit |
| 8 | #3138 | `96812a562b53` | `a4ce388e1` | résolution #3138/#3142 ; 0 conflit |
| 9 | #3135 | `800a0a11e0da` | `f8f5d12e5` | 0 conflit |
| 10 | #3139 | `f8b53c89d55b` | `6b597b2bb` | résolution #3138/#3139 ; 0 conflit |
| 11 | #3098 | `f4d50fe7a867` | `68631dc4c` | 0 conflit |
| 12 | #3132 | `b2e145c40de1` | `8dbb29512` | 0 conflit |
| 13 | #3131 | `9b8c8d00aee7` | `7ddcff97c` | 0 conflit |
| 14 | #3128 | `532396ecb251` | `7743371ec` | 0 conflit |
| 15 | #3133 | `252283d7d780` | `5b1b2c620` | 0 conflit |
| 16 | #3099 | `7f90951b4182` | `d83443a8b` | 0 conflit |
| 17 | #3070 | `7774680dfd33` | `d34990bcd` | 0 conflit |
| 18 | #3048 | `4c8bdb26094a` | `083a0e483` | 0 conflit |
| 19 | #3054 | `074f470c37a6` | `b13c08321` | 0 conflit |
| 20 | #3103 | `c99682cc5a95` | `134749cb4` | 0 conflit |
| 21 | #3111 | `9fcadf970a10` | `1f12c1c10` | 0 conflit |
| 22 | #3096 | `d326f0f85fb3` | `08d873f8b` | 0 conflit |
| 23 | #3066 | `05eae2b0c265` | `083940c4b` | 0 conflit |
| 24 | #2993 | `1a5149c0b1c5` | `fc840be32` | 0 conflit |
| 25 | #3057 | `26ee15631120` | `798856613` | 0 conflit |
| 26 | #3100 | `0d48905c8a77` | `49e232681` | conflit `alert_briefs.py`, `notify.py` résolu (voir ci-dessous) |

Chaque commit de composition a le lot en second parent, son sujet nomme les
lots et leurs SHA, et il porte un trailer `ameesh-work:` par lot. Aucun
réalignement de `main` n'a été nécessaire : la composition part de
`d93b3a468` et aucun commit de `main` n'est venu pendant la composition.

## Résolutions de composition

**#3057 × #3100** (`49e232681`) : la remise enregistrée
`55db6bef2d7e` (`refs/ameesh/resolutions/3057/3100`) n'est pas récupérable par
SHA depuis la forge. Résolution manuelle : union des deux changements —
la route du bureau externe et les rappels de #3057 restent, le couple
`covered`/`live_routed` de #3100 s'y ajoute (le bloc « ancien notificateur »
est déplacé en haut, la garde de relance de #3057 est conservée).
Témoins : `tests.test_3036_alert_liveness` **18/18** (dont les deux nouveaux
témoins #3100) et `tests.test_3057_panier_vivacite` **8/8**.

Aucune autre résolution : les 25 autres fusions sont propres.

## Adaptation mécanique du véhicule

`0f487e660` : `ui/src/api/schema.d.ts` régénéré (`npm run gen`). #3013
(paquet h-1500) a ajouté l'événement `pilot.gesture` au schéma OpenAPI sans
régénérer les types du client ; la tête de `main` est rouge sur le job `ui`
(`schema.d.ts is stale`), préexistant à ce paquet. Le delta est la seule ligne
d'énumération `type: … "pilot.gesture"`, production mécanique du générateur
(`ui/scripts/gen-api.mjs`) — aucun avis requis (§2, adaptation mécanique). Le
job `ui` n'est pas un check requis, mais un test négatif ne reste pas rouge.

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #3007 | `blocked-verdict` : remise #3007×#3014 `2e0804b0` indisponible ; sans nouvelle remise, écarté sur la directive mesh-design 14:33Z |
| #3123 | lecture impossible au calcul (`author provenance unknown` pour `c4dbe777`), non composé, à qualifier |
| #2994 | lecture impossible au calcul (gel antérieur `d8c377cf` absent), non composé, à qualifier |
| #2914 | lecture impossible au calcul (gel antérieur `66a06cf0` absent), non composé, à qualifier |

Les autres lots du calcul sont en attente et n'étaient pas prêts : #114, #390,
#2614, #2987, #3012, #3033, #3056, #3085, #3101, #3104, #3114, #3116, #3120,
#3129, #3130 (motifs du calcul : `freeze-not-head`, `verdicts-missing`,
`blocked-verdict`, conflit). Aucun lot prêt n'est laissé hors paquet.

## Contrôles

- **Liste calculée** : 27 prêts, 17 en attente, 3 lectures impossibles ;
  **27/27 prêts composés**, plus #3014 par décision du pilote. Le recalcul
  depuis l'arbre du paquet est empêché par la base du maillage
  (`election_brief` absent : migration #3012 non appliquée hors staging).
- **Arbitrage #3014** : au calcul, `freeze-not-head` (gel déclaré
  `adc791fb`, verdict pilote porté sur `e81b486a`, une résolution antérieure ;
  2 familles requises, une seule famille de verdicts de relecture). Le pilote
  mesh-design a donné son avis OK sur l'unité `7ade8c387bfc` (note 14:33Z) et
  demandé son inclusion ; le paquet la porte comme une unité unique avec
  #3134 et #3142, dont les deux relectures sont complètes.
- **Prévalidation** : indisponible (`card-stale`, carte de sélection de plus de
  48 h), non bloquant (canon §3) ; motif consigné.
- **Contrôles statiques** : `scripts/freeze-checks.py` **6/6** verts (garde
  mécanique, compilation, attribution, lint, alertes, lecteurs de doc).
- **Tests ciblés** : `test_3036_alert_liveness` 18/18 ; `test_3057_panier_vivacite`
  8/8 ; les auteurs des autres unités ont leurs tests ciblés verts sur leurs
  têtes gelées (voir leurs lots).
- **Migrations** : `0478_role_handover.sql` (#3099) seule migration ajoutée ;
  aucun numéro en collision.
- **Diff** : 107 fichiers, +7295/−454 hors manifeste ; `git diff --check`
  propre ; aucun marqueur de conflit.
- **Contrôle d'inclusion** (`scripts/paquet-inclusion.py`, tête `49e232681`) :
  **aucun défaut**, 26 compositions / 28 lots attendus, 27 prêts sur 27
  présents. Deux contenus lus « partiels », assumés et expliqués :
  - #3057 : la ligne manquante est la fin de la docstring de `_split_live`
    (`parmi les absents au lieu de « none designated »."""`) ; notre union y
    ajoute le paragraphe #3100 avant la fermeture. Aucun code de #3057 perdu
    (18/18 + 8/8 verts).
  - #3138 : la ligne `fire=ledger.ops.train_fire_read(...)` est réécrite par la
    résolution #3138/#3139 (`fire` calculé seulement si un plan existe, puis
    `fire=fire` dans le résultat) : même contenu, forme imposée par #3139.

## CI

La suite complète unique est demandée sur la tête finale qui porte ce
manifeste (étiquette `ci-full-<tête>`, PR vers `main`). Le verdict est le run
de la PR sur cette tête ; les CI des lots couverts sont remplacées par cette
preuve (§12). La fusion attend le vert du check requis `tests` ; la fusion
réelle est faite par le passage watch d'ameesh (pas d'auto-merge GitHub,
#2813), vérifiée dans le journal du porteur 10 min après le vert.
