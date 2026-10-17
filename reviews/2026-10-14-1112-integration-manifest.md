# #1112 — paquet CI et budgets, 14 octobre 2026

Autorisation directe owner : « oui go », session bureau Codex 01a1370d-790d-7051-b98c-59197b9c2858.
Processus : canon manaty/home PR49, §12.
Base : `b85777c1e3e757b9ba0caa8cef49b0db1558b251` (ameesh 1.10.0 déployé).

## Entrées immuables

| Lot | Tête | Couverture |
|---|---|---|
| #880 | `5115dec3ad98650fcd1bf7d07843dc6d62761c9f` | fusion de la tête exacte |
| #925 | `b0aae4e61ef59d01437d504a44b137d828c7ac35` | contenu déjà inclus dans #880 ; aucune duplication |
| #940 | `913eab23ee7744f30b3a7b8441cb359f165fd7cc` | fusion de la tête exacte |
| #947 | `146499d36962207e9a17692bddda764b47e605b1` | fusion de la tête exacte |
| #878 | `d75e0d4900727c0be7570c3257cdde8fa9187cb9` | fusion de la tête exacte |
| #777 | `ef1b22b95a2ead09eef1e1d10746e2abe7fc5d01` | fusion de la tête exacte |
| #1075 | `2fb4913a7bf04470168553e5a380225b6a90f84c` | fusion de la tête exacte |
| #959 | `7950cf87e1c5a07198eaba1d82fb40a63cfc2262` | fusion de la tête exacte |
| #954 | `0722ff4ef9312cb4bc68189c25b76105946f44f3` | fusion de la tête exacte |

## Adaptation finale

Seul conflit textuel : accounts.limits entre #777 et #1075. Résolution : held_cap avec le multiplicateur propre au profil. Le même multiplicateur est propagé à free_at dans accounts._release ; held_cap/exceeded/free_at acceptent un argument facultatif à 1.0. free_at inverse le rythme en divisant la part par le multiplicateur. La marge de début de fenêtre de #777, la réserve, les plafonds fixes et la limite de 90 % restent applicables. Quatre témoins ajoutés couvrent marge, reprise autour du seuil, plafond et appel accounts._release.

Workflow : composition sans conflit de #880 (45 min), #940 (échec explicite des agrégats) et #947 (trois parts psql pilotes, six en suite complète). #959 et #954 modifient des zones disjointes du test offload.

## Avis et conditions

Les avis, gels et familles réellement exécutées sont qualifiés dans le registre des lots et dans #1112 par impl08 et le pilote. Les avis existants ne sont transportés que pour les blobs conservés. Le delta budget ci-dessus exige un avis indépendant final. Les deux avis hors auteur restent nécessaires pour #940 (sécurité) et #878 si classé SQL par le pilote. Aucun GO fusion ne résulte de ce manifeste seul.

QA ciblée : 104 tests (test_1075_pace_multiplier, test_l242b2_budget, test_940_agregat_bash, test_l161f_plan), tous verts. Aucun autre changement après cette mesure sauf les deux correctifs offload test-only.

QA finale : workflow tests, workflow_dispatch sur la tête finale publiée ; tous les modules sur psycopg (4 parts) et psql (6 parts), plus plateforme macOS et agrégat requis. Contrôler les jobs et flux réels, pas seulement le vert agrégé ; annulation ou part attendue absente interdit la fusion. La PR ajoute son contrôle de version et de branche ; aucune série de relances individuelles des entrées.

Fusion : après avis requis, QA complète verte et feu pilote, compare-and-swap sur la tête finale. Si main a évolué, requalifier le delta et refaire la validation nécessaire. Déploiement distinct après fusion, preuve des trois hôtes et vérification du rythme primaire ×2 ; pas de reset de voucher.

Hors périmètre : les autres lots parmi les 94 QA, dont #748/#857 ; ils restent candidats à une vague suivante. Aucun tag/release Nexlink ni réparation Mac dans ce paquet.

## Empreintes des fichiers du paquet avant ce manifeste

| Fichier | Blob Git | SHA256 |
|---|---|---|
| `.github/workflows/tests.yml` | `fbf1e8d0cd7dec6d945c19e23a92b35c88d3dd35` | `3411dc4762a71826e9886bfbdd9fb84ea3a56f9829c9f98f7f22fd7e13f3b0cc` |
| `CONTRIBUTING.md` | `290ca60ff241723e9e7dc63800b7957fe079a232` | `cafbf97c955de01ccee48cf0d07dcdb596dbdd3b0bc5a5e244aaa588a0d8cdd0` |
| `README.md` | `7a7c2971679d9af506bfc176ce89a173f0ed4b14` | `dda39d0dda32a4a9709302e712856ff25314de6b14c3634b693ada01b33b1955` |
| `docs/COMPTES.md` | `586045d455b85de9cab795cb26693bfb17154095` | `8a95e37b9ec4139eea4198d162719da9c655d7bec6621b4def404635c24c959a` |
| `docs/EXPLOITATION.md` | `5dea0449d3a02f5bd0c3f0dd84c195faeb899b98` | `fe245798754e689972b930f9f72e583d48fc220a06cef0fe1d5dfc33b6ce39d2` |
| `scripts/test-plan.py` | `334596234a4b5519b55a3034d4c5c78a41e2481b` | `3639a0a7074986039454bf49e0071912e0231ce6c7bc162b04e2eb82b5769e52` |
| `src/ameesh/accounts.py` | `9ae896b3b79263466ef193c2877563876793e2f4` | `c73be8f7efd8ac3382b4d606c25f5b9632b343ff75c83cca526f89388c23b282` |
| `src/ameesh/cost.py` | `09c528c5cb570f202cb4aaf044ab0302000781c1` | `f625c9fadc51f808b9497c448bdaa828e0022d5194b6d70de8d44a16fd2506a7` |
| `tests/test_1075_pace_multiplier.py` | `9fe762c1ec665e455cc543ca2d6770c5ac55fadd` | `c0dc6137fd47a56bc9601552708abbcba1aff330bf2317681a0d39b26a4cbb20` |
| `tests/test_476_background_signals.py` | `5aa713b88b9689da18b99b42234b4c53707d5a02` | `4de0a542689fa9b38280eba8cb3825dcd364dc74b6e33cdd9d9e95b92575a359` |
| `tests/test_476_ownership_races.py` | `c88df2fd6da16a6ac2571299ffa4b56ac54483ec` | `245f8999d9ddb457fc07b82b25e80916b6e8cfd8aab527f23d8ddd9715762d3b` |
| `tests/test_653_db_connections.py` | `7c719a14282ab4a231f0af37216c6829c346067e` | `dae9ec3f8a98bc91faa0929457d6ac3dc8cb1a37966caddfb2e6f526ca50031b` |
| `tests/test_940_agregat_bash.py` | `e5ac862a451c0ab2a81c36a959f27893197ab329` | `89f47a350e15e863aa1c9ccc73eb7a322e4013217afe3237f8fbc9459a70895e` |
| `tests/test_l159b_offload.py` | `3c581344e85f12c60ee39d512c0edba5d8fac0ed` | `a3b132a42fa5b43b2552231ff0bdc8d696d59c74d6e935f28fb9f96b5b9fbc8b` |
| `tests/test_l161f_plan.py` | `01e72758f0cd8102ee10ec8be75decc2e9b5fc83` | `ac34a047bc71c426bce0a30bf875d1cc857b02d1368a7951fe56ed65361f4314` |
| `tests/test_l242b2_budget.py` | `1161d799db6088eb59b99a47e3d55a145effbddf` | `fc2136cd3dc1fa36b864bf74ec2555a7e0498ac66a3b5d1fd5979b917c4e05f9` |
| `tests/test_work.py` | `4dd28da5526ae3d36db9f4078f55b98d5d270e06` | `b04cdd27848daad6eb454241557b691c8fd337446c43e8ef3a4587aa2aaaeb4c` |
