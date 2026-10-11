"""Protocol peer used by integration.py. No providers or credentials."""
import json
import os
import subprocess
import sys
import time

kind = sys.argv[1]
log = os.environ['PYXIS_TEST_LOG']
session = f'{kind}-{os.getpid()}'
turn = 0

def record(action, **fields):
    with open(log, 'a', encoding='utf-8') as file:
        file.write(json.dumps({'action': action, 'kind': kind, 'pid': os.getpid(), **fields}) + '\n')

def emit(value):
    print(json.dumps(value), flush=True)

def rpc(id, value):
    emit({'id': id, 'result': value})

record('spawn', argv=sys.argv[2:], cwd=os.getcwd())
for line in sys.stdin:
    event = json.loads(line)
    if kind == 'claude':
        prompt = event['message']['content']
        record('prompt', prompt=prompt)
        if prompt.startswith('delay:'):
            time.sleep(float(prompt.split(':', 1)[1]))
        if prompt == 'spawn-child':
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
            record('child', child_pid=child.pid)
            time.sleep(60)
        if prompt == 'eof':
            sys.exit(0)
        emit({'type': 'system', 'subtype': 'init', 'session_id': session})
        if prompt == '/compact' and os.environ.get('PYXIS_TEST_NO_BOUNDARY') != '1':
            emit({'type': 'system', 'subtype': 'compact_boundary', 'session_id': session})
        turn += 1
        emit({'type': 'result', 'subtype': 'success', 'session_id': session, 'result': f'{prompt}:{turn}', 'usage': {'input_tokens': 3}, 'total_cost_usd': .1})
    elif kind == 'codex':
        method = event.get('method')
        id = event.get('id')
        if method == 'initialize':
            rpc(id, {})
        elif method == 'thread/start':
            record('thread', params=event['params'])
            rpc(id, {'thread': {'id': session}})
        elif method in ['turn/start', 'thread/compact/start']:
            compact = method == 'thread/compact/start'
            turn += 1
            tid = f'turn-{turn}'
            record('prompt', prompt='/compact' if compact else event['params']['input'][0]['text'], params=event['params'])
            # Interleave an irrelevant thread and a server approval request.
            rpc(id, {} if compact else {'turn': {'id': tid}})
            emit({'method': 'turn/completed', 'params': {'threadId': 'unrelated', 'turn': {'id': 'other', 'status': 'failed'}}})
            emit({'id': 'approval', 'method': 'item/commandExecution/requestApproval', 'params': {'threadId': session}})
            answer = json.loads(sys.stdin.readline())
            assert answer['result']['decision'] == 'decline'
            item = {'type': 'contextCompaction'} if compact else {'type': 'agentMessage', 'text': 'codex-answer', 'phase': 'final_answer'}
            emit({'method': 'item/completed', 'params': {'threadId': session, 'turnId': tid, 'item': item}})
            emit({'method': 'turn/completed', 'params': {'threadId': session, 'turn': {'id': tid, 'status': 'completed'}}})
    elif kind == 'dsh':
        method, id = event['method'], event['id']
        if method == 'initialize':
            record('initialize', params=event['params'])
            rpc(id, {'serverInfo': {'name': 'deepseek-harness-sdk-runtime'}})
        elif method == 'pyxis/session/start':
            record('session', params=event['params'])
            rpc(id, {'sessionId': session})
        elif method == 'pyxis/session/run':
            record('prompt', prompt=event['params']['prompt'])
            rpc(id, {'text': 'dsh-answer', 'stopReason': 'completed', 'usage': {}})
        elif method == 'pyxis/session/compact':
            record('prompt', prompt='/compact')
            rpc(id, {'compacted': True})
