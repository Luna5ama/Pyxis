"""Concurrent native-protocol peer. Only synthetic fixture files are touched."""
import json
import os
from pathlib import Path
import sys
import threading
import time
import subprocess

kind = sys.argv[1]
session = f'{kind}-{os.getpid()}'
lock = threading.RLock()
write_lock = threading.Lock()
active = None
rounds = 0
pending = {}
history = []
if kind == 'claude' and '--resume' in sys.argv:
    session = sys.argv[sys.argv.index('--resume') + 1]


def record(action, **fields):
    with write_lock:
        with open(os.environ['PYXIS_TEST_LOG'], 'a', encoding='utf-8') as file:
            file.write(json.dumps(dict(action=action, kind=kind, pid=os.getpid(), **fields))+'\n')


def emit(value):
    with write_lock:
        print(json.dumps(value), flush=True)


def rpc(id, result):
    emit(dict(jsonrpc='2.0', id=id, result=result))


def notify(method, **params):
    emit(dict(method=method, params=dict(threadId=session, **params)))


def state(value):
    emit(dict(type='system', subtype='session_state_changed', state=value, session_id=session))


def complete(run, result=None):
    global active
    with lock:
        if active is not run:
            return
        active = None
        cancelled = run['cancel'].is_set()
        text = result if result is not None else run['prompt']
        text += f":{run['round']}:{os.getcwd()}"
        if kind == 'claude':
            if run['compact']:
                emit(dict(type='system', subtype='compact_boundary', session_id=session))
            emit(dict(type='result', subtype='success', result=text, session_id=session,
                      usage=dict(input_tokens=run['round']), total_cost_usd=run['round']/100))
            state('idle')
        elif kind == 'codex':
            item = dict(type='contextCompaction') if run['compact'] else dict(type='agentMessage', phase='final_answer', text=text)
            notify('item/completed', turnId=run['id'], item=item)
            notify('thread/tokenUsage/updated', tokenUsage=dict(last=dict(inputTokens=2), total=dict(inputTokens=run['round']*2)))
            notify('turn/completed', turn=dict(id=run['id'], status='interrupted' if cancelled else 'completed'))
        else:
            rpc(run['rpc'], dict(compacted=True) if run['compact'] else dict(text=text, stopReason='interrupted' if cancelled else 'completed', usage=dict(inputTokens=2)))
        record('end', native_turn=run['id'], cancelled=cancelled)


def background(run):
    prompt = run['prompt']
    if prompt == 'eof':
        os._exit(0)
    if prompt == 'spawn-child':
        child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])
        run['child']=child
        if kind=='codex':notify('item/started',turnId=run['id'],item=dict(type='commandExecution',id=run['id']+'-exec'))
        record('child',child_pid=child.pid)
        run['cancel'].wait(60)
        complete(run)
        return
    if prompt == 'large-output':
        complete(run,'x'*400000)
        return
    if prompt.startswith('approve:') or prompt.startswith('questions'):
        with lock:
            if active is not run:
                return
            q = prompt.startswith('questions')
            req_id = f"interaction-{run['round']}"
            pending[req_id] = (run, q)
            if kind == 'claude':
                state('requires_action')
                emit(dict(type='control_request', request_id=req_id, request=dict(subtype='can_use_tool', tool_name='AskUserQuestion' if q else 'Write',
                    input=dict(questions=[dict(question='Pick a color', options=[dict(label='blue'), dict(label='red')])]) if q else dict(file_path=prompt[8:], content='approved'))))
            else:
                params = dict(threadId=session, turnId=run['id']) if kind == 'codex' else {}
                params.update(dict(questions=[dict(id='color', question='Pick a color', options=[dict(label='blue'), dict(label='red')])]) if q else dict(path=prompt[8:], options=['allowed-once', 'rejected', 'cancelled']))
                method = ('item/tool/requestUserInput' if q else 'item/commandExecution/requestApproval') if kind == 'codex' else ('pyxis/interaction/questions' if q else 'pyxis/interaction/approval')
                emit(dict(id=req_id, method=method, params=params))
        return
    if prompt == 'background' and kind == 'claude':
        emit(dict(type='system', subtype='task_started', task_id='child', task_type='local_agent', session_id=session))
        emit(dict(type='result', subtype='success', result='intermediate', session_id=session, usage={}))
        run['cancel'].wait(.4)
        emit(dict(type='system', subtype='task_notification', task_id='child', session_id=session))
        state('running')
    delay = float(prompt.split(':')[1]) if prompt.startswith('delay:') else (60 if prompt=='ignore-cancel' else .8 if prompt=='steer-race' else .04)
    run['cancel'].wait(delay)
    complete(run)


def start(prompt, rpc_id=None, compact=False):
    global active, rounds
    with lock:
        assert active is None, 'native peer received overlapping turns'
        rounds += 1
        active = dict(prompt=prompt, rpc=rpc_id, compact=compact, cancel=threading.Event(), id=f'turn-{rounds}', round=rounds)
        run = active
        record('prompt', prompt=prompt, cwd=os.getcwd())
        history.append(prompt)
        if kind == 'claude':
            emit(dict(type='system', subtype='init', session_id=session, model='test-model', permissionMode='auto', mcp_servers=[], claude_code_version='test'))
            state('running')
        elif kind == 'codex':
            rpc(rpc_id, {} if compact else dict(turn=dict(id=run['id'])))
            notify('turn/started', turn=dict(id=run['id']))
            emit(dict(id=rpc_id+100000,error=dict(code=-1,message='unrelated RPC response')))
            emit(dict(method='turn/completed',params=dict(threadId='unrelated',turn=dict(id=run['id'],status='failed'))))
        else:
            notify('pyxis/session/event', sessionId=session, event=dict(type='turn/start'))
        threading.Thread(target=background, args=(run,), daemon=True).start()


record('spawn', cwd=os.getcwd(), argv=sys.argv[2:])
for line in sys.stdin:
    event = json.loads(line)
    method = event.get('method')
    req_id = event.get('id')
    if kind == 'claude' and event.get('type') == 'control_request':
        subtype = event['request']['subtype']
        emit(dict(type='control_response', response=dict(subtype='success', request_id=event['request_id'], response=dict(models=[dict(value='test-model', supportedEffortLevels=['low','high','max'])]))))
        if subtype == 'interrupt':
            with lock:
                if active and active['prompt'] != 'ignore-cancel':
                    active['cancel'].set()
                    cleared = [key for key,(run,_) in pending.items() if run is active]
                    for key in cleared:
                        pending.pop(key)
                        emit(dict(type='control_cancel_request',request_id=key))
                    if cleared:
                        complete(active)
        continue
    answer_id = event.get('request_id') if kind == 'claude' else req_id
    if kind == 'claude' and event.get('type') == 'control_response':
        answer_id = event['response']['request_id']
    if answer_id in pending:
        run, question = pending.pop(answer_id)
        answer = event['response']['response'] if kind == 'claude' else event['result']
        record('answer', response=answer)
        if question:
            if run['prompt']=='questions-twice' and not run.get('answered'):
                run['answered']=True
                background(run)
            else:
                complete(run, json.dumps(answer))
        else:
            allowed = answer.get('behavior') == 'allow' or answer.get('decision') in ('accept','acceptForSession') or answer.get('outcome') == 'allowed-once'
            if allowed:
                Path(run['prompt'][8:]).write_text('approved', encoding='utf-8')
            complete(run, 'allowed' if allowed else 'denied')
        continue
    if kind == 'claude':
        if event.get('type') == 'user':
            start(event['message']['content'], compact=event['message']['content']=='/compact')
        continue
    if method == 'initialize':
        rpc(req_id, {})
    elif method in ('thread/start', 'thread/resume'):
        if method == 'thread/resume':
            session = event['params']['threadId']
            record('resume', session_id=session)
        record('thread', params=event['params'])
        rpc(req_id, dict(thread=dict(id=session), sandbox=dict(type='workspaceWrite'), approvalPolicy='on-request'))
    elif method == 'pyxis/session/start':
        if event['params'].get('resumeSessionId'):
            session = event['params']['resumeSessionId']
            record('resume', session_id=session)
        rpc(req_id, dict(sessionId=session, effectiveConfig=dict(approvalPolicy='ask')))
    elif method == 'pyxis/session/suspend':
        record('suspend', session_id=session)
        rpc(req_id, dict(suspended=True))
    elif method in ('model/list','pyxis/models/list'):
        rpc(req_id, dict(data=[dict(model='test-model', supportedReasoningEfforts=[dict(reasoningEffort='high')])], nextCursor=None))
    elif method == 'thread/backgroundTerminals/list':
        rpc(req_id,dict(data=[dict(processId=str(child_run['child'].pid),itemId=child_run['id']+'-exec')] if 'child_run' in globals() and child_run['child'].poll() is None else [],nextCursor=None))
    elif method == 'thread/backgroundTerminals/terminate':
        assert str(child_run['child'].pid)==event['params']['processId']
        child_run['child'].kill();child_run['child'].wait()
        record('terminal-stop',child_pid=child_run['child'].pid)
        rpc(req_id,{})
    elif method in ('turn/start','pyxis/session/run','thread/compact/start','pyxis/session/compact'):
        compact = 'compact' in method
        prompt = '/compact' if compact else event['params'].get('prompt', (event['params'].get('input') or [dict(text='')])[0]['text'])
        start(prompt, req_id, compact)
    elif method in ('turn/steer','pyxis/session/steer'):
        with lock:
            if active is None or (method == 'turn/steer' and event['params']['expectedTurnId'] != active['id']):
                emit(dict(id=req_id, error=dict(code=-32602, message='No matching active turn')))
            elif active['prompt']=='steer-race':
                complete(active)
                emit(dict(id=req_id,error=dict(code=-32602,message='Turn completed before steer')))
            else:
                text = event['params'].get('prompt', (event['params'].get('input') or [dict(text='')])[0]['text'])
                active['prompt'] = text
                record('steer', prompt=text)
                rpc(req_id, dict(applied=True, turnId=active['id']))
    elif method in ('turn/interrupt','pyxis/session/cancel'):
        with lock:
            if active and active['prompt'] != 'ignore-cancel':
                if active.get('child'):child_run=active
                active['cancel'].set()
                complete(active)
        rpc(req_id, {})
