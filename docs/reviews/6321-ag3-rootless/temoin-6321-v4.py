"""#6321 v4 — témoins LOCAUX (aucune VM) des corrections de l'avis impl-03 #200943, sur les octets v4 :
B3 protected_read_probe (Docker simulé : erreur de montage → KO, Permission denied → OK, lecture → KO,
rc 1 sans refus → KO ; dossier 0711 et fixture retirés) ; retour du groupe sur l'état réel ; restauration
qui retire les répertoires absents à l'instantané (code de restauration exécuté réellement, HOME jetable)."""
import importlib.util, json, pathlib, sys, tempfile, types, os, stat, subprocess, ast
SRC = sys.argv[1]; echec = 0
def cas(nom, ok):
    global echec
    print(('OK   ' if ok else 'ECHEC ') + nom); echec |= not ok
def charger():
    spec = importlib.util.spec_from_file_location('ctl', SRC); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m
with tempfile.TemporaryDirectory() as d:
    m = charger(); m.FIXTURE_DIR = pathlib.Path(d, 'fixture'); vu = {}
    def faux(rc, out, err):
        def dk(endpoint, *a, timeout=60, allow_error=False):
            vu['mode'] = stat.S_IMODE(m.FIXTURE_DIR.stat().st_mode); vu['fmode'] = stat.S_IMODE((m.FIXTURE_DIR / 'protected').stat().st_mode)
            return types.SimpleNamespace(returncode=rc, stdout=out.encode(), stderr=err.encode())
        m.docker = dk
    def essai(nom, attendu_ok, rc, out, err):
        faux(rc, out, err)
        try: m.protected_read_probe(); ok = True
        except AssertionError: ok = False
        cas(nom, ok == attendu_ok and not m.FIXTURE_DIR.exists())
    essai('B3 : conteneur démarré, « Permission denied » → preuve acceptée (dossier retiré)', True, 1, '', 'cat: /protected: Permission denied\n')
    cas('B3 : dossier de fixture 0711, fixture 0600 au moment du montage', vu.get('mode') == 0o711 and vu.get('fmode') == 0o600)
    essai('B3 : erreur de montage (faux positif v3) → KO', False, 125, '', 'docker: Error response from daemon: invalid mount config: bind source path does not exist\n')
    essai('B3 : contenu lu → KO', False, 0, 'fixture operateur 6321\n', '')
    essai('B3 : rc 1 sans « Permission denied » → KO', False, 1, '', 'cat: /protected: Input/output error\n')
    # Retour du groupe sur l'état réel
    m = charger(); calls = []
    m.command = lambda args, **k: (calls.append(args), types.SimpleNamespace(stdout=b'active', returncode=0))[1]
    def un(membres):
        m.grp = types.SimpleNamespace(getgrnam=lambda n: types.SimpleNamespace(gr_mem=membres), getgrgid=None)
    src = pathlib.Path(SRC).read_text()
    bloc = src[src.index("    if (b.get('docker_group_removal_intent')"):src.index("    save('configuration et groupe restaurés')")]
    for nom, b, membres, attendu in [('intention sauvée, gpasswd fait (ameesh absent) → rétabli', {'docker_group_removal_intent': True}, [], True),
                                      ('intention sauvée, gpasswd jamais fait (ameesh présent) → rien', {'docker_group_removal_intent': True}, ['ameesh'], False),
                                      ('aucune intention → rien', {}, [], False)]:
        calls.clear(); un(membres)
        try: exec('def _f(b):\n' + bloc, m.__dict__); m._f(b)
        except AssertionError: pass
        cas('groupe : ' + nom, any(c[:2] == ['gpasswd', '--add'] for c in calls) == attendu)
    # Restauration : répertoires absents à l'instantané retirés s'ils sont vides
    h = pathlib.Path(d, 'home'); (h / '.config/ameesh').mkdir(parents=True); (h / '.config/ameesh/env').write_text('A=1\n')
    restore = ast.literal_eval(src[src.index("        restore = r'''") + len("        restore = "):src.index("'''", src.index("        restore = r'''") + 20) + 3])
    snap = {'.config/ameesh/env': {'type': 'file', 'mode': 0o600, 'data': __import__('base64').b64encode(b'A=1\n').decode()},
            '.config/docker/daemon.json': {'type': 'absent'}, '.config/docker': {'type': 'dir_absent'},
            '.config/environment.d': {'type': 'dir_absent'}, '.docker/config.json': {'type': 'absent'}}
    (h / '.config/docker').mkdir(); (h / '.config/docker/daemon.json').write_text('{}')
    (h / '.config/environment.d').mkdir(); (h / '.config/environment.d/90-ameesh-docker-rootless.conf').write_text('DOCKER_HOST=unix:///run/user/1001/docker.sock\n')
    (h / '.config/environment.d/autre.conf').write_text('X=1\n')
    r = subprocess.run([sys.executable, '-I', '-c', restore], input=json.dumps(snap), capture_output=True, text=True, env={'HOME': str(h)})
    out = json.loads(r.stdout)
    cas('restauration : .config/docker créé par la pose → retiré', not (h / '.config/docker').exists())
    cas('restauration : environment.d non vide (fichier étranger) → gardé et signalé', (h / '.config/environment.d').is_dir() and out['dirs_left_non_empty'] == [str(h / '.config/environment.d')])
    cas('restauration : fichier de la pose retiré, fichier étranger intact', not (h / '.config/environment.d/90-ameesh-docker-rootless.conf').exists() and (h / '.config/environment.d/autre.conf').exists())
sys.exit(echec)
