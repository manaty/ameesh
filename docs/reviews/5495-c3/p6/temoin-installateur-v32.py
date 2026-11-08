"""#5495 P6 — témoin local de clear_stale (installateur v3.2), sans root : propriétaire attendu = uid
courant. Charge la fonction depuis les octets de update-l2-runtime-v32.py (aucun autre code exécuté)."""
import ast, os, pathlib, stat, sys, tempfile
src = pathlib.Path(sys.argv[1]).read_text()
fn = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == 'clear_stale')
ns = {'Path': pathlib.Path, 'stat': stat, 'os': os}
exec(compile(ast.Module(body=[fn], type_ignores=[]), 'clear_stale', 'exec'), ns)
clear, me, echec = ns['clear_stale'], os.getuid(), 0
def cas(nom, ok):
    global echec
    print(('OK   ' if ok else 'ECHEC ') + nom); echec |= not ok
with tempfile.TemporaryDirectory() as d:
    t = pathlib.Path(d, 'staging-executor.json.p6.tmp')
    cas('absent → rien à faire', clear(t, me) is False)
    t.write_text('{}'); cas('résidu régulier du bon propriétaire → retiré', clear(t, me) is True and not t.exists())
    t.write_text('{}')
    try: clear(t, me + 1); cas('mauvais propriétaire → refus nommé', False)
    except AssertionError as e: cas('mauvais propriétaire → refus nommé, fichier gardé', 'stat ' in str(e) and t.exists())
    t.unlink(); cible = pathlib.Path(d, 'ailleurs'); cible.write_text('x'); t.symlink_to(cible)
    try: clear(t, me); cas('lien symbolique → refus', False)
    except AssertionError: cas('lien symbolique → refus, rien suivi ni retiré', t.is_symlink() and cible.exists())
    t.unlink(); t.write_text('{}'); h = pathlib.Path(d, 'lien-dur'); os.link(t, h)
    try: clear(t, me); cas('nlink 2 → refus', False)
    except AssertionError: cas('fichier à liens multiples → refus', t.exists())
sys.exit(echec)
