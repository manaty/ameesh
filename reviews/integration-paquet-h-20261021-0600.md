# Paquet integration/paquet-h-20261021-0600 — composition (§12)

Base : `main` **`b4ae6da91f19`** (release 1.17.0 et fusion du paquet h-0400,
21/10 05:30 Europe/Paris). Branche `integration/paquet-h-20261021-0600`,
cible `main`, lot porteur #2959. Une seule suite complète sur la tête finale.

Composeur : mty-amh-impl-01, pour le pilote mesh-design (message du 21/10
06:10) ; aucun autre paquet composé en parallèle.

## Admissions (10 unités, 10 lots portés)

| lot | source | classe | P | avis | auteurs | objet |
|---|---|---|---|---|---|---|
| #2925 | `36ecd7b9509b` (gel 15182) | light | 1 | mty-amh-impl-02 (deepseek, #15186) | mesh-design (claude) | incident : veille GitHub et projection des issues arrêtées |
| #2928 | `c341c6158935` (gel 15076) | sensitive | 1 | auditeur (claude, #15080) + ameesh-cx3 (codex, #15145) | mty-amh-impl-07 (deepseek) | verdict posé sous `--actor` d'un autre agent : reçu et comptage |
| #2950 | `97655880317a` (gel 15192) | sensitive | 1 | auditeur (claude, #15195) + mty-amh-impl-02 (deepseek, #15204) | ameesh-cx2 (codex) | poste.sh redemarrer : ni sa propre unité ni les suivantes ne tombent |
| #2900 | `d660fcffa87b` (gel 15160) | light | 2 | auditeur (claude, #15167) | mty-amh-impl-07 (deepseek) | panier « #382 » N1 : device_state |
| #2905 | `2ec643d8d547` (gel 15061) | sensitive | 2 | auditeur (claude, #15086) + ameesh-cx3 (codex, #15149) | ameesh-dev5 (deepseek) | spend_key périmé après retour arrière |
| #2924 | `2b25b4af00e4` (gel 15128) | sensitive | 2 | auditeur (claude, #15131) + ameesh-cx3 (codex, #15134) | mty-amh-impl-01 (deepseek) | provenance conservée sous réécriture à contenu identique |
| #2932 | `929027f67689` (gel 15127) | sensitive | 2 | mty-amh-qa-01 (deepseek, #15137) | auditeur (claude) | rotation Codex : contexte du dernier appel au journal de session |
| #2935 | `7a372c6c0ba9` (gel 15088) | sensitive | 2 | auditeur (claude, #15089) + ameesh-cx3 (codex, #15093) | mty-amh-impl-06 (deepseek) | main rouge : destinataire de l'alerte |
| #2951 | `d439bb20b94b` (gel 15199) | sensitive | 2 | auditeur (claude, #15203) + mty-amh-impl-02 (deepseek, #15207) | ameesh-cx3 (codex) | relevé GitHub muet : alerte |
| #286 | `43c6f9b9f945` (gel 15100) | sensitive | 3 | auditeur (claude, #15108) + ameesh-cx3 (codex, #15112) | mty-amh-impl-03 (deepseek) | L77 : file d'entrée et autorité du résident |

Avis et classes lus par `scripts/paquet-candidats.py` sur `b4ae6da91f19`
(porte §2 : `ok` sur le gel exact, aucun avis d'un auteur). Le pilote citait
8 prêts à 06:08 ; #2950 (verdict #15204, 04:09Z) et #2951 (verdict #15207,
04:11Z) sont devenus prêts juste après et sont composés, conformément à la
liste recalculée.

## Composition (10 unités)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #2925 | `36ecd7b9509b` | `be339e81025b` | 0 conflit |
| 2 | #2928 | `c341c6158935` | `2194789d8c3a` | 0 conflit |
| 3 | #2950 | `97655880317a` | `64b74fe951e8` | 0 conflit |
| 4 | #2900 | `d660fcffa87b` | `05c336beb322` | 0 conflit |
| 5 | #2905 | `2ec643d8d547` | `66a50723fce9` | 0 conflit |
| 6 | #2924 | `2b25b4af00e4` | `e448d41921fe` | 0 conflit |
| 7 | #2932 | `929027f67689` | `0cb1c0cb9c0c` | 0 conflit |
| 8 | #2935 | `7a372c6c0ba9` | `4cc82ce8a8f4` | 0 conflit |
| 9 | #2951 | `d439bb20b94b` | `2db48427c0e5` | 0 conflit |
| 10 | #286 | `43c6f9b9f945` | `207301f4bdea` | 0 conflit |

Chaque commit de composition a le lot en second parent, son sujet nomme le
lot et son SHA, et il porte un trailer `ameesh-work: <lot>`.

## Résolutions de composition

Aucune : 10 fusions propres.

## Adaptations de composition (3, témoins seulement)

| source | changement | preuve |
|---|---|---|
| `tests/test_2931_packet_target.py` | le helper `verdict` pose le reçu exact du jalon (`provenance_receipts`, sujet `milestone:<id>`) | #2928 exige un reçu exact en classe sensible ; les 2 témoins `test_2931_packet_target` redeviennent verts |
| `tests/test_2924_history_rewrite_provenance.py` | helper `self.verdict(actor, sha)` avec reçu exact pour les verdicts synthétiques des 3 témoins concernés | #2928, classe sensible ; les 3 témoins #2924 verts |
| `tests/test_1374_fire.py` | le témoin synthétique passe `db=None` à `review_gate` (aucune base à lire, la règle #2928 ne s'applique pas) | les 3 témoins `test_1374_fire` verts ; le chemin avec reçu reste couvert par `test_2928_verdict_provenance` |

Aucune adaptation de code produit : les 10 fusions sont propres et les
comportements relus sont inchangés.

## Écartés (ce véhicule)

| lot | source | motif |
|---|---|---|
| #2906 | `1ba6c1d2205b` | `freeze-after-verdict` : gel postérieur au verdict ; un avis frais de dev2 est attendu (consigne du pilote) ; il rejoindra un paquet suivant |

## En attente au calcul (non admis)

| lot | motif |
|---|---|
| #2955 | `blocked-verdict` |
| #2944 | `verdicts-missing` ; un verdict ok (#15142) a été posé sur le gel #15140, mais la porte ne le compte pas (la correction d'une ligne de mty-amh-qa-01 est lue comme un auteur deepseek) — à revoir après ce paquet |
| #2916 | `conflict` sur `src/ameesh/exploitation.py`, `src/ameesh/slo.py` |
| #2914 | `blocked-verdict` |
| #2771 | `verdicts-missing` (un avis deepseek, deux familles requises) |
| #2895 | `blocked-verdict` ; conflit sur `docs/design/etudes/index.md` |
| #2892 | `verdicts-missing` (un avis deepseek, deux familles requises) |
| #2614 | `blocked-verdict` |
| #114 | `verdicts-missing` (un avis deepseek) ; conflit sur `src/ameesh/exploitation.py` |

## Contrôles

- Liste calculée `scripts/paquet-candidats.py --repo . --target main` sur
  `b4ae6da91f19` : **10 prêts, 10 en attente, 0 erreur** ; aucun lot prêt omis.
- Contenu : 82 fichiers, +7121/−290 hors manifeste ; aucune migration SQL ;
  `git diff --check` propre ; aucun fichier `.github/workflows/**` modifié.
- Ciblés sur l'arbre composé (local, sans CI) : 266 tests, 8 rouges de la
  première passe tous dus à #2928 (reçu exact en classe sensible) ; témoins
  adaptés ci-dessus, reprise des 3 modules : **134 tests, OK**. Aucune suite
  complète locale (une seule, en CI).
- Contrôle d'inclusion `scripts/paquet-inclusion.py` sur `b9dade7f` : 10
  entrées (10 compositions, 0 retrait), 10 lots attendus, 10 prêts,
  verdicts présents 10/10, aucun contenu à lire ni défaut (rc 0).
- CI complète : à demander sur la tête gelée (une seule suite).
