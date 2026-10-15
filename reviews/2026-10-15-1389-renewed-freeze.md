# #1389 — un gel renouvelé au même SHA ne refait pas le verdict

Source : mty-amh-impl-02, courrier 15/10 01:14 ; constats #514/#856 après gels
rafraîchis. Base : `ef4461b54ad2787c8e0444350e0a4ef56f2303fa` (main réel).
Branche : `review/deepseek5-1389-renewed-freeze`.

## Cause

`progress._work_item_milestones` décidait `verdict_current` par les seuls
instants (`avis >= dernier gel`). Un gel administratif renouvelé APRÈS un avis,
mais au MÊME commit, faisait donc passer le lot de `approved` à `review` :
`stagnation.waiting` réaffichait « verdict attendu » et le lot redevenait
stagnant, alors que gel = verdict = tête.

Relevé de première main sur le registre (15/10 01:3xZ, lecture seule) :

| Lot | Avis sur la tête | Gel renouvelé | Même SHA ? |
|---|---|---|---|
| #514 | ok @23:00:20 et 23:01:45, sha `19df643ef070` | 23:11:32, sha `19df643ef070` | oui |
| #856 | ok @23:00:36, sha `cbebe1857407` | 23:11:33, sha `cbebe1857407` | oui |

Sur ces données, la frise rendait `review` / « verdict by … » pour les deux
lots ; après correctif, `approved` / « merge (executor not designated) ».

## Correctif

`_work_item_milestones` conserve le SHA de chaque gel et de chaque avis et
décide `verdict_current` ainsi :

* un avis nomme une tête (`sha`) : il reste courant tant que le dernier gel
  nomme le MÊME commit (préfixe admis), même si le gel est plus récent — le
  renouvellement administratif ne refait pas la revue ;
* un avis d'une autre tête ne compte pas ;
* sans SHA des deux côtés (déduction par le journal, action `git-merge`), on
  retombe sur la comparaison d'instants ;
* le verdict retenu reste le plus récent : un NO-GO plus récent sur la même
  tête n'est pas masqué.

`lot_state` s'appuie sur `verdict_current` (repli par les instants pour des
jalons partiels). `stagnation.waiting` n'a pas changé : il lisait déjà
`verdict_current`.

Périmètre : diagnostic d'attente/alerte seulement. Aucune règle d'admission,
de famille ou de feu touchée ; aucun avis historique réécrit, aucune
modification SQL, de contrôle observé ou économique.

## Tests

Unitaire (`tests/test_progress.py::JalonsTest`) :

* `test_gel_renouvele_meme_sha_preserve_le_verdict` — même SHA, gel tardif ;
* `test_verdict_d_une_autre_tete_ne_compte_pas` — SHA différent ;
* `test_gel_renouvele_meme_sha_ne_masque_pas_un_no_go` — NO-GO plus tard sur
  la même tête ;
* `test_plusieurs_lecteurs_le_dernier_avis_sur_la_tete_gagne`.

Postgres (`ProgressPgTest.test_gel_renouvele_meme_sha_garde_le_verdict`) : la
frise complète (`work.milestone` + `progress.snapshot`) reste `approved` /
attente fusion au même SHA, et repasse `review` sur une autre tête.

Exécution : `scripts/test.sh tests.test_progress tests.test_plan
tests.test_l26_exploitation tests.test_420_qa_role`, pilotes psql puis psycopg.
