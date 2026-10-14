"""Read-only source checks; no imports of the reviewed code or test suites."""
import ast
import json
import subprocess
import sys
from pathlib import Path

REPO = (Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else
        next((parent for parent in Path(__file__).resolve().parents
              if (parent / ".git").exists()),
             Path(__file__).resolve().parents[2] / "ameesh"))
OLD = "a5dce622d4522988dd245de8011e77488324200d"
MAIN = "fcff03db4e77aeee6ccaa3bb09debde3e5ad089c"
FINAL = "4d265a3b948b9a004d7caee012cbd98c5c70ef4c"


def git(*args):
    return subprocess.check_output(["git", "-C", str(REPO), *args], text=True).strip()


def source(rev, path):
    return git("show", f"{rev}:{path}")


def definitions(rev, path):
    result = {}

    def visit(nodes, prefix=""):
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                result[prefix + node.name] = ast.dump(node, include_attributes=False)
            elif isinstance(node, ast.ClassDef):
                visit(node.body, prefix + node.name + ".")

    visit(ast.parse(source(rev, path)).body)
    return result


files = git("diff", "--name-only", MAIN, FINAL).splitlines()
same = [path for path in files
        if git("rev-parse", f"{OLD}:{path}") == git("rev-parse", f"{FINAL}:{path}")]
changed = sorted(set(files) - set(same))
assert len(files) == 10 and len(same) == 7
assert changed == ["src/ameesh/runner.py", "src/ameesh/storage/postgres/operations.py",
                   "tests/parts/pilote-psql.txt"]
print("753: 7/10 blobs unchanged; changed:", changed)
actual = git("rev-parse", f"{FINAL}^{{tree}}")
predicted = git("merge-tree", "--write-tree", OLD, MAIN)
assert actual == predicted == "bb4916a9db4151c07a0481acf9a1d108add94aff"
print("753: actual tree = independent merge-tree:", actual)
for path, expected in [(changed[0], "Runner.canon_sync_once"),
                       (changed[1], "Operations.adopt")]:
    before, after = definitions(OLD, path), definitions(FINAL, path)
    differences = sorted(key for key in before.keys() | after.keys()
                         if before.get(key) != after.get(key))
    assert differences == [expected], differences
    assert after[expected] == definitions(MAIN, path)[expected]
    print("753:", path, "only changed definition:", expected, "= main")
parts = source(FINAL, changed[2]).splitlines()
assert sum(line.startswith("test_753_stop_returns_lots ") for line in parts) == 1
assert sum(line.startswith("test_1037_bascule_harnais ") for line in parts) == 1
print("753: both PostgreSQL test modules present exactly once")

tree = ast.parse(source(FINAL, "src/ameesh/storage/interface.py"))
classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
domains = {node.target.id: node.annotation.id for node in classes["Storage"].body
           if isinstance(node, ast.AnnAssign) and isinstance(node.annotation, ast.Name)
           and node.annotation.id in classes}
interface = {f"{domain}.{node.name}" for domain, name in domains.items()
             for node in classes[name].body
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
             and any(isinstance(dec, ast.Attribute) and dec.attr == "abstractmethod"
                     for dec in node.decorator_list)}
contract = json.loads(source(FINAL, "src/ameesh/mediated_executor/contract.json"))
admitted = {entry["name"] for entry in contract["operations"]}
refused = {f"{domain}.{name}" for domain, names in contract["refused"]["operations"].items()
           for name in names}
assert not (admitted & refused)
assert admitted | refused == interface, (interface - (admitted | refused),
                                         (admitted | refused) - interface)
assert len(interface) == contract["refused"]["interface_total"]
assert {"work.release_agent", "operations.state_lots_activity"} <= refused
print(f"753: contract exact: {len(admitted)} admitted + {len(refused)} refused = {len(interface)}")

union = git("merge-tree", "--write-tree", "d9181f5c", MAIN)
assert union == "b4c6fa9ba726af9d9cec153ce2e26b39642ca34b"
path = "src/ameesh/review_lot.py"
before, after = definitions("d9181f5c", path), definitions(union, path)
differences = sorted(key for key in before.keys() | after.keys()
                     if before.get(key) != after.get(key))
assert differences == ["_author"]
assert after["_author"] == definitions(MAIN, path)["_author"]
assert before["_repo_of_agent"] == after["_repo_of_agent"]
print("1104: union tree:", union, "; only _author changed = main; _repo_of_agent unchanged")
