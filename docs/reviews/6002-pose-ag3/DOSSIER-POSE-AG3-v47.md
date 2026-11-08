# #6002 — AG3 v4.7 : correctif du transport C de pose-DB (STOP du 08/11 17:19Z)

Bureau (BG), 08/11 ~17:35Z. **AUCUN GESTE et aucune commande sur une VM** pour ce delta.

## Contexte

- La mise à jour de l'adaptateur v4.6 a réussi à 17:19Z : AG3 est sur M', TS `20261108T171908Z-2469621`.
- `pose-DB-vm_probe-v46.sh --apply` s'est ensuite arrêté (rc 2) à l'installation, sur l'erreur
  « psql: FATAL: invalid command-line argument for server process: -c ».
- Effets de cet arrêt :
  - la base est intacte : aucun rôle `vm_probe*`, aucune fonction ;
  - le vérificateur reste sur AG3, en root 0600, grâce au trap de v4.6 ;
  - la copie de mesh-1 a été détruite ;
  - un résidu public reste sur mesh-1 : `/var/tmp/ameesh-6002-vm-probe-20261108T171938Z-2469696/`, avec
    les 2 fichiers `.sql` en root:postgres 0640.

## Cause

`C()` passait la commande en clair à travers deux interprétations : le shell d'AG2, puis le shell de
connexion de mesh-1, auquel ssh transmet ses arguments joints par des espaces. Le saut AG2 consommait
les quotes simples : `PGOPTIONS='-c search_path=public'` arrivait sur mesh-1 sous la forme
`PGOPTIONS=-c search_path=public`, soit deux mots.

D'autres appels C ne fonctionnaient que par hasard :
- `sh -c 'cat > …'` devenait `sh -c cat` suivi d'une redirection ;
- la requête finale `-c "select …"` aurait aussi échoué.

## v4.7 (`pose-DB-vm_probe-v47.sh`)

1. **Transport C robuste.**
   - La commande est encodée en base64 : son alphabet ne contient ni espace, ni quote, ni métacaractère.
   - Les sauts AG2 et ssh ne voient qu'un seul mot inerte.
   - **Seul `bash -c` sur mesh-1 l'interprète, exactement comme elle est écrite dans le script.**
   - L'entrée standard (données SQL, vérificateur, scripts `bash -s`) traverse inchangée.
   - Tous les appels C en bénéficient : préalables, copie SQL, installation, vérificateur, mot de passe, audit, relevé final.
2. **`PGOPTIONS=-csearch_path=public`**, sans espace, pour l'installation et pour l'audit : sûr même hors transport.
3. **Résidu exact de la tentative v4.6** (`RESIDU=…20261108T171938Z-2469696`).
   - Il est retiré au départ de `--apply`, seulement s'il est conforme : root:postgres, et contenant
     exactement `_contrat-vm-probe.sql` et `role-vm-probe.sql`.
   - Sinon : STOP, sans rien effacer.
   - Absent : « pas de résidu ».
4. Rien d'autre ne change par rapport à v4.6 : empreintes de M', vérificateur réutilisé, trap qui
   garde la copie d'AG3 jusqu'au succès de `ALTER ROLE`, audit relancé une fois (#6364).

## Témoin qui exerce la vraie chaîne (`temoin-quoting-v47.sh`) : 9/9

**Banc :**
- conteneur `postgres:17` réel, schéma minimal du mesh (`schema-minimal-v47.sql` : les 7 relations
  et colonnes lues par le rôle et le contrat de M', plus une relation hors contrat) ;
- `ssh` simulé comme sshd : options ignorées, arguments joints, puis `bash -c` (via sudo vers root@
  depuis ameesh-staging) ;
- vrai `runuser`, vrai `git show` de M', vérificateur produit par `secret-vm-probe-v41.py` ;
- résidu reproduit tel quel.

**Exécution des scripts tels quels :**
- **`pose-DB-vm_probe-v46.sh --apply`** : défaut reproduit (rc 2, « invalid command-line argument … -c »),
  base intacte, vérificateur d'AG3 conservé.
- **`pose-DB-vm_probe-v47.sh --apply`** :
  - rc 0, et résidu exact retiré ;
  - **« vm_probe contract held » à l'installation ET à l'audit après mot de passe**, sur un vrai PostgreSQL 17 ;
  - `vm_probe` en SCRAM avec le vérificateur réutilisé ;
  - vérificateur d'AG3 détruit après `ALTER ROLE` ;
  - aucune copie restante sur « mesh-1 ».

## Suite (mêmes feux qu'en v4.6)

```sh
pose-DB-vm_probe-v47.sh --apply 20261108T141112Z-2455349
```

Puis `tests-AG3-vm-operator-v46.sh B`, inchangé.

Diff explicite : `delta-v46-v47.diff`. Pièces : `SHA256SUMS-v47`.
