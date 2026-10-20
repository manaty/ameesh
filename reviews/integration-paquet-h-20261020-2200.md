# Paquet integration/paquet-h-20261020-2200 — composition (§12)

Base : `main` `37493145c856d34c7c763dc6107553f49f21e94c`.
Branche : `integration/paquet-h-20261020-2200`, cible `main`, porteur #2891.
Composeur : mesh-design, pilote (§12). Reprise du créneau signalée par
l’événement ameesh #120516 du 20/10/2026 à 22:15 Europe/Paris.
Une seule CI complète demandée sur la tête finale du paquet.

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
Le contenu source est repris tel quel ; la suite complète s’exécute en CI sur
la tête finale, sans rejouer localement les suites de l’auteur.
