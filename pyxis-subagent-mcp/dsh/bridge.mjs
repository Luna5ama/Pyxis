// Thin plugin over DSH's public agent/plan/compaction services.
// Never implements its own model loop, summary prompt, or credential handling.
import { createRequire } from 'node:module';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { randomUUID } from 'node:crypto';

const require = createRequire(resolve(process.env.PYXIS_DSH_MODULE_ROOT, 'package.json'));
const { JsonRpcLineTransport } = await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-sdk-protocol')).href);
const { createUserMessage, lastAssistantStreamChunk } = await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-llm')).href);
const { setApprovalPolicy } = await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-user-approval')).href);

export const name = 'pyxis-jsonrpc-server';
export const inject = ['agents', 'llm', 'compaction', 'planMode', 'systemPrompt'];

export function apply(ctx) {
  const transport = new JsonRpcLineTransport(process.stdin, process.stdout);
  let selection;
  let cwd;
  let handle;
  let owned;
  let active = false;
  const children = new Set();
  const childWaiters = new Set();
  const offStart = ctx.on('subagent/start', info => { children.add(String(info.runId)); });
  const offEnd = ctx.on('subagent/end', info => {
    children.delete(String(info.runId));
    for (const wake of childWaiters) wake();
    childWaiters.clear();
  });
  let disposeTask;
  const dispose = () => disposeTask ??= (async () => { await handle?.dispose(); })();
  const agent = sessionId => {
    if (sessionId !== owned) throw new Error('Session is not owned by this bridge');
    const result = ctx.agents.get(sessionId);
    if (!result) throw new Error('Session agent is unavailable');
    return result;
  };

  transport.onRequest(async (method, params) => {
    if (method === 'initialize') {
      await ctx.get('loader')?.await();
      if (selection) throw new Error('Already initialized');
      const route = { provider: params.provider, model: params.model, ...(params.reasoningEffort == null ? {} : { reasoningEffort: params.reasoningEffort }) };
      await ctx.llm.resolveCallConfig(route);
      selection = route;
      cwd = resolve(params.cwd);
      return { serverInfo: { name: 'pyxis-dsh-bridge', version: '0.1.0' } };
    }
    if (active) throw new Error('A session operation is already running');
    active = true;
    try {
      if (method === 'pyxis/session/start') {
        if (!selection) throw new Error('Not initialized');
        if (owned) throw new Error('Session already exists');
        if (!['plan', 'auto'].includes(params.mode)) throw new Error('Invalid mode');
        owned = `pyxis-${randomUUID()}`;
        handle = await ctx.agents.create({ sessionId: owned, meta: { cwd }, agentOptions: {
          ...selection,
          setup(agentCtx) {
            if (params.prefixPrompt) agentCtx.systemPrompt.section({ name: 'pyxis:prefix', order: 10000, text: params.prefixPrompt });
          },
        } });
        const root = agent(owned);
        setApprovalPolicy(root.session, 'never');
        // Native plan guidance; DSH explicitly describes this as soft guidance.
        ctx.planMode.set(root, params.mode === 'plan');
        return { sessionId: owned };
      }
      if (method === 'pyxis/session/compact') {
        const root = agent(params.sessionId);
        const result = await ctx.compaction.compactNow(root, new AbortController().signal);
        return { compacted: result !== null };
      }
      if (method === 'pyxis/session/run') {
        const root = agent(params.sessionId);
        let text = '';
        let stopReason;
        let usage = null;
        let completeUsage = true;
        const accumulate = sample => {
          if (!sample) { completeUsage = false; return; }
          if (!usage) { usage = { ...sample }; return; }
          for (const key of Object.keys(usage)) {
            if (typeof usage[key] === 'number' && typeof sample[key] === 'number') usage[key] += sample[key];
            else delete usage[key];
          }
        };
        let failure;
        const offError = ctx.on('agent/error', info => { if (info.agent === root) failure = info.error; });
        const off = ctx.on('session/event', (session, event) => {
          if (String(session.id) !== owned) return;
          if (event.type === 'assistant/message') {
            accumulate(event.data.usage ?? lastAssistantStreamChunk(event.data.stream, 'usage')?.usage);
            const blocks = event.data.message?.content ?? event.data.content ?? [];
            const answer = blocks.filter(b => b.type === 'text').map(b => b.text).join('\n');
            if (answer) text = answer;
          }
          if (event.type === 'assistant/attempt') accumulate(lastAssistantStreamChunk(event.data.stream, 'usage')?.usage);
          if (event.type === 'turn/end') stopReason = event.data.reason?.kind ?? event.data.reason;
        });
        try {
          root.followup(createUserMessage({ content: [{ type: 'text', text: params.prompt }], source: { kind: 'user' } }));
          while (true) {
            await root.whenIdle();
            const seq = root.session.seq;
            if (children.size) {
              await new Promise(wake => childWaiters.add(wake));
              continue;
            }
            if (root.status === 'idle' && root.session.seq === seq) break;
          }
          if (failure) throw failure;
          return { text, stopReason, usage: completeUsage ? usage : null };
        } finally { off(); offError(); }
      }
      if (method === 'shutdown') return await dispose();
      throw new Error(`Unknown Pyxis DSH method: ${method}`);
    } finally { active = false; }
  });
  ctx.effect(() => {
    transport.start();
    return async () => { await dispose(); offStart(); offEnd(); transport.close(); };
  }, 'pyxis.jsonrpc.serve');
}
