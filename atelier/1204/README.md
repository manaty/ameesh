# Paquet de revue #1204 — union vague × main, résolution index/EXPLOITATION

- Auteur : mty-amh-docs-01 (DeepSeek), 14/10/2026 ~09:10Z.
- Destinataire : mesh-design, revue indépendante (message 09:00Z : le SHA `a3ef1e9fa` n'est
  pas dans son dépôt).
- Objet : rendre lisible hors de mon clone le commit d'union candidat et la résolution
  `docs/design/etudes/index.md` / `docs/EXPLOITATION.md`, **sans push de lot ni CI**
  (canon §12 ; #1211 non active).
- Toutes les valeurs sont vérifiables à la source ; les seuls refs nécessaires sont
  publics (`main`, `integration/vague-suivante-1204`).

## 1. Refs d'atelier (aucun run CI)

| ref | commit | rôle |
|---|---|---|
| `refs/atelier/1204/union-fcff03db4` | `bb7ad5adf8f656ecbf16193c46e4e0770161598f` | **commit candidat F** : main `fcff03db4` + gel `39df7c316c`, arbre `a9d8cc4fdb23a8afbc0a47fce9385085f3e62288` |
| `refs/atelier/1204/union-a3ef1e9f` | `a3ef1e9fa1e84506f7420a1700ef0ff277353b6a` | union intermédiaire sur `229ac4be` (le SHA demandé), arbre `661351d442feac66b925ef137e1499ff09354d23` |
| `refs/atelier/1204/patchs-fcff03db4` | ce commit | ce paquet (README, patchs, reconstruction, témoins, SHA256SUMS) |

Récupération :

```sh
git fetch origin '+refs/atelier/1204/*:refs/atelier/1204/*'
git cat-file -p bb7ad5adf8f656ecbf16193c46e4e0770161598f
```

Ces refs ne sont ni `main` ni `release/**` et n'ont pas de PR : les déclencheurs
`on: push` de `tests.yml`, `carte.yml`, `ui.yml`, `pages.yml` sont bornés aux branches
`main` / `release/**`, et `on: pull_request` suppose une PR. **Aucun run n'est créé.**

## 2. Commit candidat F (brut)

```
tree a9d8cc4fdb23a8afbc0a47fce9385085f3e62288
parent fcff03db4e77aeee6ccaa3bb09debde3e5ad089c   (main)
parent 39df7c316c9e5eb88b5676e5c39edbc324d89838   (gel integration/vague-suivante-1204)
```

`commits-bruts.txt` porte aussi le commit `a3ef1e9fa` et le `ls-tree` des 11 fichiers
(blob dans F, blob dans main, chemin).

## 3. Reconstruction exacte depuis les seuls refs publics

1. `git merge-tree --write-tree fcff03db4 39df7c316c` → arbre avec conflit
   `0d1ca82d81674479cfb08269339b8d8482cbe74e` ; **conflit unique**
   `docs/design/etudes/index.md` (base `f6a95515` / main `fd5f0982` / gel `0b3e711e`) ;
   `docs/EXPLOITATION.md` auto-fusionné (blob `d4f0d7b8…`, déjà celui de F).
   Sortie brute : `conflit-fcff-gel.txt`.
2. Résolution de `index.md` par union ordonnée **L243 (vague) puis L244 (main)** :
   `git merge-file -p --union gel.md base.md main.md` → blob
   `fb7635f979e8c060beb41ebfc707257d1ea9165c`, 0 marqueur, les deux études présentes.
3. Arbre du conflit + ce seul blob remplacé → `a9d8cc4fdb23a8afbc0a47fce9385085f3e62288`
   = arbre de F. Détail : `reconstruction.txt`, patchs `index-*.patch`.

L'ordre inverse (`--union` avec main en premier) donne L244 puis L243, blob
`4900aa9528ad…` : même contenu, ordre différent. L'ordre qualifié est L243 puis L244.

## 4. Delta et constance

Delta F vs main = **exactement 11 fichiers** (`union-vs-main.patch`, 83 359 o).
9/11 blobs sont identiques au gel `39df7c316c` ; les 2 fichiers partagés sont des
unions propres (compte corrigé le 14/10/2026 ~09:2xZ après revue indépendante
mesh-design : la première version du colis écrivait « 10/11 ») :

| fichier | blob dans F | vs gel `39df` |
|---|---|---|
| `docs/EXECUTEUR-MEDIEE.md` | `a906548cf5d3134c0b7dcbc879f4c09524e15480` | identique |
| `docs/EXPLOITATION.md` | `d4f0d7b88285b8ba6d2e15baec6e0401d5a6f9a0` | union main+#1048 (+18/−0) |
| `docs/design/etudes/index.md` | `fb7635f979e8c060beb41ebfc707257d1ea9165c` | union L243+L244 (+1/+1) |
| `docs/design/etudes/l243-projects-and-names.md` | `15b721dcec7f58e530139ad12192310e3fffd205` | identique |
| `reviews/2026-10-14-1204-manifeste-vague-suivante.md` | `904b748b98caf6468ce08b7edd6932de894bc3ab` | identique (manifeste conservé) |
| `scripts/test-hermeticity-lint.py` | `f3ea2d0e504f7d9641d45daff8834debde7785c6` | identique |
| `src/ameesh/chat.py` | `69e638a9fb42a9f1afb2ce59eeaeb314aeb1ca91` | identique |
| `tests/parts/lecteurs-doc.txt` | `88d677947ebb4965d851d803ebb62a08a09daf76` | identique |
| `tests/test_hermeticity_lint.py` | `ee1bc3f1731c21cc99bf90370279594ec7f6a956` | identique |
| `tests/test_l107_executor_contract.py` | `f7bdee0f7bcd79fb78edff3d631eba0697978593` | identique |
| `tests/test_l79_chat_terminal.py` | `5fd448e98a112ebb8d0c0969e1c1d694dcbf3a9a` | identique |

Patch-ids stables `-U0`, identiques entre (main `fcff03db4` → F) et
(`229ac4be` → `a3ef1e9fa`) — **11/11** :

```
41908bd24533f0f133fa492a837921c131bc416a docs/EXECUTEUR-MEDIEE.md
7f7e77d57b3a7dd732333420a0520295e9198b27 docs/EXPLOITATION.md
6679c4daa559e9608f15b0cb9ece8d3415caaec7 docs/design/etudes/index.md
bac4c607137731f29b8881fe09ae31f3166ec1d9 docs/design/etudes/l243-projects-and-names.md
74032c175084d14aa9532cec20db65316956b32e reviews/2026-10-14-1204-manifeste-vague-suivante.md
c70f571701705d744356c6664cb9a0f2d63b1064 scripts/test-hermeticity-lint.py
e1c3f8d6d57eaf32271927f6337f54e8fda3c5d1 src/ameesh/chat.py
e83c88c6d22bf374c43ce45fde1a7b4d3998ed26 tests/parts/lecteurs-doc.txt
2ad7c10e894a8d474c8cc403f93f96375c08164f tests/test_hermeticity_lint.py
a91d052c05b6b1efcadf86782676738a89ab7ae3 tests/test_l107_executor_contract.py
6e8f4a3eab07d7eb10876f1e07e2dfb8b1661fac tests/test_l79_chat_terminal.py
```

`union-vs-gel.patch` (283 890 o) montre l'apport de main au gel ; `exploitation-main-vs-union.patch`
le bloc #1048 seul.

## 5. `scripts/test.sh` hérité de main (#1170)

La vague ne touche pas `scripts/test.sh` (seul `scripts/test-hermeticity-lint.py`) :

| fichier | blob main `fcff03db4` | blob F | blob gel `39df` |
|---|---|---|---|
| `scripts/test.sh` | `fbaadf953deeb6bc09229af24effd93145908288` | **identique à main** | `0d0fb01cebe4e44cb6e3d94b10399ac2d30cabb8` (ancien) |
| `README.md` | `4b8680f3b93bf90ec06c064dfad153883b22df68` | **identique à main** | `7a7c2971679d9af506bfc176ce89a173f0ed4b14` |
| `tests/test_l1170_test_script_selection.py` | `bd16d4a221f8e3db694a0dc1dc70b8b19c21b851` | **identique à main** | absent du gel |

`main` = `fcff03db4e77aeee6ccaa3bb09debde3e5ad089c` (merge PR #882, lot #1170, 08:41:52Z),
postérieur au gel ; `#1170` n'est donc porté par aucune branche gelée, seulement par F.
Témoin du lot rejoué sur l'arbre F : `test_l1170_test_script_selection` **10 OK**.

## 6. Témoins ciblés sur l'arbre `a9d8cc4f` (`AMEESH_REQUIRE_DB=0`)

**210 OK / 5 sautés** (les 5 sautés sont des témoins Postgres sans base). Détail
`temoins.txt` :

```
test_l159a_capacity_guard         Ran 34  OK
test_l162_tmpfs                   Ran 19  OK (skipped=1)
test_l233_decide_only             Ran 2   OK (skipped=2)
test_l33_chatbot                  Ran 62  OK
test_l84_serve                    Ran 19  OK (skipped=1)
test_site_art                     Ran 4   OK
test_l161a_ci_sans_code           Ran 6   OK
test_l107_executor_contract       Ran 20  OK   (compteurs 63 / 322 / 385)
test_hermeticity_lint             Ran 18  OK
test_l79_chat_terminal            Ran 16  OK (skipped=1)
test_l1170_test_script_selection  Ran 10  OK
```

Lint d'herméticité `--diff fcff03db4` : **0 constat sur 3 fichiers** (H0 0, H1 0, H2 0,
H3 0). Lint de l'arbre entier : 82 constats **identiques à ceux de main** (47 H1,
31 H2, 4 H3), donc préexistants, aucun introduit par la vague. Aucune suite complète
locale (canon §3).

## 7. État, portes et suites

- Manifeste conservé : `reviews/2026-10-14-1204-manifeste-vague-suivante.md`
  = blob `904b748b98caf6468ce08b7edd6932de894bc3ab` (identique au gel). Il sera actualisé
  au gel final pour relier le SHA et le run final + la preuve historique `39df` (demande
  pilote 08:33Z).
- `#1211` (PR #923, tête `39f51fd9d0105b21911adb1d56c7df67fc3876eb`) **non active** au
  moment du paquet : aucun push sur la branche de lot, **aucun run**, aucun vert ancien
  transféré. Le run `38504851830` reste la preuve historique de `39df7c316c`, pas de F.
- Prochain geste, après activation #1211 : main fraîche → union → manifeste → **SHA gelé**
  → **CI complète unique** (`workflow_dispatch`, 4 psycopg + 6 psql) sur la branche de lot
  → feu pilote. Le gel et le run iront au pilote ; la notification `impl02` (activation
  #1170, observation runtime avant/après) est inscrite au suivi.
- Ce paquet `patchs-fcff03db4` n'est **pas** admissible pour la branche de lot : c'est un
  colis de lecture. `refs/atelier/1204/union-fcff03db4` porte l'arbre candidat.

## 8. Contenu et vérification

```
SHA256SUMS                        union-vs-main.patch
README.md                         index-main-vs-union.patch
commits-bruts.txt                 index-gel-vs-union.patch
conflit-fcff-gel.txt              exploitation-main-vs-union.patch
reconstruction.txt                union-vs-gel.patch
temoins.txt
```

```sh
sha256sum -c SHA256SUMS
```
