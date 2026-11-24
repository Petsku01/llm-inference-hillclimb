"""E3.2 offline-testable admission components; no process/network entrypoint.

The callbacks here are an integration boundary, NOT a live owned launcher.
A caller must supply revalidated identity, telemetry and bounded stop callbacks.
"""
import hashlib, json, math, re, time
from pathlib import Path
MIB=2**20
CAP=11800*MIB
PLAN='ed3d4e1bfd9e45c52f5e83f5c5b37e0b7a15e13d109589217b4b197e1dfca0e4'
AUDIT='a39ba109e504464da1a1184e82794530'
ROOT=Path('..')
class Blocked(Exception):pass
class Invalid(Exception):pass

def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(4194304),b''):h.update(b)
    return h.hexdigest()

def create(path,value):
    with Path(path).open('x') as f:json.dump(value,f,indent=2);f.write('\n')

def memory_action(total_bytes,launched):
    if type(total_bytes) is not int or total_bytes<0:raise Blocked('missing byte/precision evidence')
    return ('INVALID#3' if launched else 'BLOCKED') if total_bytes>CAP else 'ALLOW'

class MemoryGuard:
    def __init__(self,identity,stop):
        self.identity=identity;self.stop=stop;self.owned=None;self.warn=False;self.invalid=[];self.secondary=[];self.peak=0
    def bind(self,identity):
        if set(identity)!={'pid','uid','starttime','argv'}:raise Blocked('complete owned identity required')
        self.owned=dict(identity)
    def observe(self,total_bytes):
        decision=memory_action(total_bytes,self.owned is not None)
        self.peak=max(self.peak,total_bytes);self.warn|=total_bytes>=11500*MIB
        if decision=='BLOCKED':raise Blocked('prelaunch exceeds 11800MiB')
        if decision=='INVALID#3':
            self.invalid.append('3: full device total >11800MiB')
            # Revalidate before EACH signal; PID reuse never inherits ownership.
            for sig in ('TERM','WAIT5','KILL'):
                try:
                    if self.identity()!=self.owned:raise Blocked('owned identity changed; no signal')
                    if self.stop(sig) is True:break
                except Exception as e:self.secondary.append(repr(e));break
            raise Invalid('; '.join(self.invalid))
    def healthy(self,secondary=()):
        self.secondary.extend(map(repr,secondary))
        if self.invalid:raise Invalid('; '.join(self.invalid))
        if self.secondary:raise Blocked('secondary failures '+str(self.secondary))

CONFIGS=(('E0-off',8192,'f16','off'),('E0-on-ref8k',8192,'f16','on'),('kv8k-q8',8192,'q8_0','on'),('kv8k-q4',8192,'q4_0','on'),('ref32k',32768,'f16','on'),('kv32k-q8',32768,'q8_0','on'),('ref131k',131072,'f16','on'),('kv131k-q8',131072,'q8_0','on'),('kv131k-q4',131072,'q4_0','on'))

def config(name):
    row=next((r for r in CONFIGS if r[0]==name),None)
    if row is None:raise Blocked('unlisted configuration')
    n,c,t,fa=row
    return dict(configuration=n,context_tokens=c,cache_k=t,cache_v=t,fa=fa,batch=2048,ubatch=512,slots=1,rollback=0)

def estimate(cfg,other_bytes,derived):
    if type(other_bytes) is not int or other_bytes<0:raise Blocked('fresh other-use required')
    if set(derived)!={'kv','rs'} or any(type(v) is not int or v<0 for v in derived.values()):raise Blocked('missing DERIVED inputs')
    terms={'weight_file':{'bytes':5680522464,'status':'MEASURED','scope':'file size, NOT GPU residency'},'weight_alignment_allowance':{'bytes':64*MIB,'status':'POLICY'},'kv':{'bytes':derived['kv'],'status':'DERIVED'},'rs':{'bytes':derived['rs'],'status':'DERIVED'},'compute_graph_pool_runtime_allowance':{'bytes':2560*MIB,'status':'POLICY','physical_components':'UNKNOWN'},'other_device':{'bytes':other_bytes,'status':'MEASURED','scope':'caller must retain timestamp and precision'}}
    total=sum(t['bytes'] for t in terms.values())
    return dict(config=cfg,plan_sha256=PLAN,created_utc_ns=time.time_ns(),terms=terms,total_bytes=total,total_mib=total/MIB,verdict='BLOCKED_B4' if total>CAP else 'CONDITIONAL_GUARDED_FIT',physical_capacity_proven=False)

def argv(cfg,verbose_flag):
    if cfg!=config(cfg['configuration']):raise Blocked('unfrozen config')
    if cfg['cache_v']!='f16' and cfg['fa']!='on':raise Blocked('quantized V requires FA')
    if verbose_flag not in ('-v','--verbose'):raise Blocked('offline source verified verbose spelling required')
    return ['../../llama-b12359/llama-b12359/llama-server','-m','../../models/Qwen3.5-9B-Q4_K_M.gguf','--host','127.0.0.1','--port','8081','-c',str(cfg['context_tokens']),'-ctk',cfg['cache_k'],'-ctv',cfg['cache_v'],'-fa',cfg['fa'],'-ot','token_embd.weight=CUDA0','-ngl','99','-np','1','-b','2048','-ub','512',verbose_flag]

# Parse allocation observations only. They are not proof of disjoint residency.
PATTERN=re.compile(r'(?P<backend>CUDA_Host|CUDA\d+|CPU)\s+(?P<kind>model|KV|RS|compute) buffer size\s*=\s*(?P<size>[0-9.]+)\s*(?P<unit>MiB|bytes)')
def allocations(text):
    rows=[]
    for line_no,line in enumerate(text.splitlines(),1):
        m=PATTERN.search(line)
        if not m:continue
        d=m.groupdict();number=float(d.pop('size'));unit=d.pop('unit')
        size=math.ceil(number*MIB) if unit=='MiB' else int(number)
        if unit=='bytes' and number!=size:raise Blocked('noninteger byte allocation')
        rows.append({**d,'bytes_upper':size,'precision':unit,'line':line_no,'sizing_zero':size==0,'raw':line})
    if not rows:raise Blocked('missing allocation logs')
    return rows

def measured_receipt(home,pins,log,accounting):
    """Fail closed unless caller proves synchronous disjoint CUDA accounting.

    Rounded logs alone cannot establish disjointness. Allocation interval proof,
    per-PID/device telemetry precision and raw operand references are mandatory.
    """
    required={'device_upper_bytes','pid_lower_bytes','pid_upper_bytes','other_upper_bytes','known_lower_bytes','known_upper_bytes','disjoint_proof','raw_operands','precision_bytes','synchronous','graph_prepared'}
    if set(accounting)!=required:raise Blocked('missing measured accounting fields')
    a=accounting
    if not a['synchronous'] or not a['graph_prepared'] or not a['disjoint_proof'] or not a['raw_operands']:raise Blocked('unresolved accounting/runtime aggregate')
    for k in required-{'disjoint_proof','raw_operands','synchronous','graph_prepared'}:
        if type(a[k]) is not int or a[k]<0:raise Blocked('missing/negative precision/accounting')
    if not a['precision_bytes']:raise Blocked('missing precision')
    rows=allocations(Path(log).read_text());cuda=[r for r in rows if r['backend']=='CUDA0' and not r['sizing_zero']]
    if {r['kind'] for r in cuda}!={'model','KV','RS','compute'} or len(cuda)!=4:raise Blocked('missing/overlapping CUDA buffers')
    if a['known_lower_bytes']>a['known_upper_bytes'] or a['pid_lower_bytes']>a['pid_upper_bytes']:raise Blocked('inconsistent intervals')
    if a['known_lower_bytes']>a['pid_lower_bytes'] or a['known_upper_bytes']>a['pid_upper_bytes']:raise Blocked('negative residual')
    if a['pid_upper_bytes']+a['other_upper_bytes']>a['device_upper_bytes']+a['precision_bytes']:raise Blocked('inconsistent driver operands')
    if memory_action(a['device_upper_bytes'],True)!='ALLOW':raise Invalid('3: startup >11800MiB')
    result={'plan_sha256':PLAN,'pins':pins,'created_utc_ns':time.time_ns(),'buffers':rows,'accounting':a,'aggregate_runtime_residual':{'status':'MEASURED','lower_bytes':a['pid_lower_bytes']-a['known_upper_bytes'],'upper_bytes':a['pid_upper_bytes']-a['known_lower_bytes'],'individual_components':'UNKNOWN'},'log':{'path':str(log),'sha256':digest(log),'lines':[1,len(Path(log).read_text().splitlines())]},'future_highwater_bound':False}
    if result['aggregate_runtime_residual']['lower_bytes']<0:raise Blocked('negative residual interval')
    create(Path(home)/'estimates-measured.json',result);return result

def continuation(receipt,pins,other_bytes):
    if receipt['pins']!=pins:raise Blocked('measurement/config/source pins mismatch')
    if receipt['aggregate_runtime_residual']['status']!='MEASURED':raise Blocked('no POLICY fallback')
    a=receipt['accounting'];total=a['known_upper_bytes']+receipt['aggregate_runtime_residual']['upper_bytes']+other_bytes
    if memory_action(total,False)!='ALLOW':raise Blocked('measured continuation over cap')
    return {'status':'MEASURED_OBSERVED_NOT_BOUND','total_bytes':total,'policy_terms_removed':True}

def needle_select(rows,context,cycle='original'):
    chosen=[r for r in rows if r['context_tokens']==context and r['cycle']==cycle]
    if len(chosen)!=6 or sorted((r['occupancy_percent'],r['document']) for r in chosen)!=[(p,d) for p in (62,100) for d in range(3)]:raise Blocked('QT5 requires all 3/3 at both lengths')
    if any(len(r['expected'])!=4 for r in chosen):raise Blocked('four independent keys required')
    return chosen

def grade_needles(expected,actual):
    try:got=json.loads(actual)
    except (TypeError,ValueError):return False
    return got==expected

def greedy_equal(reference,candidate):
    if reference is None:return {'claim':False,'reason':'same-context f16 FA-on reference unavailable'}
    ok=reference==candidate
    return {'claim':ok,'reason':'exact declared token-ID set including EOS' if ok else 'token-ID mismatch; equivalence forbidden'}

class Reservations:
    """Offline-tested create-only overlay; never rewrites historical counters.

    Caller must hold the nonblocking GPU campaign lock across mutation/readback.
    This class does not acquire a lock or authorize live execution by itself.
    """
    def __init__(self,home):self.home=Path(home)
    def rows(self):return [json.loads(p.read_text()) for p in sorted(self.home.glob('reservation-*.json'))]
    def reserve(self,name,request_cap,wall_s,run_id):
        rows=self.rows()
        if any(not (self.home/(r['run_id']+'-terminal.json')).exists() for r in rows):raise Blocked('unknown terminal; no resume')
        if any(json.loads((self.home/(r['run_id']+'-terminal.json')).read_text())['status']!='GOOD' for r in rows):raise Blocked('prior failure; no attempt-3')
        if (not rows and name!='E0-off') or (rows and name=='E0-off'):raise Blocked('exactly one attempt-2 first')
        if name=='ref131k':raise Blocked('optional slot B4 excluded')
        if not run_id.startswith('E3-') or run_id in (AUDIT,'E3-5dae520fc6ad4e33a02ea75663485ec4') or any(r['run_id']==run_id for r in rows):raise Blocked('fresh execution ID required')
        cfg=config(name)
        if any(r['configuration']==name for r in rows):raise Blocked('one reservation per configuration')
        max_req,max_wall=(24,600) if name.startswith('E0-') else ((24,1800) if name.startswith('ref') else (180,4200))
        if not 0<request_cap<=max_req or not 0<wall_s<=max_wall:raise Blocked('per-config reservation cap')
        if len(rows)+2>14 or sum(r['request_cap'] for r in rows)+request_cap>1180 or 600+sum(r['wall_s'] for r in rows)+wall_s>34800:raise Blocked('cumulative overlay budget')
        row={'run_id':run_id,'configuration':name,'config':cfg,'request_cap':request_cap,'wall_s':wall_s,'plan_sha256':PLAN,'index':len(rows)}
        create(self.home/f'reservation-{len(rows):02}.json',row);return row
    def close(self,row,status):
        if status not in ('GOOD','BLOCKED','INVALID'):raise Blocked('terminal status')
        create(self.home/(row['run_id']+'-terminal.json'),{'status':status})

if __name__=='__main__':raise SystemExit('OFFLINE ONLY: live integration is not an execution entitlement')
