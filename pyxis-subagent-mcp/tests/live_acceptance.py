"""Opt-in synthetic acceptance against installed CLIs; never touches repo source.

python tests/live_acceptance.py --config config.candidate.local.json --binary ...
    --fixtures ABSOLUTE_EMPTY_PARENT --models-only
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
import uuid

TERMINAL = {'completed','failed','cancelled'}


class Acceptance:
    def __init__(self, client, root, kind, report):
        self.client, self.root, self.kind, self.report = client, root, kind, report
        self.sessions = []

    def start(self, cwd, prompt, **overrides):
        mode=overrides.pop('mode','auto')
        task = self.client.tool('agent_start',harness=self.kind,cwd=str(cwd),prompt=self.scope(cwd)+prompt,mode=mode,timeout_seconds=180,**overrides)
        self.sessions.append(task['session_id'])
        return task

    def scope(self, cwd):
        return f'Authorized synthetic harness acceptance test. Work only in this synthetic directory: {cwd}. Use native tools; do not delegate, use MCP tools, inspect other projects, or access the network. All shell commands must use PowerShell 7 without a profile. '

    def answer(self, interaction, allow=True):
        request = interaction['request']
        method = interaction['method']
        if self.kind == 'claude':
            tool = request['request']['tool_name']
            if tool == 'AskUserQuestion':
                answer = dict(answers={q['question']:'blue' for q in request['request']['input']['questions']})
            elif tool == 'ExitPlanMode':
                answer=dict(behavior='deny',message='This session has a fixed plan mode. Do not exit it. Use AskUserQuestion to ask the requested color question in this session.')
            elif allow:
                if tool not in {'Read','Write','Edit','Bash','PowerShell','Glob','Grep'}:
                    raise RuntimeError(f'Unexpected approval tool in synthetic test: {tool}')
                answer = dict(behavior='allow')
            else:
                answer = dict(behavior='deny',message='Synthetic denial test: do not perform this operation or try another tool.')
        elif method == 'item/tool/requestUserInput':
            answer = dict(answers={q['id']:dict(answers=['blue']) for q in request['params']['questions']})
        elif method == 'pyxis/interaction/questions':
            answer = dict(answers=[dict(id=q['id'],selected=['blue']) for q in request['params']['questions']])
        elif method == 'pyxis/interaction/approval':
            answer = dict(outcome='allowed-once' if allow else 'rejected')
        elif method in {'item/commandExecution/requestApproval','item/fileChange/requestApproval'}:
            decisions=request['params'].get('availableDecisions', ['accept','decline'])
            answer = dict(decision='accept' if allow else 'decline' if 'decline' in decisions else 'cancel')
        elif method == 'item/permissions/requestApproval':
            answer = dict(permissions=request['params']['permissions'] if allow else {},scope='turn')
        else:
            raise RuntimeError(f'Unexpected interaction: {method}')
        self.report.setdefault('interactions',[]).append(dict(method=method,request=request,response=answer))
        self.client.tool('agent_respond',interaction_id=interaction['interaction_id'],response=answer)

    def poll(self, task, allow=True):
        result = self.client.tool('agent_wait',task_ids=[task['task_id']],timeout_seconds=1)['tasks'][0]
        for interaction in result['pending_interactions']:
            self.answer(interaction,allow)
        return result

    def done(self, task, allow=True):
        end = time.monotonic()+190
        while time.monotonic() < end:
            status = self.poll(task,allow)
            if status['status'] in TERMINAL:
                events=list(status['events'])
                page=status
                while page['has_more']:
                    page=self.client.tool('agent_status',task_id=task['task_id'],cursor=page['cursor'],event_limit=128)
                    events.extend(page['events'])
                status['events']=events
                self.report.setdefault('tasks',[]).append(status)
                if status['status'] != 'completed' and not (not allow and status['status']=='cancelled'):
                    raise RuntimeError(f"Task {task['task_id']} {status['status']}: {status.get('error')}")
                return status
        raise TimeoutError(task)

    def until_file(self, task, path):
        end = time.monotonic()+140
        while not path.exists() and time.monotonic() < end:
            status = self.poll(task)
            if status['status'] in TERMINAL:
                raise RuntimeError(f'Agent ended before executing long command: {status["status"]}: {status.get("error") or status.get("result")}')
        if not path.exists():
            raise TimeoutError(path)

    def directory(self, name):
        directory = self.root/name
        directory.mkdir()
        return directory

    def smoke(self):
        cwd = self.directory('smoke')
        token = uuid.uuid4().hex
        (cwd/'seed.txt').write_text(token,encoding='utf-8')
        task = self.start(cwd,'Read seed.txt. Create written.txt with its exact contents. Then run a native shell command which reads written.txt and writes the same contents to command.txt. If the native sandbox helper fails, request native approval for exactly that synthetic command. Report the token after actually verifying both files.')
        status = self.done(task)
        assert (cwd/'written.txt').read_text(encoding='utf-8-sig').strip() == token
        assert (cwd/'command.txt').read_text(encoding='utf-8-sig').strip() == token
        assert token in status['result']['result']
        compact = self.client.tool('agent_compact',session_id=task['session_id'],timeout_seconds=180)
        self.done(compact)
        followup = self.client.tool('agent_send',session_id=task['session_id'],prompt='Read seed.txt again and report its exact token after compaction.')
        assert token in self.done(followup)['result']['result']
        self.report['smoke'] = 'passed'

    def probe(self):
        task=self.start(self.directory('probe'),'Reply exactly PONG. Do not use tools.')
        assert 'PONG' in self.done(task)['result']['result']
        self.report['probe']='passed'

    def write(self):
        cwd=self.directory('write')
        task=self.start(cwd,'Use native tools to create probe.txt containing PONG, read it back, and report its contents.')
        self.done(task)
        assert (cwd/'probe.txt').read_text(encoding='utf-8-sig').strip()=='PONG'
        self.report['write']='passed'

    def native_cycle(self):
        cwd=self.directory('native-cycle')
        token=uuid.uuid4().hex
        task=self.start(cwd,f'Remember the test token {token}. Use native tools to create probe.txt containing PONG, read it back, and report its contents.')
        self.done(task)
        assert (cwd/'probe.txt').read_text(encoding='utf-8-sig').strip()=='PONG'
        command=self.client.tool('agent_send',session_id=task['session_id'],prompt="Use the native pwsh tool to run Set-Content -LiteralPath command.txt -Value CHECKED -NoNewline, then report completion. This is an authorized synthetic file in the current working directory.")
        status=self.done(command)
        assert (cwd/'command.txt').read_text(encoding='utf-8-sig').strip()=='CHECKED'
        assert any(e['event'].get('native',{}).get('params',{}).get('event',{}).get('type')=='tool/call' and e['event']['native']['params']['event']['data'].get('name')=='pwsh' for e in status['events'])
        compact=self.client.tool('agent_compact',session_id=task['session_id'],timeout_seconds=180)
        self.done(compact)
        later=self.client.tool('agent_send',session_id=task['session_id'],prompt='Read probe.txt and command.txt and report their two values. Do not use a shell or any other tools.')
        final=self.done(later)['result']['result']
        assert 'PONG' in final and 'CHECKED' in final
        self.report['native_commands_compaction_followup']='passed'

    def concurrency(self):
        tokens = [uuid.uuid4().hex,uuid.uuid4().hex]
        tasks = [self.start(self.directory(f'concurrent-{i}'),f'Remember the secret test token {token} in this conversation. Write it to token.txt and reply with it.') for i,token in enumerate(tokens)]
        for task,token in zip(tasks,tokens):
            assert token in self.done(task)['result']['result']
        followups = [self.client.tool('agent_send',session_id=task['session_id'],prompt='Without using tools or reading files, return the test token I asked you to remember in this conversation.') for task in tasks]
        for i,(task,token) in enumerate(zip(followups,tokens)):
            text = self.done(task)['result']['result']
            assert token in text and tokens[1-i] not in text
        self.report['concurrency_and_context'] = 'passed'

    def questions(self):
        cwd = self.directory('questions')
        task = self.start(cwd,'Use your native structured user-question tool (AskUserQuestion, request_user_input, or ask_user_question) to ask me to pick a color, with exactly two options labeled blue and red. Do not guess or answer the question yourself. Wait for my answer, then report the chosen color.',mode='plan')
        before = len(self.report.get('interactions',[]))
        result=self.done(task)
        interactions = self.report.get('interactions',[])[before:]
        assert any(e['method'] in ('item/tool/requestUserInput','pyxis/interaction/questions') or e['request'].get('request',{}).get('tool_name')=='AskUserQuestion' for e in interactions), 'No native question was relayed'
        assert 'blue' in result['result']['result'].lower()
        self.report['questions'] = 'passed'

    def controls(self):
        cwd = self.directory('controls')
        helper = cwd/'long_task.py'
        helper.write_text("import os,sys,time\nfrom pathlib import Path\ntag=sys.argv[1]\nPath(tag+'.pid').write_text(str(os.getpid()))\ntime.sleep(25)\nPath(tag+'-late.txt').write_text('finished')\n",encoding='utf-8')
        def command(tag):
            return f"Run the foreground command & '{os.sys.executable}' '{helper}' '{tag}'. Do not detach it; wait for it to finish, then report. If native sandbox helper initialization prevents execution, request native approval for exactly this same synthetic command and retry; do not skip it."
        task = self.start(cwd,command('steer'))
        self.until_file(task,cwd/'steer.pid')
        steer = self.client.tool('agent_send',session_id=task['session_id'],task_id=task['task_id'],action='steer',prompt='Correction: write steered.txt containing corrected. Then report corrected and finish this task.')
        assert steer['applied'] is True, steer
        self.done(task)
        assert (cwd/'steered.txt').read_text(encoding='utf-8-sig').strip() == 'corrected'
        cancel_task = self.client.tool('agent_send',session_id=task['session_id'],prompt=self.scope(cwd)+command('cancel'))
        self.until_file(cancel_task,cwd/'cancel.pid')
        child = int((cwd/'cancel.pid').read_text())
        handle = None
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL('kernel32',use_last_error=True)
            kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
            kernel.OpenProcess.restype=wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
            kernel.CloseHandle.argtypes=[wintypes.HANDLE]
            handle=kernel.OpenProcess(0x100000,False,child)
            assert handle, f'Cannot inspect synthetic child PID {child}'
        try:
            result = self.client.tool('agent_cancel',task_id=cancel_task['task_id'])
            assert result.get('native_stop_confirmed'), result
            if handle:
                assert kernel.WaitForSingleObject(handle,10000)==0, 'Native cancellation left synthetic command running'
        finally:
            if handle: kernel.CloseHandle(handle)
        status = self.client.tool('agent_status',task_id=cancel_task['task_id'])
        assert status['status']=='cancelled', status
        resumed = self.client.tool('agent_send',session_id=task['session_id'],prompt='Write resumed.txt containing resumed, then report resumed.')
        self.done(resumed)
        assert (cwd/'resumed.txt').exists()
        assert not (cwd/'cancel-late.txt').exists(), 'Cancelled command performed its delayed write'
        self.report['steer_cancel_and_resume'] = dict(status='passed',child_pid=child)

    def approvals(self):
        for allow in (True,False):
            cwd=self.directory(f'approval-{allow}')
            if self.kind=='claude':
                settings=cwd/'.claude'
                settings.mkdir()
                (settings/'settings.json').write_text(json.dumps(dict(permissions=dict(ask=['Write','Edit','Bash','PowerShell']))),encoding='utf-8')
                prompt='Use the native Write tool to create approval.txt containing approved. A native permission rule asks for this write. If approval is denied, stop without trying another tool or writing the file.'
            elif self.kind=='dsh':
                prompt='Use the native write tool to create approval.txt containing approved. This synthetic test runtime has read-only standing policy. Try the exact write once normally. If denied by the native sandbox, request native approval to retry that exact write with sandbox_permissions=workspace-write and a justification. If approval is rejected, stop without retrying or using another tool.'
            else:
                prompt='Use your native shell execution tool to write approval.txt containing approved. Explicitly request native approval for this command using sandbox_permissions=require_escalated. This synthetic task intentionally tests the host approval callback. If denied, stop without retrying or writing the file.'
            before=len(self.report.get('interactions',[]))
            task=self.start(cwd,prompt)
            self.done(task,allow=allow)
            interactions=self.report.get('interactions',[])[before:]
            assert interactions, 'Native backend did not issue an approval request'
            assert (cwd/'approval.txt').exists()==allow, 'Approval result did not control the actual write'
            self.report[f'approval_{"allow" if allow else "deny"}']='passed'

    def close(self):
        for session in self.sessions:
            try: self.client.tool('agent_close',session_id=session)
            except RuntimeError: pass


class Mcp:
    def __init__(self, binary, config):
        self.process = subprocess.Popen([str(binary),str(config)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')
        self.messages = queue.Queue()
        self.stderr = []
        self.responses = {}
        self.seq = 0
        self.reader = threading.Thread(target=lambda:[self.messages.put(json.loads(line)) for line in self.process.stdout],daemon=True)
        self.errors = threading.Thread(target=lambda:self.stderr.extend(self.process.stderr.readlines()),daemon=True)
        self.reader.start(); self.errors.start()
        self.rpc('initialize',dict(protocolVersion='2025-11-25',capabilities={},clientInfo=dict(name='pyxis-synthetic-acceptance',version='2')))

    def rpc(self, method, params, timeout=70):
        self.seq += 1
        req_id = self.seq
        self.process.stdin.write(json.dumps(dict(jsonrpc='2.0',id=req_id,method=method,params=params))+'\n')
        self.process.stdin.flush()
        end = time.monotonic()+timeout
        while req_id not in self.responses:
            try:
                value = self.messages.get(timeout=min(.5,max(.01,end-time.monotonic())))
            except queue.Empty:
                if self.process.poll() is not None or time.monotonic()>=end:
                    raise TimeoutError(f'{method}: {"".join(self.stderr)[-2000:]}')
                continue
            if 'id' in value:
                self.responses[value['id']] = value
        value = self.responses.pop(req_id)
        if 'error' in value:
            raise RuntimeError(value['error'])
        return value['result']

    def tool(self, name, **arguments):
        value = self.rpc('tools/call',dict(name=name,arguments=arguments))
        if value.get('isError'):
            raise RuntimeError(value['content'])
        return value['structuredContent']

    def close(self):
        self.process.stdin.close()
        self.process.wait(timeout=15)
        self.reader.join(2); self.errors.join(2)
        self.process.stdout.close(); self.process.stderr.close()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--binary',type=Path,required=True)
    parser.add_argument('--fixtures',type=Path,required=True)
    parser.add_argument('--models-only',action='store_true')
    parser.add_argument('--harness',choices=['claude','codex','dsh','all'],default='all')
    parser.add_argument('--case',choices=['probe','write','native_cycle','smoke','concurrency','questions','controls','approvals','core','all'],default='all')
    parser.add_argument('--effort',help='Explicit test-only effort override; never changes runtime defaults')
    parser.add_argument('--native-effort',action='store_true',help='Explicitly select null/native effort for this test only')
    args=parser.parse_args()
    assert args.fixtures.is_absolute()
    args.fixtures.mkdir(parents=True,exist_ok=True)
    root=args.fixtures.resolve() / str(uuid.uuid4())
    root.mkdir(parents=True)
    report=dict(binary=str(args.binary.resolve()),sha256=hashlib.sha256(args.binary.read_bytes()).hexdigest(),fixtures=str(root),harnesses={})
    client=Mcp(args.binary.resolve(),args.config.resolve())
    try:
        report['tools']=[tool['name'] for tool in client.rpc('tools/list',{})['tools']]
        for kind in (['claude','codex','dsh'] if args.harness=='all' else [args.harness]):
            fixture=root/kind
            fixture.mkdir()
            models=client.tool('agent_models',harness=kind,cwd=str(fixture),refresh=True)
            report['harnesses'][kind]=dict(models=models)
            print(json.dumps(dict(harness=kind,model_discovery='passed')),flush=True)
            if not args.models_only:
                acceptance = Acceptance(client,fixture,kind,report['harnesses'][kind])
                if args.effort or args.native_effort:
                    original_start=acceptance.start
                    acceptance.start=lambda cwd,prompt,**kw:original_start(cwd,prompt,effort=None if args.native_effort else args.effort,**kw)
                    report['harnesses'][kind]['explicit_test_effort']=args.effort
                try:
                    cases=['native_cycle' if kind=='dsh' else 'smoke','concurrency','questions','controls']
                    if args.case=='all':cases+=['approvals']
                    if args.case not in ('all','core'):cases=[args.case]
                    for case in cases:
                        getattr(acceptance,case)()
                        print(json.dumps(dict(harness=kind,case=case,status='passed')),flush=True)
                finally:
                    acceptance.close()
    except Exception as error:
        report['error']=str(error)
        raise
    finally:
        client.close()
        (root/'report.json').write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
        print(str(root/'report.json'),flush=True)


if __name__=='__main__':
    main()
