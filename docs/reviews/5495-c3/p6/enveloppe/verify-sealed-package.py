import hashlib,importlib.util,json,os,subprocess,tempfile
from pathlib import Path
import re
ROOT=Path('/work');SHA=os.environ['KIT_SHA'];assert re.fullmatch('[0-9a-f]{40}',SHA)
assert os.geteuid()==1000
python=Path('/usr/bin/python3.12');expected='e50d468e8b0adfb05733f5b87b3cff34829c4a8c1aea50c865aa8bdfe4bb150f'
assert hashlib.sha256(python.read_bytes()).hexdigest()==expected
def load(name,path):
 s=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
c=load('construction','/tools/construction-runtime.py');p=load('producer','/tools/venv-staging.py')
home=ROOT/'build/home';epoch=c.verify_clone(ROOT/'source-repo',SHA);env=c._pip_env(home,epoch);env['PYTHONDONTWRITEBYTECODE']='1'
wheel=next((ROOT/'wheelhouse').glob('ameesh-*.whl'));w={'name':wheel.name,'sha256':hashlib.sha256(wheel.read_bytes()).hexdigest()}
def run(*args):subprocess.run([str(a) for a in args],check=True,env=env)
assert (ROOT/'manifest-1.json').read_bytes()==(ROOT/'manifest.json').read_bytes()
manifest=json.loads((ROOT/'manifest.json').read_text());assert manifest['format']=='ameesh-venv-staging/3',manifest['format']
with tempfile.TemporaryDirectory(prefix='l2-sealed-') as tmp:
 v=Path(tmp)/('venv-'+SHA[:12]);c.create_venv(v,python=python,with_pip=True,env=env,prompt=p.INVITE)
 c.pip(v,'install','--no-index','--find-links',str(ROOT/'wheelhouse'),'--require-hashes','--only-binary=:all:','--no-compile','--no-cache-dir','-r',str(ROOT/'requirements-staging.lock'),env=env)
 p._ecrire_marqueur(v,SHA[:7],hashlib.sha256((ROOT/'requirements-staging.lock').read_bytes()).hexdigest());p.sceller(v)
 run(python,'-B','/tools/venv-staging.py','verifier','--manifeste',ROOT/'manifest.json','--venv',v)
 refusal=subprocess.run([str(v/'bin/pip'),'install','--no-index','ameesh'],env=env,capture_output=True,text=True)
 assert refusal.returncode!=0
 package_hash=subprocess.check_output([str(v/'bin/python'),'-B','-c','from ameesh.staging_executor import _tree_sha256; import ameesh,pathlib; print(_tree_sha256(str(pathlib.Path(ameesh.__file__).parent)))'],env=env,text=True).strip()
receipt={'source_sha':SHA,'format':manifest['format'],'producer_euid':os.geteuid(),'network':'disabled by Docker','python_sha256':expected,'reproducible_public_producer':True,'separate_sealed_install_verifier':'passed','pip_install_refused':True,'fixed_venv_prompt':p.INVITE,'no_compile':True,'PYTHONDONTWRITEBYTECODE':'1','manifest_sha256':hashlib.sha256((ROOT/'manifest.json').read_bytes()).hexdigest(),'lock_sha256':hashlib.sha256((ROOT/'requirements-staging.lock').read_bytes()).hexdigest(),'wheel':w['name'],'wheel_sha256':w['sha256'],'expected_package_tree_sha256':package_hash,'distributions':manifest['distributions'],'deployment':'not performed; independent review and native pilot go required'}
(ROOT/'build-receipt.json').write_text(json.dumps(receipt,indent=2,sort_keys=True)+'\n');print(json.dumps(receipt))
