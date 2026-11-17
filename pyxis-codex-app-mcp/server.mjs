// Local stdio adapter for the installed, unmodified Codex app MCP server.
// Reuses CHIMERA listening-room's Windows named-pipe discovery convention.
import { spawn } from 'node:child_process';
import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { constants, homedir, setPriority } from 'node:os';
import { dirname, join } from 'node:path';
import { createInterface } from 'node:readline';

const pipeRoot = '\\\\.\\pipe\\';
const pipePattern = /^codex-app-tools-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i;
const callerThreadId = process.env.CODEX_APP_BRIDGE_THREAD_ID?.trim();
const log = (message) => process.stderr.write(`[codex-app-bridge] ${message}\n`);

function installedServer() {
  const root = join(process.env.CODEX_HOME || join(homedir(), '.codex'), 'plugins', 'cache', 'openai-bundled', 'codex-app-tools');
  const candidates = readdirSync(root, { withFileTypes: true })
    .filter((entry) => entry.isDirectory() && /^\d+\.\d+\.\d+$/.test(entry.name))
    .sort((a, b) => b.name.localeCompare(a.name, undefined, { numeric: true }));
  for (const entry of candidates) {
    const server = join(root, entry.name, 'server.mjs');
    const manifest = join(root, entry.name, '.codex-plugin', 'plugin.json');
    if (existsSync(server) && existsSync(manifest)
      && JSON.parse(readFileSync(manifest, 'utf8')).name === 'codex-app-tools') return server;
  }
  throw new Error('Installed Codex app tools plugin not found. Open/update Codex, then reconnect this MCP.');
}

function desktopPipe() {
  if (process.platform !== 'win32') throw new Error('This adapter is configured for Windows.');
  const pipes = readdirSync(pipeRoot).filter((name) => pipePattern.test(name)).map((name) => pipeRoot + name);
  const inherited = process.env.CODEX_APP_TOOLS_PIPE_PATH;
  if (inherited && pipes.includes(inherited)) return inherited;
  if (pipes.length !== 1) throw new Error(pipes.length === 0
    ? 'Codex app is not running. Open Codex, then reconnect this MCP.'
    : 'Multiple Codex app instances found. Select one explicitly with CODEX_APP_TOOLS_PIPE_PATH.');
  return pipes[0];
}

function lowerPriority(pid) {
  try { setPriority(pid, constants.priority.PRIORITY_LOW); }
  catch (error) { log(`Could not set Idle priority: ${error.message}`); }
}

try {
  if (!callerThreadId) throw new Error('Set CODEX_APP_BRIDGE_THREAD_ID to the chosen existing Codex chat.');
  const server = installedServer();
  const pipe = desktopPipe();
  lowerPriority(process.pid);
  const child = spawn(process.execPath, [server], {
    windowsHide: true,
    cwd: dirname(server),
    stdio: ['pipe', 'pipe', 'pipe'],
    env: { ...process.env, CODEX_APP_TOOLS_PIPE_PATH: pipe, CODEX_APP_TOOLS_CALLER_HOST_ID: 'local', CODEX_APP_TOOLS_DEFERRED: '1' },
  });
  child.on('spawn', () => lowerPriority(child.pid));
  child.stderr.pipe(process.stderr);
  let stopping = false;
  const pending = new Map();
  const input = createInterface({ input: process.stdin, crlfDelay: Infinity });
  const output = createInterface({ input: child.stdout, crlfDelay: Infinity });
  const send = (message) => process.stdout.write(`${JSON.stringify(message)}\n`);
  const failPending = (message) => {
    for (const [id] of pending) send({ jsonrpc: '2.0', id, error: { code: -32000, message } });
    pending.clear();
  };
  input.on('line', (line) => {
    if (!line.trim() || stopping) return;
    let message;
    try { message = JSON.parse(line); }
    catch { send({ jsonrpc: '2.0', id: null, error: { code: -32700, message: 'Invalid JSON' } }); return; }
    if (message?.method === 'tools/call') {
      // Only supply executor context; the target thread stays in tool arguments.
      message.params ??= {};
      message.params._meta = { ...message.params._meta, 'openai/threadId': callerThreadId };
    }
    if (message?.id != null && message.method) pending.set(message.id, message.method);
    if (!child.stdin.destroyed) child.stdin.write(`${JSON.stringify(message)}\n`);
  });
  output.on('line', (line) => {
    let message;
    try { message = JSON.parse(line); }
    catch { log('Unexpected non-JSON output from Codex MCP.'); return; }
    const method = pending.get(message.id);
    pending.delete(message.id);
    if (method === 'initialize' && message.result) {
      message.result.instructions = [message.result.instructions,
        `This client connects to the user's running Codex desktop app. Default caller context is existing Codex chat ${callerThreadId}. Tools referring to the current chat, workspace or project use that context; target threadId arguments still select other chats.`,
        'Use list_threads and read_thread with pagination to find and inspect chats. There is no standalone full-text search tool in this catalog. Preserve tool-specific authorization requirements: only send messages, create chats, delete chats, or make other user-directed changes when the user asks for that action.',
        'Keep Codex open. After restarting Codex, reconnect this MCP so its local pipe is rediscovered.'
      ].filter(Boolean).join('\n');
    }
    send(message);
  });
  const stop = () => {
    if (stopping) return;
    stopping = true;
    input.close();
    child.stdin.end();
    const timer = setTimeout(() => child.kill(), 1500);
    timer.unref();
    child.once('exit', () => clearTimeout(timer));
  };
  child.stdin.on('error', (error) => { log(error.message); failPending('Codex MCP connection closed. Reconnect this MCP.'); stop(); });
  child.on('error', (error) => { log(error.message); failPending(error.message); process.exitCode = 1; stop(); });
  child.on('exit', (code) => {
    failPending('Codex MCP exited. Keep Codex open, then reconnect this MCP.');
    input.close();
    process.exitCode = stopping ? 0 : (code || 1);
    process.stdin.destroy();
  });
  input.on('close', stop);
  process.once('SIGINT', stop);
  process.once('SIGTERM', stop);
  log(`Using ${server}; caller context ${callerThreadId}`);
} catch (error) {
  log(error.message);
  process.exitCode = 1;
}
