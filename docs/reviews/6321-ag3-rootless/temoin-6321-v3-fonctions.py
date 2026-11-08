"""#6321 v3 — (nft list ruleset simulé en plus) témoins LOCAUX (aucune VM) des fonctions ajoutées : packages_sha256, containerd_proof
(commande simulée avec la sortie apt-cache relevée sur AG3 le 08/11), ports_and_guest_gate (ss et
firewall.nft simulés d'après deploy/guest/install.sh l.594). Fonctions chargées depuis les octets."""
import ast, hashlib, pathlib, re, sys, tempfile, types
src = pathlib.Path(sys.argv[1]).read_text()
tree = ast.parse(src)
keep = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ('packages_sha256', 'containerd_proof', 'ports_and_guest_gate')]
ns = {'re': re, 'hashlib': hashlib, 'Path': pathlib.Path, 'PG_PORTS': {'55432', '55441', '55442', '55443'}}
echec = 0
def cas(nom, ok):
    global echec
    print(('OK   ' if ok else 'ECHEC ') + nom); echec |= not ok
def sortie(texte): return types.SimpleNamespace(stdout=texte.encode())
POLICY = "containerd:\n  Installed: 2.2.1-0ubuntu1~24.04.4\n  Candidate: 2.2.1-0ubuntu1~24.04.4\n  Version table:\n *** 2.2.1-0ubuntu1~24.04.4 500\n        500 http://fr-par-1.clouds.archive.ubuntu.com/ubuntu noble-updates/main amd64 Packages\n"
with tempfile.TemporaryDirectory() as d:
    d = pathlib.Path(d); deb = d / 'containerd.deb'; deb.write_bytes(b'faux deb'); h = hashlib.sha256(b'faux deb').hexdigest()
    idx = d / 'x_dists_noble-updates_main_binary-amd64_Packages'
    idx.write_text('Package: runc\nVersion: 1\nSHA256: 00\n\nPackage: containerd\nVersion: 2.2.1-0ubuntu1~24.04.4\nSHA256: %s\n' % h)
    exec(compile(ast.Module(body=keep, type_ignores=[]), 'f', 'exec'), ns)
    cas('packages_sha256 trouve la bonne strophe', ns['packages_sha256']([idx.read_text()], 'containerd', '2.2.1-0ubuntu1~24.04.4') == h)
    cas('packages_sha256 : autre version → None', ns['packages_sha256']([idx.read_text()], 'containerd', '2.2.1-0ubuntu1~24.04.3') is None)
    real_path = pathlib.Path
    class P(type(pathlib.Path())):
        pass
    def Pth(x):
        x = str(x)
        if x.startswith('/var/cache/apt/archives/'): return real_path(deb if MODE['deb'] else d / 'absent.deb')
        if x == '/var/lib/apt/lists': return real_path(d)
        return real_path(x)
    MODE = {'deb': True, 'policy': POLICY}
    def command(args):
        if args[0] == 'dpkg-query': return sortie('2.2.1-0ubuntu1~24.04.4')
        if args[:2] == ['apt-cache', 'policy']: return sortie(MODE['policy'])
        raise AssertionError(args)
    ns.update(command=command, Path=Pth)
    p = ns['containerd_proof']()
    cas('containerd_proof : origine noble-updates et sha256 du .deb = index', p['deb_sha256'] == h and p['origin'] == 'noble-updates/main')
    MODE['deb'] = False
    cas('containerd_proof : .deb absent du cache → dit, non comparé', 'non comparé' in ns['containerd_proof']()['deb_sha256'])
    MODE['deb'] = True; deb.write_bytes(b'autre')
    try: ns['containerd_proof'](); cas('.deb altéré → refus', False)
    except AssertionError: cas('.deb altéré → refus', True)
    MODE['policy'] = POLICY.replace('noble-updates', 'ppa-inconnu')
    try: ns['containerd_proof'](); cas('origine autre que noble-updates → refus', False)
    except AssertionError: cas('origine autre que noble-updates → refus', True)
    fw = d / 'firewall.nft'
    fw.write_text('    meta skuid 1002 ip daddr 127.0.0.0/8 tcp dport { 2375,2376,3000,5432,55432,55441,55442,55443,34949 } reject with tcp reset\n')
    SS = {'v': 'LISTEN 0 4096 127.0.0.1:55432 0.0.0.0:*\nLISTEN 0 4096 127.0.0.1:55441 0.0.0.0:*\nLISTEN 0 4096 127.0.0.1:55442 0.0.0.0:*\nLISTEN 0 4096 127.0.0.1:55443 0.0.0.0:*\nLISTEN 0 128 0.0.0.0:22 0.0.0.0:*\n'}
    NFT = {'v': 'table inet ameesh-guest {\n chain out {\n  meta skuid 1002 ip daddr 127.0.0.0/8 tcp dport { 2375, 5432, 55432, 55441, 55442, 55443, 34949 } reject with tcp reset\n }\n}\n'}
    ns.update(command=lambda args: sortie(NFT['v'] if args[0] == 'nft' else SS['v']), GUEST_FIREWALL=fw)
    cas('ports 127.0.0.1 seulement + garde refusant les 4 ports → OK', ns['ports_and_guest_gate']()['guest_gate_denies'] == ['55432', '55441', '55442', '55443'])
    SS['v'] += 'LISTEN 0 4096 0.0.0.0:55441 0.0.0.0:*\n'
    try: ns['ports_and_guest_gate'](); cas('55441 aussi sur 0.0.0.0 → refus', False)
    except AssertionError: cas('55441 aussi sur 0.0.0.0 → refus', True)
    SS['v'] = SS['v'].replace('LISTEN 0 4096 0.0.0.0:55441 0.0.0.0:*\n', '').replace('127.0.0.1:55443', '[::]:55443')
    try: ns['ports_and_guest_gate'](); cas('55443 sur :: → refus', False)
    except AssertionError: cas('55443 sur :: → refus', True)
    SS['v'] = SS['v'].replace('[::]:55443', '127.0.0.1:55443')
    NFT['v'] = NFT['v'].replace('55442, ', '')
    try: ns['ports_and_guest_gate'](); cas('jeu chargé sans 55442 (texte conforme) → refus', False)
    except AssertionError as e: cas('jeu chargé sans 55442 (texte conforme) → refus', 'chargé' in str(e))
    NFT['v'] = NFT['v'].replace('55441, ', '55441, 55442, '); fw.write_text(fw.read_text().replace('55442,', ''))
    try: ns['ports_and_guest_gate'](); cas('garde sans 55442 → refus', False)
    except AssertionError: cas('garde sans 55442 → refus', True)
sys.exit(echec)
