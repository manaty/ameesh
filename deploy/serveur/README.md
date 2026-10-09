# Profil serveur VM d'un mesh ameesh (lot L55)

Un mesh par organisation (décision 0033) : sa base, ses exécuteurs et ses
personas tournent sur **une VM de l'organisation**, pour que le travail ne
dépende plus du matériel des humains. Les appareils des humains (postes,
autres serveurs) rejoignent le mesh par un **réseau privé WireGuard**.
Postgres n'est jamais exposé sur Internet.

```
   poste d'un humain ──WireGuard (UDP 51820)──►  VM du mesh (Debian 12)
   10.77.0.N                                      10.77.0.1
   exécuteurs locaux (abonnements)                Postgres TLS (localhost, wg0)
   ameesh attach                                  ameesh-runner@<persona> (clés API)
                                                  sauvegarde quotidienne
```

## Fichiers

| Fichier | Rôle |
|---|---|
| `terraform/` | crée la VM, son IP et son groupe de sécurité (SSH restreint, WireGuard) chez Scaleway |
| `cloud-init.yaml` | premier démarrage : paquets, clone d'ameesh, lance l'installation |
| `installer-serveur.sh` | installation idempotente : WireGuard, Postgres TLS, utilisateur `ameesh`, ameesh, gabarits systemd, sauvegardes |
| `ameesh-runner@.service` | un exécuteur par persona, service système sous `ameesh` |
| `ameesh-sauvegarde.{service,timer}`, `sauvegarder-base.sh` | `pg_dump` quotidien, 14 jours en local, copie hors de la VM si configurée |
| `ajouter-appareil.sh` | déclare un appareil dans WireGuard et affiche sa configuration |

## Mise en place (actes de l'opérateur)

1. **Projet de l'organisation.** Les identifiants de l'hébergeur doivent être
   ceux de l'**organisation du mesh**, jamais ceux d'une autre (0033 §1).
   Vérifier `scw info` (organisation, projet) avant tout `apply`.
2. **Créer la VM** :

   ```bash
   cd deploy/serveur/terraform
   cat > mesh.auto.tfvars <<EOF
   project_id    = "<projet de l'organisation>"
   organisation  = "<id court>"
   instance_type = "PRO2-S"
   ssh_cidrs     = ["<IP d'administration>/32"]
   ameesh_ref    = "<étiquette ou commit>"
   EOF
   terraform init && terraform plan && terraform apply
   ```

   `*.tfvars` et l'état Terraform ne sont pas versionnés. Garder l'état hors
   du dépôt, dans un stockage d'objets de l'organisation par exemple.
3. **Attendre l'installation** : `ssh root@<ip> 'cloud-init status --wait && cat /root/ameesh-serveur.txt'`.
   Le journal est dans `/var/log/ameesh-installation.log`.
4. **Ajouter les appareils** : `ajouter-appareil.sh <nom>` sur la VM. Copier la
   configuration affichée dans `/etc/wireguard/ameesh.conf` sur l'appareil,
   puis `wg-quick up ameesh`. La base est joignable en
   `postgresql://ameesh@10.77.0.1:5432/ameesh?sslmode=require` ; le mot de
   passe est dans `/etc/ameesh/db.env` sur la VM et n'est jamais affiché.
5. **Canon** : déclarer l'hôte (`hotes/<organisation>-mesh-1.md`) dans le canon
   de l'organisation, renseigner `canons` dans
   `/home/ameesh/.config/ameesh/config.json`, puis
   `runuser -l ameesh -c "ameesh canon sync --fetch"`.
6. **Clés des harnais** : dans `/etc/ameesh/harnais.env` (`0640 root:ameesh`),
   uniquement des **clés API de l'organisation** (0033 §3). Les abonnements
   des humains restent sur leurs appareils.
7. **Lancer une persona** : `systemctl enable --now ameesh-runner@<persona>`.

## Retour arrière

- **Une persona** : `agent-runner stop <persona>`, puis `systemctl disable --now ameesh-runner@<persona>`.
  Elle peut ensuite reprendre sur un poste avec la même procédure, à condition
  que sa session y soit présente (sauvegarde des sessions : lot L53).
- **La VM** : `pg_dump` final (`systemctl start ameesh-sauvegarde`), copie
  hors de la VM, puis `terraform destroy`.

## Limites connues de ce lot

- Un seul rôle Postgres `ameesh` pour tout le mesh : les rôles par hôte et par
  agent sont le lot L56.
- Les personas partagent l'utilisateur Unix `ameesh` : l'isolation par
  utilisateur ou par pod est le volet E de l'étude v2.
- Les harnais (Node, `dsh`, `codex`, `claude`) ne sont pas installés par ce
  lot : chaque organisation installe ceux de ses personas.
- Pas encore éprouvé sur une VM réelle à la livraison du lot.
