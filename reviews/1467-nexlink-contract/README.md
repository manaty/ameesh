# Contrat Nexlink à qualifier

`candidate.json` est une carte de sources, sans contrat exécutable ni
activation. Elle porte sur l'union sans conflit de :

- paquet-5 / develop `d73f3602898f878b40f1cd49ea69f770fe23a426` ;
- #1470 `6553d3ae11741f4163d6e65b450c97a40e9e2c17` ;
- arbre `a252cec5d7569d2a645b32a7f03c014f68363da8`.

La lecture porte sur les objets Git, sans checkout, branche, poussée ou CI.
Elle énumère vingt workflows de code et les jobs, conditions et matrices
déclarés, avec vingt-neuf blobs de workflows et de sélection. Les workflows
réutilisables locaux sont inclus. L'inventaire gouvernant ci-dessous complète
la carte ; la portée des suites reste à qualifier avant contrat exécutable.

La tête de #1470 seule ne contient pas `civil-crypto.yml` et `lot-plan.yml` ;
elle ne suffit donc pas à décrire les suites du paquet-5. Le sélecteur de
#1470 force bien `integration/**` et `train-*` en mode complet, y compris au
dispatch ; `related-tests.mjs` conserve ce mode sans lancer de sélection.

La liste de vingt suites de code, la portée des plateformes et le traitement
de Docs gate sont soumis au pilote. Docs gate possède la classification
documentation seule ; les jobs `lot-plan`, statut et reprise du plan ne
remplacent aucune suite exécutée. Les plateformes hébergées et le pont vers
un appareil ne sont pas activés par cette carte.

Les éléments auteur reçus le 15 octobre à 14:14 citent des exécutions réelles
sur paquet-5 ; ils ne donnent aucune exécution sur `integration/train-*`.
Aucun résultat de paquet-5 n'est transféré au futur train. Les blobs liés à
chaque futur manifeste devront correspondre à sa composition finale exacte.

## Entrées gouvernantes (15 octobre, reprise après rotation)

`scope.json` déclare un périmètre conservateur de scripts, manifestes,
lockfiles, configurations Jest/Vitest/Playwright/TypeScript, initialisation et
doubles de test, plugins mobiles et configuration du workspace Rust. Les
répertoires de scripts sont pris entiers : un import local dans ces
répertoires ne disparaît pas parce que son nom manque à une liste manuelle.
Cela inclut le sélecteur, les rapporteurs, les lanceurs SQL, les onze familles
de synchronisation, les scripts `postinstall` mobiles et le patch Rust vendored.

`inventory.py` développe ce périmètre dans les objets Git déjà cités par la
carte, vérifie les vingt-neuf sources initiales et refuse une entrée absente,
un lien symbolique ou un sous-module dans le périmètre déclaré. Il n'exécute
aucun code Nexlink et ne construit aucun nouvel arbre, branche ou contrat.

```sh
python reviews/1467-nexlink-contract/inventory.py --repo /chemin/vers/nexlink
```

| Groupe | Chemins trouvés |
| --- | ---: |
| Carte workflows/sélection | 29 |
| Lanceurs et auxiliaires | 465 |
| Paquets Node et configurations | 104 |
| Workspace Rust | 86 |

Ces groupes se recoupent : 678 chemins distincts. Le résultat est produit à
la demande ; aucune seconde liste d'empreintes n'est conservée. Ce périmètre
est une déclaration d'audit, pas un analyseur de dépendances arbitraires : un
import vers un nouveau répertoire demande de compléter sa qualification.

Trois portées restent explicites dans `scope_questions` :

- `scripts/ci/scripts-suite.mjs` exclut
  `scripts/deploy-workflow-shell.test.mjs`, confié à `deploy-api.yml` ; ce
  workflow de déploiement ne fait pas partie des vingt suites candidates.
  L'auteur de #1470 a été saisi du raccord de ce témoin pur à une suite code.
- Jest mobile exclut les `.live.test.tsx` ; la baseline website couvre
  Vitest et le mock Playwright d'assistance distante, pas toute la recette
  e2e staging. La portée « complète » doit préciser cette frontière.
- Les matrices desktop hébergées et le pont appareil dépendent de variables
  et d'autorisations propres, à qualifier par le pilote.

Les références d'actions GitHub, les capacités et variables des runners, les
installations npm sans verrou de `shared/compute` et `shared/page-host`, et
les dépendances Python/outils téléchargées restent des entrées externes
énumérées dans `external_inputs`. Une lecture de leurs chemins locaux ne
prouve pas leur exécution. L'inventaire reste `unqualified` et n'est consommé
par aucun runtime.
