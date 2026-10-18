"""Install an independently built candidate, preserving the prior MCP registration.

Only the named MCP table and companion skill change. The rollback manifest
stores that table, not unrelated Codex settings or credentials.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tomllib
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tests'))
from live_acceptance import Mcp

TABLE=re.compile(r'(?ms)^\[mcp_servers\.pyxis-subagent-mcp\][^\r\n]*\r?\n.*?(?=^\[|\Z)')
TOOLS=['agent_models','agent_start','agent_send','agent_list','agent_status','agent_wait','agent_respond','agent_cancel','agent_close','agent_compact']

def digest(value):return hashlib.sha256(value).hexdigest()

def atomic(path, data):
    temporary=path.with_name(path.name+'.pyxis-'+uuid.uuid4().hex+'.tmp')
    try:
        temporary.write_bytes(data)
        os.replace(temporary,path)
    finally:
        temporary.unlink(missing_ok=True)

def registration(path):
    data=path.read_bytes()
    text=data.decode('utf-8')
    table=TABLE.search(text)
    if not table:raise RuntimeError('Expected exactly one existing Pyxis MCP table')
    if len(TABLE.findall(text))!=1:raise RuntimeError('Ambiguous Pyxis MCP table')
    parsed=tomllib.loads(text)['mcp_servers']['pyxis-subagent-mcp']
    return data,text,table,parsed

def replace_field(table,key,value,newline):
    line=key+' = '+json.dumps(value,ensure_ascii=False)
    pattern=re.compile(r'(?m)^'+re.escape(key)+r'[ \t]*=[^\r\n]*')
    if pattern.search(table):return pattern.sub(lambda _:line,table,count=1)
    return table.rstrip('\r\n')+newline+line+newline+newline

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--binary',type=Path)
    parser.add_argument('--runtime-config',type=Path)
    parser.add_argument('--codex-config',type=Path,required=True)
    parser.add_argument('--skill-source',type=Path,default=ROOT.parent/'pyxis-subagent/SKILL.md')
    parser.add_argument('--skill-destination',type=Path)
    parser.add_argument('--rollback',type=Path)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    config=args.codex_config.resolve(strict=True)
    original,text,match,current=registration(config)
    if args.rollback:
        manifest=json.loads(args.rollback.read_text(encoding='utf-8'))
        if Path(manifest['codex_config'])!=config:raise RuntimeError('Rollback configuration path differs')
        if match.group()!=manifest['installed_table']:raise RuntimeError('Pyxis registration changed since installation')
        restored=text[:match.start()]+manifest['previous_table']+text[match.end():]
        if args.apply:
            destination=Path(manifest['skill_destination'])
            if digest(destination.read_bytes())!=manifest['installed_skill_sha256']:
                raise RuntimeError('Installed skill changed; preserve it before rolling back')
            atomic(config,restored.encode('utf-8'))
            if manifest['previous_skill'] is None:destination.unlink()
            else:atomic(destination,manifest['previous_skill'].encode('utf-8'))
        print(json.dumps(dict(action='rollback',applied=args.apply,codex_config=str(config))))
        return
    if not args.binary or not args.runtime_config:parser.error('--binary and --runtime-config are required')
    binary=args.binary.resolve(strict=True)
    runtime=args.runtime_config.resolve(strict=True)
    # Never overwrite the currently registered executable.
    previous_binary=Path(current['command']).resolve(strict=True)
    if binary==previous_binary:raise RuntimeError('Candidate must have its own immutable path')
    client=Mcp(binary,runtime)
    try:
        discovered=[tool['name'] for tool in client.rpc('tools/list',{})['tools']]
    finally:client.close()
    specialists=list(json.loads(runtime.read_text(encoding='utf-8-sig')).get('specialists',{}))
    expected=specialists+TOOLS
    if set(discovered)!=set(expected):raise RuntimeError('Candidate tool discovery differs from runtime config')
    newline='\r\n' if '\r\n' in match.group() else '\n'
    installed=match.group()
    for key,value in [('command',str(binary)),('args',[str(runtime)]),('enabled_tools',expected)]:
        installed=replace_field(installed,key,value,newline)
    updated=text[:match.start()]+installed+text[match.end():]
    parsed=tomllib.loads(updated)['mcp_servers']['pyxis-subagent-mcp']
    assert parsed['command']==str(binary) and parsed['enabled_tools']==expected
    skill_source=args.skill_source.resolve(strict=True)
    destination=(args.skill_destination or config.parent/'skills/pyxis-subagent/SKILL.md').resolve()
    manifest=dict(version=1,codex_config=str(config),previous_table=match.group(),installed_table=installed,
        previous_config_sha256=digest(original),installed_config_sha256=digest(updated.encode('utf-8')),
        previous_binary=str(previous_binary),previous_binary_sha256=digest(previous_binary.read_bytes()),
        candidate=str(binary),candidate_sha256=digest(binary.read_bytes()),runtime_config=str(runtime),runtime_config_sha256=digest(runtime.read_bytes()),
        skill_source=str(skill_source),installed_skill_sha256=digest(skill_source.read_bytes()),skill_destination=str(destination),previous_skill=destination.read_text(encoding='utf-8') if destination.exists() else None,tools=expected)
    if not args.apply:
        print(json.dumps({key:manifest[key] for key in ('candidate','candidate_sha256','codex_config','tools')},indent=2))
        return
    directory=ROOT/'.research/deploy'/uuid.uuid4().hex
    directory.mkdir(parents=True)
    manifest_path=directory/'manifest.json'
    manifest_path.write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    # Detect concurrent edits after preflight before replacing the table.
    if config.read_bytes()!=original:raise RuntimeError('Codex configuration changed during deployment')
    destination.parent.mkdir(parents=True,exist_ok=True)
    atomic(destination,skill_source.read_bytes())
    try:atomic(config,updated.encode('utf-8'))
    except BaseException:
        if manifest['previous_skill'] is None:destination.unlink()
        else:atomic(destination,manifest['previous_skill'].encode('utf-8'))
        raise
    assert digest(config.read_bytes())==manifest['installed_config_sha256']
    assert digest(previous_binary.read_bytes())==manifest['previous_binary_sha256']
    print(str(manifest_path))

if __name__=='__main__':main()
