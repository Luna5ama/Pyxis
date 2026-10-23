// Service-level tests exercise the actual embedded plugin, independently of
// inference and the Rust protocol simulators. All modules and files are local.
import assert from 'node:assert/strict';
import { mkdir, writeFile, rm } from 'node:fs/promises';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { randomUUID } from 'node:crypto';

const base = resolve(dirname(fileURLToPath(import.meta.url)), '../target/test-fixtures');
const fixture = resolve(base, randomUUID());
await mkdir(fixture, { recursive: true });
async function module(name, code) {
  const directory = resolve(fixture, 'node_modules/@deepseek-ai', name);
  await mkdir(directory, { recursive: true });
  await writeFile(resolve(directory, 'package.json'), JSON.stringify({ type: 'module', main: 'index.js' }));
  await writeFile(resolve(directory, 'index.js'), code);
}
try {
  await writeFile(resolve(fixture, 'package.json'), '{}');
  await module('dsh-sdk-protocol', `export class JsonRpcLineTransport {
    constructor(){ globalThis.transport=this; this.events=[]; this.requests=[]; }
    onRequest(handler){this.call=handler;}
    notify(method,params){this.events.push({method,params});}
    request(method,params,signal){return new Promise((resolve,reject)=>{
      this.requests.push({method,params,resolve,reject});
      signal?.addEventListener('abort',()=>reject(signal.reason),{once:true});
    });}
    start(){} close(){}
  }`);
  await module('dsh-llm', 'export const createUserMessage=x=>x; export const lastAssistantStreamChunk=()=>undefined;');
  await module('dsh-user-approval', 'export const setApprovalPolicy=(session,value)=>session.policy=value;');
  await module('dsh-tool-ask-user', 'export const apply=ctx=>ctx.tools.register({name:"ask_user_question"});');
  await module('dsh-scope', 'export const carrierKeyOf=x=>x.carrier;');
  process.env.PYXIS_DSH_MODULE_ROOT=fixture;
  const bridge=await import('../dsh/bridge.mjs');
  const listeners=new Map();
  const agents=new Map();
  let idle=[];
  let root;
  let disposed=false;
  const tools=new Map([['exit_plan_mode',{}]]);
  const ctx={
    on(name,handler){const list=listeners.get(name)??[];list.push(handler);listeners.set(name,list);return()=>list.splice(list.indexOf(handler),1);},
    effect(fn){fn();},
    get(){return {await:async()=>{}};},
    llm:{resolveCallConfig:async route=>route,listProviders:()=>[{id:'native'}],listModels:async()=>[{id:'model'}],resolveModelInfo:async()=>({id:'model',reasoningEfforts:['max']})},
    planMode:{set(agent,value){agent.plan=value;}},
    sandboxPolicy:{resolve:()=>({mode:'workspace-write'})},
    compaction:{compactNow:async()=>({})},
    agents:{get:id=>agents.get(id),async create(options){
      assert.equal(options.agentOptions.setup,undefined,'setup belongs to create options, not model options');
      root={status:'idle',session:{id:options.sessionId,header:{},seq:0},
        followup(message){this.status='running';this.message=message;this.session.seq++;},
        steer(message){this.steered=message;},
        cancel(){settle('interrupted');},
        whenIdle(){return this.status==='idle'?Promise.resolve():new Promise(resolve=>idle.push(resolve));}};
      agents.set(options.sessionId,root);
      const agentCtx={tools:{get:name=>tools.get(name),register:tool=>tools.set(tool.name,tool),restrict:({deny})=>deny.forEach(name=>tools.delete(name))},systemPrompt:{section:section=>{root.prefix=section.text;}}};
      agentCtx.inject=(_services,apply)=>apply(agentCtx);
      await options.setup?.(agentCtx,root);
      return {dispose:async()=>{disposed=true;}};
    }},
  };
  function emit(name,...args){for(const handler of listeners.get(name)??[])handler(...args);}
  function settle(reason='completed'){
    root.status='idle';
    emit('session/event',root.session,{type:'assistant/message',data:{message:{content:[{type:'text',text:'done'}]},usage:{inputTokens:3}}});
    emit('session/event',root.session,{type:'turn/end',data:{reason:{kind:reason}}});
    for(const wake of idle.splice(0))wake();
  }
  const tick=()=>new Promise(resolve=>setImmediate(resolve));
  bridge.apply(ctx);
  const transport=globalThis.transport;
  await transport.call('initialize',{cwd:fixture,provider:'native',model:'model',reasoningEffort:'max'});
  const session=await transport.call('pyxis/session/start',{mode:'plan',interactive:true,prefixPrompt:'stable prefix'});
  assert.equal(root.prefix,'stable prefix');
  assert(tools.has('ask_user_question'));
  assert(!tools.has('exit_plan_mode'));
  assert.equal(root.session.policy,'ask');
  const policy={mode:'normal',maxRetries:5,initialDelayMs:500,maxDelayMs:10000,jitterRatio:.1,retryableCodes:['SERVER']};
  const recover=listeners.get('agent/request-error')[0];
  const request={agent:root,retryPolicy:policy};
  let delegated=false;
  assert.deepEqual(await recover(request,async()=>{delegated=true;return {kind:'retry'};}),{kind:'retry'});
  assert(delegated);
  assert.deepEqual(request.retryPolicy.retryableCodes,['SERVER','INVALID_REQUEST']);
  assert.equal(request.retryPolicy.maxRetries,5);
  assert.deepEqual(policy.retryableCodes,['SERVER'],'do not mutate inherited provider policy');
  const unrelated={agent:{session:{id:'unrelated',header:{}}},retryPolicy:policy};
  await recover(unrelated,async()=>undefined);assert.equal(unrelated.retryPolicy,policy);
  const disabled={agent:root,retryPolicy:{...policy,maxRetries:0}};
  await recover(disabled,async()=>undefined);assert.equal(disabled.retryPolicy.maxRetries,0);
  const params={sessionId:session.sessionId};
  const run=transport.call('pyxis/session/run',{...params,prompt:'synthetic'});
  assert.equal((await transport.call('pyxis/session/steer',{...params,prompt:'corrected'})).applied,true);
  assert.equal(root.steered.content[0].text,'corrected');
  const signal=new AbortController();
  const approval=listeners.get('approval/request')[0]({agent:root,signal:signal.signal,operation:{command:'synthetic'}},()=>{throw Error('lost host approval');});
  await tick();
  const question=listeners.get('user-questions/request')[0]({agent:root,questions:[{id:'q',question:'color'}]},()=>{throw Error('lost host question');});
  await tick();
  assert.deepEqual(transport.requests.map(x=>x.method),['pyxis/interaction/approval','pyxis/interaction/questions']);
  transport.requests[0].resolve({outcome:'rejected'});
  transport.requests[1].resolve({answers:[{id:'q',selected:['blue']}]});
  assert.equal(await approval,'rejected');
  assert.equal((await question).answers[0].selected[0],'blue');
  assert.equal(transport.events.filter(x=>x.method==='pyxis/interaction/resolved').length,2);
  await transport.call('pyxis/session/cancel',params);
  assert.equal((await run).stopReason,'interrupted');
  const later=transport.call('pyxis/session/run',{...params,prompt:'later'});
  const child={session:{id:'child',header:{parentSession:root.session.id}}};
  agents.set('child',child);
  const childApproval=listeners.get('approval/request')[0]({agent:child},()=>{throw Error('lost child approval');});
  await tick();transport.requests.at(-1).resolve({outcome:'allowed-once'});assert.equal(await childApproval,'allowed-once');
  listeners.get('subagent/start')[0].call({carrier:root},{runId:'child-run'});
  settle();
  let ended=false;later.then(()=>ended=true);
  await tick();assert.equal(ended,false,'root idle must not complete while its child is running');
  emit('subagent/end',{runId:'child-run'});
  const result=await later;
  assert.equal(result.text,'done');assert.equal(result.usage.inputTokens,3);
  assert.deepEqual(result.usage.raw_samples,[{inputTokens:3}]);
  assert.equal((await transport.call('pyxis/session/compact',params)).compacted,true);
  await transport.call('pyxis/session/suspend',params);assert(disposed);
  await transport.call('shutdown',{});
  // Reconnect through the native resume API, with the same setup and policy.
  ctx.agents.resume=async options=>{
    assert.equal(options.resumeSessionId,session.sessionId);
    assert.equal(options.sessionId,undefined);
    return ctx.agents.create({...options,sessionId:options.resumeSessionId});
  };
  bridge.apply(ctx);
  const resumedTransport=globalThis.transport;
  await resumedTransport.call('initialize',{cwd:fixture,provider:'native',model:'model'});
  const resumed=await resumedTransport.call('pyxis/session/start',{mode:'plan',interactive:true,prefixPrompt:'resumed prefix',resumeSessionId:session.sessionId});
  assert.equal(resumed.sessionId,session.sessionId);
  assert.equal(root.prefix,'resumed prefix');
  assert.equal(root.session.policy,'ask');
  await resumedTransport.call('shutdown',{});
  console.log('DSH bridge service tests passed: setup, native question registration, fixed mode, approvals, controls, child barrier, usage, compaction, cleanup');
} finally {
  assert.equal(dirname(fixture),base);
  await rm(fixture,{recursive:true,force:true});
}
