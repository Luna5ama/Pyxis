"""Reversible installation test with synthetic configuration and skill paths."""
import json
from pathlib import Path
import subprocess
import sys
from integration import ROOT, BINARY, Fixture

fixture=Fixture()
manifest_path=None
try:
    root=Path(fixture.name)
    runtime=root/'runtime.json'
    runtime.write_text(json.dumps(dict(version=2,runtimes={'claude':dict(executable=sys.executable,args=[str(ROOT/'tests/fake_dynamic.py'),'claude'],model='test-model')},specialists={})),encoding='utf-8')
    config=root/'config.toml'
    original='title = "synthetic configuration"\r\n\r\n[mcp_servers.pyxis-subagent-mcp]\r\ncommand = '+json.dumps(str(ROOT/'target/debug/pyxis-subagent-mcp.exe'))+'\r\nargs = ["old-config"]\r\nenabled_tools = ["old-tool"]\r\ntool_timeout_sec = 3700\r\n\r\n[unrelated]\r\nkeep = "exactly"\r\n'
    config.write_bytes(original.encode())
    skill=root/'SKILL.md';skill.write_text('previous skill',encoding='utf-8')
    command=[sys.executable,str(ROOT/'scripts/deploy.py'),'--binary',str(BINARY),'--runtime-config',str(runtime),'--codex-config',str(config),'--skill-destination',str(skill)]
    # The installed candidate must differ from the retained prior executable.
    if BINARY.resolve()==(ROOT/'target/debug/pyxis-subagent-mcp.exe').resolve():
        copy=root/BINARY.name;copy.write_bytes(BINARY.read_bytes());command[command.index('--binary')+1]=str(copy)
    preview=subprocess.run(command,check=True,capture_output=True,text=True,encoding='utf-8')
    assert config.read_bytes()==original.encode()
    result=subprocess.run([*command,'--apply'],check=True,capture_output=True,text=True,encoding='utf-8')
    manifest_path=Path(result.stdout.strip())
    manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    assert manifest['tools']==['agent_models','agent_start','agent_send','agent_list','agent_status','agent_wait','agent_respond','agent_cancel','agent_close','agent_compact']
    changed=config.read_bytes().decode()
    assert changed.startswith('title = "synthetic configuration"\r\n\r\n')
    assert changed.endswith('[unrelated]\r\nkeep = "exactly"\r\n')
    assert 'tool_timeout_sec = 3700\r\n' in changed
    subprocess.run([sys.executable,str(ROOT/'scripts/deploy.py'),'--codex-config',str(config),'--rollback',str(manifest_path),'--apply'],check=True,capture_output=True)
    assert config.read_bytes()==original.encode()
    assert skill.read_text(encoding='utf-8')=='previous skill'
    print('Deployment preview, candidate discovery, exact unrelated bytes, preserved timeout, skill installation and rollback passed')
finally:
    if manifest_path:
        assert manifest_path.parent.parent==(ROOT/'.research/deploy')
        manifest_path.unlink();manifest_path.parent.rmdir()
    fixture.cleanup()
