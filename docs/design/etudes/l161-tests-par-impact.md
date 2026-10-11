---
type: Study
title: "Étude — tests choisis selon l'impact, échecs remontés en temps réel"
description: "Mesure de la CI d'ameesh et de Nexlink ; carte test → code mesurée (couverture par test, fichiers lus, tables touchées) ; impactés d'abord ; flux des résultats vers le courrier de l'agent du lot ; lots L161a–L161i estimés et datés."
status: draft
tags: [ci, tests, couverture, selection, temps-reel, courrier, runners]
generated: { by: "claude3/claude-opus-5-5", at: "2026-10-11T04:30:00+02:00" }
stale_after: 2027-01-11
sources:
  - { resource: "../../.github/workflows/tests.yml", title: "CI d'ameesh (L116 : parts parallèles)" }
  - { resource: "../../scripts/test-parts.py", title: "Découpage de la suite en parts (L116)" }
  - { resource: "../EXPLOITATION.md", title: "Courrier regroupé avant le réveil (L125)" }
  - { resource: "decisions/0018-vitesse-des-agents.md", title: "Décision 0018 (interruption de tour)" }
  - { resource: "etudes/boucle-auto-reparation.md", title: "Étude : boucle d'auto-réparation (A1, A8)" }
---

# Contexte

Idée du propriétaire, 2026-10-11 à 03:25 : « quand on passe les suites de
tests sur une version, chaque suite pourrait lister les fichiers du code qui la
concernent ; au moment de passer les tests, on lancerait en priorité les tests
les plus impactés par la version, et on pourrait remonter les erreurs en temps
réel à l'agent, de sorte qu'il corrige et repousse ». Lot n° 171 du suivi
(L161), étude seulement.

Constat de départ : la CI d'ameesh passe toute la suite sur chaque PR, même
quand seul le numéro de version change ; l'agent ne connaît un échec qu'à la
fin de l'exécution ; en local, le résultat dépend du poste. Côté Nexlink, les
tests desktop sous macOS et Windows ne tournent qu'à l'étiquette : la nuit du
10 au 11 octobre, deux étiquettes desktop ont échoué au build.

# En bref

1. **D'abord le gaspillage, sans carte.** ameesh : une PR de documentation ou
   de version seule ne passe plus toute la suite (18 % des PR de la semaine,
   44 % des minutes de CI des PR depuis L116), et une exécution de main n'est
   plus annulée par la fusion suivante (L161a). Nexlink : plus de Backend
   suite pour une poussée de documentation seule (80 % de ses minutes sur
   `develop`), et les tests desktop Windows et macOS passent dès qu'une
   poussée touche `desktop/`, pas seulement à l'étiquette (9 des 17 étiquettes
   de la semaine ont échoué sur ces tests) (L161d).
2. **Remonter chaque échec dès qu'il apparaît** : flux JSON lignes, routé par
   ameesh au courrier de l'agent du lot ; un seul urgent par version poussée,
   verdict en courrier ordinaire, rouge hérité jamais imputé à l'auteur,
   retrait à la poussée suivante ; les agents n'attendent plus la CI dans leur
   tour (L161b, L161c).
3. **Mesurer le lien test → code, ne jamais le déclarer** : couverture par
   test, sous-processus compris, fichiers de données lus et tables touchées,
   au grain de la fonction ; carte régénérée à chaque fusion sur main (L161e).
4. **Impactés d'abord, rien d'autre sauté sur PR** tant que les fuites ne sont
   pas mesurées pendant deux semaines ; jamais rien sauté pour du code sur la
   branche d'intégration (L161f, L161i).
5. **Nexlink reprend flux, routage et sélection** avec ses outils natifs
   (`--findRelatedTests`, `vitest related`, test de la migration d'abord), plus
   une carte nocturne pour l'e2e (L161g, L161h).

# Méthode

Lecture seule.

* ameesh : `gh run list` et `gh api …/jobs` sur les 141 exécutions du
  workflow `tests` (2026-10-04 22:11 UTC → 2026-10-11 01:20 UTC), dont 58
  depuis la fusion de L116 (2026-10-10 18:07 UTC) ; journaux horodatés des
  sept exécutions en échec ; fichiers des 60 PR.
* Attente des agents : durée des appels `gh pr checks --watch` et
  `gh run watch` dans les transcripts des sessions Claude du compte principal
  de ce poste, projets manaty seulement, cinq derniers jours.
* Nexlink : workflows lus sur `develop`, 5 543 exécutions relevées du 04 au
  11 octobre, journaux d'un échantillon d'échecs ; deux chiffres contrôlés par
  sondage (étiquettes desktop, poussées de documentation).
* Mécanismes de mesure de la carte vérifiés sur un exemple jouet
  (coverage.py 7.16.2, Python 3.14). Pas de pilote sur la vraie suite en
  local : charge de l'hôte de 13,4 pour 12 CPU au moment de l'étude.

# Ce qui se passe aujourd'hui

## La CI d'ameesh, mesurée

Depuis L116, une PR passe 4 parts psycopg et 1 part psql (modules
« pilote ») ; un push sur main ou `release/**`, 4 parts psycopg et 6 parts
psql ; plus un job macOS (couche plateforme) et l'agrégat `tests`.

| Depuis L116 | Médiane | p90 | Max | Minutes-runner par exécution |
|---|---|---|---|---|
| PR verte, durée murale | 3,5 min | 6,2 min | 6,9 min | 13,3 |
| PR rouge, durée murale | 3,1 min | 4,8 min | 5,9 min | — |
| main verte, durée murale | 5,9 min | 7,5 min | 8,7 min | 41,5 |
| avant L116 : PR verte | 22,5 min | 25,4 min | 29,1 min | 31,1 |

* **Où passe le temps d'un job** : dans l'étape de tests (part psycopg 153 s
  en médiane, part psql de PR 86 s, part psql de main 290 s). Le reste est
  fixe et court : conteneur Postgres 11 s, installation 5 s. Moins de tests,
  c'est donc une part presque proportionnellement plus courte.
* **File d'attente** : 40 jobs sur 424 ont attendu un runner plus de 60 s
  (jusqu'à 4 min) ; 9 exécutions sur 54 en ont eu au moins un. C'est la
  limite de jobs simultanés du compte, atteinte quand plusieurs poussées
  coïncident (12 jobs par push sur main, 7 par PR).
* **Annulations** : 21 exécutions annulées par la concurrence, dont 7 sur
  main (3 depuis L116). Ces fusions n'ont jamais eu leur propre verdict.
* **Durées de référence** : `tests/parts/durees.json`, rafraîchie à la main,
  ne connaît que 76 des 91 modules ; les 15 autres sont pesés par estimation.

## Les échecs : quand on aurait pu savoir

Depuis L116 : 5 exécutions de PR rouges sur 33 (15 %), aucune sur main,
aucune relance d'un même commit (pas d'instabilité observée sur la période).

| PR | Test en échec | 1er échec | Fin de l'exécution | Écart | Impacté par |
|---|---|---|---|---|---|
| #53 | `test_cluster` (contrat du rôle) | +1,3 min | +5,9 min | 4,6 min | migration 0051, `deploy/sql/role-superviseur.sql` |
| #49 | `test_l61_allers_retours` | +1,1 min | +3,2 min | 2,2 min | `storage/postgres/projects.py` |
| #44 | `test_cluster` (contrat du rôle) | +1,0 min | +3,0 min | 2,0 min | migration 0049, `deploy/sql/role-superviseur.sql` |
| #43 | `test_l63_platform` (macOS) | +0,3 min | +3,1 min | 2,9 min | `platform/*.py` |
| #40 | `test_l117_account_spread` | +0,8 min | +3,1 min | 2,4 min | module de test modifié par la PR |

Avant L116, l'écart atteignait 8,2 et 9,6 min. Les cinq échecs récents
auraient tous été dans l'ensemble impacté. Deux viennent d'une **dépendance
qu'aucun import ne montre** : une nouvelle colonne (fichier SQL de migration)
doit être accordée ou exclue dans `deploy/sql/role-superviseur.sql`, ce que
vérifie `test_cluster`. Le lien doit donc couvrir les fichiers de données, pas
seulement le code Python.

## Ce que changent les PR

Sur les 60 PR : 31 de code, 12 de code avec migration, 7 de documentation
seule, 4 de version seule (`pyproject.toml` seul : #48, #54, #99, #104),
2 de CI ou scripts, 4 autres (`deploy/`, exemples). Une PR de code touche en
médiane 5 fichiers de `src/` (p90 : 20), souvent des fichiers centraux
(`cli.py`, `main.py`, `mesh_cli.py`, `runner.py`, `storage/interface.py`).

* Les 11 PR de documentation ou de version seule (18 %) ont passé toute la
  suite. Depuis L116, ce sont 8 des 19 PR : 3,1 à 6,9 min chacune, 51 min au
  total, soit 44 % des minutes de CI des PR.
* Une montée de version sur une branche `release/v1.6.x` passait **trois
  fois** la suite pour une ligne : push de la branche (`release/**`, suite
  complète, 5,8 à 8,7 min), PR (6,1 à 6,9 min, ralentie par la première), puis
  main. Depuis 1.7.0, la branche s'appelle `version/…` : deux passages.
* « Documentation seule » ne veut pas dire « aucun test » :
  `test_l33_chatbot` indexe `site/docs` et `docs/design` et vérifie une
  recherche ; `test_canon` lit `examples/canon`. Une règle par motifs de
  chemins les raterait ; la carte les mesure.

## L'attente des agents

* Par PR, la CI cumulée (toutes ses exécutions non annulées) vaut 6,3 min en
  médiane depuis L116 (24,3 avant) ; 1,6 exécution par PR en moyenne.
* Entre la fin d'une exécution rouge et la poussée corrective : 1,8 à
  16,1 min (médiane 6 min).
* Les agents attendent **dans leur tour** : `gh pr checks <n> --watch`
  (consigne de l'auditeur, étape 5) ou `gh run watch`. Sur cinq jours :
  10 attentes bloquantes sur ameesh (médiane 3,6 min, p90 8,8, 43 min au
  total), 38 sur Nexlink (médiane 6,2 min, p90 9,2, 184 min au total), et 30
  autres lancées en arrière-plan. C'est une **borne basse** : ni les sessions
  Codex, ni les autres comptes, ni la VM ne sont comptés, et l'outil coupe une
  commande à 10 min (plusieurs attentes Nexlink s'arrêtent à 9,7 min).
* En local, la suite dépend du poste (base de test, charge, variables).
  `tests/support.py` neutralise déjà une trentaine de variables, mais un poste
  sous pression ne peut pas servir de référence.

## Nexlink

Du 04 au 11 octobre : 5 543 exécutions. La branche d'intégration est
`develop` ; `main`, branche par défaut du dépôt, a 1 179 commits de retard. Le
flux réel est la poussée sur `agent/**` puis sur `develop`, presque sans PR.

* **Où tournent les tests** : sur les runners GitHub (`ubuntu-latest`), sauf
  les matrices Windows et macOS de Build Desktop et de Host Rust. La flotte
  auto-hébergée ne porte encore que le banc de latence, une sonde et une
  branche d'essai. Le banc la mesure 1,6 fois plus lente que GitHub pour la
  suite backend (12,8 contre 8,1 min), avec une file de 2,1 min en médiane
  (p90 5,8) contre 3 s.
* **Aucune sélection par changement** : ni `paths-filter`, ni
  `--findRelatedTests`, ni `--changedSince` ; des filtres `paths:` statiques
  sur certains workflows, aucun sur Backend suite.

| Workflow (événement) | Exécutions | Médiane / p90 | Échecs | 1er échec, avant la fin |
|---|---|---|---|---|
| Backend suite (push `develop`, `agent/**`) | 3 874 | 4,7 / 7,8 min | 32,7 % | ≤ 1,5 min : un seul job, et jest affiche déjà chaque fichier en échec |
| Remote help (push) | 165 | 4,0 / 5,0 min | 17 % | 2,5 à 3,7 min : plusieurs jobs, signal par job |
| SQL rules (push) | 258 | 2,5 / 3,3 min | 11,6 % | ~0,1 min ; ~1,5 min si le test de la migration passait d'abord |
| Build Desktop (étiquette) | 17 | 9,9 / 13,9 min | 88 % (15/17) | 8,6 à 10,2 min |

* **Backend suite sur `develop`** : 81 % des exécutions de la semaine suivent
  une poussée qui ne change que de la documentation (57 % sur les 30
  dernières, par sondage). Elles portent 82 % des échecs, un rouge hérité que
  la poussée n'a pas causé, et 80 % des minutes-runner (≈ 7 900 sur ≈ 9 800).
  Backend suite consomme en tout ≈ 20 000 minutes-runner par semaine.
* **Ce qui échoue** (99 échecs de Backend suite) : garde-fous 55 %, jest
  unitaires 23 %, e2e 16 % (dont des débordements de tas).
* **Desktop, nuit du 10 au 11.** Les tests desktop ne tournent sous Windows et
  macOS que dans Build Desktop, à l'étiquette ; la suite desktop en revue est
  Ubuntu seulement.
  * `desktop-v0.2.6` (19:43) : rouge sous Windows, 2 tests sur 776 (chemin
    POSIX attendu, mode de fichier).
  * `desktop-v0.2.7` (20:46) : rouge sous macOS (13 tests) et Windows
    (15 tests) : architecture de l'hôte, dossier temporaire en lien
    symbolique, `process.getuid` absent, `fsync` refusé sur un dossier.
  * Les agents ont contourné par 3 lancements manuels d'un workflow d'essai
    Windows et 7 poussées de workflows d'essai. `desktop-v0.2.8` est publiée
    à 01:39, six heures après la première étiquette rouge.
  * Sur la semaine, 9 des 17 étiquettes ont échoué sur des tests Windows (8)
    ou macOS (1) jamais lancés avant l'étiquette ; les autres échecs viennent
    de la signature macOS (4) et de la publication (4).
* **Outils** :
  * 18 paquets npm indépendants (ni workspaces, ni turbo, ni nx) ; le code de
    `shared/*` est recopié par des scripts de synchronisation ;
  * jest 30 (backend, desktop, mobile), jest 29 (`shared/*`), vitest 5 et
    Playwright 1.59 (website) ; dependency-cruiser dans le backend ;
  * 79 des 87 specs e2e du backend importent `AppModule` : leur graphe
    d'imports les relie à presque tout ;
  * SQL : 330 migrations numérotées, 309 fichiers de test nommés par numéro
    de migration ; `scripts/sql-tests.sh` applique toute la chaîne (~44 s),
    passe les tests dans l'ordre numérique et accepte déjà une liste de
    numéros.

# 1. Lien test → code, mesuré

## Pourquoi ni les imports ni une liste à la main

* **Imports** : 65 des 91 modules de test d'ameesh lancent la CLI ou
  l'exécuteur en sous-processus (`python -m ameesh.cli`, `ameesh.runner`,
  `ameesh.main`), et `ameesh.main` atteint par imports 107 des 125 fichiers de
  `src/`. En fermeture statique, un module de test « dépend » en médiane de
  107 fichiers ; 68 modules sur 91 dépendent de plus de 80 % du code. Une
  sélection par imports choisirait presque toujours tout.
* **Déclaration à la main** : elle vieillit dès la première PR, et elle oublie
  précisément les liens invisibles (migration → `deploy/sql` →
  `test_cluster`, documentation → `test_l33_chatbot`). Une déclaration n'est
  admise que comme liste « toujours » pour les objets globaux (section 2),
  justifiée ligne par ligne, comme `tests/parts/pilote-psql.txt`.

## Trois mesures, une carte

L'unité de **sélection** reste le module de test (`tests/test_*.py`) : L116
découpe déjà par module, et un module garde son `setUpClass` et son schéma
jetable. L'unité de **mesure** est le test (contexte par test), pour pouvoir
affiner plus tard.

1. **Code Python exécuté, par test, sous-processus compris** (coverage.py) :
   * dans le processus de la suite, un contexte dynamique par test
     (`dynamic_context = test_function`, ou `Coverage.switch_context` posé par
     la classe de résultat de `scripts/test-parts.py`) ;
   * dans les sous-processus, un démarrage par `COVERAGE_PROCESS_START` et un
     fichier `.pth` (`coverage.process_startup()`), avec un **contexte
     statique lu dans l'environnement** : `context = ${AMEESH_TEST_CONTEXT-}`.
     La classe de résultat pose `AMEESH_TEST_CONTEXT = test.id()` au début de
     chaque test. `child_env()` copie `os.environ` : la CLI et l'exécuteur
     lancés par le test en héritent ;
   * vérifié sur l'exemple jouet : les lignes exécutées par le sous-processus
     sont attribuées au bon test. L'option récente `[run] patch = subprocess`
     mesure bien les sous-processus, mais leur transmet le contexte du parent
     (vide) : elle ne convient pas ;
   * les contextes dynamiques imposent le traceur C (coverage 7.16 refuse le
     cœur `sysmon` avec eux). Les sous-processus, qui n'ont qu'un contexte
     statique, peuvent garder `sysmon` avec leur propre fichier de
     configuration. Surcoût à mesurer dans L161e, hors du chemin critique.
2. **Fichiers de données lus, par test** :
   * un hook d'audit Python (`sys.addaudithook`), installé par le même
     démarrage dans le parent et les sous-processus, note les événements
     `open`, `os.listdir` et `os.scandir` sur les fichiers du dépôt qui ne
     sont pas du Python ;
   * il voit donc les migrations, `deploy/sql/*.sql`, les descripteurs de
     harnais, les consignes `.md` du paquet, `progress_page.html`,
     `pyproject.toml`, la documentation lue par `test_l33_chatbot` et
     `examples/canon` ;
   * vérifié sur l'exemple jouet : un fichier SQL lu par un sous-processus est
     attribué au bon test ;
   * la lecture d'un dossier compte : un nouveau fichier dans `migrations/`
     touche les tests qui listent ce dossier.
3. **Tables touchées, par classe** :
   * chaque classe de test a son propre schéma (`t_<classe>_<hex>`). Avant de
     le supprimer, `tearDownClass` lit `pg_stat_user_tables` pour ce schéma
     (insertions, mises à jour, lectures séquentielles et par index) ;
   * `pg_stat_force_next_flush()` vide les statistiques de la connexion du
     test ; les sous-processus vident les leurs en se fermant ;
   * une migration est rattachée aux tables qu'elle crée ou modifie
     (`create table`, `alter table`, `create index … on`,
     `create trigger … on`, `grant … on`).

**Grain du code : la fonction** (bloc), comme le fait pytest-testmon pour
pytest.

* Chaque fichier Python est découpé en blocs (fonctions, méthodes, reste du
  module), avec une empreinte de leur arbre syntaxique sans numéros de ligne.
  Un module de test dépend des blocs dont il a exécuté au moins une ligne.
* Une PR change un bloc si son empreinte diffère entre le commit de la carte
  et la tête de la PR.
* Une fonction nouvelle n'est appelée que depuis un bloc modifié : ses tests
  sont trouvés par l'appelant.
* Un changement au niveau du module (constante, table d'aiguillage) touche
  tous les tests qui ont importé ce module : c'est large, mais juste.
* Au grain du fichier, `cli.py` ou `runner.py` (4 661 lignes) choisiraient
  presque toute la suite.

**Règle SQL.** Les migrations d'ameesh sont **ajoutées**, jamais réécrites.
Une nouvelle migration impacte :

* les tests qui lisent le schéma entier ou `deploy/sql` (trouvés par la
  mesure 2 : `test_cluster`, `test_migrations`) ;
* les tests qui touchent les tables qu'elle modifie (mesure 3).

Une migration existante modifiée, ou une migration non additive (`drop`,
`rename`, changement de type), impacte tous les tests de base.

## La carte : contenu, stockage, fraîcheur

* **Production** : un workflow `carte.yml`, à chaque push sur main (et à la
  main), hors du chemin critique, avec une concurrence « dernier commit
  seulement ». Il passe la suite psycopg sous mesure en 4 parts, plus les
  modules pilote en psql (le code propre au pilote psql n'est exécuté que
  là), puis fusionne les mesures.
* **Contenu** : `carte.json.gz`, schéma `ameesh-carte/1`, de l'ordre de 1 à
  2 Mo.
  * commit de main et versions des outils ;
  * par module de test : durée par pilote, échecs des 14 derniers jours,
    instabilité ;
  * par bloc : empreinte et modules ;
  * par fichier de données et par dossier : modules ;
  * par table : modules ;
  * la liste des blocs qu'aucun test n'exécute.
* **Stockage** : cache GitHub Actions `carte-<sha>` (une PR lit les caches de
  sa branche de base ; clé de repli `carte-`), et artefact de l'exécution
  (30 jours) pour ameesh et les agents. **Jamais dans le dépôt** : avec 25
  fusions sur main en 7 h le soir du 10 octobre, la carte changerait à chaque
  fusion et créerait des conflits.
* **Durées** : la carte remplace la table rafraîchie à la main.
  `test-parts.py` lit les durées de la carte ; `durees.json` ne reste qu'en
  repli.
* **Fraîcheur** :
  * une PR se compare au **commit de la carte**, pas à sa base. Ce qui a
    changé sur main depuis la carte compte comme changé : la sélection
    s'élargit, elle ne se rétrécit jamais ;
  * carte absente, plus vieille que 48 h ou sans ancêtre commun : suite
    complète.

# 2. Ordre et sélection

## L'ordre

Le job `plan` classe chaque module :

| Classe | Contenu |
|---|---|
| P0 | modules de test modifiés ou nouveaux (absents de la carte) |
| P1 | impactés : blocs modifiés, fichiers de données ou dossiers lus, tables touchées par une migration ; liste « toujours » si ses déclencheurs changent |
| P2 | en échec dans les 7 derniers jours, sur n'importe quelle branche |
| P3 | le reste |

`scripts/test-parts.py` répartit chaque classe à son tour sur les parts (le
plus lourd d'abord, dans la part la moins chargée, comme aujourd'hui) :
chaque part commence par ses modules impactés. Dans une classe, les modules
courts passent d'abord, pour un premier signal plus tôt. Aujourd'hui, une
part tourne dans l'ordre alphabétique : un test impacté peut passer en
dernier.

**Liste « toujours »** (déclarée, justifiée) : les tests d'objets globaux au
cluster Postgres, que la mesure par schéma ne voit pas. Ce sont les rôles
créés par `test_cluster`, les canaux `LISTEN/NOTIFY` (`test_events_attach`),
la panne et la reconnexion (`test_l72_panne_de_base`). Ils sont déclenchés dès
qu'une migration, `db.py` ou `deploy/sql` change.

## Quand sauter le reste

| Événement | Changement | Ce qui tourne |
|---|---|---|
| push sur main, `release/**`, étiquette, manuel | tout | suite complète, deux pilotes, impactés d'abord ; **jamais sautée, jamais annulée** par la poussée suivante |
| PR | documentation seule : fichiers sous `docs/`, `site/`, `examples/` ou `*.md` hors du paquet | les seuls tests qui lisent ces fichiers : déclarés jusqu'à la carte (`test_l33_chatbot` pour `docs/design` et `site/docs`, `test_canon` pour `examples/canon`), mesurés ensuite ; moins d'une minute |
| PR | version seule : seule la ligne `version =` de `pyproject.toml` | `test_version` et construction du paquet (`pip wheel . --no-deps`), moins d'une minute |
| PR | socle des tests ou CI (`tests/support.py`, `tests/fakebin/`, `scripts/test-parts.py`, `.github/`, dépendances de `pyproject.toml`), fichier inconnu de la carte, carte périmée | tout, impactés d'abord |
| PR | code, cas général | **phase 1** : impactés d'abord, puis le reste ; **phase 2**, si le bilan la justifie : impactés seuls sur PR, le reste sur main |

**Nexlink.** La poussée sur `agent/**` y tient lieu de PR, et `develop` de
main. Exception proposée, à décider par le propriétaire : sur la branche
d'intégration, un commit qui ne change que de la documentation lue par aucun
test reprend le verdict du commit de code précédent, au lieu de relancer la
suite (81 % des exécutions de Backend suite sur `develop`). Sur ameesh, main
garde la suite complète à chaque fusion, comme demandé : 6 min, minutes
gratuites.

**Feu vert provisoire.** Quand P0 et P1 sont verts dans toutes les parts,
ameesh le dit à l'agent (événement passif). L'agent peut préparer la
description ou demander une relecture, jamais fusionner : la fusion exige
`tests` vert.

**Passage en phase 2** (L161i). On compte les **fuites** : un échec, dans le
reste d'une PR ou sur main, d'un module que la carte n'avait pas choisi pour
ce changement. La phase 2 ne vient que si :

* les fuites restent sous 1 pour 50 PR pendant deux semaines ;
* et la sélection est « sûre » : carte de moins de 24 h, aucun bloc modifié
  inconnu de la carte, aucun fichier du socle.

Un échec sur main va alors à l'agent du lot fusionné (section 3).

**Signal gratuit.** Un bloc modifié qu'**aucun test n'exécute** est signalé
à l'agent (« lignes modifiées sans test »). En phase 2, il force la suite
complète.

## Risques

| Risque | Parade |
|---|---|
| test instable : un urgent pour rien réveille un agent et lui fait chercher un défaut absent | réessai immédiat du test en échec, dans le même processus, avant tout envoi ; s'il passe : « instable », événement passif, compté dans la carte ; une mise en quarantaine passe par un lot, jamais en silence |
| rouge hérité : la base était déjà rouge (82 % des échecs de Backend suite sur `develop`) | l'échec n'est pas imputé à l'auteur de la poussée (section 3) |
| dépendance cachée (données, tables, variables, objets globaux) | mesures 2 et 3 ; liste « toujours » ; suite complète sur main ; fuites comptées |
| dépendance à l'ordre, révélée par le nouvel ordre | l'ordre reste déterministe (même changement, même ordre, sur main comme sur PR) ; un passage nocturne de la suite complète en ordre inverse détecte ces dépendances |
| carte fausse ou périmée | comparaison au commit de la carte ; suite complète au-delà de 48 h ; fuites mesurées |
| la mesure change le comportement (tests de vitesse plus lents sous couverture) | la carte n'est jamais bloquante : un échec sous mesure est noté, pas reporté sur les PR |
| données partagées | une base par part, un schéma par classe, `TRUNCATE` à chaque test : déjà en place ; les rôles sont globaux, d'où la liste « toujours » |
| sous-processus lancé avec un environnement vide (il perd `COVERAGE_PROCESS_START`) | la carte compte les sous-processus sans données et les signale |
| PR venue d'une bifurcation (dépôt public) : jeton en lecture seule | pas de flux, verdict final seulement |

# 3. Remontée en temps réel

## Le flux des résultats

`scripts/test-parts.py lancer --flux F.jsonl` écrit une ligne JSON à la fin
de chaque test (schéma `ameesh-test-event/1`), plus une ligne de début et une
de fin par part :

```json
{"v": 1, "type": "test", "depot": "manaty/ameesh", "tete": "<sha poussé>",
 "fusion": "<sha testé>", "execution": 38097324740, "part": "psycopg 3/4",
 "module": "test_cluster", "test": "tests.test_cluster.SuperviseurRoleTest.test_…",
 "classe": "P1", "raison": "deploy/sql/role-superviseur.sql",
 "statut": "echec", "reessai": "echec", "duree_s": 0.8,
 "ts": "2026-10-11T00:08:17.776Z", "extrait": "AssertionError: … (40 lignes au plus)",
 "rejouer": "python -m unittest tests.test_cluster.SuperviseurRoleTest.test_…"}
```

JSON lignes plutôt que JUnit incrémental : chaque ligne est complète dès
qu'elle est écrite, alors qu'un XML JUnit n'est valide qu'une fois fermé. Le
JUnit, utile aux humains dans GitHub, est produit à la fin à partir du flux.
Pour Nexlink, un reporter jest et un reporter vitest écrivent le même format.

## Du runner à ameesh

| Voie | Latence | Ce qu'il faut | Pour |
|---|---|---|---|
| **A. Check run GitHub** mis à jour pendant les tests (une annotation par échec), relevé par ameesh | relevé toutes les 15 s | `permissions: checks: write` sur les jobs ; un relevé dans `ameesh notify` sur l'hôte désigné (`github.host`, comme L126), seulement pour les exécutions en cours des branches de lots ouverts ; requêtes conditionnelles (un 304 ne compte pas dans le quota) | ameesh et Nexlink, d'abord : ni point d'entrée, ni secret |
| **B. Envoi direct** à ameesh, authentifié par le jeton OIDC de GitHub | ~1 s | un point d'entrée HTTPS sur la VM qui vérifie le jeton (émetteur GitHub, audience `ameesh`, dépôt, branche) ; aucun secret stocké ; c'est la brique A1 de l'étude [auto-réparation](boucle-auto-reparation.md) | quand ce point d'entrée existera |
| **C. Runner auto-hébergé** qui écrit lui-même (`ameesh ci report`) | ~1 s | un rôle Postgres d'insertion seule dans la table des résultats ; jamais le rôle de l'exécuteur, car le code de la PR tourne sur le runner | quand la flotte auto-hébergée portera les suites |

Une étape d'arrière-plan du job (`scripts/ci-flux.py publier`) suit le
fichier de flux et met à jour le check run « tests (flux) » ; une étape
`if: always()` le ferme avec le bilan. Dans un workflow à plusieurs jobs, la
fin en échec d'un job est déjà un signal : le relevé le transmet sans
attendre les autres jobs (2,5 à 10 min gagnées sur Nexlink).

## Du résultat au courrier de l'agent

* **Le lot** : par la branche (`work_items.branch`, L118 ; une PR sur
  ameesh, une branche `agent/…` sur Nexlink), sinon par la ligne
  `ameesh-work: <id>` du commit de tête, sinon par le « Closes #n » de
  l'issue du lot (L126).
* **Le destinataire** : l'assigné du lot. S'il est arrêté, les règles du
  courrier en souffrance s'appliquent (orchestrateurs de l'équipe).
* **L'expéditeur** : `ameesh` (`work.SYSTEM_SENDER`). Chaque message porte
  `work_item_id` : il est rangé dans le fil du lot.
* **Ce qui est envoyé**, conforme à L125 :

| Message | Nature | Effet sur un agent au repos |
|---|---|---|
| premier échec confirmé (après réessai) de la tête courante | `--urgent` | réveil immédiat ; un tour en cours n'est pas interrompu (réservé aux habilités, [0018](../decisions/0018-vitesse-des-agents.md)) : le hook le remet au prochain appel d'outil |
| échecs suivants de la même tête | ordinaire | regroupés par la fenêtre de 90 s : un tour de plus au plus |
| verdict final (vert, ou rouge avec la liste) | ordinaire | réveil à la fin de la fenêtre |
| impactés verts (feu vert provisoire) | événement passif | lu au tour suivant, n'ouvre pas de tour |
| test instable (échec, puis réussite au réessai) | événement passif | idem |
| rouge hérité : le test échouait déjà au dernier passage connu de la base (main pour une PR, `develop` pour une branche `agent/…`) | une ligne dans le verdict, jamais d'urgent à l'auteur ; signalé une fois au lot qui a fait passer la base au rouge (premier commit rouge, ligne `ameesh-work:`) | aucun pour l'auteur |
| échec sur main (ou `develop`) | `--urgent` à l'assigné du lot fusionné, événement aux orchestrateurs de l'équipe, alerte `ci_main_red` poussée par `ameesh notify` au responsable humain (elle bloque les déploiements) | réveil immédiat |

* Au plus un urgent par tête poussée : la CI seule ne peut pas déclencher
  l'alerte `turn_churn`.
* **Contenu** : test, part, raison de l'impact, extrait de l'erreur
  (40 lignes au plus), commande pour rejouer, tête et lien ; « la suite
  continue, verdict à la fin ; une nouvelle poussée annule ce passage ».
* **Mémoire** : une table des résultats (une migration, numéro réservé au
  début du lot). Elle garde les échecs et les instables par test, et les
  bilans par module et par exécution, pas chaque réussite (1 875 tests, des
  dizaines d'exécutions par jour). Elle sert à dédoublonner les envois, à
  l'ordre (P2), aux instables, au rouge hérité, aux fuites et à
  `ameesh ci status <lot>`.
* **Consigne des agents** (ORCHESTRATEUR.md, AUDITEUR.md) : après une
  poussée, plus de `gh pr checks --watch` ni de `gh run watch` dans le tour ;
  l'agent termine son tour ou continue son travail, ameesh le réveille.

## Nouvelle poussée : annuler, retirer

* **GitHub** : le groupe de concurrence par PR annule déjà l'exécution
  dépassée. Sur main, `cancel-in-progress` ne doit plus valoir
  (`${{ github.event_name == 'pull_request' }}`) : chaque fusion garde son
  verdict.
* **ameesh** : dès qu'une nouvelle tête apparaît pour la branche du lot, le
  relevé abandonne l'ancienne exécution. Les messages de CI **non encore
  remis** pour l'ancienne tête sont soldés « obsolète, remplacé par <sha> »
  sans être montrés ; un message déjà remis reste, car l'agent a pu agir.
* **Runners auto-hébergés** : l'annulation doit tuer les conteneurs de test
  (étape de nettoyage `if: always()`, conteneurs étiquetés par exécution) ;
  sinon, la poussée suivante démarre sur une machine chargée.

# 4. Ce que cela demande

## À la CI GitHub d'ameesh

* Un job **`plan`** (~20 s) : un historique suffisant pour le diff, la carte
  restaurée du cache, et `scripts/test-plan.py`, qui rend la nature du
  changement, les classes et les matrices (lues par `fromJSON` dans les
  sorties du job). `plan.json` passe aux parts en artefact.
* Les **parts** : `needs: plan`, matrice dynamique, `--plan plan.json
  --flux flux.jsonl`, publication du check run. Un job sans module est sauté
  par `if:`, car une matrice vide n'est pas permise.
* L'**agrégat `tests`**, seul contrôle exigé, accepte `skipped` quand le plan
  l'a décidé. Le nom du contrôle ne change pas, la protection de branche non
  plus. Un filtre `paths:` au niveau du workflow ne convient pas : le contrôle
  exigé resterait en attente.
* `carte.yml` (section 1) et la **concurrence** de main.
* Le dépôt est public : les minutes sont gratuites, la contrainte est le
  nombre de jobs simultanés. Moins de jobs pour les PR sans code, c'est moins
  d'attente pour les autres.

## Aux runners auto-hébergés

Sur Nexlink, la flotte auto-hébergée ne porte encore aucune suite, et le
banc la mesure plus lente. Rien dans cette étude n'exige d'y déplacer les
suites. Si elles y vont, il faut :

* la carte dans un cache **sur disque** du runner : pas de limite de 10 Go,
  restauration instantanée, mise à jour par le job de carte ;
* des services chauds (Postgres, conteneurs) **un par emplacement de
  runner**, nommés par `RUNNER_NAME`, jamais partagés entre deux jobs
  simultanés ;
* des emplacements réservés aux **impactés** (étiquette `rapide`) : sinon, le
  premier signal attend derrière la suite longue d'une autre poussée. GitHub
  ne sait pas prioriser des jobs ; les étiquettes le font ;
* la voie C pour le flux, avec un rôle d'insertion seule ;
* le nettoyage à l'annulation.

## ameesh d'abord, Nexlink ensuite

* **Deux gains Nexlink n'ont besoin ni de carte ni de flux** et peuvent partir
  tout de suite, en parallèle (L161d) :
  * ne plus lancer Backend suite pour une poussée de documentation seule ;
  * passer les tests desktop sous Windows et macOS dès qu'une poussée touche
    `desktop/`, en reprenant la branche de filtres de suites en revue, avec
    la matrice de Build Desktop sans signature ni publication.
* **ameesh d'abord pour le reste** : un seul langage, un seul cadre de test,
  91 modules ; la CI et le routage (courrier, lots, `notify`) sont dans le
  même dépôt, et `test-parts.py` (L116) existe déjà. On y règle le format du
  flux, le routage, les messages, le rouge hérité et la mesure des fuites.
* **Puis Nexlink** reprend le flux et le routage, avec ses outils :
  * base de comparaison `develop`, jamais `main` : `github.event.before` sur
    `develop`, `origin/develop` sur `agent/**` ; `fetch-depth: 0` ;
  * `jest --findRelatedTests` et `vitest related --run` pour les tests
    unitaires : en JavaScript, les imports sont explicites et le graphe de
    modules suffit au premier niveau, alors que les sous-processus d'ameesh
    l'interdisaient ;
  * en SQL, le test de la migration modifiée d'abord (`sql-tests.sh <n>`),
    puis le reste ;
  * pour l'e2e, que le graphe relie à tout (`AppModule`), une carte par
    couverture V8 passée la nuit, un passage par spec (~445).

# 5. Lots, estimations et gains

## Lots

Durées : travail d'un agent jusqu'à la PR verte, revue comprise. Chaque lot
réserve son numéro de migration au début s'il en a une.

| Lot | Contenu | Dépend de | Durée | Date proposée |
|---|---|---|---|---|
| **L161a** — ameesh : PR sans code, concurrence de main | job `plan` minimal (documentation seule avec ses lecteurs déclarés, version seule, socle) ; agrégat qui accepte `skipped` ; `test_version` et construction du paquet pour une PR de version ; `cancel-in-progress` limité aux PR | — | 2 h | 2026-10-12 |
| **L161b** — ameesh : flux et ordre simple | `--flux` (`ameesh-test-event/1`) ; ordre P0, P2 et liste « toujours » ; fin de l'ordre alphabétique dans une part ; réessai immédiat ; JUnit produit à la fin | L161a | 3 h | 2026-10-12 |
| **L161c** — ameesh : remontée en temps réel | check run publié pendant les tests ; relevé dans `ameesh notify` ; routage au lot ; messages selon L125 ; rouge hérité ; table des résultats (migration) ; retrait à la poussée suivante ; `ci_main_red` ; `ameesh ci status` ; consignes sans `--watch` | L161b | 6 h | 2026-10-13 |
| **L161d** — Nexlink sans carte | Backend suite non lancée pour une poussée de documentation seule (sur `develop` : verdict du commit de code précédent, si le propriétaire l'accepte) ; tests desktop Windows et macOS sur les poussées qui touchent `desktop/` ; SQL : test de la migration modifiée d'abord | — | 4 h | 2026-10-12 |
| **L161e** — ameesh : carte mesurée | `carte.yml` : couverture par test, sous-processus compris ; hook d'audit (fichiers, dossiers) ; tables par classe ; empreintes de blocs ; cache et artefact ; durées tenues à jour ; mesure du surcoût | L161a | 6 h | 2026-10-14 |
| **L161f** — ameesh : sélection par la carte | P1 par blocs, fichiers et tables ; matrices dynamiques ; feu vert provisoire ; « lignes modifiées sans test » ; compte des fuites | L161b, L161e | 4 h | 2026-10-15 |
| **L161g** — Nexlink : flux et sélection | reporters jest et vitest vers `ameesh-test-event/1` ; signal par job ; routage par branche `agent/…` ; rouge hérité sur `develop` ; `--findRelatedTests` et `vitest related --run` sur les unitaires | L161c, L161d | 6 h | 2026-10-16 |
| **L161h** — Nexlink : carte e2e, runners auto-hébergés | carte nocturne par couverture V8, un passage par spec e2e ; si la flotte porte les suites : voie C, carte sur disque, emplacements `rapide`, nettoyage à l'annulation | L161g | 6 h | 2026-10-20 |
| **L161i** — bilan à deux semaines | fuites et gains mesurés contre ce document ; décision du propriétaire sur la phase 2 (ameesh, puis Nexlink) | L161f | 1 h | 2026-10-30 |

Total : 38 h. Ordre :

1. L161a et L161d tout de suite : indépendants, ce sont les plus gros gains
   mesurés par heure de travail ;
2. L161b et L161e en parallèle ;
3. L161c et L161f ;
4. L161g, puis L161h ;
5. L161i deux semaines après L161f.

L161a à L161d n'ont besoin d'aucune carte.

## Gains attendus

| Gain | ameesh (mesures depuis L116) | Nexlink (mesures de la semaine) |
|---|---|---|
| Changements sans code | PR de documentation ou de version seule : 3,1 à 6,9 min de CI → moins d'1 min (18 % des PR de la semaine, 44 % des minutes de CI des PR depuis L116) ; une montée de version : un passage de suite au lieu de deux ou trois | plus de Backend suite pour une poussée de documentation seule : ≈ 7 900 minutes-runner par semaine, et 82 % de ses échecs (rouges hérités) qui ne réveillent plus personne à tort |
| Desktop avant l'étiquette | — (la couche plateforme passe déjà sous macOS sur chaque PR) | tests Windows et macOS sur les poussées qui touchent `desktop/` : 9 des 17 échecs d'étiquette vus avant l'étiquette ; la nuit du 10 au 11 aurait coûté une poussée corrigée, pas six heures et dix exécutions d'essai |
| Premier échec connu de l'agent | +3,0 à +5,9 min → +0,5 à +2 min : **2 à 5 min plus tôt** par exécution rouge (médiane 2,4 min par le seul temps réel, 0,3 à 0,7 min de plus par l'ordre) | par job : 2,5 à 3,7 min (Remote help), 8,6 à 10,2 min (Build Desktop) ; dans le job backend : ≤ 1,5 min ; SQL : ~1,5 min par l'ordre |
| Attente dans le tour de l'agent | 43 min sur cinq jours relevées (borne basse) → 0 : l'attente sort du tour | 184 min sur cinq jours relevées (borne basse) → 0 |
| Feu vert provisoire | ~1 à 1,5 min au lieu de 3,5 pour une PR de code (à confirmer par la carte) | garde-fous et unitaires verts avant l'e2e, qui pèse ~4 des ~5 min de tests du backend |
| Phase 2, si adoptée | verdict d'une PR de code ~1,5 à 2 min au lieu de 3,5 (à confirmer : part des modules réellement choisis) | unitaires : jusqu'à 1,9 min par `--findRelatedTests` ; e2e : rien sans la carte nocturne |
| Branche d'intégration | durée inchangée (jamais rien sauté), mais chaque fusion a son verdict, et un échec arrive à l'agent en ~1 à 2 min (impactés d'abord, par rapport au commit précédent de main) au lieu de ~6 | idem pour le code |

Par PR d'ameesh, en phase 1 : de 6,1 min de CI cumulée en moyenne à ~3 à
4,5 min (−25 % à −50 % selon la part de PR sans code). Le gain se concentre
sur les PR sans code et sur les PR rouges. Le reste du gain est l'objet du
bilan L161i : l'agent apprend un échec pendant que la suite tourne encore,
et ne bloque plus son tour.

# Hors périmètre

* La sélection en local (`ameesh test --impact` dans un conteneur propre) :
  possible avec la même carte, mais un poste sous pression ne doit pas servir
  de référence ; à reconsidérer après L161i.
* Les tests de mutation et la priorisation par apprentissage : sans objet tant
  que les fuites ne sont pas mesurées.
