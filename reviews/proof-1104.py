import ast, json, os, subprocess, sys, types
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path.cwd()/"src"))
from ameesh import review_lot, lot_class, plan_git, lot_measures, work
SHA="560edc726899f4119f58ebf3cb454e76614ca468"
def source(path):return subprocess.check_output(["git","show",f"{SHA}:{path}"],text=True)
def load_functions(path, names, ns):
 tree=ast.parse(source(path))
 selected=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in names]
 exec(compile(ast.Module(body=selected,type_ignores=[]),path,"exec"),ns)
load_functions("src/ameesh/review_lot.py",{"class_at_merge","open_for_merge","_repo_of_agent"},review_lot.__dict__)
root=Path(os.environ["TMPDIR"])/"proof-1104-git"
root.mkdir(exist_ok=True)
env=dict(os.environ,GIT_AUTHOR_NAME="review",GIT_AUTHOR_EMAIL="review@example.invalid",GIT_COMMITTER_NAME="review",GIT_COMMITTER_EMAIL="review@example.invalid")
def git(*args):return subprocess.check_output(["git","-C",str(root),*args],env=env,text=True).strip()
git("init","-q","-b","main")
(root/"socle.md").write_text("base\n");git("add",".");git("commit","-q","-m","base")
git("checkout","-q","-b","lot")
(root/"secret.sql").write_text("select 1;\n");git("add",".");git("commit","-q","-m","SQL")
frozen=git("rev-parse","HEAD");git("checkout","-q","main");git("merge","-q","--no-ff","lot","-m","merged SQL")
item={"id":1,"state":"merged","assignee":"a","app":"ameesh","branch_target":"main"}
freeze={"id":1,"kind":"frozen","sha":frozen,"at_ts":1}
with mock.patch.object(work,"milestones",return_value=[freeze]), mock.patch.object(lot_measures,"diff_range",side_effect=OSError("temporary git read failure")):
 result=review_lot.class_at_merge(types.SimpleNamespace(cfg=None),1,item=item,repo=str(root),target="main",sha=frozen)
print("classification_after_range_failure",json.dumps({k:result.get(k) for k in ("class","doubt","why")}))
assert result["class"]=="light" and not result["doubt"], "witness changed"
state={"id":1,"state":"qa"}; calls=[]
def close_storage(*args,**kwargs):state["state"]="merged";return dict(state)
ops=types.SimpleNamespace(close_merged=close_storage)
def lost_once(*args,**kwargs):
 calls.append(1)
 return {"lot":1,"created":False,"why":"transient write failure"}
ns={"__name__":"ameesh.work","__package__":"ameesh","Db":object,"DbError":RuntimeError,"WorkError":RuntimeError,"MERGED_STATES":{"merged","promoted"},"get":lambda *_:dict(state),"milestones":lambda *_:[],"storage":types.SimpleNamespace(of=lambda _:types.SimpleNamespace(work=ops))}
load_functions("src/ameesh/work.py",{"_pr_number","_review_after_merge","close_merged"},ns)
with mock.patch.object(review_lot,"open_for_merge",side_effect=lost_once):
 first=ns["close_merged"](None,1,sha=frozen)
 second=ns["close_merged"](None,1,sha=frozen)
print("retry_after_lost_review",json.dumps({"first":first["result"],"second":second["result"],"review_attempts":len(calls)}))
assert first["result"]=="merged" and second["result"]=="already" and len(calls)==1
