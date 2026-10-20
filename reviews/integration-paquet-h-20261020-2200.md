# Paquet integration/paquet-h-20261020-2200 — composition (§12)

Base : `main` `37493145c856d34c7c763dc6107553f49f21e94c`.
Branche : `integration/paquet-h-20261020-2200`, cible `main`, porteur #2891.
Composeur : mesh-design, pilote (§12). Reprise du créneau signalée par
l’événement ameesh #120516 du 20/10/2026 à 22:15 Europe/Paris.
Une seule CI complète demandée sur la tête finale du paquet.

Main a avancé de `37493145c856` à `c5cd599a83d4` (release 1.16.0 :
`pyproject.toml` + notes) sans chevauchement avec le contenu du paquet ; la
base de composition reste `37493145c856` (§12, aucun chevauchement).

## Admissions

| lot | source | classe | avis | motif |
|---|---|---|---|---|
| #2889 | `4ff97962fa9689c01f67a1fff66b613b6f06608e` | light | ameesh-dev3 (DeepSeek), OK #14819, gel #14817 | auteur ameesh-cx2 (Codex) ; [issue #2211](https://github.com/manaty/ameesh/issues/2211), [PR #2212](https://github.com/manaty/ameesh/pull/2212) ; course du témoin OAuth interrompu, deux ordres déterministes |

## Composition

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #2889 | `4ff97962fa9689c01f67a1fff66b613b6f06608e` | `9577ddc9d585dd365cd827b29de35649c27fea79` | fusion propre, aucune adaptation |

1 unité de fusion, 1 lot porté : #2889 4ff97962fa96.
Le commit de composition conserve la tête source comme second parent et porte
le trailer `ameesh-work: 2889`.
S’y ajoute une adaptation de test après la CI rouge (section suivante), sans
changement du contenu relu de #2889 ni de sa tête de composition.

## Adaptation de test après la CI rouge (20/10 20:59Z)

La suite complète du run [39490886347](https://github.com/manaty/ameesh/actions/runs/39490886347)
a rougi sur une seule part, `psql 15/16`, et un seul test :
`tests.test_l514_wake_on_event.DoctorHooksTest.test_hook_binaire_sous_home_variable_est_trouve`,
`assertNotIn("127", sortie)`. La sortie du doctor cite le chemin du fichier de
hooks, dont le répertoire temporaire est aléatoire : le suffixe
`l514-hooks-kcw3127v` contient « 127 », et un doctor pourtant vert était lu
comme rouge. Le même test est vert dans le run `tests`
[39491160526](https://github.com/manaty/ameesh/actions/runs/39491160526) :
c’est une course de la suite, sans rapport avec #2889.

Correction `1b88e90f3b3c` (adaptation, sans trailer) : les six témoins « 127 »
de `DoctorHooksTest` portent désormais sur le message `exits 127` du doctor,
et non sur un nombre libre de la sortie ; le sens est conservé des deux côtés
(binaire absent → KO, binaire présent → aucun KO). Test seul, aucun code de
production ; vérifié localement par une reproduction forçant un chemin
temporaire contenant `127` (rouge avant, vert après), 20 tests
`DoctorHooksTest` et les 50 tests de contrat `test_l232d_oauth` verts.
Test seul : aucun avis requis (§11 bis, adaptation de suite), le contenu relu
de #2889 (`4ff97962fa96`) ne change pas.

## Écartés

| lot | motif |
|---|---|
| #114 | `verdicts-missing` : second avis indépendant requis sur `89bdc6b50e93` |
| #2614 | `blocked-verdict` : gel effectif refusé |
| #2771 | `no-verdict` : aucun gel effectif favorable |
| #2888 | `verdicts-missing` : seule famille Codex acquise sur `e47f3a41c9dd`, famille Claude indépendante attendue |

## Contrôles

Inventaire complet par `scripts/paquet-candidats.py --project ameesh --target main` :
1 prêt (#2889), 4 en attente, 0 erreur ; aucun lot prêt omis.
Composition sans conflit ; aucun fichier de production ni migration changé.
L’auteur rapporte 114 tests ciblés verts ; la relecture indépendante est acquise.
Le contenu source est repris tel quel ; l’adaptation de test est la seule
modification hors manifeste. Contrôles locaux de l’adaptation : reproduction
forcée du chemin `127` (rouge sur la tête précédente, vert sur celle-ci), 20
`DoctorHooksTest` et 50 tests de contrat `test_l232d_oauth` verts. La suite
complète s’exécute en CI sur la tête finale, sans rejouer localement les
suites de l’auteur.
