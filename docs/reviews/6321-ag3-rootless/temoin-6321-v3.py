"""#6321 v3 — témoins LOCAUX (aucune VM, Docker simulé) des B d'impl-11 #200929 sur les octets du
contrôleur v3 : (1) échec pendant la copie → source arrêtée consignée AVANT, retour la relance ;
(2) échec après création de la cible, avant santé → ID consigné, retour la reconnaît (étiquette) et
l'arrête ; (3) conteneur rootless sans étiquette → retour refuse (charge inconnue) ; (4) phase C
--apply hors fenêtre → refus avant toute mutation ; (5) retour hors fenêtre sans secours → refus,
avec secours + courrier → accepté."""
import importlib.util, json, pathlib, sys, tempfile, types, itertools
SRC = sys.argv[1]
echec = 0
def cas(nom, ok):
    global echec
    print(('OK   ' if ok else 'ECHEC ') + nom); echec |= not ok

def charger(root):
    spec = importlib.util.spec_from_file_location('ctl', SRC); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    m.ROOT = pathlib.Path(root); m.ROOT.mkdir(mode=0o700, exist_ok=True); m.HOME = str(m.ROOT)
    st = {'rf': {'ameesh-pg': {'Id': 'src1', 'Running': True}}, 'rl': {}, 'n': itertools.count(1), 'fail': None}
    def ins(endpoint, name):
        if endpoint == m.RF:
            c = st['rf'][name]
            return {'Id': c['Id'], 'State': {'Running': c['Running'], 'Health': {'Status': 'healthy' if c['Running'] else 'unhealthy'}},
                    'HostConfig': {'RestartPolicy': {'Name': 'no'}}, 'Config': {'Env': ['POSTGRES_USER=agent_mesh']}, 'Image': 'sha256:img'}
        c = next(v for k, v in st['rl'].items() if k == name or v['name'] == name)
        return {'Id': c['id'], 'State': {'Running': c['Running'], 'Health': {'Status': 'starting'}}, 'Image': 'sha256:img'}
    def dk(endpoint, *a, timeout=60, allow_error=False):
        out = ''
        if a[:2] == ('image', 'inspect'): out = json.dumps([{'Size': 1}])
        elif a[0] == 'info': out = json.dumps({'SecurityOptions': ['name=rootless'], 'Driver': 'overlay2', 'DockerRootDir': m.HOME + '/.local/share/docker'})
        elif a[0] == 'stop':
            (st['rf'][a[-1]] if endpoint == m.RF else next(v for k, v in st['rl'].items() if k == a[-1] or v['name'] == a[-1]))['Running'] = False
        elif a[0] == 'start': st['rf'][a[-1]]['Running'] = True
        elif a[0] == 'run' and endpoint == m.RL:
            name = next(x.split('=', 1)[1] for x in a if x.startswith('--name='))
            label = next((x.split('=', 2)[2] for x in a if x.startswith('--label=ameesh.operator=')), '')
            cid = 'tgt%d' % next(st['n']); st['rl'][cid] = {'id': cid, 'name': name, 'label': label, 'Running': True}; out = cid
        elif a[0] == 'ps' and endpoint == m.RL:
            out = '\n'.join('%s|%s|%s' % (v['id'], v['name'], v['label']) for v in st['rl'].values())
        return types.SimpleNamespace(stdout=out.encode(), returncode=0)
    def stream(prod, cons, label, timeout=600):
        if st['fail'] == 'copie' and label.startswith('copie-'): raise RuntimeError('flux coupé (simulé)')
    m.common_checks = lambda apply=False: None; m.prerequisites = lambda: {}
    m.pg_sources = lambda: [{'name': 'ameesh-pg', 'id': 'src1', 'image': 'sha256:img', 'volume': 'ameesh-pg-data', 'source_path': '/x',
                             'port': '55432', 'role': 'agent_mesh', 'cmd': [], 'healthcheck': None, 'env': ['POSTGRES_USER=agent_mesh'],
                             'restart': 'no', 'running': True, 'health': 'healthy'}]
    m.authority = lambda name, keys: dict(st.get('auth', {}), restore_active_native_units=[])
    m.outside_cutoffs = lambda: None
    m.inspect = ins; m.docker = dk; m.stream = stream
    m.volume_manifest = lambda e, i, v: ('h', b'm'); m.database_baseline = lambda e, c, r: {'postgres': []}
    def user(args, data=None, timeout=60):
        if args[0] == 'systemctl' and 'UnitFileState' in args: return 'enabled'
        if args[0] == 'systemctl' and 'LoadState' in args: return 'loaded'
        if args[0] == 'systemctl' and 'ActiveState' in args: return 'active'
        if args[0] == 'systemctl': return '{}'
        if args[0] == 'docker': return 'default'
        if 'mktemp' in args[0]: return m.HOME + '/.docker-operator-6321-x'
        return '{}'
    m.user = user
    m.user_json = lambda code, payload: '{"restored": true}' if 'restored' in code else '/tmp/env'
    m.command = lambda args, data=None, timeout=60, allow_error=False: types.SimpleNamespace(stdout=b'1000 /x', returncode=0)
    m.Path = lambda *p: pathlib.Path('/inexistant-6321') if p and str(p[0]).startswith(('/run/user/1001/docker.sock', '/home/ameesh')) else pathlib.Path(*p)
    m.time = types.SimpleNamespace(monotonic=itertools.count(0, 100).__next__, sleep=lambda s: None)
    return m, st

def socket_present(m):
    m.Path = lambda *p: pathlib.Path('/') if p and str(p[0]) == '/run/user/1001/docker.sock' else (pathlib.Path('/inexistant-6321') if p and str(p[0]).startswith('/home/ameesh') else pathlib.Path(*p))

with tempfile.TemporaryDirectory() as d:
    # (1) échec pendant la copie
    m, st = charger(d + '/a'); st['fail'] = 'copie'
    try: m.phase_a(True); cas('copie : erreur attendue', False)
    except RuntimeError: pass
    rec = json.loads((m.ROOT / 'phase-A-progress.json').read_text())
    cas('copie interrompue : arrêt de la source consigné AVANT (intention stop_source src1)', {'kind': 'stop_source', 'name': 'ameesh-pg', 'id': 'src1'} in rec['intents'] and not st['rf']['ameesh-pg']['Running'])
    socket_present(m); m.retour(True)
    cas('copie interrompue : le retour relance la source arrêtée', st['rf']['ameesh-pg']['Running'])
    # (2) échec après création, avant santé
    m, st = charger(d + '/b')
    try: m.phase_a(True); cas('santé : erreur attendue', False)
    except AssertionError as e: pass
    rec = json.loads((m.ROOT / 'phase-A-progress.json').read_text())
    cas('cible créée sans santé : ID consigné avant l attente (targets)', rec.get('targets', {}).get('ameesh-pg', '').startswith('tgt'))
    socket_present(m); m.retour(True)
    tgt = next(iter(st['rl'].values()))
    cas('cible sans santé : le retour la reconnaît (étiquette 6321) et l arrête', not tgt['Running'] and st['rf']['ameesh-pg']['Running'])
    # (3) conteneur rootless sans étiquette
    m, st = charger(d + '/c'); st['rl']['x'] = {'id': 'x', 'name': 'intrus', 'label': '', 'Running': True}
    (m.ROOT / 'phase-A-progress.json').write_text(json.dumps({'phase': 'x', 'intents': []})); socket_present(m)
    try: m.retour(True); cas('charge inconnue refusée', False)
    except AssertionError as e: cas('conteneur rootless sans étiquette : retour refusé (charge inconnue)', 'inconnue' in str(e) and st['rl']['x']['Running'])
    # (4) phase C hors fenêtre (garde réelle, horloge réelle hors du 09/11 13:00-15:30Z)
    m, st = charger(d + '/d'); real = importlib.util.module_from_spec(importlib.util.spec_from_file_location('r', SRC))
    importlib.util.spec_from_file_location('r', SRC).loader.exec_module(real); m.outside_cutoffs = real.outside_cutoffs
    (m.ROOT / 'phase-B-progress.json').write_text(json.dumps({'phase': 'phase B terminée ; x'}))
    try: m.phase_c(True, 'a' * 40); cas('phase C hors fenêtre refusée', False)
    except AssertionError as e: cas('phase C --apply hors fenêtre : refus avant toute mutation', 'fenêtre' in str(e) and not (m.ROOT / 'protected-read-fixture').exists())
    # (5) retour hors fenêtre : refus sans secours, accepté avec secours + courrier
    m, st = charger(d + '/e'); m.outside_cutoffs = real.outside_cutoffs; (m.ROOT / 'phase-A-progress.json').write_text(json.dumps({'phase': 'x'}))
    try: m.retour(True); cas('retour hors fenêtre sans secours refusé', False)
    except AssertionError: cas('retour hors fenêtre sans secours : refusé', not (m.ROOT / 'retour-progress.json').exists())
    st['auth'] = {'secours_hors_fenetre': True, 'secours_mail': 200999}; m.retour(True)
    r = json.loads((m.ROOT / 'retour-progress.json').read_text())
    cas('retour hors fenêtre avec secours + courrier : accepté et consigné', r['secours_hors_fenetre'] and r['secours_mail'] == 200999)
sys.exit(echec)
