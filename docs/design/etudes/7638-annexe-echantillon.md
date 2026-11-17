---
type: Study
title: "Annexe reproductible de l’étude #7638 — échantillon de 60 tours de gouvernance"
description: "Tirages, identifiants, classes, coûts et facteurs de l’échantillon de 60 tours de l’étude #7638 ; aucun contenu de conversation n’est reproduit."
status: draft
tags: [gouvernance, echantillon, reproduction, tours, cout]
generated: { by: "mty-amh-impl-01/deepseek", at: "2026-11-17T03:02:00Z" }
stale_after: 2027-02-17
sources:
  - { resource: "base du mesh (AMEESH_DSN), tables turn_costs, ameesh_events, work_items, 2026-11-10 02:25Z → 2026-11-17 02:25Z", title: "population, tirage et étiquettes" }
  - { resource: "7638-gouvernance-mecanique.md", title: "étude principale" }
---

# Annexe — échantillon de 60 tours de gouvernance

Cette annexe donne tout ce qui permet de retrouver les 60 tours de l’étude
#7638, leur classe et les estimations qui en découlent : population, requête
de tirage, identifiants, étiquettes, coûts, règle de classement et facteurs
d’extrapolation. Elle ne reproduit aucun contenu de conversation : les motifs
sont des catégories, pas des citations.

## A. Population et tirage

- **Fenêtre** : 2026-11-10T02:25Z → 2026-11-17T02:25Z (7 jours glissants).
- **Agents** : `auditeur`, `mesh-design`, `orchestrateur` (les trois agents
  `claude-opus-5-5` de gouvernance du mesh Manaty).
- **Tour avec geste** : un `pilot.gesture` du même agent tombe entre
  `turn_costs.started_at` et `turn_costs.recorded_at`. Les trois agents sont
  mono-tour : la jointure est exacte à la seconde.
- **Population échantillonnée** : les tours **sans geste** — 2 983 tours,
  905,87 $ —, parce que c’est là que se joue la décision de classer.
- **Tirage** : 20 tours par agent, triés par `md5(turn_id)` croissant. Le
  tirage est déterministe, sans graine ni hasard : rejouer la requête sur la
  même fenêtre redonne les mêmes 60 identifiants.
- **Lecture** : consigne d’entrée et texte final de chaque tour, lus
  intégralement pour classer ; aucun texte n’est reporté ici.

Requête de tirage (PostgreSQL, base du mesh) :

```sql
WITH labels AS (
  SELECT DISTINCT ON (e.data->>'turn_id')
         e.data->>'turn_id' AS turn_id,
         e.data->>'turn'   AS label
  FROM ameesh_events e
  WHERE e.type = 'turn.context'
    AND e.created_at >= '2026-11-09T00:00Z'
    AND e.created_at <  '2026-11-17T03:00Z'
  ORDER BY e.data->>'turn_id', e.id
),
population AS (
  SELECT t.turn_id, t.agent, t.usd, l.label,
         EXISTS (
           SELECT 1 FROM ameesh_events g
           WHERE g.type = 'pilot.gesture'
             AND g.data->>'agent' = t.agent
             AND g.created_at BETWEEN t.started_at AND t.recorded_at
         ) AS has_gesture
  FROM turn_costs t
  LEFT JOIN labels l ON l.turn_id = t.turn_id
  WHERE t.agent IN ('auditeur','mesh-design','orchestrateur')
    AND t.recorded_at >= '2026-11-10T02:25Z'
    AND t.recorded_at <  '2026-11-17T02:25Z'
),
ranked AS (
  SELECT *, row_number() OVER (PARTITION BY agent ORDER BY md5(turn_id)) AS rn
  FROM population
  WHERE NOT has_gesture
)
SELECT agent, rn, turn_id, label, usd
FROM ranked
WHERE rn <= 20
ORDER BY agent, rn;
```

Les 60 lignes du tableau B sont exactement la sortie de cette requête,
complétée par la classe et le motif.

## B. Échantillon classé

Légende : **J** = jugement ; **M1** = triage sans conséquence ; **M2** =
contrôle d’état / rapport périodique ; **M3** = relais / routage ; **M4** =
continuité (consigne, reprise, suite). « Retirable S1–S4 » dit si la règle
proposée supprime le tour ; un tour mixte est classé **J** (règle
conservatrice), donc `non`.

| # | Agent | `turn_id` | Étiquette | USD | Classe | Retirable S1–S4 | Motif (sans contenu) |
|---:|---|---|---|---:|---|---|---|
| 1 | auditeur | `da9b227c7c92448898bdfeefbf9c3fba` | messages | 0,560155 | J | non | verdict de revue |
| 2 | auditeur | `3bce7eb4cd1a473683a6048e1e049803` | événements | 0,182198 | M3 | oui | relais vers le pilote |
| 3 | auditeur | `5756f9329da7408fa380d18a7403d2a9` | messages | 0,295895 | J | non | verdict de revue bloqué |
| 4 | auditeur | `2b90d411e2c84f1fb2746c7395283a1d` | messages | 0,872266 | J | non | deux verdicts de revue |
| 5 | auditeur | `9ce8fc6d81bb4e9193285e07e2e07ad6` | messages | 0,670083 | J | non | verdict de revue |
| 6 | auditeur | `c5a895cd772141238ab0f1054e74b3a9` | messages | 0,151850 | M2 | oui | passage horaire, rien n’a changé |
| 7 | auditeur | `f05a649f996047c395fa6099ddbba8bd` | messages | 0,108369 | J | non | avis technique conditionnel |
| 8 | auditeur | `043c2967280d427ea69d9db51b6498e1` | consigne | 0,382670 | M4 | non | synthèse de session |
| 9 | auditeur | `e65642042e5f4ce6b7d3217f069c91b9` | consigne | 0,106424 | M4 | non | synthèse de session |
| 10 | auditeur | `d7e2085260d342359cac6f4ee0d9b02f` | messages | 1,107997 | J | non | verdict de revue bloqué |
| 11 | auditeur | `40ab2b8ff70144169ed6c916e3a08faf` | consigne | 0,605527 | M4 | non | synthèse de session |
| 12 | auditeur | `4951f290cc28454aa7aa3fe3bc017aad` | suite | 0,201395 | M4 | non | continuation après plafond |
| 13 | auditeur | `a2ba4e2819c0453c91ef8b6f17109fbf` | messages | 0,534093 | J | non | verdict de revue |
| 14 | auditeur | `4b41cb443d92425881c3b68bbce043f4` | messages | 0,553847 | J | non | verdict de revue SQL |
| 15 | auditeur | `ae9e36a6eb5b4c368e5efc08c672e8f8` | messages | 0,331169 | J | non | adjudication de constat |
| 16 | auditeur | `021eb361c9c348e88de5a63d765c4036` | messages | 0,192329 | J | non | avis technique conditionnel |
| 17 | auditeur | `23d369ab18fb463296dbc4fa1d479234` | messages | 0,249109 | J | non | verdict de revue |
| 18 | auditeur | `b69c2b93da334bbe844a9de785348faa` | événements | 0,146645 | M3 | oui | relais vers le pilote |
| 19 | auditeur | `579bc237040e4d038112b321b87e42a8` | messages | 0,584847 | J | non | verdict de revue |
| 20 | auditeur | `fd2102cbf67b413fa62a181ea6890496` | messages | 0,192948 | M2 | oui | passage horaire, rapport |
| 1 | mesh-design | `38201876437d4614aafdc60afb6fc45f` | messages | 0,115828 | M1 | oui | écho de ses propres gestes |
| 2 | mesh-design | `80058c0b3d774021859e3eb01f82f896` | messages | 0,197567 | M1 | oui | revue déjà chez l’auteur |
| 3 | mesh-design | `1135b9707cfa4488b87e3fcd2dc90c24` | messages | 0,088236 | M1 | oui | aucune action requise |
| 4 | mesh-design | `2c821f923e734f5bb75badae84b27ed8` | messages | 1,186902 | J | non | diagnostic de paquet, ouverture de lot |
| 5 | mesh-design | `f2663307492b48769c83160afa098dbe` | messages | 0,263708 | J | non | arbitrage infra conditionnel |
| 6 | mesh-design | `29f2eec7f8da45d59e97a1d72a4bffb7` | messages | 0,228822 | J | non | arbitrage de revue |
| 7 | mesh-design | `4a071775bf6c471abed98d9e67b88382` | messages | 0,071213 | M1 | oui | alerte postérieure à la clôture |
| 8 | mesh-design | `cd4af69527eb429eb51d272970ff6058` | messages | 0,077345 | M1 | oui | aucune action requise |
| 9 | mesh-design | `accf1bce7b2c469bbe604e9dfe525156` | messages | 0,261315 | J | non | application de décision owner |
| 10 | mesh-design | `9412a6e4b7b444a3ae9ecf30e061edc6` | messages | 0,286525 | J | non | diagnostic de CI |
| 11 | mesh-design | `04cfd1e10cbf46d99491c3be14b1911b` | événements | 0,159597 | M2 | oui | contrôle d’état CI |
| 12 | mesh-design | `5cc6b1beb6f144cb8306e682f708f61d` | messages | 0,228477 | J | non | arbitrage de configuration |
| 13 | mesh-design | `849371f5dd7345ddb0b737815be3803b` | messages | 0,701693 | J | non | passe de veille, ouverture de lot |
| 14 | mesh-design | `5940400773494146becf7a8e18389ff5` | messages | 0,346190 | M3 | oui | relances, dépendances, routage |
| 15 | mesh-design | `ae5f0b713bd3480f8bdf7518e0cbcea5` | reprise | 0,214466 | M4 | oui | reprise de paquet |
| 16 | mesh-design | `ab11d40a608349ca9a4cb70820c13064` | événements | 0,308554 | J | non | arbitrage de hold |
| 17 | mesh-design | `e719461bdfb841db967d22eea2ba787c` | messages | 0,811885 | J | non | escalade d’accès admin |
| 18 | mesh-design | `6b137be7632947e692acbacb5866dbf2` | messages | 0,229908 | M2 | oui | contrôle d’état paquet/CI |
| 19 | mesh-design | `bf80abce19d24cc49a4188b78128178d` | reprise | 0,323693 | M4 | oui | reprise de paquet |
| 20 | mesh-design | `fecc1163953e49999e81b1281cbbe5af` | événements | 0,369544 | J | non | composition de paquet, ouverture de lot |
| 1 | orchestrateur | `3c11f5b2e2544c29bc9f6e27b18a03d9` | événements | 0,053489 | M1 | oui | pause budget attendue, aucune action |
| 2 | orchestrateur | `4b197171962d4eff881f6163b1c29b43` | messages | 0,280165 | J | non | diagnostic de déploiement |
| 3 | orchestrateur | `277d168964e74d3faea04ced4e19781f` | messages | 0,255795 | M3 | oui | routage, dépendance, consigne |
| 4 | orchestrateur | `6c2640d302dd4813a1318befe853550a` | messages | 0,029775 | M1 | oui | aucune action requise |
| 5 | orchestrateur | `027f13ea3c1849828bd7fbadeb790035` | messages | 0,435652 | J | non | décision de pilote sur la feuille de route |
| 6 | orchestrateur | `0c4699fe5cd64ec18e37bca51245df15` | événements | 0,272786 | J | non | décision de paquet et vérification |
| 7 | orchestrateur | `592061f1255745de95a7a1a6b8541ee9` | messages | 0,277702 | M2 | oui | état des lots et alertes |
| 8 | orchestrateur | `ffee46bdf59649718b4936fc6833d8f7` | messages | 0,216248 | M3 | oui | relais, correction de note |
| 9 | orchestrateur | `5d31657a7fe6428cae916bc98ddf2c9a` | événements | 0,323187 | J | non | fusion de paquet, go staging |
| 10 | orchestrateur | `e63d00afdeee4f5f862faf6eb1eef6c8` | événements | 0,096315 | M1 | oui | fusion automatique, rien à faire |
| 11 | orchestrateur | `acae046fae7745de884c05b7680f42ad` | messages | 0,071837 | M1 | oui | aucune action requise |
| 12 | orchestrateur | `bb56233f08664fb28becfda54a333173` | messages | 0,585355 | J | non | diagnostic de test instable, ouverture de lot |
| 13 | orchestrateur | `8096477282694931ba99f850026879a1` | messages | 0,109896 | M1 | oui | contrôle : composeur vivant |
| 14 | orchestrateur | `3cfa708a20bd4119a7775f54b027fe90` | messages | 0,138257 | M2 | oui | état du paquet et de la file |
| 15 | orchestrateur | `c209f8fce2314deaac0e35a88642447d` | messages | 0,316545 | J | non | release, ordre des migrations |
| 16 | orchestrateur | `449e55e2725940b5a1e6da4ae7925418` | messages | 0,066438 | M2 | oui | release attestée, rien à faire |
| 17 | orchestrateur | `18268d6bcbf54dd3ae45d65217130fe1` | messages | 0,186890 | M3 | oui | dépendance et parquage de lot |
| 18 | orchestrateur | `6842f0f412e14b97b5db69e22be9d1c5` | suite | 0,336833 | M4 | non | continuation après plafond |
| 19 | orchestrateur | `53d8905e394b49bcbd437df36ed62919` | messages | 0,129034 | M2 | oui | compte rendu d’opération prod |
| 20 | orchestrateur | `466250d8087148b1a54c4894edc0df25` | prioritaire | 0,353215 | J | non | application de décision owner |

## C. Facteurs d’extrapolation

Chaque agent est une strate : le facteur est le nombre de tours de la
population divisé par les 20 tours tirés. Les coûts sont ceux du grand livre
(`turn_costs.usd`).

| Agent | Population sans geste (tours) | Coût (USD) | Échantillon (tours) | Coût (USD) | Facteur tours |
|---|---:|---:|---:|---:|---:|
| auditeur | 878 | 349,589 | 20 | 8,030 | 43,90 |
| mesh-design | 1 178 | 361,224 | 20 | 6,471 | 58,90 |
| orchestrateur | 927 | 195,056 | 20 | 4,535 | 46,35 |
| **Total** | **2 983** | **905,869** | **60** | **19,037** | — |

## D. Extrapolation par classe

L’estimation brute applique le facteur de chaque strate. Le total brut
(943,89 $) dépasse de 4,2 % le coût exact de la population (905,87 $) : les 60
tours tirés sont un peu plus chers que la moyenne. La colonne « USD calibré »
applique donc aux parts de coût le total exact de la population
(facteur 0,9597) ; c’est cette colonne qui est reprise dans l’étude. Les tours,
eux, se somment exactement à 2 983.

| Classe | Échantillon (tours) | Échantillon (USD) | Estimation (tours) | USD brut | USD calibré | Part du coût |
|---|---:|---:|---:|---:|---:|---:|
| Jugement | 29 | 13,274 | 1 440 | 658,8 | 632,2 | 69,8 % |
| M1 — triage sans conséquence | 10 | 0,912 | 526 | 49,2 | 47,2 | 5,2 % |
| M2 — contrôle d’état / rapport | 8 | 1,346 | 391 | 66,4 | 63,7 | 7,0 % |
| M3 — relais / routage | 6 | 1,334 | 286 | 65,4 | 62,7 | 6,9 % |
| M4 — continuité | 7 | 2,171 | 340 | 104,2 | 100,0 | 11,0 % |
| **Mécanique (M1–M4)** | **31** | **5,762** | **1 543** | **285,1** | **273,7** | **30,2 %** |
| **Total** | **60** | **19,037** | **2 983** | **943,9** | **905,9** | **100,0 %** |

Le facteur de calibration est `905,87 / 943,89 = 0,9597`. Les parts de tours
ne sont pas calibrées : elles valent déjà exactement 2 983.

## E. Étiquettes exactes de continuité et sous-ensemble évitable de S4

Les étiquettes de continuité sont comptées exactement, sans extrapolation.
Mais une étiquette `reprise` ou `suite` n’est pas, à elle seule, un tour
mécanique supprimable : le tableau sépare la population de l’étiquette du
sous-ensemble que la règle S4 retire réellement.

| Étiquette | Tours (tous) | USD | Tours sans geste | USD sans geste | Dont lots de paquet | USD lots de paquet |
|---|---:|---:|---:|---:|---:|---:|
| `consigne` | 97 | 30,18 | 89 | 25,13 | 29 | 5,93 |
| `reprise` | 217 | 79,99 | 135 | 42,38 | 61 | 13,75 |
| `suite` | 47 | 34,75 | 33 | 22,35 | 3 | 1,69 |
| **Total** | **361** | **144,92** | **257** | **89,86** | **93** | **21,37** |

S4 ne retient que les **61 reprises de lots porteurs de paquet sans geste**
(13,75 $) : le composeur peut les prendre en charge et les clore lui-même.
Le sous-ensemble « lots de paquet » est identifié par le titre du lot
(`work_items.title LIKE 'Paquet%'`) : les champs `package_id` et
`package_parent` ne sont pas renseignés pour ces lots dans la fenêtre, et le
titre est le marqueur stable du porteur. Les 89 `consigne` (25,13 $) restent
une synthèse de rotation — la garde de l'étude en conserve une par rotation ;
les 33 `suite` (22,35 $) sont la continuation du travail après un plafond,
pas un geste mécanique ; les 74 reprises hors paquet (28,63 $) ne sont pas
portées par le composeur. Les 257 tours et 90 $ de la ligne « total » sont
donc la **population exacte des étiquettes de continuité**, pas le gain de S4.

## F. Limites de lecture

- 60 tours, 20 par agent : précision d’environ ±12 points à 95 % sur une
  proportion proche de 50 %. Les volumes et coûts par classe sont des
  estimations, pas des comptes exacts.
- La frontière jugement/mécanique est un jugement documenté, appliqué de
  façon conservatrice (tout tour qui produit un verdict, un diagnostic, un
  arbitrage ou une décision conditionnelle est classé J).
- Les 776 tours avec geste (451,41 $) ne sont pas échantillonnés ; aucun gain
  de l’étude ne les compte. Les 61 reprises de paquet de S4 sont, elles,
  comptées exactement.
- La fenêtre est figée ; une requête rejouée plus tard peut voir des
  messages ou des tours enregistrés après coup avec un horodatage dans la
  fenêtre. La date de lecture de cette annexe est le 2026-11-17T03:02:00Z.
