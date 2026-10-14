PIÈCE CONSTANTE #364 — fournie par l'auteur (mty-amh-impl-04), 14/10 06:13Z. Lecture seule : aucune CI, aucun push de branche de lot, aucune mutation de branche gelée. Document du pré-manifeste #1260.

1) TÊTE GELÉE
- tête : 90aae6672b5d0dd624a503bdeb8943a7caaf7623 (PR manaty/ameesh#693, branche lot/l232b), arbre 2039734bdb41369a4e1f330f4c9944bb579b8766 ; commit du 13/10 16:59:42Z, non amendée depuis ; revérifiée à la source 14/10 06:13Z : refs/pull/693/head = 90aae667, PR OPEN, MERGEABLE, mergeStateStatus BLOCKED.
- base de comparaison : merge-base(90aae667, main) = b132d5d4397efdd7ba05e7682bdbf68eab61703e ; delta = 73 fichiers, +13817/−99, 43 commits.
- réf. de fusion testée : refs/pull/693/merge = 069e9fea9bd54759c362b2bc48e3af672338410f, parents afa32932e72c459bf1d23314912647b23b306b73 + 90aae6672b5d0dd624a503bdeb8943a7caaf7623, arbre e8043342ab353837dfbcc24cf412d6fdf00aba23. Contient tests/test_653_db_connections.py blob 7c719a14282ab4a231f0af37216c6829c346067e et tests.yml timeout-minutes 45 (l.245). Son premier parent afa32932e est périmé : main = 7305957e88b7f511c2e853e64a6573f8e574832e au 14/10 06:13Z.

2) CLASSE
- sensible : SQL/migration (src/ameesh/migrations/0417_host_release_fences.sql), droits et rôles (deploy/sql/role-*.sql), concurrence (fences/verrous), prod/déploiement (deploy/**). §2 : deux avis d'autres familles + feu pilote.

3) AVIS (sources exactes)
- R1 Codex ameesh-cx1 : PR #693, commentaire 6150849386, 13/10 17:02:35Z, sur 90aae667 — « GO statique sur le correctif, P2 levé ; 0 B / 0 N / 0 n nouveaux » (delta 6db727ac..90aae667) ; précise ne pas remplacer les relectures antérieures du reste du lot.
- R2 Claude bureau : verdict ok 17:08Z sur 90aae667, consigné par le pilote dans la note et le feu du 13/10 17:17Z (« deux avis non-DeepSeek posés sur cette tête » ; conditions : CI exacte verte toutes parts exécutées, aucune annulée #940, tête inchangée, fusion --match-head-commit par l'auteur).
- chaîne antérieure, contenus conservés : R1 auditeur 13/10 14:42Z sur febbf0ad (NO GO 5 P1/6 P2) ; R2 mty-amh-qa-01 (DeepSeek) 16:22/16:32Z sur 5270b1e9 (2 B/1 P2) ; B1/B2 corrigés en 6db727ac et P2/K2 en 90aae667, revus (cx1 16:55Z NO GO puis 17:02Z GO). Aucun avis sous le repli DeepSeek : les deux avis finaux sont Codex + Claude, familles distinctes entre elles et de l'auteur (DeepSeek).

4) DÉPENDANCES
- #671 (merge b21b80678d8b) : apporte #925 (test_653 blob 7c719a14282ab4a231f0af37216c6829c346067e) et #880 (plafond psql 45 min) ; ancêtre de main (vérifié).
- #1112 (afa32932e) : manifeste et budgets ; ancêtre de main (vérifié).
- #1211 : non actif au 14/10 06:13Z (PR #923 OPEN, tête 39f51fd9d0105b21911adb1d56c7df67fc3876eb, non ancêtre de main) ; condition de la CI du paquet.
- 069e9fea est à jour du correctif test_653 et du plafond 45, mais pas de la main courante ; aucune relance individuelle (ruling pilote 05:48Z).

5) ANCIENS RUNS NON VERTS sur 90aae667 (tentative 1, source API GitHub ; aucun vert applicable)
| run | workflow | conclusion | faits |
|---|---|---|---|
| 38435051980 | tests | failure | psycopg 4/4 rouge (flake test_653, corrigé par #925) ; psql 1/1 annulé au plafond 30 min (#880) ; agrégat tests rouge |
| 38435052029 | carte | cancelled | mesures psycopg 1-4/4 vertes, mesure psql 1/1 annulée, job carte vert |
| 38485229711 | tests | cancelled 01:13Z (purge de flotte) | verts : psql 1/3, psycopg 2/4, psycopg 3/4, plateforme macOS, plan ; annulés : psycopg 1/4, psycopg 4/4, psql 2/3, psql 3/3 ; agrégats purgés |
| 38485229713 | carte | failure | seul job carte rouge : couverture sqlite « database disk image is malformed » (mesure-psycopg-1), infra hors diff, cause suivie #1257 ; mesures psycopg 1-4/4 et mesure psql 1/1 vertes |
- gh pr checks 693 au 14/10 06:13Z : carte fail, tests fail, psycopg 1/4 fail (flake du run 38435051980), autres parts pass ; lecteurs de la doc skipping.
- têtes antérieures (6db727ac : 38432908411/30 ; 5270b1e9 : 38428236069/87 ; febbf0ad : 38408426006/134) : runs annulés, sans objet pour cette pièce.
- conséquences : feu conditionnel 17:17Z non satisfait ; rulings pilotes 05:48Z et 06:01Z : aucun réarmement, aucun close/reopen individuel, reprise par le paquet #1260.

6) FICHIERS ET BLOBS (tête 90aae667 ; statut A/M, blob git, SHA256 du contenu)
| fichier | st | blob | sha256 |
|---|---|---|---|
| deploy/mise-a-jour/adaptateur-hote-release.py | A | 4fae7c30f0e9f06d44803e22377259443236be90 | deb4ccaf0414f6c0df0b8ff7778184da6cbf18e8cdb118011055a5584d6bb8ca |
| deploy/mise-a-jour/autorite-release.py | A | eab7053d912a730bb655dd170675ff88449a0196 | 9eef599ab2b927a3c8b42d7ca956f6e44f15db1b5d21dbf44744a04ce895117b |
| deploy/mise-a-jour/bootstrap-release.py | A | bc24d0c2484c6eae866cad344a69e6e90fbca0ea | d6c176e38aaeb4101cfd9e42af39831a3f9699acfb74308407864c682e97f383 |
| deploy/mise-a-jour/compte-release.py | A | 8448bb6e3fc1bae35fd45e6cda2f5fb1d4a017a3 | 28359b1936ec055ec7ec33ce937c509f43ff773054d2a3f1d2c3119ac48b17d6 |
| deploy/mise-a-jour/configuration-release.py | A | 57d20d6f88c4fb3084820b41bfbcd543e192f12e | 864eb8f1c8d082fc6da4dbf6a27256a798ea08f52bd31bbd52a90767f8aaa7b1 |
| deploy/mise-a-jour/controleur-release.py | A | 92a1e23b19fb58edb02eb7fe114ca83c65bf07e4 | e370dfa87c3208594e55a8b74ba2fe365fc673312aa36bb3eabb2e786d62b622 |
| deploy/mise-a-jour/delegation-release.py | A | f6d0493abae8e27e988e654c71567b5e20f1158e | 5b0f5ce4341a672e1124ce0e6c670fb9cc76b22fd01f20e6535c46b338798f3a |
| deploy/mise-a-jour/environnement-versionne.py | A | fdb6e72c6afdae7d8dffd1c1418c364ee3678afe | d5bf0b40683d43c1cbb23bdcd75daa827eae9cd9bdcfc438223cedb603905c8e |
| deploy/mise-a-jour/fence-release.py | A | bd648f1ceb025e37f826b5d2aec2919959c2938b | 2c38753791dd1be0a2964774f29b033471c821d81ee53639e93c9b11849f47d0 |
| deploy/mise-a-jour/fichiers-bootstrap.py | A | 9a623d467316c43006b59009f85afd857c6587e8 | a2b9f9d8a4a2e7d13ed880a0a670f02c4c00849469450769a365b0cf3ed8cbb1 |
| deploy/mise-a-jour/helper-redemarrage.py | A | d1d611ae77c0d85586a89737ffbf1204497833d8 | d1f97e0cb5afdc55b3ffe9979de150314a6da4939fdc1b3ae6da9806512074c4 |
| deploy/mise-a-jour/hote-release.py | A | cdca153a8b3f557d2c83571f3e42eb31e80ab550 | e206a439ee6e5c731e54b000f46035d5baf9f1a2de89a0c70df625304859c724 |
| deploy/mise-a-jour/installateur-bootstrap.py | A | 109ab24691f8cf180ae4d6f8b99456353ecb9907 | ca07f407601673b022976c8ebe1189b9cb6dbad59f1b080e43237785e8416449 |
| deploy/mise-a-jour/pilote-sante.py | A | 41acd8deda9608f4878c2dd5ba28cca6627264d5 | d9e2231f49b23b36bf21ebe5398c82af4187303a49450be1a5c44009ad07296d |
| deploy/mise-a-jour/poste.sh | M | 77aaab1747d85596cf8c4f6febe0724f38b60a2f | 139076ae0d42f5111f9ae5ee41b0b9466bb8400379cc640cf37d94030a24ad15 |
| deploy/mise-a-jour/retention-privee.py | A | 4fedd5fa2c20b175fcac6a1049683f9f7907f9a2 | 3ec88d088fa23dd0532d2a28bfd1268c4505703c5d169e4b68e1167b8c6595ae |
| deploy/mise-a-jour/retention-scaleway.py | A | 15e791dc559480fe76b31668ed6679aa7007121c | a3bf00d832d82a127bc8c4c3972c4a4737e987de568f2d4d292871f50333bf9a |
| deploy/mise-a-jour/runtime-release.py | A | a210a7302f4b40fd78e3e19a96d1e1aa218269a9 | 209a8f0475c038fc303618813021e08d3703d09359fb8655c77ad2b02c3e6816 |
| deploy/mise-a-jour/sante-release.py | A | 1fa9cb186f45c4a458072d063dce7105d46fbcf5 | 8bb35566b56671593e5221f9c3c2581480e55981315866d77724208de9883316 |
| deploy/mise-a-jour/sauvegarde-release.py | A | 9ca7b360b64564bb2edabeac212e3ee2d9acdec4 | b79c139e3aa475101b114407854d6f44d49d67376ab97859352e944745f91665 |
| deploy/mise-a-jour/sauvegarder-prive.py | A | 11fb8bf5dc1cb6114813a98b9cca78ca54cd3496 | 382f958f6fee626d4101767194e50bf20a0e6e33d8e299e3ad6d823bd920429c |
| deploy/mise-a-jour/service-release.py | A | a7c42f6029d06f8adc9705aed188e207bf4e549b | 2e891ec4f1d9420ab0c18c918e0fe3532378fc0c5906b257f8086f49c6779a52 |
| deploy/mise-a-jour/transport-release.py | A | b92964a03daecd0f2711612a936a36e74c7ae510 | 93f30a52e6ef95109bad669b7b8468be92933cdc7ffb6892aa7a12b0e86cd089 |
| deploy/mise-a-jour/unite-controleur.py | A | fe6e2f08e52dd0cca0d32c740821f40039ce7aa8 | af4ef6c24c8a9bf02c965bad69dcc21d62ffd503b34a5b5b88375c60f6cdd49c |
| deploy/mise-a-jour/verification-release.py | A | 6680a29f19a7bce11274063c59d65eddd188d3df | 467aaf6962ee4682d9d27ed43aa2cb0925017dc1749a677b702b6231c4b68864 |
| deploy/serveur/README.md | M | 6b8941bc30e728b1b9b9ba55e63f44cc6663e1ef | e3a43acffab8c1bb85b17cc13464d91b345063db2bb6d4270a195bc54d673da2 |
| deploy/serveur/installer-serveur.sh | M | b08941ee93cfb6e8fba3bf38ba28defff8ebcd98 | 64b662ea07b42093e55acbedf33b5161b99a0eb2ec15865fb66fc530f8509f4c |
| deploy/serveur/sauvegarder-base.sh | M | d28a1a0f741175f128ce097c0589ef85d8cc0b50 | 20f10cf0c1b4bf90634e88969fe413ba9243ad52250071bda855408d5acc10d2 |
| deploy/sql/role-executor.sql | M | d01d5c38bde75ba8dfe6bfb32527b953fdcf13ea | 03a1c976fb77364b1c55ebfa5be59bc65130d223207872c01ddd331b45837546 |
| deploy/sql/role-module-writers.sql | M | 3db08d421e698d83fb46d3725ed716913d53d985 | 641475e623b981eae60a6c9b03b62a5504a8f98493c24562ddefdf27ba79be47 |
| deploy/sql/role-release-controller.sql | A | c132504b52eb6502eb2a7a4946973d5a556cd9e9 | f992f553d90b0ca9b5dd371b032c322da5a9de57edde8ef631a05c1fec246f8d |
| deploy/sql/role-superviseur.sql | M | 441dbbd44c1201c1fa8d9b031d6a8cb50270c041 | 9784c1bd7d9e8e5094cf088c437a7f722b761549dd537365b66dc15c19db144d |
| docs/BASCULE.md | M | 62fb073525be23bcbc7b05e038e6baf42c736a1f | 6cc2772299c2a5c8bcceb3d7d764d02c6d3d359a24b72255cfa5130156a93994 |
| docs/L232B-PRIVATE-BACKUPS.md | A | 3ac6c3ab00f7cde8d9ba5e391d333688e63f6780 | dd976f103cc6ebc63aa0e657800c67f8ba8aeb0fdb7ba8419469842836b13443 |
| docs/L232B-RELEASE-CONTROLLER.md | A | 36d9b2a028a4754e059c5f0edd58b5d36a50d924 | 3488fc79dfe54d979a6f18e6690397a2f6c2a1f8cbcd60a445db0f050f991bd4 |
| docs/L232B-VERSIONED-ENVIRONMENTS.md | A | 9b34146e9d23c8e00efa7c318765f7b74bb91fc1 | 898be5234807e4704ef42f6073a8bf2572297fa55360ec43023204a873cf7859 |
| src/ameesh/cli.py | M | 5621de3ed6576a5667e479de98803bf4f6dc5b24 | 0dab8ad872032f9055af317ddab85fef9a8584a580e64c8cc3a372ff59171929 |
| src/ameesh/db.py | M | ecb56bd3599b7b817f87919ec21a18d16de7b3eb | 0ff65e174751e2ad4c7b66c820d92e8322d0ec466fc2ed675b1ed8dc40e7dea0 |
| src/ameesh/migrations.py | M | 3c6393007a161d725d420bbc1db1efbba6af1dd2 | 523ed2d4abc9c1eb57cdfed2548228df975b6b5a87e15671f235cf30a42a05f5 |
| src/ameesh/migrations/0417_host_release_fences.sql | A | 4a04180c60d08c03a350f3dbb9ff52edd22a5ec6 | d255c97963b030ad4b4780400bbe3a71a11e0d802a375dc84b54a33b5762484c |
| src/ameesh/storage/postgres/registry.py | M | 4aff97486aa85bb605ded57134b6b57c0db1791c | 8d9a9b3c8314599fca3057b2e2ad13a48c7fa05a36d0fd290d7a7082e59da992 |
| src/ameesh/storage/postgres/release_fences.py | A | 62764c2982867bd915db04c7d88e6669512f21b0 | c1f4299ac362cbf831a3117a0bde07499ef6581021033607e27e72e57e1a65e4 |
| tests/parts/pilote-psql.txt | M | ad7564e0eee2c70259307c81f6fa64105d71cc74 | bff0ff76afbe27d1b0359d707c74da3a5fef4f95a8d694b15ff0e971c7c07b60 |
| tests/support.py | M | bd56d8c69c44e0b077ecdf7b14db0fdf8f8108de | 145e64666ec6dfd2b45a97d0fee6b439f9bd003a95c03a76d2b56ca89cdca9f0 |
| tests/test_518_dsn_argv.py | M | 1dd75282c92c1fae4bf020aca315e614addab0cc | 60207228cbaad29a3f5e6c9854a9873e79d7564362d565c680e968a3223f42bb |
| tests/test_cluster.py | M | cd6800d8c838bb8363aac4de5129c4a3c2859503 | 0b30759e75a302ebb7979ac0e14d728adeca62f872254585c948f5c02d54e5f1 |
| tests/test_l232b_account.py | A | 3874468789592fb5b1fc9d4b6d66b41b4215fe12 | c9b33d3fcbceaffc551d7f9d9c4cebe89e18d0070638865bd042ab4f9e3b0e16 |
| tests/test_l232b_backup_restore.py | A | 1eb6ce3e2d28df561ea0f7b9101fdb35a057a5c4 | bc1ac10e357adb52e69bfe82f2b7afaecd5e3c6723cde58d39e53090b159853e |
| tests/test_l232b_bootstrap.py | A | 439b50909921aefa93fa8a8625e15091a4d0fe82 | c73643bddfc07c6cc3a9923084a34a66af97fb9b1165990cbcf2abbddd363289 |
| tests/test_l232b_bootstrap_files.py | A | b17d690ee3a4811c73134f5ef754790f5fe2eb2b | 8ed453bf50cc7f1a92f268b0316f4016a00fe58ffe6fa79dca240e47a98a6c00 |
| tests/test_l232b_bootstrap_installer.py | A | c177a0c5a67f22a4ab0a5b2e1f22e66439a0e50b | 90a46e1adc20bf2e11f62a4aa978dd214d13215283cff337b3b583d0c37ae50e |
| tests/test_l232b_delegation.py | A | af0dc3d963ecb610a37087f4731c43c97ba8204c | c8b13c91e9d269cc928370cdad1f84523fd17b29f9f60cee5ffa19b855a87b01 |
| tests/test_l232b_health_turn.py | A | f667eae319d98fc36e370cc2a37d2213b15e8835 | 362f88703ab5eed022fa71370ce1667aa8913275af212183ff15138ad768e227 |
| tests/test_l232b_host_factory.py | A | 7e6edfab198e526f7a3db34e4a023e6cd25b2666 | 2f1ef96037618ade8e3b777cbf797dc5b5509cef66a6010a044bcbe8454dc1b3 |
| tests/test_l232b_migration_worker.py | A | 576608f156c6373f666bf054b4fe0c8a533acb5f | bce8970de48d58899c22e2d9bc624f85ac84c77bacc283b90515f5ef78647653 |
| tests/test_l232b_private_backup.py | A | 732fe618398b4aceedad8f847350c3844ecb0aea | 6edc3b2d30bf63f88958f072a5f1e2aaf84df44221495fb56cf26f6f11ccdf54 |
| tests/test_l232b_release_authority.py | A | 6c9a2fb896836ba004041612590fced5f48f26f1 | 530c6344e0d452e4a10f30452547ea2a43141edfabcb9814915dbfb04498ac0d |
| tests/test_l232b_release_configuration.py | A | e3b13af590178e5c97991b4354f6c7be96ff7daa | d68da722b7be27b0778aad5816d381ee53bb2bf31190b8ea45461010aac7ed6e |
| tests/test_l232b_release_controller.py | A | 778fcf25c4b7714710fd9b93788ffabb3b530e80 | 74332c36cf73f996feafbf637ff4062b9073b9dadd0e0a2bba8390e707e53092 |
| tests/test_l232b_release_fence.py | A | 4c7a4343000e095a8998a6e90f464643c2be3e66 | 3a63ef0d09a2b592115d98d0a547ef93f9af0757607f29cdc22e5e26731b93d4 |
| tests/test_l232b_release_health.py | A | f7568fbf27f9187ee238203d0dc29b40d210190c | fdb78c3b77c467abfb56606ae316e722f86fe0cfb2119fbbfd5f375984ab0a16 |
| tests/test_l232b_release_recovery.py | A | 23caadb2a88aefeab5ad0a26a95680d37188a606 | 5fbf4e615efab5f2e689c1b1b190560b2e0a11bf7783c0d9fac7fe9d40021a8b |
| tests/test_l232b_release_scripts.py | A | 1436ae7643e6459425e90b6010474144b91dfb1c | 7594157e783c544ceeeb033a54c46e52320c70b64c54c941ef5962ccbcd723c8 |
| tests/test_l232b_release_service.py | A | 0c06f9439a2ac13a8b754eec967acd0091821135 | 50792b84952b833fb7186ce64065a34c4a8c4028b073067430839d3f3efcb85e |
| tests/test_l232b_release_verification.py | A | d568c050f8efb5a2f7df143a784de8cbea1ef87c | 2c46464a0f5b83957b0055d03ca9189d22432f1472ecd8d00aef78e99b6a92eb |
| tests/test_l232b_restart_helper.py | A | d741d94eb3eb6e359ccb689ad96d000c59601fd2 | 31553fd24a4cfcfd7cd368c90892a5a7e23951565c5a62416c410f75add3e3ff |
| tests/test_l232b_retention.py | A | 5a1025f10ca2b31f26ce5802879923a847866421 | 803bdda7f3b5f5bb41fa81d96d365b269cd268215ca195f12997c7ee65556f69 |
| tests/test_l232b_retention_provider.py | A | df99543ab07c0f57090e6b156fedf1a04edd3a2c | d9d5db63add9b6a507073a75746b521f7183a04571271b202577432cae31702f |
| tests/test_l232b_runtime.py | A | a0b7541f797c345eb57a82ec90bcad16882b4a06 | 3029f57ac6826906c10ea163a2dcdf0970ce4a7e4301d28b27744ce84174e8c1 |
| tests/test_l232b_scaleway_retention.py | A | 5fd7ba99c063720bce077a9a38578e11cd7ae8a9 | 3c76156197d608f07196dd9617b92eeb2680c9464cc56b1378ff3018639afca9 |
| tests/test_l232b_ssh_transport.py | A | d92ddaa00313dfb7b4c54844d28b3b3c27c60c36 | 7a7995a6c93345d93885584e6859f40c910aa1cbe6f015d43724981fe51bc53c |
| tests/test_l232b_versioned_environment.py | A | 34c7e7fa10a7bc43b8a24316b38af5cc1691d693 | 92a1dc9fcce56401f50c21b29251164f3d27dcc8648351c5eb98ffca4913f4a2 |
| tests/test_l56_role_executor.py | M | 60445907bcb86d1222f4ea2cdd5b4bed40aea029 | fa97aace75a3732dbafef900a4176c7c4899b69ba150ed96476e6c27cd676c27 |

7) UNION MAIN FRAÎCHE (provisoire, base pré-#1211 : main 7305957e88b7f511c2e853e64a6573f8e574832e, 14/10 06:13Z)
- ordre testé 987 → 364 → 974 ; trois fusions propres (git merge-tree --write-tree, rc=0, aucun conflit) :
  - main+987 : arbre 37abd5a2538ef41558e004aebd0628e0f5d40f11 ; 3 des 4 blobs de #987 constants ; .github/workflows/tests.yml adapté (tête cabebe0032deb17befc065140cbdcd6f99b543a2 → composé 00b374d7dbd3d7b9d2c62e78a06f4f21e4fecc24) car main a aussi modifié ce fichier → avis sur le delta résolu requis (§12).
  - +364 : arbre 4b65a17719bbaafe7481017dd7311e59de1160d3 ; 64 des 73 blobs constants, 9 adaptés (main + lot, unions, aucun conflit) :
    deploy/sql/role-executor.sql : d01d5c38bde75ba8dfe6bfb32527b953fdcf13ea → a309ffd37c8159a9fa756b4b24f257d57bd1a701
    deploy/sql/role-module-writers.sql : 3db08d421e698d83fb46d3725ed716913d53d985 → 8294030c5d5ad5613c99cb17db91565b12ecde99
    deploy/sql/role-superviseur.sql : 441dbbd44c1201c1fa8d9b031d6a8cb50270c041 → d010d12bff168c9bf1cc31e60c9b657337c934d5
    src/ameesh/cli.py : 5621de3ed6576a5667e479de98803bf4f6dc5b24 → 8fa6a7b9f2c5f980d693ca4729155d0ef0b7131c
    src/ameesh/storage/postgres/registry.py : 4aff97486aa85bb605ded57134b6b57c0db1791c → 719714745811bd6ae1636bfbf0bfe0eb580ad201
    tests/parts/pilote-psql.txt : ad7564e0eee2c70259307c81f6fa64105d71cc74 → bd2f421a75d5002d2edc482b441eb62ea7e2fb43
    tests/support.py : bd56d8c69c44e0b077ecdf7b14db0fdf8f8108de → a240ddd947e35925de5dbf19f66288fea8fa30eb
    tests/test_cluster.py : cd6800d8c838bb8363aac4de5129c4a3c2859503 → fec5af4e77c8a63b3bd76e4674f05a0f7dbeb375
    tests/test_l56_role_executor.py : 60445907bcb86d1222f4ea2cdd5b4bed40aea029 → 10db514ce956c98d5134279a82e15fd880ea004c
    → avis sur le delta final (9 fichiers) requis avant fusion sur main (§12).
  - +974 : arbre a00ae7b57c620c1edf28ac552a28b9f6f53cf9ac ; #974 ne modifie que 2 docs du jeu #364 (docs/L232B-PRIVATE-BACKUPS.md, docs/L232B-RELEASE-CONTROLLER.md), placé après #364.
- union = exactement 77 fichiers = 73 (#364) ∪ 4 (#987), aucun fichier étranger ; les 2 fichiers #974 sont déjà dans le jeu #364.
- portée provisoire : la base bougera avec #1211 (tests.yml sera de nouveau adapté) ; à refaire à l'identique sur la main exacte après activation de #1211, puis geler.

8) ORDRE #974
- #974 (PR #824, tête 7e65733c3fe46d5af4db144e6f54552d689c049c, base lot/l232b) reste APRÈS #364 : sa base est la branche du lot, il ne peut pas fusionner avant #693. Delta = 2 fichiers docs (+18), classe légère, relecture d'après fusion §2.
- Dans le paquet : entrée après #364 (docs, aucun conflit). À défaut : après la fusion du paquet, retarget de #824 sur main et fusion docs-only. Aucun test DB requis.
- #1002 reste après #987 (mandat #1260).

9) LIMITES
- Aucune CI lancée, aucune branche gelée touchée, aucune fusion ; objets d'union provisoires locaux (merge-tree, sans ref).
- Aucun feu : cette préparation n'en donne aucun. Le paquet exige, après #1211 actif : base main exacte, manifeste admissible, avis sur les adaptations, arbre gelé, suite complète unique (workflow_dispatch), feu pilote distinct.
