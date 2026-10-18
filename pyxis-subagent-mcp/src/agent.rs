use crate::{
    config::{AgentConfig, Harness, Limits, Mode},
    harness::{Backend, Control, DynamicEvent},
};
use anyhow::{Context, Result, bail, ensure};
use serde::Deserialize;
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, VecDeque},
    path::PathBuf,
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, Ordering},
    },
    time::{Duration, Instant},
};
use tokio::{
    sync::{Notify, Semaphore, mpsc, oneshot},
    task::JoinHandle,
};

static NEXT_ID: AtomicU64 = AtomicU64::new(1);
fn id(prefix: &str) -> String {
    format!(
        "{prefix}-{}-{}",
        std::process::id(),
        NEXT_ID.fetch_add(1, Ordering::Relaxed)
    )
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Start {
    harness: Harness,
    cwd: PathBuf,
    prompt: String,
    model: Option<String>,
    provider: Option<String>,
    #[serde(default, deserialize_with = "effort_override")]
    effort: Option<Option<String>>,
    mode: Option<Mode>,
    timeout_seconds: Option<u64>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Send {
    session_id: String,
    prompt: String,
    #[serde(default)]
    action: Option<String>,
    task_id: Option<String>,
    timeout_seconds: Option<u64>,
}

fn effort_override<'de, D: serde::Deserializer<'de>>(
    deserializer: D,
) -> std::result::Result<Option<Option<String>>, D::Error> {
    Option::<String>::deserialize(deserializer).map(Some)
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Target {
    session_id: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct TaskTarget {
    task_id: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Status {
    task_id: Option<String>,
    session_id: Option<String>,
    #[serde(default)]
    cursor: u64,
    #[serde(default = "default_event_limit")]
    event_limit: usize,
}
fn default_event_limit() -> usize {
    64
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Wait {
    task_ids: Vec<String>,
    #[serde(default)]
    cursor: BTreeMap<String, u64>,
    timeout_seconds: Option<u64>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Respond {
    interaction_id: String,
    response: Value,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Compact {
    session_id: String,
    timeout_seconds: Option<u64>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Models {
    harness: Harness,
    cwd: PathBuf,
    provider: Option<String>,
    refresh: Option<bool>,
}

const EVENT_BYTES: usize = 64 * 1024;
const TASK_BYTES: usize = 1024 * 1024;
const RESULT_BYTES: usize = 256 * 1024;

#[derive(Clone)]
struct Event {
    cursor: u64,
    data: Value,
    bytes: usize,
}
struct Task {
    id: String,
    session_id: String,
    prompt: String,
    compact: bool,
    automatic: bool,
    deadline: Instant,
    state: &'static str,
    events: VecDeque<Event>,
    event_bytes: usize,
    next_cursor: u64,
    result: Option<Value>,
    error: Option<String>,
    interactions: BTreeMap<String, Value>,
    effective: Value,
    queued_permit: Option<tokio::sync::OwnedSemaphorePermit>,
}
struct Session {
    id: String,
    config: AgentConfig,
    wake: Arc<Notify>,
    control: mpsc::Sender<Control>,
    worker: JoinHandle<()>,
    running: Option<String>,
    queue: VecDeque<String>,
    last_used: Instant,
    invalid: bool,
    effective: Value,
    _live: tokio::sync::OwnedSemaphorePermit,
}
struct Submission {
    fingerprint: String,
    result: Option<std::result::Result<Value, String>>,
    ready: Arc<Notify>,
}
#[derive(Default)]
struct Registry {
    sessions: BTreeMap<String, Session>,
    tasks: BTreeMap<String, Task>,
    completed: VecDeque<String>,
    submissions: BTreeMap<String, Submission>,
    submission_order: VecDeque<String>,
    models: BTreeMap<String, (Instant, Value)>,
}
pub struct Manager {
    runtimes: BTreeMap<Harness, AgentConfig>,
    limits: Limits,
    state: Mutex<Registry>,
    changed: Notify,
    active: Arc<Semaphore>,
    live: Arc<Semaphore>,
    queued: Arc<Semaphore>,
    model_slot: Semaphore,
}
fn terminal(state: &str) -> bool {
    matches!(state, "completed" | "failed" | "cancelled")
}
fn effective(config: &AgentConfig) -> Value {
    json!({"harness":config.harness,"model":config.model,"effort":config.reasoning_effort,
        "provider":config.provider,"cwd":config.cwd,"mode":config.mode})
}
impl Manager {
    pub fn new(
        runtimes: BTreeMap<Harness, AgentConfig>,
        limits: Limits,
        resources: crate::session::Resources,
    ) -> Arc<Self> {
        Arc::new(Self {
            runtimes,
            active: resources.active,
            live: resources.live,
            queued: resources.queued,
            limits,
            state: Mutex::new(Registry::default()),
            changed: Notify::new(),
            model_slot: Semaphore::new(1),
        })
    }
    pub fn spawn_reaper(self: &Arc<Self>) -> JoinHandle<()> {
        let weak = Arc::downgrade(self);
        tokio::spawn(async move {
            let mut timer = tokio::time::interval(Duration::from_millis(100));
            loop {
                timer.tick().await;
                let Some(me) = weak.upgrade() else { break };
                me.reap();
            }
        })
    }
    // A keyed submission is owned by the manager, independently of the lifetime
    // of the MCP request waiting for its acknowledgement.
    pub async fn call(self: &Arc<Self>, name: &str, mut args: Value) -> Result<Value> {
        let key = args.get("idempotency_key").cloned();
        let Some(key) = key else {
            return self.dispatch(name, args).await;
        };
        ensure!(
            matches!(name, "agent_start" | "agent_send" | "agent_compact"),
            "this tool does not accept an idempotency key"
        );
        let key = key
            .as_str()
            .context("idempotency_key must be a string")?
            .to_owned();
        ensure!(
            !key.trim().is_empty() && key.len() <= 256,
            "idempotency_key must contain 1..256 bytes"
        );
        args.as_object_mut()
            .context("arguments must be an object")?
            .remove("idempotency_key");
        let fingerprint = format!("{name}:{}", args);
        let (ready, create) = {
            let mut state = self.state.lock().unwrap();
            if let Some(submission) = state.submissions.get(&key) {
                ensure!(
                    submission.fingerprint == fingerprint,
                    "idempotency key was already used with different arguments"
                );
                (submission.ready.clone(), false)
            } else {
                let cap =
                    self.limits.retained_tasks + self.limits.queued_tasks + self.limits.sessions;
                if state.submissions.len() >= cap {
                    if let Some(expired) = state.submission_order.pop_front() {
                        state.submissions.remove(&expired);
                    }
                }
                ensure!(
                    state.submissions.len() < cap,
                    "idempotency capacity reached"
                );
                let ready = Arc::new(Notify::new());
                state.submissions.insert(
                    key.clone(),
                    Submission {
                        fingerprint,
                        result: None,
                        ready: ready.clone(),
                    },
                );
                (ready, true)
            }
        };
        if create {
            let me = Arc::clone(self);
            let tool = name.to_owned();
            let task_key = key.clone();
            tokio::spawn(async move {
                let result = me.dispatch(&tool, args).await.map_err(|e| format!("{e:#}"));
                let mut state = me.state.lock().unwrap();
                if let Some(entry) = state.submissions.get_mut(&task_key) {
                    entry.result = Some(result);
                    entry.ready.notify_waiters();
                    state.submission_order.push_back(task_key);
                }
            });
        }
        loop {
            let wait = ready.notified();
            tokio::pin!(wait);
            wait.as_mut().enable();
            {
                let state = self.state.lock().unwrap();
                let entry = state
                    .submissions
                    .get(&key)
                    .context("idempotency record expired")?;
                if let Some(result) = &entry.result {
                    return result.clone().map_err(anyhow::Error::msg);
                }
            }
            wait.await;
        }
    }
    async fn dispatch(self: &Arc<Self>, name: &str, value: Value) -> Result<Value> {
        match name {
            "agent_models" => self.models(serde_json::from_value(value)?).await,
            "agent_start" => self.start(serde_json::from_value(value)?),
            "agent_send" => self.send(serde_json::from_value(value)?).await,
            "agent_list" => {
                ensure!(
                    value.is_null() || value == json!({}),
                    "agent_list takes no arguments"
                );
                Ok(self.list())
            }
            "agent_status" => self.status(serde_json::from_value(value)?),
            "agent_wait" => self.wait(serde_json::from_value(value)?).await,
            "agent_respond" => self.respond(serde_json::from_value(value)?).await,
            "agent_cancel" => self.cancel(serde_json::from_value(value)?).await,
            "agent_close" => self.close(serde_json::from_value(value)?).await,
            "agent_compact" => {
                let args: Compact = serde_json::from_value(value)?;
                self.enqueue(
                    args.session_id,
                    true,
                    false,
                    String::new(),
                    args.timeout_seconds,
                )
            }
            _ => bail!("unknown agent tool"),
        }
    }
    fn event(&self, task_id: &str, mut data: Value) {
        let mut state = self.state.lock().unwrap();
        let effective_update = (data["type"] == "effective_config").then(|| data["config"].clone());
        let mut effective_session = None;
        if let Some(task) = state.tasks.get_mut(task_id) {
            if data["type"] == "interaction" {
                if let Some(id) = data["interaction_id"].as_str() {
                    task.interactions.insert(id.into(), data.clone());
                }
                task.state = "waiting_input";
            }
            if data["type"] == "interaction_resolved" {
                if let Some(id) = data["interaction_id"].as_str() {
                    task.interactions.remove(id);
                }
                if task.interactions.is_empty() && !terminal(task.state) {
                    task.state = "running";
                }
            }
            if data["type"] == "effective_config" {
                task.effective = data["config"].clone();
                effective_session = Some(task.session_id.clone());
            }
            let mut bytes = data.to_string().len();
            if bytes > EVENT_BYTES {
                data = json!({"type":"oversized_event","original_bytes":bytes,"native_type":data["native"]["type"],"native_method":data["native"]["method"]});
                bytes = data.to_string().len();
            }
            task.next_cursor += 1;
            task.events.push_back(Event {
                cursor: task.next_cursor,
                data,
                bytes,
            });
            task.event_bytes += bytes;
            while task.events.len() > self.limits.retained_events || task.event_bytes > TASK_BYTES {
                if let Some(event) = task.events.pop_front() {
                    task.event_bytes -= event.bytes;
                }
            }
        }
        if let (Some(sid), Some(config)) = (effective_session, effective_update) {
            if let Some(session) = state.sessions.get_mut(&sid) {
                session.effective = config;
            }
        }
        drop(state);
        self.changed.notify_waiters();
    }
    fn snapshot(task: &Task, cursor: u64, limit: usize) -> Value {
        let oldest = task
            .events
            .front()
            .map_or(task.next_cursor + 1, |e| e.cursor);
        let mut bytes = 0;
        let events = task
            .events
            .iter()
            .filter(|e| e.cursor > cursor)
            .take(limit)
            .take_while(|e| {
                bytes += e.bytes;
                bytes <= EVENT_BYTES
            })
            .map(|e| json!({"cursor":e.cursor,"event":e.data}))
            .collect::<Vec<_>>();
        let next = events
            .last()
            .and_then(|e| e["cursor"].as_u64())
            .unwrap_or(cursor);
        json!({"task_id":task.id,"session_id":task.session_id,"status":task.state,"effective_config":task.effective,
            "automatic":task.automatic,"cursor":next,"latest_cursor":task.next_cursor,"expired_before":oldest.saturating_sub(1),
            "cursor_expired":cursor<oldest.saturating_sub(1),"events":events,"has_more":next<task.next_cursor,
            "result":task.result,"error":task.error,"pending_interactions":task.interactions.values().collect::<Vec<_>>()})
    }
    fn session_snapshot(session: &Session) -> Value {
        json!({"session_id":session.id,"effective_config":session.effective,"running_task_id":session.running,
            "queued_task_ids":session.queue,"invalid":session.invalid})
    }
    fn validate_timeout(timeout: Option<u64>) -> Result<Duration> {
        let secs = timeout.unwrap_or(3600);
        ensure!(
            (1..=86400).contains(&secs),
            "timeout_seconds must be 1..86400"
        );
        Ok(Duration::from_secs(secs))
    }
    fn canonical_cwd(path: &PathBuf) -> Result<PathBuf> {
        ensure!(path.is_absolute(), "cwd must be absolute");
        let path = crate::config::native_path(
            std::fs::canonicalize(path).with_context(|| format!("cwd {}", path.display()))?,
        );
        ensure!(path.is_dir(), "cwd must be a directory");
        Ok(path)
    }
    fn task(
        sid: &str,
        tid: &str,
        prompt: String,
        compact: bool,
        automatic: bool,
        timeout: Duration,
        config: &AgentConfig,
    ) -> Task {
        Task {
            id: tid.into(),
            session_id: sid.into(),
            prompt,
            compact,
            automatic,
            deadline: Instant::now() + timeout,
            state: "queued",
            events: VecDeque::new(),
            event_bytes: 0,
            next_cursor: 0,
            result: None,
            error: None,
            interactions: BTreeMap::new(),
            effective: effective(config),
            queued_permit: None,
        }
    }
    fn start(self: &Arc<Self>, args: Start) -> Result<Value> {
        ensure!(!args.prompt.trim().is_empty(), "prompt is blank");
        ensure!(args.prompt.len() < TASK_BYTES, "prompt exceeds 1 MiB");
        let mut config = self
            .runtimes
            .get(&args.harness)
            .context("harness is not configured")?
            .clone();
        config.cwd = Some(Self::canonical_cwd(&args.cwd)?);
        if let Some(v) = args.model {
            ensure!(!v.trim().is_empty(), "model is blank");
            config.model = v;
        }
        if let Some(v) = args.effort {
            ensure!(
                v.as_ref().is_none_or(|s| !s.trim().is_empty()),
                "effort is blank"
            );
            config.reasoning_effort = v;
        }
        if let Some(v) = args.provider {
            ensure!(
                args.harness == Harness::Dsh,
                "provider is only supported for dsh"
            );
            ensure!(!v.trim().is_empty(), "provider is blank");
            config.provider = Some(v);
        }
        if let Some(v) = args.mode {
            config.mode = v;
        }
        let duration = Self::validate_timeout(args.timeout_seconds)?;
        let sid = id("session");
        let tid = id("task");
        let wake = Arc::new(Notify::new());
        let (control, controls) = mpsc::channel(32);
        let live = self
            .live
            .clone()
            .try_acquire_owned()
            .context("session capacity reached")?;
        let queued = self
            .queued
            .clone()
            .try_acquire_owned()
            .context("queue capacity reached")?;
        let mut state = self.state.lock().unwrap();
        ensure!(
            state.sessions.len() < self.limits.sessions,
            "session capacity reached"
        );
        ensure!(
            state.tasks.values().filter(|t| t.state == "queued").count() < self.limits.queued_tasks,
            "queue capacity reached"
        );
        let mut task = Self::task(&sid, &tid, args.prompt, false, false, duration, &config);
        task.queued_permit = Some(queued);
        state.tasks.insert(tid.clone(), task);
        let me = Arc::clone(self);
        let worker_sid = sid.clone();
        let worker_config = config.clone();
        let worker_wake = wake.clone();
        let worker = tokio::spawn(async move {
            me.worker(worker_sid, worker_config, worker_wake, controls)
                .await
        });
        state.sessions.insert(
            sid.clone(),
            Session {
                id: sid.clone(),
                effective: effective(&config),
                config,
                wake,
                control,
                worker,
                running: None,
                queue: VecDeque::from([tid.clone()]),
                last_used: Instant::now(),
                invalid: false,
                _live: live,
            },
        );
        drop(state);
        self.event(&tid, json!({"type":"task_queued"}));
        Ok(json!({"session_id":sid,"task_id":tid}))
    }
    fn enqueue(
        &self,
        sid: String,
        compact: bool,
        automatic: bool,
        prompt: String,
        timeout: Option<u64>,
    ) -> Result<Value> {
        let duration = Self::validate_timeout(timeout)?;
        ensure!(prompt.len() < TASK_BYTES, "prompt exceeds 1 MiB");
        let tid = id("task");
        let queued = self
            .queued
            .clone()
            .try_acquire_owned()
            .context("queue capacity reached")?;
        let mut state = self.state.lock().unwrap();
        ensure!(
            state.tasks.values().filter(|t| t.state == "queued").count() < self.limits.queued_tasks,
            "queue capacity reached"
        );
        let session = state.sessions.get_mut(&sid).context("unknown session")?;
        ensure!(!session.invalid, "session is invalid");
        let config = session.config.clone();
        session.queue.push_back(tid.clone());
        session.wake.notify_one();
        if !automatic {
            session.last_used = Instant::now();
        }
        let mut task = Self::task(&sid, &tid, prompt, compact, automatic, duration, &config);
        task.queued_permit = Some(queued);
        state.tasks.insert(tid.clone(), task);
        drop(state);
        self.event(&tid, json!({"type":"task_queued"}));
        Ok(json!({"session_id":sid,"task_id":tid}))
    }
    async fn send(&self, args: Send) -> Result<Value> {
        ensure!(!args.prompt.trim().is_empty(), "prompt is blank");
        match args.action.as_deref().unwrap_or("queue") {
            "queue" => {
                ensure!(args.task_id.is_none(), "queue does not accept task_id");
                self.enqueue(
                    args.session_id,
                    false,
                    false,
                    args.prompt,
                    args.timeout_seconds,
                )
            }
            "steer" => {
                ensure!(
                    args.timeout_seconds.is_none(),
                    "steer cannot change task timeout"
                );
                let target = args.task_id.context("steer requires task_id")?;
                let tx = {
                    let state = self.state.lock().unwrap();
                    let s = state
                        .sessions
                        .get(&args.session_id)
                        .context("unknown session")?;
                    if s.running.as_deref() != Some(&target) {
                        return Ok(
                            json!({"applied":false,"reason":"task is not currently running"}),
                        );
                    }
                    s.control.clone()
                };
                let (reply_tx, reply_rx) = oneshot::channel();
                tx.send(Control::Steer {
                    task_id: target,
                    prompt: args.prompt,
                    reply: reply_tx,
                })
                .await?;
                match tokio::time::timeout(Duration::from_secs(30), reply_rx).await {
                    Ok(Ok(reply)) => reply,
                    _ => Ok(
                        json!({"applied":null,"reason":"steer acknowledgement unavailable; application is unknown"}),
                    ),
                }
            }
            _ => bail!("action must be queue or steer"),
        }
    }
    fn list(&self) -> Value {
        let state = self.state.lock().unwrap();
        json!({"sessions":state.sessions.values().map(Self::session_snapshot).collect::<Vec<_>>(),
            "limits":{"active_tasks":self.limits.active_tasks,"sessions":self.limits.sessions,"queued_tasks":self.limits.queued_tasks},
            "retention":{"completed_tasks":self.limits.retained_tasks,"events_per_task":self.limits.retained_events,"event_bytes_per_task":TASK_BYTES,"result_bytes":RESULT_BYTES}})
    }
    fn status(&self, args: Status) -> Result<Value> {
        ensure!(
            args.event_limit > 0 && args.event_limit <= 128,
            "event_limit must be 1..128"
        );
        let state = self.state.lock().unwrap();
        match (args.task_id, args.session_id) {
            (Some(id), None) => Ok(state
                .tasks
                .get(&id)
                .map(|t| Self::snapshot(t, args.cursor, args.event_limit))
                .unwrap_or_else(|| json!({"task_id":id,"status":"unknown_or_expired"}))),
            (None, Some(id)) => Ok(Self::session_snapshot(
                state
                    .sessions
                    .get(&id)
                    .context("unknown or closed session")?,
            )),
            _ => bail!("provide exactly one of task_id or session_id"),
        }
    }
    async fn wait(&self, args: Wait) -> Result<Value> {
        ensure!(
            !args.task_ids.is_empty() && args.task_ids.len() <= 8,
            "wait requires 1..8 task IDs"
        );
        let timeout = args.timeout_seconds.unwrap_or(60);
        ensure!(timeout <= 60, "wait timeout cannot exceed 60 seconds");
        let end = tokio::time::Instant::now() + Duration::from_secs(timeout);
        loop {
            let notified = self.changed.notified();
            tokio::pin!(notified);
            notified.as_mut().enable();
            let snapshots = {
                let state = self.state.lock().unwrap();
                args.task_ids
                    .iter()
                    .map(|id| {
                        state
                            .tasks
                            .get(id)
                            .map(|t| Self::snapshot(t, *args.cursor.get(id).unwrap_or(&0), 64))
                            .unwrap_or_else(|| json!({"task_id":id,"status":"unknown_or_expired"}))
                    })
                    .collect::<Vec<_>>()
            };
            let actionable = snapshots.iter().any(|v| {
                v["status"] == "waiting_input"
                    || v["status"] == "unknown_or_expired"
                    || v["status"].as_str().is_some_and(terminal)
            });
            if actionable || tokio::time::Instant::now() >= end {
                return Ok(json!({"tasks":snapshots,"timed_out":!actionable}));
            }
            if tokio::time::timeout_at(end, notified).await.is_err() {
                // Refresh after the timeout; returning the prior snapshot would
                // hide progress emitted during this wait.
                let state = self.state.lock().unwrap();
                return Ok(
                    json!({"timed_out":true,"tasks":args.task_ids.iter().map(|id|state.tasks.get(id).map(|t|Self::snapshot(t,*args.cursor.get(id).unwrap_or(&0),64)).unwrap_or_else(||json!({"task_id":id,"status":"unknown_or_expired"}))).collect::<Vec<_>>()}),
                );
            }
        }
    }
    async fn respond(&self, args: Respond) -> Result<Value> {
        let (tid, _) = args
            .interaction_id
            .split_once(':')
            .context("invalid interaction ID")?;
        let tx = {
            let state = self.state.lock().unwrap();
            let task = state.tasks.get(tid).context("unknown or expired task")?;
            ensure!(
                task.interactions.contains_key(&args.interaction_id),
                "interaction is stale or already answered"
            );
            state
                .sessions
                .get(&task.session_id)
                .context("session closed")?
                .control
                .clone()
        };
        let (reply_tx, reply_rx) = oneshot::channel();
        tx.send(Control::Respond {
            interaction_id: args.interaction_id,
            response: args.response,
            reply: reply_tx,
        })
        .await?;
        tokio::time::timeout(Duration::from_secs(30), reply_rx)
            .await
            .context("response acknowledgement timed out")?
            .context("session ended before response acknowledgement")?
    }
    async fn cancel(&self, args: TaskTarget) -> Result<Value> {
        let tx = {
            let mut state = self.state.lock().unwrap();
            let task = state
                .tasks
                .get_mut(&args.task_id)
                .context("unknown or expired task")?;
            if terminal(task.state) {
                return Ok(json!({"cancelled":false,"reason":"task already ended"}));
            }
            if task.state == "queued" {
                task.state = "cancelled";
                task.queued_permit.take();
                task.prompt.clear();
                let sid = task.session_id.clone();
                if let Some(session) = state.sessions.get_mut(&sid) {
                    session.queue.retain(|t| t != &args.task_id);
                    session.wake.notify_one();
                }
                state.completed.push_back(args.task_id.clone());
                drop(state);
                self.event(
                    &args.task_id,
                    json!({"type":"task_ended","status":"cancelled","queued":true}),
                );
                return Ok(json!({"cancelled":true,"queued":true}));
            }
            let sid = task.session_id.clone();
            state
                .sessions
                .get(&sid)
                .context("session closed")?
                .control
                .clone()
        };
        let (reply_tx, reply_rx) = oneshot::channel();
        tx.send(Control::Cancel {
            task_id: args.task_id.clone(),
            reply: reply_tx,
        })
        .await?;
        match tokio::time::timeout(Duration::from_secs(10), reply_rx).await {
            Ok(Ok(reply)) => reply,
            _ => {
                self.invalidate_by_task(
                    &args.task_id,
                    "native stop did not confirm within 10 seconds",
                    true,
                );
                Ok(json!({"cancelled":true,"session_invalid":true}))
            }
        }
    }
    fn invalidate_by_task(&self, tid: &str, why: &str, abort: bool) {
        let mut state = self.state.lock().unwrap();
        let Some(sid) = state.tasks.get(tid).map(|t| t.session_id.clone()) else {
            return;
        };
        if let Some(session) = state.sessions.get_mut(&sid) {
            session.invalid = true;
            session.running = None;
            session.queue.clear();
            session.last_used = Instant::now();
            if abort {
                session.worker.abort();
            }
        }
        let failed = state
            .tasks
            .values_mut()
            .filter(|t| t.session_id == sid && !terminal(t.state))
            .map(|t| {
                t.state = "failed";
                t.error = Some(why.into());
                t.interactions.clear();
                t.prompt.clear();
                t.queued_permit.take();
                t.id.clone()
            })
            .collect::<Vec<_>>();
        state.completed.extend(failed);
        drop(state);
        self.changed.notify_waiters();
    }
    async fn close(&self, args: Target) -> Result<Value> {
        let running = {
            let mut state = self.state.lock().unwrap();
            let session = state
                .sessions
                .get_mut(&args.session_id)
                .context("unknown or closed session")?;
            session.invalid = true;
            session.running.clone()
        };
        if let Some(task_id) = running {
            let _ = self.cancel(TaskTarget { task_id }).await;
        }
        let session = {
            let mut state = self.state.lock().unwrap();
            let session = state
                .sessions
                .remove(&args.session_id)
                .context("session already closed")?;
            let mut ended = Vec::new();
            for task in state
                .tasks
                .values_mut()
                .filter(|t| t.session_id == args.session_id && !terminal(t.state))
            {
                task.state = "cancelled";
                task.interactions.clear();
                task.prompt.clear();
                task.queued_permit.take();
                ended.push(task.id.clone());
            }
            state.completed.extend(ended);
            session
        };
        session.worker.abort();
        let _ = session.worker.await;
        self.changed.notify_waiters();
        Ok(json!({"closed":true}))
    }
    async fn models(&self, args: Models) -> Result<Value> {
        let mut config = self
            .runtimes
            .get(&args.harness)
            .context("harness is not configured")?
            .clone();
        config.cwd = Some(Self::canonical_cwd(&args.cwd)?);
        if let Some(provider) = &args.provider {
            ensure!(
                args.harness == Harness::Dsh,
                "provider only supported by dsh"
            );
            config.provider = Some(provider.clone());
        }
        let key = format!("{:?}:{:?}:{:?}", args.harness, config.cwd, args.provider);
        let _permit = self.model_slot.acquire().await?;
        if !args.refresh.unwrap_or(false) {
            let state = self.state.lock().unwrap();
            if let Some((time, value)) = state.models.get(&key) {
                if time.elapsed() < Duration::from_secs(300) {
                    return Ok(value.clone());
                }
            }
        }
        let models = tokio::time::timeout(
            Duration::from_secs(60),
            Backend::models(&config, args.provider.as_deref()),
        )
        .await
        .context("model discovery timed out")??;
        let mut state = self.state.lock().unwrap();
        if state.models.len() >= 32 {
            if let Some(first) = state.models.keys().next().cloned() {
                state.models.remove(&first);
            }
        }
        state.models.insert(key, (Instant::now(), models.clone()));
        Ok(models)
    }
    // Registry mutations are brief. Only the session actor owns its backend.
    // The ordinary queue stays in Pyxis until a global active permit is held.
    async fn worker(
        self: Arc<Self>,
        sid: String,
        config: AgentConfig,
        wake: Arc<Notify>,
        mut controls: mpsc::Receiver<Control>,
    ) {
        let mut backend: Option<Backend> = None;
        let mut last_task: Option<String> = None;
        let mut cache_due: Option<crate::session::Deadline> = None;
        let mut ttl = config.cache.ttl_seconds;
        loop {
            let notified = wake.notified();
            tokio::pin!(notified);
            notified.as_mut().enable();
            let queued = {
                let state = self.state.lock().unwrap();
                let Some(session) = state.sessions.get(&sid) else {
                    break;
                };
                if session.invalid {
                    break;
                }
                !session.queue.is_empty()
            };
            if !queued {
                tokio::select! {
                    _=notified=>continue,
                    control=controls.recv()=>{if let Some(control)=control{Self::stale(control);}else{break;}},
                    event=async {match backend.as_mut(){Some(b)=>b.idle_event().await,None=>std::future::pending().await}}=>{
                        match event {
                            Ok(event)=>{
                                if event.get("id").is_some() && (event.get("method").is_some() || event["type"]=="control_request") {
                                    if let Some(tid)=&last_task {self.invalidate_by_task(tid,"unexpected interactive request outside an active task",false);}break;
                                }
                                if let Some(tid)=&last_task{self.event(tid,json!({"type":"idle_progress","native":event}));}
                            }
                            Err(error)=>{if let Some(tid)=&last_task{self.invalidate_by_task(tid,&format!("{error:#}"),false);}break;}
                        }
                    },
                    _=async {match cache_due{Some(d)=>tokio::time::sleep_until(d.compact_at).await,None=>std::future::pending().await}}=>{
                        let due=cache_due.take().unwrap();
                        if tokio::time::Instant::now()<due.expires_at {
                            match self.enqueue(sid.clone(),true,true,String::new(),Some(config.request_timeout_seconds)) {
                                Ok(task)=>{if let Some(tid)=&last_task{self.event(tid,json!({"type":"automatic_compaction_queued","task":task}));}}
                                Err(error)=>{if let Some(tid)=&last_task{self.event(tid,json!({"type":"automatic_compaction_skipped","error":format!("{error:#}")}));}}
                            }
                        }
                    }
                }
                continue;
            }
            let permit = tokio::select! {
                permit=self.active.clone().acquire_owned()=>match permit{Ok(p)=>p,Err(_)=>break},
                _=notified=>continue,
                control=controls.recv()=>{if let Some(control)=control{Self::stale(control);}else{break;}continue;}
            };
            let next = {
                let mut state = self.state.lock().unwrap();
                let position = state
                    .sessions
                    .get(&sid)
                    .and_then(|s| {
                        s.queue
                            .iter()
                            .position(|id| state.tasks.get(id).is_some_and(|t| !t.automatic))
                    })
                    .unwrap_or(0);
                let Some(session) = state.sessions.get_mut(&sid) else {
                    break;
                };
                if session.invalid {
                    break;
                }
                let Some(tid) = session.queue.remove(position) else {
                    continue;
                };
                session.running = Some(tid.clone());
                let Some(task) = state.tasks.get_mut(&tid) else {
                    continue;
                };
                if task.state != "queued" {
                    continue;
                }
                task.state = "running";
                task.queued_permit.take();
                (
                    tid,
                    task.prompt.clone(),
                    task.compact,
                    task.automatic,
                    task.deadline,
                )
            };
            let (tid, prompt, compact, automatic, deadline) = next;
            self.changed.notify_waiters();
            if !compact {
                cache_due = None;
            }
            let started = tokio::time::Instant::now();
            let startup = if backend.is_none() {
                Some(
                    tokio::time::timeout_at(
                        tokio::time::Instant::from_std(deadline),
                        Backend::start_dynamic(&config),
                    )
                    .await,
                )
            } else {
                None
            };
            if let Some(startup) = startup {
                match startup {
                    Ok(Ok(b)) => {
                        let mut state = self.state.lock().unwrap();
                        if let Some(s) = state.sessions.get_mut(&sid) {
                            s.effective = b.effective_config.clone();
                        }
                        drop(state);
                        self.event(
                            &tid,
                            json!({"type":"effective_config","config":b.effective_config}),
                        );
                        backend = Some(b);
                    }
                    other => {
                        let error = match other {
                            Ok(Err(e)) => format!("{e:#}"),
                            _ => "task timed out during initialization".into(),
                        };
                        self.finish(&sid, &tid, Err(anyhow::anyhow!(error)));
                        self.invalidate_by_task(&tid, "session initialization failed", false);
                        break;
                    }
                }
            }
            self.event(&tid,json!({"type":"effective_config","config":backend.as_ref().unwrap().effective_config}));
            let tx = {
                let state = self.state.lock().unwrap();
                state.sessions.get(&sid).unwrap().control.clone()
            };
            let event_tid = tid.clone();
            let me = Arc::clone(&self);
            let run = backend.as_mut().unwrap().run_dynamic(
                &prompt,
                compact,
                &tid,
                &mut controls,
                move |event: DynamicEvent| me.event(&event_tid, event.into_value()),
            );
            tokio::pin!(run);
            let (outcome, lost) = tokio::select! {
                result=&mut run=>{let lost=result.is_err();(result,lost)},
                _=tokio::time::sleep_until(tokio::time::Instant::from_std(deadline))=>{
                    let (reply,_)=oneshot::channel();
                    let _=tx.try_send(Control::Cancel {task_id:tid.clone(),reply});
                    let stopped=tokio::time::timeout(Duration::from_secs(10),&mut run).await;
                    let lost=!matches!(stopped,Ok(Ok(ref outcome)) if outcome.cancelled);
                    (Err(anyhow::anyhow!("task total timeout exceeded")),lost)
                }
            };
            if let Ok(outcome) = &outcome {
                if !compact && !outcome.cancelled && config.cache.enabled {
                    if config.cache.use_usage_ttl {
                        ttl = crate::session::observed_ttl(&outcome.usage).unwrap_or(ttl);
                    }
                    cache_due = Some(crate::session::deadline(
                        started,
                        ttl,
                        config.cache.compact_margin_seconds,
                    ));
                }
            }
            self.finish(&sid, &tid, outcome);
            last_task = Some(tid.clone());
            drop(permit);
            if lost {
                self.invalidate_by_task(
                    &tid,
                    if automatic {
                        "automatic compaction failed; context is invalid"
                    } else {
                        "harness failure; context is invalid"
                    },
                    false,
                );
                break;
            }
        }
        // Dropping backend closes its Job Object / process group. Invalid
        // sessions remain queryable until explicitly closed or idle-reaped.
    }
    fn stale(control: Control) {
        match control {
            Control::Steer { reply, .. } => {
                let _ = reply.send(Ok(json!({"applied":false,"reason":"target task ended"})));
            }
            Control::Respond { reply, .. } => {
                let _ = reply.send(Ok(json!({"accepted":false,"reason":"interaction expired"})));
            }
            Control::Cancel { reply, .. } => {
                let _ = reply.send(Ok(json!({"cancelled":false,"reason":"target task ended"})));
            }
        }
    }
    fn finish(&self, sid: &str, tid: &str, outcome: Result<crate::harness::Outcome>) {
        let mut state = self.state.lock().unwrap();
        if let Some(task) = state.tasks.get_mut(tid) {
            match outcome {
                Ok(outcome) => {
                    task.state = if outcome.cancelled {
                        "cancelled"
                    } else {
                        "completed"
                    };
                    let mut value = serde_json::to_value(outcome).unwrap();
                    if value.to_string().len() > RESULT_BYTES {
                        if let Some(text) = value["result"].as_str() {
                            let end = text
                                .char_indices()
                                .map(|(i, _)| i)
                                .take_while(|i| *i < RESULT_BYTES / 2)
                                .last()
                                .unwrap_or(0);
                            let text = text[..end].to_owned();
                            value["result"] = json!(text);
                            value["result_truncated"] = json!(true);
                        }
                    }
                    if value.to_string().len() > RESULT_BYTES {
                        value["usage"] = json!({"expired":true,"reason":"native usage exceeded result retention budget"});
                        value["usage_truncated"] = json!(true);
                    }
                    task.result = Some(value);
                }
                Err(error) => {
                    task.state = "failed";
                    let mut message = format!("{error:#}");
                    if message.len() > 64 * 1024 {
                        let mut end = 64 * 1024;
                        while !message.is_char_boundary(end) {
                            end -= 1;
                        }
                        message.truncate(end);
                        message.push_str(" [error truncated]");
                    }
                    task.error = Some(message);
                }
            }
            task.prompt.clear();
            task.queued_permit.take();
            task.interactions.clear();
        }
        state.completed.push_back(tid.into());
        if let Some(session) = state.sessions.get_mut(sid) {
            session.running = None;
            session.last_used = Instant::now();
        }
        self.prune(&mut state);
        let status = state.tasks.get(tid).map(|t| t.state);
        drop(state);
        self.event(tid, json!({"type":"task_ended","status":status}));
    }
    fn prune(&self, state: &mut Registry) {
        while state.completed.len() > self.limits.retained_tasks {
            if let Some(tid) = state.completed.pop_front() {
                state.tasks.remove(&tid);
            }
        }
    }
    fn reap(&self) {
        let mut state = self.state.lock().unwrap();
        let now = Instant::now();
        let mut expired = Vec::new();
        for task in state
            .tasks
            .values_mut()
            .filter(|t| t.state == "queued" && t.deadline <= now)
        {
            task.state = "failed";
            task.queued_permit.take();
            task.prompt.clear();
            task.error = Some("task total timeout exceeded while queued".into());
            expired.push(task.id.clone());
        }
        state.completed.extend(expired.iter().cloned());
        let pending: std::collections::BTreeSet<_> = state
            .tasks
            .iter()
            .filter(|(_, t)| t.state == "queued")
            .map(|(id, _)| id.clone())
            .collect();
        let mut idle = Vec::new();
        for session in state.sessions.values_mut() {
            let previous = session.queue.len();
            session.queue.retain(|id| pending.contains(id));
            if session.running.is_none()
                && session.queue.is_empty()
                && session.last_used.elapsed() >= Duration::from_secs(self.limits.idle_seconds)
            {
                idle.push(session.id.clone());
            }
            if session.queue.len() != previous {
                session.wake.notify_one();
            }
        }
        for sid in idle {
            if let Some(session) = state.sessions.remove(&sid) {
                session.worker.abort();
            }
        }
        self.prune(&mut state);
        drop(state);
        for tid in expired {
            self.event(
                &tid,
                json!({"type":"task_ended","status":"failed","reason":"queue timeout"}),
            );
        }
        self.changed.notify_waiters();
    }
    pub async fn close_all(&self) {
        let sessions = {
            let mut state = self.state.lock().unwrap();
            std::mem::take(&mut state.sessions)
        };
        for session in sessions.values() {
            session.worker.abort();
        }
        for (_, session) in sessions {
            let _ = session.worker.await;
        }
    }
}

pub fn definitions() -> Vec<Value> {
    fn tool(name: &str, description: &str, properties: Value, required: &[&str]) -> Value {
        json!({"name":name,"description":description,"inputSchema":{"type":"object","properties":properties,"required":required,"additionalProperties":false}})
    }
    let sid = json!({"session_id":{"type":"string"}});
    let tid = json!({"task_id":{"type":"string"}});
    vec![
        tool(
            "agent_models",
            "Query native model and effort capabilities",
            json!({"harness":{"enum":["claude","codex","dsh"]},"cwd":{"type":"string"},"provider":{"type":"string"},"refresh":{"type":"boolean"}}),
            &["harness", "cwd"],
        ),
        tool(
            "agent_start",
            "Start an independent agent session and task",
            json!({"harness":{"enum":["claude","codex","dsh"]},"cwd":{"type":"string"},"prompt":{"type":"string"},"model":{"type":"string"},"provider":{"type":"string"},"effort":{"type":["string","null"],"description":"Omit for runtime default; null explicitly selects the native default."},"mode":{"enum":["plan","auto"]},"timeout_seconds":{"type":"integer"},"idempotency_key":{"type":"string"}}),
            &["harness", "cwd", "prompt"],
        ),
        tool(
            "agent_send",
            "Queue a turn or steer the current task",
            json!({"session_id":{"type":"string"},"prompt":{"type":"string"},"action":{"enum":["queue","steer"]},"task_id":{"type":"string"},"timeout_seconds":{"type":"integer"},"idempotency_key":{"type":"string"}}),
            &["session_id", "prompt"],
        ),
        tool("agent_list", "List live sessions", json!({}), &[]),
        tool(
            "agent_status",
            "Get task status and cursor-paginated events",
            json!({"task_id":{"type":"string"},"session_id":{"type":"string"},"cursor":{"type":"integer","minimum":0},"event_limit":{"type":"integer","minimum":1,"maximum":128}}),
            &[],
        ),
        tool(
            "agent_wait",
            "Wait for completion or interaction on several tasks",
            json!({"task_ids":{"type":"array","items":{"type":"string"}},"cursor":{"type":"object"},"timeout_seconds":{"type":"integer"}}),
            &["task_ids"],
        ),
        tool(
            "agent_respond",
            "Answer a pending approval or question",
            json!({"interaction_id":{"type":"string"},"response":{}}),
            &["interaction_id", "response"],
        ),
        tool(
            "agent_cancel",
            "Cancel a running or queued task",
            tid,
            &["task_id"],
        ),
        tool(
            "agent_close",
            "Close a session and its process tree",
            sid.clone(),
            &["session_id"],
        ),
        tool(
            "agent_compact",
            "Queue a native context compaction",
            json!({"session_id":{"type":"string"},"timeout_seconds":{"type":"integer"},"idempotency_key":{"type":"string"}}),
            &["session_id"],
        ),
    ]
}
