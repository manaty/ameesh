# #1566 — fraîcheur d'un relevé au classement : les deux N de la relecture de #1561

- Lot : `manaty/ameesh#1154` (#1566), classe légère, branche `lot/1566-fraicheur-l226b`.
- Source : avis ameesh-dev2 du 15/10/2026 07:02, **GO 0 B / 2 N (P3)** sur `1660ff6a`
  (relecture de #1561) ; admission au paquet de 11 h confirmée par mesh-design.
- Arbitrage de conception : **GO du pilote mesh-design, 15/10/2026 18:02Z**,
  enregistré au journal du lot (dépendances #1580 et #1672 ajoutées au même moment).
- Empilement : union dédiée `#1580` tranche 1/2 `@1579cd8dc` + `#1672` `@23185d29`
  (commit `1e66478b`), **sans tranche 3**, les deux têtes gelées inchangées.

## Contexte

#1561 range tout relevé périmé après tout relevé courant (`accounts._rank_key`),
avant même l'ordre déclaré (L226b) : un compte dont une fenêtre datée s'est
remise à zéro depuis la lecture, une autre courant encore, n'est plus un
instantané. La relecture Claude de #1561 a laissé deux N (P3), non bloquants :

- **N2** (théorique) — `cost.stale_reading` ne regardait que les fenêtres
  datées : une fenêtre datée échue **plus** une fenêtre ouverte sans `resets_at`
  donnait `all(passed) = True`, donc « non périmé ».
- **N1** (conception) — le classement inverse l'ordre de bascule : un secondaire
  écarté par son plafond 5 h n'est pas repris après la remise à zéro tant qu'un
  primaire reste courant.

## N2 — une fenêtre non datée ne prouve pas sa remise à zéro

Règle : une fenêtre sans `resets_at` ne prouve pas sa remise à zéro ; elle
compte comme **encore ouverte**, exactement comme une fenêtre datée non échue.
`cost.stale_reading` ne conclut « courant » sur `all(passed)` que si **toutes**
les fenêtres sont datées ; sinon le relevé reste partiel (`True`).

Implémentation : `src/ameesh/cost.py`, `stale_reading` (commit N2, rebasé sur
l'union). Témoins : `MelangeDateeNonDateeTest`,
`tests/test_1566_fraicheur_l226b.py`.

## N1 — la confiance bornée du classement d'un relevé partiel

Règle du pilote (citation du GO, 15/10/2026 18:02Z) : un relevé partiel ne
rejoint `freshness = 1` que si

1. son **âge est connu, fini et non futur** ;
2. `âge <= reste` d'**au moins une** fenêtre datée encore ouverte — même
   frontière que `cost.reading_outdated` (#1672), **égalité admise** ;
3. **chaque** fenêtre encore ouverte, **y compris non datée**, garde
   strictement `RESERVE` de marge sous son plafond effectif
   `Evaluation.limits` : `usage < max(0, plafond_effectif − RESERVE)`,
   **égalité refusée**.

Interdits explicites du GO : pas de nouveau seuil `lag = 0,5` ; pas de
`held_cap` seul (il ignorerait plafond fixe, court ou d'overage) ; aucun
plafond ni réserve existant relevé. Sans date utilisable ou marge démontrée :
`freshness = 2`, **jamais 0** (0 reste le relevé absent, L117). La raison
annonce un relevé **partiel** encore informateur, pas une lecture fraîche
mesurée.

Ce qui ne change pas : `placeable` et `cost.reading_outdated` (#1672), la
continuité (une session déjà sur le compte y reste), le forçage manuel, la
règle L117 (relevé absent d'abord), L71 (fenêtres échues / remises à zéro
comptent 0 %).

Note d'implémentation : la marge `RESERVE` s'applique **littéralement** au
plafond effectif `Evaluation.limits`, comme l'énonce le GO. Quand l'appelant
passe `reserve=True` (agent non critique), les plafonds de rythme et de fenêtre
courte sont déjà abaissés de `RESERVE` : la règle ajoute donc 10 points de
marge supplémentaires sur ces plafonds-là. C'est conservateur et conforme à la
lettre de l'arbitrage ; les plafonds fixes (`account_cap`, `overage_cap`) ne
sont abaissés qu'une fois.

Implémentation : `accounts._informative_partial`, appelée par `_rank_key`
(et par `reason_of` pour nommer la raison), `src/ameesh/accounts.py`.

## Témoins (`tests/test_1566_fraicheur_l226b.py`)

- `PartielInformateurTest.test_partiel_informateur_garde_le_rang_courant` —
  fraîcheur 1 (jamais 0), passe devant un relevé courant par le budget ; raison
  « partial reading still informative », pas « furthest behind its pace ».
- `test_age_frontiere_egalite_admise` — égalité `âge == reste` admise, une
  seconde de plus refusée.
- `test_marge_frontiere_egalite_refusee` — égalité au seuil refusée, un cheveu
  dessous admis ; plafond sous la réserve : aucune marge, refus.
- `test_fenetre_non_datee_ouverte_compte_dans_la_marge` — la fenêtre non datée
  (N2) compte dans la marge, comme les datées.
- `test_fenetre_non_datee_seule_ne_prouve_aucune_ouverture` — sans fenêtre
  datée ouverte, pas de borne d'âge : fraîcheur 2.
- `test_sans_age_connu_refus` — `read_at` absent : fraîcheur 2, raison
  « tried (no current reading) ».
- `test_plafond_effectif_et_non_held_cap` — le plafond effectif (fixe, court)
  décide, pas le rythme ; plafond absent : refus.
- `test_incident_1672_reste_refuse` — le cas réel du 15/10 (relevé de 4 jours,
  7 j à 9 h de sa remise à zéro) reste non informateur et écarté du placement,
  même avec `paced=False`.
- `test_paced_false_garde_la_regle_mais_ouvre_le_seuil_de_rythme` — garde de
  budget désactivée : le partiel informateur au seuil de rythme redevient
  utilisable (`unpaced_ok`), jamais le relevé écarté par l'âge.
