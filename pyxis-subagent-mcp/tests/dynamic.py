"""Session/control integration tests against concurrent native protocol peers."""
import json
import os
from pathlib import Path
import time
import unittest
from integration import Client, ROOT


class Dynamic(unittest.TestCase):
    def client(self, **kwargs):
        client = Client(dynamic=True, kinds=('claude','codex','dsh'), **kwargs)
        self.addCleanup(client.close)
        client.seq = 100
        return client

    def tool(self, client, name, **args):
        client.seq += 1
        client.send(client.seq, 'tools/call', dict(name=name, arguments=args))
        result = client.get(client.seq, seconds=35)['result']
        self.assertFalse(result['isError'], result)
        return result['structuredContent']

    def error(self, client, name, **args):
        client.seq += 1
        client.send(client.seq, 'tools/call', dict(name=name, arguments=args))
        self.assertTrue(client.get(client.seq)['result']['isError'])

    def start(self, client, kind='claude', prompt='hello', **args):
        return self.tool(client,'agent_start',harness=kind,cwd=client.temp.name,prompt=prompt,**args)

    def wait(self, client, task):
        return self.tool(client,'agent_wait',task_ids=[task['task_id']],timeout_seconds=5)['tasks'][0]

    def test_discovery_models_idempotency_and_configuration(self):
        c = self.client()
        c.send(2,'tools/list')
        self.assertEqual(len(c.get(2)['result']['tools']),10)
        for kind in ('claude','codex','dsh'):
            models = self.tool(c,'agent_models',harness=kind,cwd=c.temp.name)
            self.assertFalse(models['catalog_is_entitlement_check'])
        a = self.start(c,idempotency_key='first')
        b = self.start(c,idempotency_key='first')
        self.assertEqual(a,b)
        self.error(c,'agent_start',harness='claude',cwd=c.temp.name,prompt='different',idempotency_key='first')
        self.error(c,'agent_start',harness='claude',cwd=c.temp.name,prompt='x',executable='other')
        self.error(c,'agent_start',harness='codex',cwd=c.temp.name,prompt='x',provider='other')

    def test_fifo_different_cwd_and_independent_sessions(self):
        c = self.client()
        a = self.start(c,prompt='delay:.3')
        b = self.tool(c,'agent_send',session_id=a['session_id'],prompt='second')
        d = self.tool(c,'agent_send',session_id=a['session_id'],prompt='third')
        other = Path(c.temp.name)/'other'
        other.mkdir()
        e = self.tool(c,'agent_start',harness='claude',cwd=str(other),prompt='isolated')
        self.assertIn(':1:',self.wait(c,e)['result']['result'])
        self.assertIn(':3:',self.wait(c,d)['result']['result'])
        self.assertIn(':2:',self.wait(c,b)['result']['result'])
        self.assertIn(str(other),self.wait(c,e)['result']['result'])
        prompts = [e['prompt'] for e in c.events() if e['action']=='prompt' and e['cwd']==c.temp.name]
        self.assertEqual(prompts,['delay:.3','second','third'])

    def test_cross_backend_concurrency_and_capacity(self):
        c = self.client(limits=dict(active_tasks=2,sessions=3,queued_tasks=3))
        tasks = [self.start(c,kind,prompt='delay:.6') for kind in ('claude','codex','dsh')]
        self.error(c,'agent_start',harness='claude',cwd=c.temp.name,prompt='overflow')
        time.sleep(.2)
        states = [self.tool(c,'agent_status',task_id=t['task_id'])['status'] for t in tasks]
        self.assertEqual(states.count('running'),2)
        self.assertEqual(states.count('queued'),1)
        for task in tasks:
            self.assertEqual(self.wait(c,task)['status'],'completed')

    def test_all_three_allow_deny_and_structured_questions(self):
        c = self.client()
        for kind in ('claude','codex','dsh'):
            for allowed in (False,True):
                path = Path(c.temp.name)/f'{kind}-{allowed}.txt'
                task = self.start(c,kind,prompt=f'approve:{path}')
                status = self.wait(c,task)
                self.assertEqual(status['status'],'waiting_input')
                interaction = status['pending_interactions'][0]['interaction_id']
                response = (dict(behavior='allow') if allowed else dict(behavior='deny',message='denied')) if kind=='claude' else (dict(decision='accept' if allowed else 'decline') if kind=='codex' else dict(outcome='allowed-once' if allowed else 'rejected'))
                self.tool(c,'agent_respond',interaction_id=interaction,response=response)
                self.assertEqual(self.wait(c,task)['status'],'completed')
                self.assertEqual(path.exists(),allowed)
                self.error(c,'agent_respond',interaction_id=interaction,response=response)
            task = self.start(c,kind,prompt='questions')
            status = self.wait(c,task)
            interaction = status['pending_interactions'][0]['interaction_id']
            self.error(c,'agent_respond',interaction_id=interaction,response={})
            answer = dict(answers={'Pick a color':'blue'}) if kind=='claude' else (dict(answers={'color':dict(answers=['blue'])}) if kind=='codex' else dict(answers=[dict(id='color',selected=['blue'])]))
            self.tool(c,'agent_respond',interaction_id=interaction,response=answer)
            self.assertIn('blue',self.wait(c,task)['result']['result'])

    def test_steer_cancel_and_continue_keep_native_session(self):
        c = self.client()
        for kind in ('claude','codex','dsh'):
            task = self.start(c,kind,prompt='delay:.8')
            c.wait_event(lambda events:any(e['action']=='prompt' and e['kind']==kind and e['prompt']=='delay:.8' for e in events))
            result = self.tool(c,'agent_send',session_id=task['session_id'],action='steer',task_id=task['task_id'],prompt='corrected')
            self.assertTrue(result['applied'],result)
            self.assertIn('corrected',self.wait(c,task)['result']['result'])
            late = self.tool(c,'agent_send',session_id=task['session_id'],action='steer',task_id=task['task_id'],prompt='late')
            self.assertFalse(late['applied'])
            current = self.tool(c,'agent_send',session_id=task['session_id'],prompt='delay:10')
            c.wait_event(lambda events:any(e['action']=='prompt' and e['kind']==kind and e['prompt']=='delay:10' for e in events))
            self.assertTrue(self.tool(c,'agent_cancel',task_id=current['task_id'])['cancelled'])
            self.assertEqual(self.wait(c,current)['status'],'cancelled')
            resumed = self.tool(c,'agent_send',session_id=task['session_id'],prompt='resumed')
            self.assertEqual(self.wait(c,resumed)['status'],'completed')
            self.assertEqual(sum(e['action']=='spawn' and e['kind']==kind for e in c.events()),1)

    def test_queue_cancel_deadline_and_wait_cancellation(self):
        c = self.client()
        task = self.start(c,prompt='delay:2')
        queued = self.tool(c,'agent_send',session_id=task['session_id'],prompt='never-start',timeout_seconds=1)
        cancelled = self.tool(c,'agent_send',session_id=task['session_id'],prompt='cancelled')
        self.tool(c,'agent_cancel',task_id=cancelled['task_id'])
        c.send(40,'tools/call',dict(name='agent_wait',arguments=dict(task_ids=[task['task_id']],timeout_seconds=60)))
        c.send(None,'notifications/cancelled',dict(requestId=40))
        self.assertEqual(c.get(40)['error']['code'],-32800)
        time.sleep(1.2)
        self.assertEqual(self.tool(c,'agent_status',task_id=queued['task_id'])['status'],'failed')
        self.assertEqual(self.wait(c,task)['status'],'completed')
        self.assertFalse(any(e.get('prompt') in ('never-start','cancelled') for e in c.events()))

    def test_result_background_compaction_and_retention(self):
        c = self.client(limits=dict(retained_tasks=2,retained_events=4))
        task = self.start(c,prompt='background')
        time.sleep(.1)
        self.assertEqual(self.tool(c,'agent_status',task_id=task['task_id'])['status'],'running')
        self.assertNotEqual(self.wait(c,task)['result']['result'],'intermediate')
        compact = self.tool(c,'agent_compact',session_id=task['session_id'])
        self.assertTrue(self.wait(c,compact)['result']['compacted'])
        next_task = self.tool(c,'agent_send',session_id=task['session_id'],prompt='after-compact')
        self.assertEqual(self.wait(c,next_task)['status'],'completed')
        self.assertEqual(self.tool(c,'agent_status',task_id=task['task_id'])['status'],'unknown_or_expired')
        status = self.tool(c,'agent_status',task_id=next_task['task_id'],cursor=0,event_limit=1)
        self.assertTrue(status['cursor_expired'])
        self.assertTrue(status['has_more'])
        self.assertLess(status['cursor'],status['latest_cursor'])

    def test_invalid_session_does_not_run_queued_work_and_idle_reaps(self):
        c = self.client(limits=dict(idle_seconds=1))
        task = self.start(c,prompt='eof')
        queued = self.tool(c,'agent_send',session_id=task['session_id'],prompt='must-not-run')
        self.assertEqual(self.wait(c,task)['status'],'failed')
        self.assertEqual(self.wait(c,queued)['status'],'failed')
        self.assertTrue(self.tool(c,'agent_status',session_id=task['session_id'])['invalid'])
        self.error(c,'agent_send',session_id=task['session_id'],prompt='reuse')
        time.sleep(1.3)
        self.assertEqual(self.tool(c,'agent_list')['sessions'],[])

    def test_nested_pyxis_exposes_no_execution_tools(self):
        c = self.client(PYXIS_SUBAGENT_NESTED='1')
        c.send(2,'tools/list')
        self.assertEqual(c.get(2)['result']['tools'],[])
        c.send(3,'tools/call',dict(name='agent_start',arguments={}))
        self.assertEqual(c.get(3)['error']['code'],-32602)

    def test_legacy_and_managed_tasks_share_resource_limits(self):
        c=self.client(legacy=True,limits=dict(active_tasks=1,sessions=2,queued_tasks=1))
        task=self.start(c,prompt='delay:.7')
        c.wait_event(lambda events:any(e.get('prompt')=='delay:.7' for e in events))
        c.call(500,'delay:.2',kind='legacy')
        time.sleep(.06)
        self.assertEqual(sum(e['action']=='spawn' for e in c.events()),1)
        self.error(c,'agent_send',session_id=task['session_id'],prompt='queue-overflow')
        self.assertFalse(c.get(500)['result']['isError'])
        self.assertEqual(self.wait(c,task)['status'],'completed')
        self.error(c,'agent_start',harness='dsh',cwd=c.temp.name,prompt='session-overflow')

    def test_native_stop_timeout_invalidates_and_fails_queue(self):
        c=self.client()
        task=self.start(c,prompt='ignore-cancel')
        c.wait_event(lambda events:any(e.get('prompt')=='ignore-cancel' for e in events))
        queued=self.tool(c,'agent_send',session_id=task['session_id'],prompt='never-run')
        result=self.tool(c,'agent_cancel',task_id=task['task_id'])
        self.assertTrue(result['session_invalid'])
        self.assertEqual(self.wait(c,task)['status'],'failed')
        self.assertEqual(self.wait(c,queued)['status'],'failed')
        self.error(c,'agent_send',session_id=task['session_id'],prompt='reuse-invalid')
        self.assertFalse(any(e.get('prompt')=='never-run' for e in c.events()))

    def test_native_steer_completion_race_does_not_reach_next_turn(self):
        c=self.client()
        for kind in ('codex','dsh'):
            task=self.start(c,kind,prompt='steer-race')
            c.wait_event(lambda events:any(e.get('prompt')=='steer-race' and e['kind']==kind for e in events))
            result=self.tool(c,'agent_send',session_id=task['session_id'],task_id=task['task_id'],action='steer',prompt='must-not-leak')
            self.assertFalse(result['applied'])
            self.assertEqual(self.wait(c,task)['status'],'completed')
            next_task=self.tool(c,'agent_send',session_id=task['session_id'],prompt='next')
            self.assertIn('next:',self.wait(c,next_task)['result']['result'])
        self.assertFalse(any(e.get('prompt')=='must-not-leak' for e in c.events()))

    def test_interaction_ids_are_not_recycled_within_one_task(self):
        c=self.client()
        for kind in ('claude','codex','dsh'):
            task=self.start(c,kind,prompt='questions-twice')
            first=self.wait(c,task)['pending_interactions'][0]['interaction_id']
            answer=dict(answers={'Pick a color':'blue'}) if kind=='claude' else (dict(answers={'color':dict(answers=['blue'])}) if kind=='codex' else dict(answers=[dict(id='color',selected=['blue'])]))
            self.tool(c,'agent_respond',interaction_id=first,response=answer)
            second=self.wait(c,task)['pending_interactions'][0]['interaction_id']
            self.assertNotEqual(first,second)
            self.error(c,'agent_respond',interaction_id=first,response=answer)
            self.tool(c,'agent_respond',interaction_id=second,response=answer)
            self.assertEqual(self.wait(c,task)['status'],'completed')

    def test_timeout_while_waiting_for_approval_never_grants_consent(self):
        c=self.client()
        path=Path(c.temp.name)/'denied-on-timeout.txt'
        task=self.start(c,prompt=f'approve:{path}',timeout_seconds=1)
        interaction=self.wait(c,task)['pending_interactions'][0]['interaction_id']
        time.sleep(1.2)
        self.assertEqual(self.wait(c,task)['status'],'failed')
        self.assertFalse(path.exists())
        self.error(c,'agent_respond',interaction_id=interaction,response=dict(behavior='allow'))
        next_task=self.tool(c,'agent_send',session_id=task['session_id'],prompt='valid-context')
        self.assertEqual(self.wait(c,next_task)['status'],'completed')

    def test_manual_compaction_preserves_fifo_and_large_results_are_bounded(self):
        c=self.client()
        task=self.start(c,prompt='delay:.3')
        compact=self.tool(c,'agent_compact',session_id=task['session_id'])
        later=self.tool(c,'agent_send',session_id=task['session_id'],prompt='large-output')
        result=self.wait(c,later)
        self.assertTrue(result['result']['result_truncated'])
        self.assertLess(len(json.dumps(result)),400000)
        self.assertTrue(self.wait(c,compact)['result']['compacted'])
        self.assertEqual([e['prompt'] for e in c.events() if e['action']=='prompt'],['delay:.3','/compact','large-output'])

    def test_close_cancels_queue_before_any_followup_can_start(self):
        c=self.client()
        task=self.start(c,prompt='delay:10')
        c.wait_event(lambda events:any(e.get('prompt')=='delay:10' for e in events))
        later=self.tool(c,'agent_send',session_id=task['session_id'],prompt='never-after-close')
        self.tool(c,'agent_close',session_id=task['session_id'])
        self.assertEqual(self.wait(c,later)['status'],'cancelled')
        self.assertFalse(any(e.get('prompt')=='never-after-close' for e in c.events()))

    def test_native_default_effort_and_unlisted_exact_model_are_preserved(self):
        c=self.client()
        task=self.start(c,'claude',effort=None)
        self.assertIsNone(self.wait(c,task)['effective_config']['effort'])
        unknown=self.start(c,'codex',model='exact-unlisted-model',effort='max')
        result=self.wait(c,unknown)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['effective_config']['model'],'exact-unlisted-model')
        rejected=self.start(c,'codex',effort='unsupported')
        self.assertEqual(self.wait(c,rejected)['status'],'failed')

    def test_automatic_compaction_is_idle_and_one_shot(self):
        c=self.client(cache=dict(ttl_seconds=3,compact_margin_seconds=2,use_usage_ttl=False))
        task=self.start(c,prompt='delay:2')
        time.sleep(1.2)
        self.assertFalse(any(e.get('prompt')=='/compact' for e in c.events()))
        self.wait(c,task)
        c.wait_event(lambda events:any(e.get('prompt')=='/compact' for e in events))
        time.sleep(1.2)
        self.assertEqual(sum(e.get('prompt')=='/compact' for e in c.events()),1)

    @unittest.skipUnless(os.name=='nt','Windows Job Object ownership acceptance')
    def test_disconnect_cleans_native_process_trees(self):
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        c=self.client()
        for kind in ('claude','codex','dsh'):self.start(c,kind,prompt='spawn-child')
        c.wait_event(lambda events:sum(e['action']=='child' for e in events)==3)
        handles=[kernel.OpenProcess(0x100000,False,e['child_pid']) for e in c.events() if e['action']=='child']
        try:
            self.assertTrue(all(handles))
            c.close()
            for handle in handles:self.assertEqual(kernel.WaitForSingleObject(handle,3000),0)
        finally:
            for handle in handles:
                if handle:kernel.CloseHandle(handle)

    def test_codex_native_cancel_cleans_its_remaining_background_terminal(self):
        c=self.client()
        task=self.start(c,'codex',prompt='spawn-child')
        c.wait_event(lambda events:any(e['action']=='child' for e in events))
        result=self.tool(c,'agent_cancel',task_id=task['task_id'])
        self.assertTrue(result['native_stop_confirmed'])
        self.assertTrue(any(e['action']=='terminal-stop' for e in c.events()))
        next_task=self.tool(c,'agent_send',session_id=task['session_id'],prompt='same-context')
        self.assertEqual(self.wait(c,next_task)['status'],'completed')


if __name__=='__main__':
    unittest.main(verbosity=2)
