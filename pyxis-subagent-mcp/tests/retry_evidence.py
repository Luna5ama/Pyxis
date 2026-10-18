"""Check a real DSH failure report: bounded retries without replaying tools."""
import json
from pathlib import Path
import sys

report=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
task=report['harnesses']['dsh']['tasks'][0]
events=[e['event'].get('native',{}).get('params',{}).get('event',{}) for e in task['events']]
retries=[e['data'] for e in events if e.get('type')=='llm/retry']
started=[e['data'] for e in events if e.get('type')=='llm/retry-started']
tools=[e['data'] for e in events if e.get('type')=='tool/call']
assert len(retries)==5 and [r['retry'] for r in retries]==[1,2,3,4,5]
assert len(started)==5
assert all(r['failure']['code']=='INVALID_REQUEST' and r['maxRetries']==5 for r in retries)
assert len({(r['retryId'],r['turn'],r['step']) for r in retries})==1
assert len(tools)==1 and tools[0]['name']=='read', 'Already completed tools must not be replayed'
assert task['status']=='failed' and '400' in task['error'], 'Retry exhaustion must remain visible'
print('Native 400 retries verified: five retries in one turn/step, one read invocation, exhaustion visible')
