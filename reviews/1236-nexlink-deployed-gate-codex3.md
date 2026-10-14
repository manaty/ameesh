# #1236 — porte QA Nexlink activée, vérification indépendante Codex3

14/10/2026, demande bureau17:04, planactivationv20§10. **Avis favorable borné à Nexlink : rôle/opt-in effectifs au registre et comportements attendus vérifiés sur fixtures isolées du paquet déployé12408e2b. 0 défaut produit trouvé.** Aucun lot réel créé, annoté ou promu par cette vérification ; aucun verdict QA réel enregistré. Le volet ameesh relève de mty-amh-qa-01, contacté directement pour coordination ; les quatre déploiements hôtes annoncés par le bureau ne sont pas requalifiés ici.

Lecture directe, transaction READ ONLY sur le registre actif : codex3 porte relecture/implementation/qa et projects=[nexlink] ; mty-amh-qa-01 porte qa et projects=[ameesh]. Seul opt-in IT actif observé : nexlink, typesrelease/publication ; QA garde promoted seulement, merged ne reçoit aucune garde QA. Déclaration/proof garde merged/promoted préexistante conservée. Les lignesproject_modules sont identiques à outils/1236-phase5-activation-proof.json du bureau et au relevé après fixtures.

Paquet réellement importé : ~/.local/share/ameesh/venv/lib/python3.14/site-packages/ameesh ; les cinq fichiers work.py, modules/it/__init__.py, modules/storage.py, modules/__init__.py et migrations/0427_qa_role_guard.sql sont byte-identiques au snapshot src-12408e2 et à Git12408e2b. Aucun import du src de test pour le produit : seul le répertoire des utilitaires tests est ajouté à sys.path. Preuvesdeployed-source-proof.json.

Six scénarios indépendants sur chaque pilote, exécutés via ameesh heavy, sans rejouer les suites auteur : psycopg6/6 en2,841s, psql6/6 en14,111s, aucun skip. Ils utilisent des schémas t_nexlinkreview_<uuid>, des fichiers fictifs et les seules données de rôle/politique lues en staging ; ils réemploient les utilitaires de préparation PgTestCase/QaEvidenceTest, avec leurs propres scénarios. La garde SQL0427 et les fonctions IT déployées sont exercées, sans transport CLI/rattachement réel de la session.

- Codex3 distinct de l’assigne, du déclarant et de l’auteurpreuve écrit un verdict ok lié au deliveryid courant : la fixturepromoted est ouverte. La même fixturemerged était atteinte sans QA ; promoted sans QA est refusé.
- Codex3 assigne, déclarant ou auteurpreuve : trois refus qa-same-actor distincts.
- Acteurimplementation sansqa : qa-role-required ; acteurqa ameesh sur le lotfixtureNexlink : qa-role-scope.
- Nouvelle déclaration de livraison, même digest : deliveryid différent, binding QA ancien invalide ; promotion refusée puis autorisée après nouveau verdict.

Complément ciblé du dernier scénario sur chacun des pilotes (1/1 psycopg1,988s ; 1/1 psql8,099s) : fixture déjàpromoted, livraison remplacée à digestidentique ; audit public rapporte stale_qa, retourmerged de la seulefixture, promotion refusée avec l’ancien verdict puis autorisée avec le courant. Le labelstale_qa est contrôlé dans son état gardé, tandis que merged reste hors auditQA. Aucun rejeu des cinq autres scénarios après ce complément.

Deux erreurs de construction de la sonde, corrigées avant ses verdicts : mauvais chemin modules/it.py (it est un package) ; tentative de changer app d’une fixtureacme, rejetée par gardeimmutableidentity. La fixtureNexlink est désormais créée directement comme Nexlink. Logs de préparation conservés, aucun défaut produit attribué à ces erreurs.

Nettoyage et limites : après les deux séries et compléments, zéro schéma t_nexlinkreview_% subsiste ; rôles/opt-in actifs relus identiques à l’avant, en READ ONLY. Les rôles/acteurs des gestes sont simulés dans la fixture ; ce contrôle établit le comportement du garde SQL avec le paquet déployé, pas une promotion ni un rattachement QA réel. L’activationcode/règleNexlink est corroborée localement ; les sondes des autres hôtes et le voletameesh restent distincts.

Preuves publiques : reviews/evidence/1236/codex3-nexlink/ — probe.py, quatre logs verts, erreurspréparation, lecturesregistre, preuvesource, nettoyage et manifestSHA256. Aucun secret/DSN dans ces sorties. Aucune CI lancée/sondée, aucune fusion/déploiement/promotionréelle par Codex3.
