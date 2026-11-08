# #6002 — pose AG3 v4.4 : B d'impl-11 (#200903) seulement

Bureau (BG), 08/11 ~14:00Z. **Rien d'autre ne change** par rapport à v4.3.

**AUCUN GESTE**, et aucune commande lancée sur une VM, même à blanc (consigne du coordinateur).
Les essais ont tourné uniquement dans un conteneur jetable sur BG.

## Défaut

Dans `acl-retour-v43.sh`, branche « reçu absent », la boucle `getfacl -p "$d" 2>/dev/null | grep -q …`
prenait une **erreur de lecture** pour une entrée absente. Avec `--sans-acl`, on obtenait donc rc 0
même si les quatre lectures échouaient. impl-11 l'a reproduit.

## Correction (`acl-retour-v44.sh`)

- `getfacl` absent : **STOP**, y compris avec `--sans-acl`.
- Chaque lecture `getfacl -p` est capturée à part, et son **rc est exigé**. Un échec donne STOP,
  y compris avec `--sans-acl`.
- La sortie doit en plus contenir `# file: <parent>`. Une lecture inattendue donne STOP.
- La recherche de `user:vm-operator:` ne porte que sur des lectures réussies.

Les fichiers `retour-AG3-vm-operator-v44.sh`, `pose-AG3-vm-operator-v44.sh` et `temoin-acl-v44.sh`
ne changent que pour pointer vers `acl-retour-v44.sh`.

## Témoin réel en conteneur (`temoin-acl-v44.sh`) : 0 échec

Les 18 cas de v4.3 sont repris, avec deux ajouts :
- **`getfacl` simulé en rc 1, avec `--sans-acl`** : STOP, rc 1 ;
- **`getfacl` absent** (paquet `acl` retiré dans le conteneur), avec `--sans-acl` : STOP, rc 1.

## Conséquence à connaître (point ouvert)

`acl` n'est pas installé sur AG3 aujourd'hui. Si une pose échoue **avant** l'étape 6 bis (qui
installe `acl`), le retour s'arrête, même avec `--sans-acl` : l'absence d'ACL ne peut pas être lue.

Deux voies, à trancher :
- (a) installer `acl` par un geste root séparé, puis relancer le retour ;
- (b) admettre une lecture de repli de l'attribut étendu `system.posix_acl_access` (Python,
  `os.listxattr`), avec le même principe : toute erreur donne STOP.

Ce delta ne fait ni l'un ni l'autre.

Diff explicite : `delta-v43-v44.diff`.
