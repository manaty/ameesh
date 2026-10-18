# Paquet integration/paquet-h-20261019-0100 — composition (§12)

Base : `main` **`5720b4612b23`** (fusion du paquet h-2300, PR #1929).
Branche `integration/paquet-h-20261019-0100`, cible `main`, **PR #1946**.
Une seule suite complète est demandée sur la tête de ce manifeste.

Demandes : auditeur #110913 (01:2x) — composer le paquet suivant sur
`main 5720b461`, à partir de la liste calculée, écarts motivés au manifeste ;
mesh-design #110916 — #313 (gel `4157537`) prêt, deux verdicts hors auteur
(Codex + DeepSeek), `merge-tree` rc 0, 0 migration, classe sensible.

## Admissions (19 unités, 19 lots portés)

- #2559
- #2552
- #2512
- #2525
- #2532
- #313
- #1398
- #2395
- #2464
- #2567
- #2546
- #2504
- #2563
- #2508
- #2557
- #2522
- #2510
- #2517
- #1854

## Composition (19 unités)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #2559 | `5af339266861` (tête gelée) | `44d3432473fe` | sensible, 2 avis : ameesh-dev7 (claude), ameesh-cx3 (codex) — SLO « review GO → merge » : compter les verdicts requis par la porte de fusion (2 familles sur chemins critiques), pas un seul |
| 2 | #2552 | `1a4671f86940` (tête gelée) | `3df4b22397b6` | sensible, 2 avis : ameesh-cx1 (codex), ameesh-dev1 (claude) — Prise automatique L165 : un lot « REVUE après fusion » est pris par un agent de la famille de l'auteur |
| 3 | #2512 | `8631e12e9d76` (tête gelée) | `3302d48ebe90` | léger — Ménage /tmp : unsaved_git_work ignore les fichiers ignorés et les branches locales d'un clone |
| 4 | #2525 | `4f9509962681` (tête gelée) | `0086153f3c97` | léger — Ménage /tmp : _git_copies ne voit ni un dépôt nu ni une copie git imbriquée |
| 5 | #2532 | `8c15ffe0fef3` (tête gelée) | `75224c116c98` | léger — Tests #2512 : addCleanup remet AMEESH_SYSTEM_TMP à "" au lieu de supprimer la variable absente |
| 6 | #313 | `4157537025aa` (tête gelée) | `1e3554335bf3` | sensible, 2 avis : ameesh-cx2 (codex), mty-amh-qa-01 (deepseek) — L65b : refuser ou signaler un compte fournisseur déclaré par deux meshes sur le même hôte |
| 7 | #1398 | `851d4e9d5c50` (tête gelée) | `a42e9a00720b` | sensible, 2 avis : ameesh-dev4 (claude), ameesh-cx3 (codex) — API cluster : compléter documentation, refus et bornes de lecture |
| 8 | #2395 | `eebc58d7a9fb` (tête gelée) | `307bba2cdbf3` | sensible, 2 avis : ameesh-dev1 (claude), ameesh-cx2 (codex) — Notifications : ne plus jamais pousser un slo_breach à l'owner (canon home#108, décision owner 18/10) |
| 9 | #2464 | `d9dfafd7aac5` (tête gelée) | `3f616f11ae2c` | sensible, 2 avis : ameesh-cx1 (codex), ameesh-dev7 (claude) — #2404 suite : liste des prêts — origin/HEAD release/* du clone radar classe « release » les lots sans cible ; --no-ready laisse passer un prêt omis |
| 10 | #2567 | `f560abd43845` (tête gelée) | `f1d4b0af07ac` | léger — Gel statique : normaliser la racine du dépôt depuis un sous-répertoire |
| 11 | #2546 | `050962b8f676` (tête gelée) | `b1ab4fe70de4` | léger — CI : refuser une tête dont le message porte un marqueur de saut ([skip ci]/[ci skip]) |
| 12 | #2504 | `4441452e001d` (tête gelée) | `6d16c97010ea` | sensible, 2 avis : ameesh-dev1 (claude), ameesh-cx3 (codex) — Relecture : chercher le request couvrant par lot au-delà des 200 courriers récents |
| 13 | #2563 | `95fa8444447d` (tête gelée) | `d25033e55b2b` | sensible, 2 avis : ameesh-cx2 (codex), ameesh-dev1 (claude) — Déduplication de relecture : lire les requests du lot par l'index work_item_id |
| 14 | #2508 | `fe0ebab13ebd` (tête gelée) | `f487ae73a11f` | sensible, 1 avis : ameesh-cx3 (codex) — #2426 N (dev7) : ligne vide avant packet_sweep_once ; sync() lit tous les lots au lieu du manifeste |
| 15 | #2557 | `414d78314c19` (tête gelée) | `084a6b91f988` | léger — Rejeu force (#2541) : un alias de destinataire ou une boîte purgée laissent repartir un doublon |
| 16 | #2522 | `1cc8120c5df5` (tête gelée) | `a6465c1ef892` | léger — test_actions : preuve non vacuous du délai connecteur et release en addCleanup |
| 17 | #2510 | `6e9427093327` (tête gelée) | `098af1bdaa91` | léger — docs/EXECUTEUR-MEDIEE.md:516 : « les 66 opérations » → 67 après #1349 |
| 18 | #2517 | `fd812a84fccd` (tête gelée) | `680400e463fd` | léger — docs/ORCHESTRATEUR.md L165 : documenter la garde de famille à la prise d'un lot REVUE (#2377) |
| 19 | #1854 | `93e0a0f11d82` (tête gelée) | `77bc4011cf46` | léger — #377/L86 : 3 N de la R2 Claude avant câblage serve — kid non hashable (TypeError), kid inconnu (relecture JWKS non bornée), JSON tronqué (JSONDecodeError hors OidcError) |

Chaque commit de composition a le lot en second parent, son sujet nomme
le lot et son SHA, et il porte un trailer `ameesh-work: <lot>`. Les unités
#2525 et #2532 sont empilées sur #2512, et #2563 sur #2504 : leur tête
contient le socle de ces lots, composé avant elles ; la fusion n'ajoute que
leur delta propre. Aucun conflit de composition n'a été rencontré.

## Contrôles

- Liste calculée `scripts/paquet-candidats.py` à 01:26 (Europe/Paris),
  base `5720b461`: 19 lots prêts, 24 en attente, 0 lecture en erreur.
  Les 19 prêts sont tous composés (aucun écarté).
- `paquet-inclusion` (tête `77bc4011` + ce manifeste) : **rc 0**, 19 lots
  attendus, 19 entrées de composition, 15 contenus présents, 2 partiels,
  1 vide, aucun défaut. Les trois contenus à lire sont expliqués ci-dessous ;
  aucun n'est une perte.
- Tests ciblés de composition : `tests.test_1398_peek_boundaries` (9) et la
  plus grande partie de `tests.test_1617_review_designation_pg` (26) sans
  échec en psql, puis arrêt volontaire du run local (coût des passes
  complètes) ; la suite complète du paquet en CI qualifie le reste.
- Contenu : 60 fichiers, +3915/−412 hors manifeste ; aucune migration.
- Correctif de composition (CI 39176999732) : `test_l107_executor_contract`
  exigeait 396 refusées et 463 opérations d'interface, chiffres dérivés du
  contrat de la tête ; #2504 avait mis à jour 395/462 sur une base qui ne
  portait pas encore son refus. Les deux compteurs de
  `docs/EXECUTEUR-MEDIEE.md` sont réalignés (aucun contenu de lot perdu) ;
  le test est vert localement, puis le run complet est redemandé sur cette
  tête.

## Contenus à lire (avertissements d'inclusion)

- **#2504, partiel** — `src/ameesh/storage/postgres/mailbox.py` : 16/18 lignes.
  Les deux lignes absentes sont l'ancien prédicat
  `btrim(coalesce(work_item_id, '')) = %s` et son commentaire, remplacés par
  #2563 (empilé sur #2504) par le prédicat indexable `work_item_id = %s` :
  c'est l'objet même de #2563, sans perte du besoin de #2504. Le reste du
  delta #2504 (`mail.py`, `interface.py`, `review_designation*.py`, `work.py`,
  `contract.json`, témoins) est présent.
- **#2510, vide** — son unique delta (la ligne `RemoteStorage` « 66
  opérations » → « 67 ») est déjà fait à l'identique par #1398 ; la ligne
  finale de `docs/EXECUTEUR-MEDIEE.md` dit bien 67 opérations. Aucun contenu
  perdu : le fait est porté deux fois.
- **#2512, partiel** — `src/ameesh/housekeeping.py` 78/80 lignes et
  `docs/EXPLOITATION.md` 6/7 lignes. Les lignes absentes sont réécrites par
  #2525 (empilé sur #2512 : `_git_copies` reconnaît désormais un dépôt nu et
  une copie git imbriquée, doc ajustée) ; les témoins de #2512 sont corrigés
  par #2532 (addCleanup qui supprime la variable). La fonctionnalité de #2512
  reste portée par la tête, avec les correctifs de ses relectures.

## Écartés (ce véhicule)

Aucun lot prêt du calcul n'est écarté : les 19 lots prêts sont composés.

## Non prêts au calcul (recontrôlés à la porte)

| lot | motif |
|---|---|
| #2198 | verdicts-missing : 2 avis exigés (sensible), un seul acquis (ameesh-cx3) sur `e91a0254` ; second avis à désigner |
| #2520 | verdicts-missing : 2 avis exigés (sensible, `.github/**`), un seul acquis (ameesh-dev7) sur `4f2519bf` ; second avis à désigner |
| #2443 | verdicts-missing : 2 avis exigés (sensible, migration `0465`/`storage/**`), un seul acquis (ameesh-dev1) sur `ae077364` ; second avis à désigner |

Les autres lots en attente du calcul le sont pour `verdicts-missing`,
`blocked-verdict`, `no-verdict`, conflit ou cible non publiée ; ils ne sont
pas des lots prêts et n'entrent pas dans ce véhicule.
