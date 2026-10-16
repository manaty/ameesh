from types import SimpleNamespace
from unittest import mock
import json
from ameesh import accounts

NOW=100.0
items=[accounts.Profile('codex','primaire',path='/synthetic/a',priority=30),accounts.Profile('codex','secondaire',path='/synthetic/b',priority=20)]
class Store:
    def __init__(self):
        self.held={};self.identity_rows={};self.identity_reads=0;self.hold_reads=0;self.published=[];self.inject=None
    def active(self,*args):return {'account':'primaire','forced':False}
    def holds(self,*args):
        self.hold_reads+=1
        return {n:{'until_ts':NOW+600,'reason':'codex plan: in overage'} for n in self.held}
    def identities(self,*args):
        self.identity_reads+=1
        result={k:list(v) for k,v in self.identity_rows.items()}
        if self.inject:self.inject(self)
        return result
    def publish_identity(self,host,harness,name,identity):
        self.published.append((name,identity));self.identity_rows.setdefault(name,[]).append({'identity':identity,'last_ts':NOW})

def run(store,items=items,extra=None):
    with mock.patch.object(accounts.storage,'of',return_value=SimpleNamespace(accounts=store)), mock.patch.object(accounts.auth_failure,'failing',return_value={}), mock.patch.object(accounts,'evaluate',side_effect=lambda book,p,*args,**kw:accounts.Evaluation(p)):
        if extra:
            with extra: return accounts.choose(None,'synthetic-host','codex',items,None,now=NOW,critical=True)
        return accounts.choose(None,'synthetic-host','codex',items,None,now=NOW,critical=True)

out={}
s=Store()
s.inject=lambda store:store.held.update(secondaire=True) if store.identity_reads==2 else None
c=run(s)
out['publication_between_final_reads']={'chosen':c.profile.name if c.profile else None,'held_at_return':sorted(s.held),'holds_reads':s.hold_reads,'identity_reads':s.identity_reads}
assert c.profile.name in s.held

s=Store()
def hold_secondary_at_validation(store):
    if store.hold_reads==2:store.held['secondaire']=True
original_holds=s.holds
def holds(*a):
    if s.hold_reads==1:s.held['secondaire']=True
    return original_holds(*a)
s.holds=holds
real_rank=accounts.rank
ranks=0
def rank(*a,**kw):
    global ranks
    ranks+=1
    if ranks==2:s.held['primaire']=True
    return real_rank(*a,**kw)
c=run(s,extra=mock.patch.object(accounts,'rank',side_effect=rank))
out['retry_is_not_validated']={'chosen':c.profile.name if c.profile else None,'held_at_return':sorted(s.held),'holds_reads':s.hold_reads,'ranks':ranks}
assert c.profile.name in s.held

s=Store()
a=run(s)
other=[items[0],accounts.Profile('codex','secondaire',path='/synthetic/another-folder',priority=20)]
b=run(s,other)
out['critical_configs_never_publish']={'chosen_a':a.profile.name if a.profile else None,'chosen_b':b.profile.name if b.profile else None,'different_folders':accounts.identity_of(a.profile)!=accounts.identity_of(b.profile),'published':s.published,'identities':s.identity_rows}
assert a.profile.name==b.profile.name=='secondaire' and not s.published
print(json.dumps(out,indent=2))
