"""Opt-in native idle/resume acceptance; uses isolated synthetic conversations."""
import argparse
import json
from pathlib import Path
import time
import uuid
from live_acceptance import Mcp


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--harness', choices=('claude', 'codex', 'dsh', 'all'), default='all')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1] / 'target' / 'test-fixtures' / str(uuid.uuid4())
    root.mkdir(parents=True)
    config = json.loads(args.config.read_text(encoding='utf-8-sig'))
    config.setdefault('limits', {})['idle_seconds'] = 2
    for runtime in config['runtimes'].values():
        runtime['cache'] = dict(enabled=False)
    path = root / 'config.json'
    path.write_text(json.dumps(config), encoding='utf-8')
    client = Mcp(args.binary.resolve(), path)
    report = {}
    try:
        def done(task):
            end = time.monotonic() + 180
            while time.monotonic() < end:
                status = client.tool('agent_wait', task_ids=[task['task_id']], timeout_seconds=1)['tasks'][0]
                assert not status['pending_interactions'], status
                if status['status'] in ('completed', 'failed', 'cancelled'):
                    assert status['status'] == 'completed', status
                    return status
            raise TimeoutError(task)
        for kind in (('claude', 'codex', 'dsh') if args.harness == 'all' else (args.harness,)):
            cwd = root / kind
            cwd.mkdir()
            token = uuid.uuid4().hex
            try:
                task = client.tool('agent_start', harness=kind, cwd=str(cwd),
                    prompt=f'Synthetic conversation test. Remember token {token}. Reply OK. Do not use tools or delegate.', timeout_seconds=180)
                first = done(task)
                end = time.monotonic() + 20
                while time.monotonic() < end:
                    status = client.tool('agent_status', task_id=task['task_id'], event_limit=128)
                    events = list(status['events'])
                    page = status
                    while page['has_more']:
                        page = client.tool('agent_status', task_id=task['task_id'], cursor=page['cursor'], event_limit=128)
                        events.extend(page['events'])
                    if any(e['event'].get('type') == 'session_suspended' for e in events):
                        break
                    time.sleep(.1)
                else:
                    (root / f'{kind}-status.json').write_text(json.dumps(status, indent=2), encoding='utf-8')
                    raise RuntimeError('No session_suspended event; status saved')
                later = client.tool('agent_send', session_id=task['session_id'],
                    prompt='Reply with the token I asked you to remember. Do not use tools or delegate.', timeout_seconds=180)
                second = done(later)
                assert first['result']['session_id'] == second['result']['session_id'], second
                assert token in second['result']['result'], second
                report[kind] = dict(status='passed', native_session_id=second['result']['session_id'])
                client.tool('agent_close', session_id=task['session_id'])
            except Exception as error:
                report[kind] = dict(status='failed', error=str(error))
            print(json.dumps({kind: report[kind]}), flush=True)
    finally:
        client.close()
        (root / 'stderr.log').write_text(''.join(client.stderr), encoding='utf-8')
        (root / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(root / 'report.json', flush=True)
    assert all(v['status'] == 'passed' for v in report.values()), report


if __name__ == '__main__':
    main()
