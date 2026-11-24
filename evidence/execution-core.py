"""E3.2 variable-context primitives. No live entrypoint or execution entitlement.

All effects are explicit injected interfaces; the guarded launcher stays closed
until independent RAW/lifecycle acceptance and exact source pins are complete.
"""
import copy, hashlib, importlib.util, json, pathlib, sys
sys.dont_write_bytecode=True
HERE=pathlib.Path(__file__).parent

def load(name,path):
    s=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m

g=load('e32_core_guard',HERE/'guarded_runtime.py')

class Workloads:
    def __init__(self,home=HERE):
        home=pathlib.Path(home)
        self.panels=json.loads((home/'panel-tokenized.json').read_text())['tasks']
        self.functional=json.loads((home/'functional-tokenized.json').read_text())['tasks']
        self.documents=json.loads((home/'needles.local.json').read_text())['documents']
        self._panel={(t['class'],t['stage'],t['index']):t for t in self.panels}
        if len(self._panel)!=624 or len(self.functional)!=10 or len(self.documents)!=54:
            raise g.Blocked('frozen workload cardinality')
    def panel(self,klass,stage,index):
        try:return copy.deepcopy(self._panel[klass,stage,index])
        except KeyError:raise g.Blocked('unlisted panel class/stage/index')
    def needles(self,cfg,cycle):
        return copy.deepcopy(g.needle_select(self.documents,cfg['context_tokens'],cycle))
    def function(self,name):
        found=[t for t in self.functional if t['id']==name]
        if len(found)!=1:raise g.Blocked('unlisted functional task')
        return copy.deepcopy(found[0])

def request_body(cfg,task):
    if cfg!=g.config(cfg['configuration']):raise g.Blocked('unfrozen config')
    if task['input_tokens']!=len(task['input_token_ids']):raise g.Blocked('input token count drift')
    if task['input_tokens']+task['max_tokens']>cfg['context_tokens']:raise g.Blocked('tokenized context overflow')
    if 'context_tokens' in task and task['context_tokens']!=cfg['context_tokens']:raise g.Blocked('needle context mismatch')
    body={'model':'qwen3.5-9b','messages':copy.deepcopy(task['messages']),'stream':True,'stream_options':{'include_usage':True},'max_tokens':task['max_tokens'],'temperature':0,'top_p':1,'top_k':0,'min_p':0,'repeat_penalty':1,'seed':0,'chat_template_kwargs':{'enable_thinking':False},'cache_prompt':False}
    if 'tools' in task:body['tools']=copy.deepcopy(task['tools'])
    return body

def merge_tools(chunks):
    calls={}
    for chunk in chunks:
        i=chunk.get('index')
        if type(i) is not int or i<0:raise g.Blocked('invalid tool index')
        call=calls.setdefault(i,{'id':'','type':'function','function':{'name':'','arguments':''}})
        if chunk.get('id'):
            if call['id'] and call['id']!=chunk['id']:raise g.Blocked('tool ID drift')
            call['id']=chunk['id']
        if chunk.get('type','function')!='function':raise g.Blocked('unknown tool type')
        for key in ('name','arguments'):call['function'][key]+=chunk.get('function',{}).get(key,'')
    if sorted(calls)!=list(range(len(calls))):raise g.Blocked('noncontiguous tool indices')
    return [calls[i] for i in sorted(calls)]

def strict_json(text):
    def pairs(rows):
        obj={}
        for key,value in rows:
            if key in obj:raise ValueError('duplicate JSON key '+key)
            obj[key]=value
        return obj
    return json.loads(text,object_pairs_hook=pairs)

def grade_task(task,text,tools=()):
    if task.get('id')=='QT4-call':
        if len(tools)!=1 or not tools[0].get('id'):return False
        f=tools[0]['function']
        try:return {'name':f['name'],'arguments':strict_json(f['arguments'])}==task['expected']
        except (TypeError,ValueError,KeyError):return False
    if 'occupancy_percent' in task:
        try:return strict_json(text)==task['expected']
        except (TypeError,ValueError):return False
    if 'expected' in task:return text.strip()==task['expected'] and not tools
    words=text.split();grams=[tuple(words[i:i+8]) for i in range(max(0,len(words)-7))]
    return bool(text.strip()) and not tools and (len(grams)<=50 or 1-len(set(grams))/len(grams)<=.5)

def execute_request(cfg,task,home,request_id,send,backend_tokens,boundary,healthy,abort,deadline):
    """Run one already-authorized task via injected bounded transport.

    send(body) must implement timeout/cancel, return an HTTPResponse-like object.
    backend_tokens(body) must return actual served template AND token IDs. They
    are compared before send, never accepted by count alone. Callers persist a
    counted request intent before invoking this primitive. This function alone
    does not reserve or authorize a model load. All timing uses monotonic_ns.
    """
    import re,threading,time
    if not re.fullmatch(r'[A-Za-z0-9_.-]+',request_id):raise g.Blocked('unsafe request ID')
    if deadline<=time.monotonic():raise g.Blocked('request/stage deadline')
    home=pathlib.Path(home);body=request_body(cfg,task);healthy()
    served=backend_tokens(body)
    if served!={'prompt':task['rendered_prompt'],'tokens':task['input_token_ids']}:raise g.Blocked('served template/token-ID drift')
    g.create(home/(request_id+'-request.json'),body)
    raw={'request_id':request_id,'stage':task.get('stage','functional'),'class':task.get('class','functional'),'task_id':task.get('id'),'config':cfg,'success':False,'terminal_validated':False,'send_ns':None,'first_token_ns':None,'end_ns':None,'text':'','events':[],'lines':[],'secondary_errors':[]}
    receiver=None;response=None;timer=None;primary=None;chunks=[];usage=None;finish=None;done=False
    expired=threading.Event();completed=threading.Event()
    def expire():
        if not completed.wait(max(0,min(deadline,time.monotonic()+120)-time.monotonic())):
            expired.set()
            try:abort()
            except Exception as e:raw['secondary_errors'].append('abort '+repr(e))
    def retain(item,stream):
        arrived,line=item;offset=stream.tell();stream.write(line);stream.flush()
        raw['lines'].append({'arrival_ns':arrived,'offset':offset,'bytes':len(line)})
        return arrived,line
    rmod=load('e32_receiver_primitive',HERE.parents[1]/'E1g/receiver.py')
    with (home/(request_id+'-stream.log')).open('xb') as stream:
        try:
            timer=threading.Thread(target=expire,name='e32-deadline',daemon=True);timer.start()
            boundary('before-send');healthy()
            if expired.is_set():raise g.Blocked('request deadline')
            raw['send_ns']=time.monotonic_ns();response=send(body)
            if response.status!=200:raise g.Blocked('HTTP status '+str(response.status))
            receiver=rmod.Receiver(response,'a2',abort);receiver.start()
            while True:
                item=receiver.take()
                if item is None:break
                arrived,line=retain(item,stream);healthy()
                if expired.is_set():raise g.Blocked('request deadline')
                text=line.decode().strip()
                if not text or text.startswith(':'):continue
                if not text.startswith('data:'):raise g.Blocked('unexpected SSE field')
                payload=text[5:].strip()
                if payload=='[DONE]':done=True;continue
                event=json.loads(payload);raw['events'].append(event)
                if event.get('error'):raise g.Blocked('server error '+str(event['error']))
                choices=event.get('choices',[])
                if len(choices)>1:raise g.Blocked('multiple choices forbidden')
                for choice in choices:
                    if choice.get('index',0)!=0:raise g.Blocked('choice index')
                    delta=choice.get('delta',{})
                    if delta.get('reasoning') or delta.get('reasoning_content'):raise g.Blocked('thinking enabled')
                    piece=delta.get('content') or '';tool=delta.get('tool_calls',[])
                    if piece or tool:
                        if raw['first_token_ns'] is None:
                            raw['first_token_ns']=arrived;boundary('first-token');healthy()
                        raw['text']+=piece;chunks.extend(tool)
                    if choice.get('finish_reason') is not None:
                        if finish is not None:raise g.Blocked('duplicate finish')
                        finish=choice['finish_reason']
                if event.get('usage'):
                    if usage is not None:raise g.Blocked('duplicate usage')
                    usage=event['usage']
            raw['end_ns']=receiver.end_ns
            if receiver.error:raise g.Blocked('receiver '+receiver.error)
            if not done or receiver.terminal_ns is None or finish not in ('stop','length','tool_calls') or usage is None:raise g.Blocked('missing terminal/finish/usage')
            if expired.is_set():raise g.Blocked('request deadline')
            if raw['first_token_ns'] is None:raise g.Blocked('empty output')
            prompt=usage.get('prompt_tokens');n=usage.get('completion_tokens');cached=usage.get('prompt_tokens_details',{}).get('cached_tokens')
            if type(prompt) is not int or prompt!=task['input_tokens'] or type(n) is not int or not 0<n<=task['max_tokens'] or type(cached) is not int or cached!=0:raise g.Blocked('usage/cache/context mismatch or unavailable')
            if prompt+task['max_tokens']>cfg['context_tokens']:raise g.Blocked('reported overflow')
            tools=merge_tools(chunks);raw['tools']=tools
            if not grade_task(task,raw['text'],tools):raise g.Blocked('frozen output key/integrity failure')
            if tools and finish!='tool_calls':raise g.Blocked('tool finish mismatch')
            dt=(raw['end_ns']-raw['first_token_ns'])/1e9;ttft=(raw['first_token_ns']-raw['send_ns'])/1e9
            if dt<=0 or ttft<=0:raise g.Blocked('receiver clock interval')
            raw['metrics']={'prompt_tokens':prompt,'completion_tokens':n,'cached_tokens':cached,'decode_client_tps':(n-1)/dt,'ttft_client_s':ttft,'cold_prompt_tokens_per_ttft':prompt/ttft}
            raw['terminal_validated']=True;raw['success']=True
        except Exception as e:primary=e
        finally:
            if receiver is not None:
                try:
                    if not receiver.done.is_set():receiver.stop()
                    receiver.join(timeout=2)
                    if receiver.is_alive():raise g.Blocked('receiver failed to join')
                    while True:
                        item=receiver.take()
                        if item is None:break
                        retain(item,stream)
                except Exception as e:raw['secondary_errors'].append(repr(e));primary=primary or e
                raw.update(end_ns=receiver.end_ns,receiver_joined=not receiver.is_alive(),receiver_bytes=receiver.total_bytes,receiver_peak_buffered=receiver.peak_buffered,receiver_error=receiver.error)
            else:raw['receiver_joined']=True
            raw['consumer_end_ns']=time.monotonic_ns()
            try:boundary('end');healthy()
            except Exception as e:
                # Safety INVALID always dominates transport/cleanup failures.
                if isinstance(e,g.Invalid):primary=e
                else:primary=primary or e
                raw['secondary_errors'].append(repr(e))
            if expired.is_set() or time.monotonic()>=deadline:primary=primary or g.Blocked('request/stage deadline including boundary work')
            completed.set()
            if response is not None and raw['receiver_joined']:
                try:response.close()
                except Exception as e:raw['secondary_errors'].append(repr(e));primary=primary or e
            if timer is not None:
                timer.join(timeout=1);raw['timer_joined']=not timer.is_alive()
                if timer.is_alive():primary=primary or g.Blocked('deadline thread failed to join')
            else:raw['timer_joined']=True
            if raw['secondary_errors']:primary=primary or g.Blocked('secondary lifecycle errors')
            if primary:raw['success']=False;raw['error']=repr(primary)
            g.create(home/(request_id+'.json'),raw)
    if primary:
        if isinstance(primary,(g.Blocked,g.Invalid)):raise primary
        raise g.Blocked(str(primary)) from primary
    return raw

def needle_results(tasks,raws):
    if len(tasks)!=6 or len({t['id'] for t in tasks})!=6:raise g.Blocked('six distinct needle documents required')
    context={t['context_tokens'] for t in tasks};cycles={t['cycle'] for t in tasks}
    if len(context)!=1 or len(cycles)!=1:raise g.Blocked('no context/cycle pooling')
    if sorted((t['occupancy_percent'],t['document']) for t in tasks)!=[(p,d) for p in (62,100) for d in range(3)]:raise g.Blocked('incomplete 3/3 envelope')
    byid={r['task_id']:r for r in raws};distinct=len(raws)==len(byid)==6 and set(byid)=={t['id'] for t in tasks}
    lengths={p:[] for p in (62,100)}
    for t in tasks:
        r=byid.get(t['id'],{})
        ok=distinct and r.get('success') is True and set(t['expected'])=={'K25','K50','K75','K90'} and grade_task(t,r.get('text',''))
        lengths[t['occupancy_percent']].append({'task_id':t['id'],'document':t['document'],'passed':bool(ok)})
    return {'passed':all(r['passed'] for rr in lengths.values() for r in rr),'lengths':lengths,'context_tokens':next(iter(context)),'cycle':next(iter(cycles))}

def greedy_diff(reference,candidate):
    """Compare supplied generated token IDs; never re-tokenize normalized text.

    A single-pair result is not the declared-set equivalence claim. The caller
    must require all twelve frozen prompts, including all six QT5 documents.
    Chat SSE does not supply this proof: a missing complete token-ID capture
    remains unavailable, never inferred from text or finish_reason.
    """
    out={'claim':False,'first_difference':None,'reason':'same-context f16 FA-on reference unavailable'}
    if reference is None:return out
    if reference.get('cache_k')!='f16' or reference.get('cache_v')!='f16' or reference.get('fa')!='on':return dict(out,reason='invalid f16 FA-on reference')
    for k in ('context_tokens','fa','sampling','prompt_ids','eos_id'):
        if k not in reference or k not in candidate or reference[k]!=candidate[k]:return dict(out,reason='paired '+k+' mismatch/missing')
    for row in (reference,candidate):
        ids=row.get('token_ids')
        if row.get('complete') is not True or not isinstance(ids,list) or not ids or any(type(t) is not int or t<0 for t in ids) or ids[-1]!=row.get('eos_id'):
            return dict(out,reason='complete generated token-ID sequence including EOS missing')
    a=reference['token_ids'];b=candidate['token_ids']
    difference=next((i for i,(x,y) in enumerate(zip(a,b)) if x!=y),None)
    if difference is None and len(a)!=len(b):difference=min(len(a),len(b))
    equal=difference is None;q4=candidate.get('cache_k')=='q4_0' or candidate.get('cache_v')=='q4_0'
    return {'claim':equal and not q4,'pair_equal':equal,'first_difference':difference,'reference_length':len(a),'candidate_length':len(b),'reason':'q4 always explicitly lossy' if q4 else ('exact finite pair match including EOS' if equal else 'token-ID mismatch; equivalence forbidden')}

class GPULock:
    """Nonblocking flock; caller retains ownership through cleanup receipts."""
    def __init__(self,path=None):
        self.path=pathlib.Path(path or g.ROOT/'runs/.gpu-lock');self.handle=None;self.held=False
    def __enter__(self):
        import fcntl
        if self.held:raise g.Blocked('lock object already held')
        self.handle=self.path.open('a+')
        try:fcntl.flock(self.handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as e:
            self.handle.close();self.handle=None;raise g.Blocked('GPU lock busy; no waiting') from e
        self.held=True;return self
    def __exit__(self,*args):
        import fcntl
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(),fcntl.LOCK_UN);self.handle.close()
        self.handle=None;self.held=False

def managed_unit_argv(unit,worker,wall_s):
    import re
    if not re.fullmatch(r'inference9b-e32-[A-Za-z0-9-]+\.service',unit):raise g.Blocked('unowned unit name')
    if type(wall_s) is not int or not 0<wall_s<=4200 or not worker or not pathlib.Path(worker[0]).is_absolute():raise g.Blocked('invalid managed command')
    return ['systemd-run','--user','--unit='+unit,'--property','MemoryMax=infinity','--property','RuntimeMaxSec='+str(wall_s),'--property','KillMode=control-group','--',*worker]

def start_managed(lock,watcher,guard,cfg,verbose_flag,spawn,identity):
    """Injection bridge, NOT a watcher implementation or live spawn adapter.

    The caller must already have verified audit/source/library/allocation pins,
    host window/AC/profile and committed a budget reservation. watcher.start()
    must acknowledge the first periodic sample; healthy() must fail on stalls.
    spawn() must establish managed-unit containment/ancestor readbacks.
    """
    import time
    if lock is None or not lock.held:raise g.Blocked('GPU lock not held')
    command=g.argv(cfg,verbose_flag)
    watcher.start();watcher.healthy();sample=watcher.snapshot()
    if set(sample)!={'memory_bytes','processes','monotonic_ns','precision_bytes'}:raise g.Blocked('idle telemetry unavailable')
    age=time.monotonic_ns()-sample['monotonic_ns']
    if not 0<=age<=2_000_000_000 or type(sample['precision_bytes']) is not int or sample['precision_bytes']<=0:raise g.Blocked('stale/imprecise idle telemetry')
    if sample['processes']!=[] or sample['memory_bytes']!=3*g.MIB:raise g.Blocked('fresh GPU idle 3MiB and no consumers required')
    guard.observe(sample['memory_bytes']);watcher.healthy()
    owned=spawn(command)
    if identity()!=owned:raise g.Blocked('spawn owned identity mismatch; no signalling')
    guard.bind(owned);watcher.healthy()
    return owned

def cleanup_managed(lock,abort,join,drain,stop,readback,settings,guard=None):
    """Ordered normal stop: bounded abort/join/drain before owned stop.

    Callbacks enforce 30s drain/60s stop and per-signal identity revalidation.
    Failed drain never authorizes a normal forced mid-prefill stop. Emergency
    stopping is only MemoryGuard's owned TERM/WAIT5/KILL exception.
    """
    errors=[];snapshot=None
    try:
        if lock is None or not lock.held:raise g.Blocked('cleanup requires retained GPU lock')
        for action in (abort,join,drain,stop):
            if action() is False:raise g.Blocked('cleanup phase did not confirm completion')
        snapshot=readback()
        expected={'models':[],'owned_pids':[],'port_8081':[],'gpu_processes':[],'gpu_memory_bytes':3*g.MIB,'unit_state':'inactive','settings':settings}
        if snapshot!=expected:raise g.Blocked('cleanup residual/models/port/GPU/unit/settings mismatch')
    except Exception as e:errors.append(repr(e))
    if guard is not None:
        guard.healthy(errors)
    if errors:raise g.Blocked('; '.join(errors))
    return {'verified':True,'readback':snapshot,'lock_held':lock.held}

class Campaign:
    """Additive budget ledger anchored to immutable attempt-1 counter/terminal.

    This layer counts entitlements, not GPU readiness. It is never sufficient
    to load: caller must also satisfy independent source/estimate/lifecycle
    gates. All calls require the same retained exclusive campaign flock.
    """
    def __init__(self,home,baseline,baseline_sha,lock,gate):
        self.home=pathlib.Path(home);self.baseline=pathlib.Path(baseline);self.baseline_sha=baseline_sha;self.lock=lock;self.gate=gate
        self._check()
        old=json.loads(self.baseline.read_text())
        if old!={'plan_sha':'43af0b7fed9a0d07a5e9ea165c61f879bdd7f08ac16d927218106454b6236eb3','reservations_consumed':1,'requests_consumed':0,'reserved_wall_s':600,'caps':{'reservations':14,'requests':1180,'wall_s':34800},'terminal':'BLOCKED','no_resume_or_reset':True}:raise g.Blocked('unexpected immutable campaign baseline')
        terminal=self.baseline.parent/'budgets/E3-5dae520fc6ad4e33a02ea75663485ec4-terminal.json'
        t=json.loads(terminal.read_text())
        if t.get('status')!='BLOCKED' or t.get('no_retry') is not True or t.get('requests')!=0 or t.get('model_loads')!=0:raise g.Blocked('historical attempt-1 terminal drift')
        binding={'plan_sha256':g.PLAN,'baseline_path':str(self.baseline),'baseline_sha256':baseline_sha,'terminal_path':str(terminal),'terminal_sha256':g.digest(terminal),'remaining_caps':{'reservations':13,'requests':1180,'wall_s':34200}}
        path=self.home/'counter-overlay.json'
        if path.exists():
            if json.loads(path.read_text())!=binding:raise g.Blocked('counter overlay binding drift')
        else:g.create(path,binding)
    def _check(self):
        if not self.lock.held:raise g.Blocked('campaign mutation without GPU lock')
        if g.digest(self.baseline)!=self.baseline_sha:raise g.Blocked('historical counter pin mismatch')
    def rows(self):
        self._check();paths=sorted(self.home.glob('reservation-*.json'))
        if [p.name for p in paths]!=[f'reservation-{i:02}.json' for i in range(len(paths))]:raise g.Blocked('counter sequence gap')
        return [json.loads(p.read_text()) for p in paths]
    def totals(self):
        rows=self.rows();intents=list(self.home.glob('E3-*/intents/*.json'))
        return {'reservations':1+len(rows),'request_caps':sum(r['request_cap'] for r in rows),'wall_s':600+sum(r['wall_s'] for r in rows),'requests':len(intents)}
    def reserve(self,name,request_cap,wall_s,run_id,cycle='original'):
        import re,time
        self._check()
        if self.gate()!={'label':'REAL','plan_sha256':g.PLAN,'run_id':g.AUDIT,'status':'GOOD'}:raise g.Blocked('exact current-path audit gate not REAL/GOOD')
        if not re.fullmatch(r'E3-[0-9a-f]{32}',run_id) or run_id=='E3-5dae520fc6ad4e33a02ea75663485ec4':raise g.Blocked('fresh execution ID required')
        cfg=g.config(name);rows=self.rows()
        if (self.home/run_id).exists() or any(r['run_id']==run_id for r in rows):raise g.Blocked('execution ID already consumed')
        for row in rows:
            terminal=self.home/row['run_id']/'terminal.json'
            if not terminal.exists():raise g.Blocked('unknown terminal; no resume/replacement')
            if json.loads(terminal.read_text()).get('status')!='GOOD':raise g.Blocked('prior failure; campaign halted')
        if name=='ref131k':raise g.Blocked('B4 optional reference excluded before load')
        if cycle=='original':
            allowed=[r[0] for r in g.CONFIGS if r[0]!='ref131k']
            originals=[r['configuration'] for r in rows if r['cycle']=='original']
            if len(originals)>=len(allowed) or name!=allowed[len(originals)]:raise g.Blocked('ordered config chain; no retry/skip')
            if any(r['cycle']!='original' for r in rows):raise g.Blocked('original work after rebuild forbidden')
            max_req,max_wall=(24,600) if name.startswith('E0-') else ((24,1800) if name.startswith('ref') else (180,4200))
        elif cycle in ('rebuild-1','rebuild-2'):
            if cfg['context_tokens'] not in (32768,131072) or cfg['cache_k']=='f16':raise g.Blocked('rebuild only selected 32k/131k candidate')
            original=[r for r in rows if r['configuration']==name and r['cycle']=='original']
            if len(original)!=1:raise g.Blocked('qualified original missing')
            same=[r for r in rows if r['configuration']==name]
            if [r['cycle'] for r in same]!=(['original'] if cycle=='rebuild-1' else ['original','rebuild-1']):raise g.Blocked('rebuild order/no retries')
            rebuilds=[r for r in rows if r['cycle']!='original']
            if len(rebuilds)>=4 or any(r['config']['context_tokens']==cfg['context_tokens'] and r['configuration']!=name for r in rebuilds):raise g.Blocked('one selected recipe per context/two recipes max')
            max_req,max_wall=40,1800
        else:raise g.Blocked('unlisted cycle')
        if type(request_cap) is not int or type(wall_s) is not int or not 0<request_cap<=max_req or not 0<wall_s<=max_wall:raise g.Blocked('per-reservation cap')
        totals=self.totals()
        if totals['reservations']+1>14 or totals['request_caps']+request_cap>1180 or totals['wall_s']+wall_s>34800:raise g.Blocked('cumulative 1+13/1180/34800 cap')
        row={'run_id':run_id,'configuration':name,'cycle':cycle,'config':cfg,'request_cap':request_cap,'wall_s':wall_s,'plan_sha256':g.PLAN,'gate_run_id':g.AUDIT,'index':len(rows),'reserved_monotonic_ns':time.monotonic_ns()}
        # Reservation is committed first: every later constructor failure consumes it.
        g.create(self.home/f'reservation-{len(rows):02}.json',row)
        run=self.home/run_id;run.mkdir();(run/'intents').mkdir()
        g.create(run/'config.json',{**cfg,'plan_sha256':g.PLAN,'gate_run_id':g.AUDIT,'run_id':run_id,'cycle':cycle})
        return row
    def _owned(self,row):
        self._check()
        if row not in self.rows():raise g.Blocked('unowned reservation')
        run=self.home/row['run_id']
        if (run/'terminal.json').exists():raise g.Blocked('terminal reservation immutable')
        return run
    def intent(self,row,request_id,stage):
        import re,time
        run=self._owned(row)
        if not re.fullmatch(r'[A-Za-z0-9_.-]+',request_id):raise g.Blocked('unsafe intent ID')
        intents=[json.loads(p.read_text()) for p in (run/'intents').glob('*.json')]
        caps={'prewarm':120,'anchor':12,'admission':12,'measurement':12,'functional':24 if row['cycle']=='original' else 40}
        if stage not in caps or sum(x['stage']==stage for x in intents)>=caps[stage] or len(intents)>=row['request_cap']:raise g.Blocked('request/stage cap')
        if time.monotonic_ns()>=row['reserved_monotonic_ns']+(row['wall_s']-90)*10**9:raise g.Blocked('reservation deadline; reserve 30s drain +60s stop')
        if self.totals()['requests']>=1180:raise g.Blocked('global request cap')
        value={'run_id':row['run_id'],'request_id':request_id,'stage':stage,'monotonic_ns':time.monotonic_ns()}
        g.create(run/'intents'/(request_id+'.json'),value);return value
    def close(self,row,status,cleanup):
        run=self._owned(row)
        if status not in ('GOOD','BLOCKED','INVALID'):raise g.Blocked('terminal label')
        if status=='GOOD' and cleanup.get('verified') is not True:raise g.Blocked('GOOD requires successful cleanup')
        g.create(run/'terminal.json',{'run_id':row['run_id'],'status':status,'cleanup':cleanup,'requests':len(list((run/'intents').glob('*.json'))),'plan_sha256':g.PLAN})

def admit_first_load(home,cfg,other_bytes,derived,pins):
    """Persist config-bound POLICY estimate before any caller attempts spawn."""
    estimate=g.estimate(cfg,other_bytes,derived);estimate['pins']=pins
    g.create(pathlib.Path(home)/'estimate.json',estimate)
    with (pathlib.Path(home)/'estimate.md').open('x') as f:
        f.write('# First guarded load: POLICY, not a physical bound\n\n')
        for name,term in estimate['terms'].items():f.write(f"- {name}: {term['bytes']} bytes ({term['status']})\n")
        f.write(f"\nTotal: {estimate['total_bytes']} bytes; cap {g.CAP} bytes. {estimate['verdict']}\n")
    if estimate['total_bytes']>g.CAP:raise g.Blocked('POLICY estimate above cap; no load')
    return estimate

def startup_measurement(home,pins,log,accounting,derived,guard):
    """Bind measured accounting to config and independently derived KV/RS.

    The injected synchronous accounting must carry actual disjoint residency
    proof and raw operands, not log sums masquerading as physical residency.
    """
    if 'config' not in pins or pins['config']!=g.config(pins['config']['configuration']):raise g.Blocked('exact config pins missing')
    guard.observe(accounting['device_upper_bytes'])
    rows=[r for r in g.allocations(pathlib.Path(log).read_text()) if r['backend']=='CUDA0' and not r['sizing_zero']]
    for kind,key in (('KV','kv'),('RS','rs')):
        matches=[r for r in rows if r['kind']==kind]
        if len(matches)!=1 or matches[0]['bytes_upper']!=derived[key]:raise g.Blocked('derived/startup '+kind+' reconciliation failed')
    receipt=g.measured_receipt(home,pins,log,accounting);guard.healthy();return receipt

def append_highwater(home,receipt,pins,device_upper_bytes,other_upper_bytes,guard):
    """Append lazy observed peak, preserving create-only startup measurement."""
    import time
    home=pathlib.Path(home)
    if receipt.get('pins')!=pins:raise g.Blocked('highwater pin drift')
    g.continuation(receipt,pins,other_upper_bytes)
    if type(device_upper_bytes) is not int or device_upper_bytes<0:raise g.Blocked('device byte precision unavailable')
    paths=sorted(home.glob('highwater-*.json'))
    if [p.name for p in paths]!=[f'highwater-{i:04}.json' for i in range(len(paths))]:raise g.Blocked('highwater sequence gap')
    previous=g.digest(paths[-1]) if paths else g.digest(home/'estimates-measured.json')
    prior_peak=json.loads(paths[-1].read_text())['peak_bytes'] if paths else receipt['accounting']['device_upper_bytes']
    row={'index':len(paths),'pins':pins,'monotonic_ns':time.monotonic_ns(),'device_upper_bytes':device_upper_bytes,'other_upper_bytes':other_upper_bytes,'peak_bytes':max(prior_peak,device_upper_bytes),'previous_sha256':previous,'status':'INVALID#3' if device_upper_bytes>g.CAP else 'MEASURED_OBSERVED_NOT_BOUND','policy_fallback':False}
    g.create(home/f'highwater-{len(paths):04}.json',row)
    guard.observe(device_upper_bytes);guard.healthy();return row

if __name__=='__main__':raise SystemExit('OFFLINE components only: managed launcher remains fail-closed')
