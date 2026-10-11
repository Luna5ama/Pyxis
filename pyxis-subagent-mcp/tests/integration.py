"""Run after cargo build: python tests/integration.py [path-to-binary]."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(sys.argv.pop(1)).resolve() if len(sys.argv) > 1 else ROOT / 'target/debug' / ('pyxis-subagent-mcp.exe' if os.name == 'nt' else 'pyxis-subagent-mcp')

class Client:
    def __init__(self, cache=None, kinds=('claude',), request_timeout=3600, **env):
        self.temp = tempfile.TemporaryDirectory(prefix='pyxis-test-')
        self.log = Path(self.temp.name) / 'events.jsonl'
        config = {}
        for kind in kinds:
            config[kind] = dict(executable=sys.executable, args=[str(ROOT/'tests/fake_harness.py'), kind], harness=kind, mode='plan', model='test-model', reasoning_effort='high', description=f'{kind} specialist', prefix_prompt='stable prefix', provider='test-provider', dsh_module_root=str(ROOT), cache=cache or dict(enabled=False),request_timeout_seconds=request_timeout)
        path = Path(self.temp.name)/'config.json'
        path.write_text(json.dumps(config), encoding='utf-8')
        self.p = subprocess.Popen([str(BINARY), str(path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', env={**os.environ, 'PYXIS_TEST_LOG':str(self.log), **env})
        self.messages, self.logs = queue.Queue(), []
        self.reader = threading.Thread(target=lambda: [self.messages.put(json.loads(line)) for line in self.p.stdout], daemon=True)
        self.reader.start()
        self.errors = threading.Thread(target=lambda: self.logs.extend(self.p.stderr.readlines()), daemon=True)
        self.errors.start()
        self.send(1, 'initialize', {'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'test','version':'1'}})
        assert 'result' in self.get(1)

    def send(self, id, method, params=None):
        message = dict(jsonrpc='2.0', method=method)
        if id is not None: message['id'] = id
        if params is not None: message['params'] = params
        self.p.stdin.write(json.dumps(message)+'\n'); self.p.stdin.flush()

    def call(self, id, prompt, kind='claude'):
        self.send(id, 'tools/call', {'name':kind,'arguments':{'prompt':prompt}})

    def get(self, id, seconds=8):
        deadline = time.monotonic()+seconds
        while time.monotonic()<deadline:
            message = self.messages.get(timeout=max(.01, deadline-time.monotonic()))
            if message.get('id') == id: return message
        raise TimeoutError(id)

    def events(self):
        if not self.log.exists(): return []
        return [json.loads(line) for line in self.log.read_text(encoding='utf-8').splitlines()]

    def wait_event(self, predicate, seconds=8):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            if predicate(self.events()): return
            time.sleep(.02)
        raise AssertionError(self.events())

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=8)
        self.reader.join(timeout=2); self.errors.join(timeout=2)
        self.p.stdout.close(); self.p.stderr.close()
        self.temp.cleanup()

class Integration(unittest.TestCase):
    def client(self, **kwargs):
        client=Client(**kwargs); self.addCleanup(client.close); return client

    def test_discovery_is_lazy_and_sessions_are_private_to_process(self):
        c=self.client()
        c.send(2,'tools/list'); tools=c.get(2)['result']['tools']
        self.assertEqual([t['name'] for t in tools],['claude'])
        self.assertFalse(c.events())
        c.call(3,'first'); a=c.get(3)['result']['structuredContent']
        c.call(4,'second'); b=c.get(4)['result']['structuredContent']
        self.assertEqual(a['session_id'], b['session_id'])
        self.assertEqual(b['result'],'second:2')
        self.assertEqual(len([e for e in c.events() if e['action']=='spawn']),1)
        c2=self.client(); c2.call(3,'new'); other=c2.get(3)['result']['structuredContent']
        self.assertNotEqual(a['session_id'], other['session_id'])

    def test_all_adapters_and_native_controls(self):
        c=self.client(kinds=('claude','codex','dsh'),cache=dict(ttl_seconds=4,compact_margin_seconds=3,use_usage_ttl=False))
        for id,kind in enumerate(('claude','codex','dsh'),2):
            c.call(id,'task',kind); self.assertFalse(c.get(id)['result']['isError'])
        c.wait_event(lambda events: sum(e.get('prompt')=='/compact' for e in events)==3)
        events=c.events()
        init=next(e for e in events if e['action']=='initialize')
        self.assertEqual(init['params']['model'],'test-model')
        thread=next(e for e in events if e['action']=='thread')
        self.assertEqual(thread['params']['sandbox'],'read-only')
        turn=next(e for e in events if e['kind']=='codex' and e.get('prompt')=='task')
        self.assertEqual(turn['params']['collaborationMode']['mode'],'plan')

    def test_busy_session_never_compacts_and_compaction_is_one_shot(self):
        c=self.client(cache=dict(ttl_seconds=6,compact_margin_seconds=4,use_usage_ttl=False))
        c.call(2,'delay:3'); c.wait_event(lambda events: any(e.get('prompt')=='delay:3' for e in events))
        c.send(3,'ping'); self.assertEqual(c.get(3)['result'],{})
        self.assertFalse(c.get(2)['result']['isError'])
        c.wait_event(lambda events: any(e.get('prompt')=='/compact' for e in events))
        time.sleep(3)
        events=c.events()
        self.assertEqual(sum(e.get('prompt')=='/compact' for e in events),1)
        self.assertEqual(len([e for e in events if e['action']=='spawn']),1)

    def test_expired_window_is_not_compacted(self):
        c=self.client(cache=dict(ttl_seconds=2,compact_margin_seconds=1,use_usage_ttl=False))
        c.call(2,'delay:3'); self.assertFalse(c.get(2)['result']['isError'])
        time.sleep(.2)
        self.assertFalse(any(e.get('prompt')=='/compact' for e in c.events()))

    def test_cancel_discards_active_process_and_next_call_starts_fresh(self):
        c=self.client(); c.call(2,'delay:20')
        c.wait_event(lambda events:any(e.get('prompt')=='delay:20' for e in events))
        c.send(None,'notifications/cancelled',{'requestId':2})
        self.assertEqual(c.get(2)['error']['code'],-32800)
        c.call(3,'next'); self.assertEqual(c.get(3)['result']['structuredContent']['result'],'next:1')
        self.assertEqual(len([e for e in c.events() if e['action']=='spawn']),2)

    def test_cancelling_a_queued_call_does_not_kill_active_session(self):
        c=self.client(); c.call(2,'delay:1')
        c.wait_event(lambda events:any(e.get('prompt')=='delay:1' for e in events))
        c.call(3,'queued')
        c.send(None,'notifications/cancelled',{'requestId':3})
        self.assertEqual(c.get(3)['error']['code'],-32800)
        self.assertFalse(c.get(2)['result']['isError'])
        c.call(3,'reuse-request-id')
        self.assertEqual(c.get(3)['result']['structuredContent']['result'],'reuse-request-id:2')
        self.assertFalse(any(e.get('prompt')=='queued' for e in c.events()))

    def test_timeout_discards_process(self):
        c=self.client(request_timeout=1); c.call(2,'delay:20')
        self.assertTrue(c.get(2)['result']['isError'])
        c.call(3,'next')
        self.assertEqual(c.get(3)['result']['structuredContent']['result'],'next:1')

    @unittest.skipUnless(os.name=='nt','Windows Job Object cleanup')
    def test_cancel_kills_descendant_process(self):
        import ctypes
        from ctypes import wintypes
        c=self.client(); c.call(2,'spawn-child')
        c.wait_event(lambda events:any(e['action']=='child' for e in events))
        child=next(e['child_pid'] for e in c.events() if e['action']=='child')
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=kernel.OpenProcess(0x100000,False,child)
        self.assertTrue(handle)
        try:
            c.send(None,'notifications/cancelled',{'requestId':2}); c.get(2)
            self.assertEqual(kernel.WaitForSingleObject(handle,3000),0)
        finally:kernel.CloseHandle(handle)

    def test_compaction_requires_boundary_and_failure_restarts(self):
        c=self.client(cache=dict(ttl_seconds=2,compact_margin_seconds=1,use_usage_ttl=False),PYXIS_TEST_NO_BOUNDARY='1')
        c.call(2,'first'); c.get(2)
        c.wait_event(lambda events:any(e.get('prompt')=='/compact' for e in events))
        time.sleep(.2)
        c.call(3,'next'); self.assertEqual(c.get(3)['result']['structuredContent']['result'],'next:1')

    def test_invalid_arguments_are_protocol_errors_and_eof_is_tool_error(self):
        c=self.client()
        c.send(2,'tools/call',{'name':'claude','arguments':{'prompt':'x','session_id':'caller'}})
        self.assertEqual(c.get(2)['error']['code'],-32602)
        c.call(3,'eof'); self.assertTrue(c.get(3)['result']['isError'])
        c.call(4,'recovery'); self.assertFalse(c.get(4)['result']['isError'])

if __name__=='__main__': unittest.main(verbosity=2)
