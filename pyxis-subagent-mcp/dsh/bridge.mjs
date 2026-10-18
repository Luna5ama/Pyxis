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
const { apply: registerAskUser } = await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-tool-ask-user')).href);
const { carrierKeyOf } = await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-scope')).href);

export const name = 'pyxis-jsonrpc-server';
export const inject = ['agents', 'llm', 'compaction', 'planMode', 'systemPrompt', 'approval', 'userQuestions', 'tools', 'sandboxPolicy'];

export function apply(ctx) {
  const transport = new JsonRpcLineTransport(process.stdin, process.stdout);
  let selection;
  let cwd;
  let handle;
  let owned;
  let active = false;
  let interactive = false;
  const children = new Set();
  const childWaiters = new Set();
  const owns = candidate => {
    const seen = new Set();
    while (candidate && !seen.has(candidate)) {
      if (String(candidate.session?.id) === owned) return true;
      seen.add(candidate);
      candidate = ctx.agents.get(candidate.session?.header.parentSession);
    }
    return false;
  };
  // Extend policy only; the installed native llm-retry plugin owns retry
  // counting, durable progress, exponential backoff and cancellation. Its
  // recovery point repeats the failed model request before tool dispatch.
  const offRetryPolicy = ctx.on('agent/request-error', (request, next) => {
    const policy = request.retryPolicy;
    if (owns(request.agent) && policy?.mode === 'normal' && !policy.retryableCodes.includes('INVALID_REQUEST')) {
      request.retryPolicy = { ...policy, retryableCodes: [...policy.retryableCodes, 'INVALID_REQUEST'] };
    }
    return next();
  }, { prepend: true });
  const offStart = ctx.on('subagent/start', function(info) {
    if (owns(carrierKeyOf(this))) children.add(String(info.runId));
  });
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
  const hostRequest = async (method, payload, signal) => {
    const interactionKey = randomUUID();
    try { return await transport.request(method, { ...payload, interactionKey }, signal); }
    finally { transport.notify('pyxis/interaction/resolved', { interactionKey }); }
  };
  const offApproval = ctx.on('approval/request', (request, next) => {
    if (!interactive || !owns(request.agent)) return next();
    const { agent: _agent, signal, ...payload } = request;
    return hostRequest('pyxis/interaction/approval', {
      ...payload, options: ['allowed-once', 'rejected', 'cancelled'],
    }, signal).then(result => {
      if (!['allowed-once', 'rejected', 'cancelled'].includes(result?.outcome)) throw new Error('Invalid approval outcome');
      return result.outcome;
    });
  });
  const offQuestions = ctx.on('user-questions/request', (request, next) => {
    if (!interactive || String(request.agent?.session.id) !== owned) return next();
    const { agent: _agent, signal, ...payload } = request;
    return hostRequest('pyxis/interaction/questions', payload, signal);
  });

  transport.onRequest(async (method, params) => {
    if (method === 'initialize') {
      await ctx.get('loader')?.await();
      if (selection) throw new Error('Already initialized');
      const route = { provider: params.provider, model: params.model, ...(params.reasoningEffort == null ? {} : { reasoningEffort: params.reasoningEffort }) };
      selection = params.discoveryOnly ? route : await ctx.llm.resolveCallConfig(route);
      cwd = resolve(params.cwd);
      return { serverInfo: { name: 'pyxis-dsh-bridge', version: '0.2.0' } };
    }
    if (method === 'pyxis/models/list') {
      const providers = ctx.llm.listProviders();
      const selected = params.provider ? providers.filter(p => p.id === params.provider) : providers;
      if (params.provider && selected.length === 0) throw new Error(`Unknown provider: ${params.provider}`);
      return { providers: await Promise.all(selected.map(async provider => ({
        ...provider, models: await Promise.all((await ctx.llm.listModels(provider.id)).map(async model => {
          try { return await ctx.llm.resolveModelInfo(provider.id, model.id); }
          catch (error) { return { ...model, capabilityError: error.message }; }
        })),
      }))) };
    }
    if (method === 'pyxis/session/steer') {
      const root = agent(params.sessionId);
      if (!active || root.status !== 'running') return { applied: false, reason: 'No active turn' };
      root.steer(createUserMessage({ content: [{ type: 'text', text: params.prompt }], source: { kind: 'user' } }));
      return { applied: true };
    }
    if (method === 'pyxis/session/cancel') {
      const root = agent(params.sessionId);
      root.cancel({ kind: 'user' });
      await root.whenIdle();
      return { stopped: true };
    }
    if (method === 'shutdown') return await dispose();
    if (active) throw new Error('A turn or compaction is already running');
    active = true;
    try {
      if (method === 'pyxis/session/start') {
        if (!selection) throw new Error('Not initialized');
        if (owned) throw new Error('Session already exists');
        if (!['plan', 'auto'].includes(params.mode)) throw new Error('Invalid mode');
        owned = `pyxis-${randomUUID()}`;
        handle = await ctx.agents.create({ sessionId: owned, meta: { cwd }, agentOptions: selection,
          setup(agentCtx) {
            agentCtx.inject(['tools', 'userQuestions', 'systemPrompt'], scope => {
              // The SDK profile omits the model-facing question consumer.
              // Its callbacks must retain a native injected scoped context.
              if (params.interactive && !scope.tools.get('ask_user_question')) registerAskUser(scope);
              const switches = ['enter_plan_mode', 'exit_plan_mode'].filter(name => scope.tools.get(name));
              if (switches.length) scope.tools.restrict({ deny: switches });
              if (params.prefixPrompt) scope.systemPrompt.section({ name: 'pyxis:prefix', order: 10000, text: params.prefixPrompt });
            });
          },
        });
        const root = agent(owned);
        interactive = params.interactive === true;
        setApprovalPolicy(root.session, interactive ? 'ask' : 'never');
        // Native plan guidance; DSH explicitly describes this as soft guidance.
        ctx.planMode.set(root, params.mode === 'plan');
        return { sessionId: owned, effectiveConfig: { ...selection, cwd, mode: params.mode, approvalPolicy: interactive ? 'ask' : 'never', sandbox: ctx.sandboxPolicy.resolve({ session: root.session }), planModeIsSoftGuidance: true } };
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
        const rawSamples = [];
        let rawBytes = 0;
        let omittedSamples = 0;
        const accumulate = sample => {
          const bytes = JSON.stringify(sample ?? null).length;
          if (rawBytes + bytes <= 64 * 1024 && rawSamples.length < 1024) {
            rawSamples.push(sample == null ? null : structuredClone(sample));
            rawBytes += bytes;
          } else omittedSamples++;
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
          transport.notify('pyxis/session/event', { sessionId: owned, event });
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
          return { text, stopReason, usage: { ...(usage ?? {}), raw_samples: rawSamples, omitted_raw_samples: omittedSamples, complete: completeUsage } };
        } finally { off(); offError(); }
      }
      throw new Error(`Unknown Pyxis DSH method: ${method}`);
    } catch (error) {
      process.stderr.write(`${error.stack ?? error.message}\n`);
      throw error;
    } finally { active = false; }
  });
  ctx.effect(() => {
    transport.start();
    return async () => { await dispose(); offStart(); offEnd(); offApproval(); offQuestions(); offRetryPolicy(); transport.close(); };
  }, 'pyxis.jsonrpc.serve');
}
