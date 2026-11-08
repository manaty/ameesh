#!/bin/bash
# #6002 v4.2 (procédure imprimée avec le rouge) ; v4.1, O4 (#200777) — LECTURE SEULE : compare la carte producteur d'AG3 aux unités runners actives.
# Écart = le reçu #6296 passe au rouge ; message opérateur : « carte producteur à mettre à jour, geste root ».
# Procédure (fiche AG3) : root réécrit /etc/ameesh-vm-adapter/health-producer.json (0644 root:root,
# liste exhaustive des unités runners actives), sans autre changement ; puis reçu frais attendu < 70 s.
set -u
AG3=51.15.137.245
r=$(ssh -F none -o BatchMode=yes -o ConnectTimeout=10 "root@$AG3" 'python3 -I -c "
import json, subprocess
carte = sorted(json.load(open(\"/etc/ameesh-vm-adapter/health-producer.json\"))[\"units\"])
out = subprocess.run([\"systemctl\", \"--user\", \"-M\", \"ameesh@\", \"list-units\", \"ameesh-runner-agent@*\", \"--state=active\", \"--no-legend\"],
                     capture_output=True, text=True, check=True).stdout
actives = sorted(l.split()[0] for l in out.splitlines() if l.strip())
print(json.dumps({\"carte\": carte, \"actives\": actives}))"') || { echo "ECHEC lecture de la carte producteur ou des unités (erreur = KO)"; exit 1; }
python3 -I -c '
import json, sys
d = json.loads(sys.argv[1]); c, a = set(d["carte"]), set(d["actives"])
if c == a:
    print("OK   carte producteur = %d unités runners actives" % len(a)); sys.exit(0)
print("ROUGE carte producteur à mettre à jour, geste root (fiche AG3) : en trop %s, manquantes %s" % (sorted(c - a), sorted(a - c)))
print("  procédure : root réécrit /etc/ameesh-vm-adapter/health-producer.json (root:root 0644), clé units = "
      "unités ameesh-runner-agent@* actives, rien d autre ; reçu frais < 70 s ; relancer ce vérificateur")
sys.exit(1)' "$r"
