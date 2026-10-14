"""Independent Nexlink scenarios; isolated PgTestCase schema, deployed package."""
import sys,os,pathlib,json,unittest,hashlib,inspect
source=pathlib.Path('/home/smichea/.local/share/ameesh/src-12408e2')
sys.path.insert(0,str(source)) # tests only; installed ameesh remains site-packages
from tests.test_420_qa_role import QaEvidenceTest
from tests.support import PgTestCase
from ameesh import work,db as db_mod,modules
from ameesh.modules import it
from ameesh.modules.storage import ModuleStore
live=json.loads((pathlib.Path(__file__).parent/'live-readonly.json').read_text())
assert live['read_only'] and live['roles'][0]['name']=='codex3' and 'qa' in live['roles'][0]['roles']
assert len(live['modules'])==1 and live['modules'][0]['project']=='nexlink'
qa_states=[r['states'] for r in live['modules'][0]['rules'] if r.get('evidence_kind')=='qa'];assert qa_states==[['promoted']]
package=pathlib.Path(inspect.getfile(work)).parent
for name in ('work.py','modules/it/__init__.py','modules/storage.py','modules/__init__.py','migrations/0427_qa_role_guard.sql'):
 assert (package/name).read_bytes()==(source/'src/ameesh'/name).read_bytes(),name
class NexlinkReview(QaEvidenceTest):
 def setUp(self):
  PgTestCase.setUp(self)
  assert self.schema.startswith('t_') and self.schema!='public'
  with self.db.transaction() as tx:ModuleStore(tx).replace('',live['modules'])
  self.lot=work.add(self.db,title='isolated Nexlink QA fixture',type='release',app='nexlink')['id']
  self.artifact=pathlib.Path(self.tmp)/'fixture-artifact.py';self.artifact.write_text("print('isolated Nexlink proof')\n")
  for row in live['roles']:self.register(row['name'],roles=row['roles'],projects=row['projects'],team=row['team'])
  self.register('dev-1',roles=['implementation'],projects=['nexlink'])
  self.register('proof-1',roles=['implementation'],projects=['nexlink'])
  self.db.execute("UPDATE work_items SET assignee='dev-1' WHERE id=%s",(self.lot,))
  self.assertEqual(ModuleStore(self.db).latest(self.lot,'it','qa'),None)
 def qa(self,actor='codex3',verdict='ok',digest=None):
  d=self.current_delivery()
  return it.record_qa(self.db,self.lot,sha256=digest or d['subject'],verdict=verdict,scope=['isolated-nexlink-fixture'],report='fixture://1236-codex3',actor=actor)
 def prepare(self):self.deliver();self.prove(actor='proof-1')
 def test_independent_opens_current_and_merged_without_qa(self):
  self.prepare();self.go_and_merge()
  self.assertEqual(work.get(self.db,self.lot)['state'],'merged')
  with self.assertRaises(db_mod.DbError):work.move(self.db,self.lot,'promoted')
  q=self.qa();self.assertEqual(q['actor'],'codex3');self.assertEqual(q['binding_id'],self.current_delivery()['id'])
  self.assertEqual(work.move(self.db,self.lot,'promoted')['state'],'promoted')
 def test_same_assignee_refused(self):
  self.prepare();self.db.execute("UPDATE work_items SET assignee='codex3' WHERE id=%s",(self.lot,))
  with self.assertRaisesRegex(db_mod.DbError,'qa-same-actor'):self.qa()
 def test_same_delivery_refused(self):
  self.deliver(actor='codex3');self.prove(actor='proof-1')
  with self.assertRaisesRegex(db_mod.DbError,'qa-same-actor'):self.qa()
 def test_same_proof_refused(self):
  self.deliver();self.prove(actor='codex3')
  with self.assertRaisesRegex(db_mod.DbError,'qa-same-actor'):self.qa()
 def test_role_and_project_scope_refused(self):
  self.prepare()
  with self.assertRaisesRegex(db_mod.DbError,'qa-role-required'):self.qa(actor='dev-1')
  with self.assertRaisesRegex(db_mod.DbError,'qa-role-scope'):self.qa(actor='mty-amh-qa-01')
 def test_new_delivery_same_digest_invalidates_prior_qa(self):
  self.prepare();self.qa();self.go_and_merge();work.move(self.db,self.lot,'promoted');old=self.current_delivery()
  self.deliver();self.prove(actor='proof-1');new=self.current_delivery()
  self.assertEqual(old['subject'],new['subject']);self.assertNotEqual(old['id'],new['id'])
  self.assertTrue(any(e['rule']=='stale_qa' and e['lot']==self.lot for e in modules.audit(self.db)))
  work.move(self.db,self.lot,'merged',correct='isolated fixture delivery replacement')
  with self.assertRaises(db_mod.DbError):work.move(self.db,self.lot,'promoted')
  self.assertNotEqual(ModuleStore(self.db).latest(self.lot,'it','qa')['binding_id'],new['id'])
  self.qa();self.assertEqual(work.move(self.db,self.lot,'promoted')['state'],'promoted')
names=['test_independent_opens_current_and_merged_without_qa','test_same_assignee_refused','test_same_delivery_refused','test_same_proof_refused','test_role_and_project_scope_refused','test_new_delivery_same_digest_invalidates_prior_qa']
names=sys.argv[1:] or names
print(json.dumps({'deployed_package':str(package),'schema_policy':'t_* fixture only','driver':os.environ.get('AMEESH_DRIVER'),'cases':names}),flush=True)
suite=unittest.TestSuite(NexlinkReview(n) for n in names)
r=unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(0 if r.wasSuccessful() and r.testsRun==len(names) and not r.skipped else 1)
