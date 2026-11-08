set -e; H=/e/home; ST=$H/.local/share/ameesh/venv
python3.12 /e/k5c1/bascule.py preparer --venv $ST --src /e/repo --version 5c1e679 --python /usr/bin/python3.12 --lock /e/k5c1/requirements-staging.lock --wheelhouse /e/k5c1/wheelhouse --manifeste /e/k5c1/manifest.json --verificateur /e/k5c1/venv-staging.py >/dev/null
mkdir $ST; python3.12 /e/k5c1/bascule.py activer --venv $ST --version 5c1e679 --precedent 5c1e679 >/dev/null; echo "1. courant=$(readlink $H/.local/share/ameesh/venv-courant)"
git -C /e/repo -c advice.detachedHead=false checkout -q --detach 97285a076dee285097c412d3bd0f2649438a3257
python3.12 /e/kM/bascule.py preparer --venv $ST --src /e/repo --version 97285a0 --python /usr/bin/python3.12 --lock /e/kM/requirements-staging.lock --wheelhouse /e/kM/wheelhouse --manifeste /e/kM/manifest.json --verificateur /e/kM/venv-staging.py >/dev/null
python3.12 /e/kM/bascule.py activer --venv $ST --version 97285a0 --precedent 5c1e679 >/dev/null; echo "2. courant=$(readlink $H/.local/share/ameesh/venv-courant)"
python3.12 /e/kM/venv-staging.py verifier --manifeste /e/kM/manifest.json --venv $H/.local/share/ameesh/venv-97285a0 >/dev/null && echo "3. vérificateur M OK"
$ST/bin/python -B -c 'import json,ameesh; from ameesh.staging_executor import _tree_sha256; print("4. arbre", _tree_sha256(ameesh.__path__[0]))'
python3.12 /e/kM/bascule.py retour --venv $ST >/dev/null; echo "5. après retour courant=$(readlink $H/.local/share/ameesh/venv-courant)"
$ST/bin/python -B -c 'import ameesh; from ameesh.staging_executor import _tree_sha256; print("6. arbre", _tree_sha256(ameesh.__path__[0]))'
