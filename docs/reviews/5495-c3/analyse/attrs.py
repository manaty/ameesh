"""Attributs ameesh.<module>.<nom> utilisés par un outil de séquence (cible) et absents d'une source (lanceur)."""
import ast, subprocess, sys
repo, cible, lanceur, outil = sys.argv[1:5]
src = subprocess.run(["git","-C",repo,"show",f"{cible}:{outil}"],capture_output=True,text=True,check=True).stdout
tree = ast.parse(src)
alias = {}
for n in ast.walk(tree):
    if isinstance(n, ast.ImportFrom) and n.module == "ameesh":
        for a in n.names: alias[a.asname or a.name] = a.name
used = set()
for n in ast.walk(tree):
    if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id in alias:
        used.add((alias[n.value.id], n.attr))
def defined(rev, mod):
    for p in (f"src/ameesh/{mod}.py", f"src/ameesh/{mod}/__init__.py"):
        r = subprocess.run(["git","-C",repo,"show",f"{rev}:{p}"],capture_output=True,text=True)
        if r.returncode == 0:
            t = ast.parse(r.stdout); names = set()
            for n in ast.walk(t):
                if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)): names.add(n.name)
                elif isinstance(n,ast.Assign):
                    for x in n.targets:
                        if isinstance(x,ast.Name): names.add(x.id)
                elif isinstance(n,ast.AnnAssign) and isinstance(n.target,ast.Name): names.add(n.target.id)
                elif isinstance(n,(ast.Import,ast.ImportFrom)):
                    for a in n.names: names.add((a.asname or a.name).split(".")[0])
            return names
    return None
manque = []
for mod, attr in sorted(used):
    names = defined(lanceur, mod)
    if names is None: manque.append(f"{mod} (module absent)")
    elif attr not in names: manque.append(f"{mod}.{attr}")
print(outil, "modules", sorted(set(alias.values())), "attributs utilisés", len(used))
print("absents du lanceur", lanceur[:9], ":", manque or "aucun")
