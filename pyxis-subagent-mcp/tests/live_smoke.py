"""Real-provider smoke test. Uses the CLI's existing login and consumes model usage.

python tests/live_smoke.py config.local.json [--binary path] [--output path]
"""
import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('config', type=Path)
parser.add_argument('--binary', type=Path, default=ROOT/'target/release'/('pyxis-subagent-mcp.exe' if os.name=='nt' else 'pyxis-subagent-mcp'))
parser.add_argument('--output', type=Path)
args = parser.parse_args()
config = json.loads(args.config.read_text(encoding='utf-8-sig'))
base = args.config.resolve().parent
for tool in config.values():
    for key in ('executable', 'cwd', 'dsh_module_root'):
        if key in tool and tool[key] is not None:
            path = Path(tool[key])
            if not path.is_absolute() and (key != 'executable' or len(path.parts)>1): tool[key]=str(base/path)
    tool['cache'] = dict(enabled=True,ttl_seconds=180,compact_margin_seconds=150,use_usage_ttl=False)
    tool['request_timeout_seconds']=180

with tempfile.TemporaryDirectory(prefix='pyxis-live-') as temp:
    path=Path(temp)/'config.json'; path.write_text(json.dumps(config),encoding='utf-8')
    process=subprocess.Popen([str(args.binary.resolve()),str(path)],cwd=ROOT,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')
    messages, logs, records=queue.Queue(),[],[]
    def read_errors():
        for line in process.stderr:
            try: event=json.loads(line)
            except json.JSONDecodeError: event={'diagnostic':line.strip()}
            logs.append(event)
            if event.get('action','').startswith('compact'): print(event['tool'],event['action'],flush=True)
    reader=threading.Thread(target=lambda:[messages.put(json.loads(line)) for line in process.stdout],daemon=True)
    errors=threading.Thread(target=read_errors,daemon=True)
    reader.start();errors.start()
    def request(id,method,params):
        process.stdin.write(json.dumps(dict(jsonrpc='2.0',id=id,method=method,params=params))+'\n');process.stdin.flush()
        result=messages.get(timeout=185)
        assert result.get('id')==id,result
        assert 'error' not in result,result
        return result['result']
    def call(id,name,prompt):
        result=request(id,'tools/call',dict(name=name,arguments=dict(prompt=prompt)))
        assert result.get('isError') is False,result
        return result['structuredContent']
    try:
        request(1,'initialize',dict(protocolVersion='2025-11-25',capabilities={},clientInfo=dict(name='pyxis-live-smoke',version='1')))
        tools=request(2,'tools/list',{})['tools']
        assert [t['name'] for t in tools]==sorted(config)
        marker='pyxis-'+uuid.uuid4().hex[:10]
        for id,name in enumerate(config,10):
            first=call(id,name,f'Remember marker {marker}. Reply only ok. Do not use tools.')
            second=call(id+100,name,'What marker did I ask you to remember? Reply only that marker. Do not use tools.')
            assert marker in second['result'],second
            assert first['session_id']==second['session_id']
            records.append(dict(tool=name,first=first,second=second))
            print(name,'multi-turn passed',flush=True)
        deadline=time.monotonic()+180
        while True:
            outcomes={e.get('tool'):e for e in logs if e.get('action') in ('compact_completed','compact_no_history','compact_failed','compact_window_missed')}
            if len(outcomes)==len(config):break
            assert time.monotonic()<deadline,'Timed out waiting for compaction'
            time.sleep(.1)
        for id,record in enumerate(records,210):
            compact=outcomes[record['tool']]
            assert compact['action']=='compact_completed',compact
            final=call(id,record['tool'],'What marker did I ask you to remember? Reply only that marker. Do not use tools.')
            assert marker in final['result'],final
            assert final['session_id']==record['first']['session_id']
            record.update(compact=compact,after_compact=final)
            print(record['tool'],'native compact + follow-up passed',flush=True)
    finally:
        process.stdin.close()
        try:process.wait(timeout=10)
        except subprocess.TimeoutExpired:process.kill();process.wait();raise
        reader.join(timeout=2);errors.join(timeout=2)
        process.stdout.close();process.stderr.close()
        if args.output:
            args.output.parent.mkdir(parents=True,exist_ok=True)
            args.output.write_text(json.dumps(dict(records=records,logs=logs),ensure_ascii=False,indent=2),encoding='utf-8')
