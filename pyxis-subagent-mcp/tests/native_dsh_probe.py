"""Opt-in installed SDK baseline, with no Pyxis plugin and no credential handling."""
import argparse
import json
from pathlib import Path
import queue
import subprocess
import threading
import time
import uuid

parser=argparse.ArgumentParser()
parser.add_argument('--config',type=Path,required=True)
parser.add_argument('--cwd',type=Path,required=True)
parser.add_argument('--effort',default='max')
parser.add_argument('--write-test',action='store_true')
parser.add_argument('--smoke-test',action='store_true')
args=parser.parse_args()
args.cwd.mkdir(parents=True,exist_ok=True)
config=json.loads(args.config.read_text(encoding='utf-8-sig'))['runtimes']['dsh']
command=[config['executable'],*config.get('args',[]),'--profile','sdk']
p=subprocess.Popen(command,cwd=str(args.cwd.resolve()),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')
messages=queue.Queue()
errors=[]
threading.Thread(target=lambda:[messages.put(json.loads(line)) for line in p.stdout],daemon=True).start()
threading.Thread(target=lambda:errors.extend(p.stderr.readlines()),daemon=True).start()
def send(id,method,params):
    p.stdin.write(json.dumps(dict(jsonrpc='2.0',id=id,method=method,params=params))+'\n');p.stdin.flush()
try:
    route=dict(cwd=str(args.cwd.resolve()),provider=config['provider'],model=config['model'])
    if args.effort!='native': route['reasoningEffort']=args.effort
    send(1,'initialize',route)
    end=time.monotonic()+90
    while True:
        event=messages.get(timeout=max(.01,end-time.monotonic()))
        if event.get('id')==1:
            if 'error' in event:raise RuntimeError(event['error'])
            break
    prompt='Authorized synthetic API probe. Reply exactly PONG. Do not use tools, access files, or delegate.'
    if args.write_test:prompt='Authorized synthetic SDK acceptance test. Use native tools to create probe.txt in the current working directory containing PONG, read it back, and report its contents. Do not delegate or access any other directory or network service.'
    if args.smoke_test:
        (args.cwd/'seed.txt').write_text('SYNTHETIC-TOKEN',encoding='utf-8')
        prompt='Authorized synthetic SDK acceptance test. Work only in the current directory. Do not delegate, use MCP tools, inspect other projects, or access the network. Read seed.txt. Create written.txt with its exact contents. Then run a native PowerShell command without a profile which reads written.txt and writes the same contents to command.txt. Report the token after actually verifying both files.'
    send(2,'session/prompt',dict(sessionId='synthetic-native-'+uuid.uuid4().hex,contentBlocks=[dict(type='text',text=prompt)]))
    while True:
        event=messages.get(timeout=max(.01,end-time.monotonic()))
        if event.get('error'):raise RuntimeError(event['error'])
        if event.get('method')=='session.event' and event['params']['event']['type']=='request/header':
            data=event['params']['event']['data']
            print(json.dumps(dict(header_keys=list(data),tool_count=len(data.get('header',{}).get('tools',[])))),flush=True)
        if event.get('method')=='session.event' and event['params']['event']['type'] in ('tool/call','tool/result'):
            print(json.dumps(dict(type=event['params']['event']['type'],name=event['params']['event']['data'].get('name'))),flush=True)
        if event.get('method')=='session.event' and event['params']['event']['type']=='turn/end':
            reason=event['params']['event']['data']['reason']
            print(json.dumps(dict(native_sdk=True,effort=args.effort,stop_reason=reason)),flush=True)
            if reason.get('kind')!='completed':raise RuntimeError(reason)
            break
finally:
    p.stdin.close()
    try:p.wait(timeout=10)
    except subprocess.TimeoutExpired:p.kill();p.wait()
