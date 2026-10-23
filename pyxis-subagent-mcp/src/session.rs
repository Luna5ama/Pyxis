use crate::{
    config::AgentConfig,
    harness::{Backend, Outcome},
};
use anyhow::{Result, bail};
use serde_json::{Value, json};
use std::sync::Arc;
use tokio::{
    sync::{Mutex, Notify, OwnedSemaphorePermit, Semaphore, watch},
    task::JoinHandle,
    time::{Instant, sleep_until, timeout},
};

#[derive(Clone)]
pub struct Resources {
    pub active: Arc<Semaphore>,
    pub live: Arc<Semaphore>,
    pub queued: Arc<Semaphore>,
    pub idle: std::time::Duration,
}
impl Resources {
    pub fn new(limits: &crate::config::Limits) -> Self {
        Self {
            active: Arc::new(Semaphore::new(limits.active_tasks)),
            live: Arc::new(Semaphore::new(limits.sessions)),
            queued: Arc::new(Semaphore::new(limits.queued_tasks)),
            idle: std::time::Duration::from_secs(limits.idle_seconds),
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Deadline {
    pub compact_at: Instant,
    pub expires_at: Instant,
}

/// Use the shortest observed TTL bucket, or retain the last known TTL on read-only hits.
pub fn observed_ttl(usage: &Value) -> Option<u64> {
    if let Some(turns) = usage["native_turns"].as_array() {
        return turns.iter().filter_map(observed_ttl).min();
    }
    let creation = &usage["cache_creation"];
    if creation["ephemeral_5m_input_tokens"].as_u64().unwrap_or(0) > 0 {
        Some(300)
    } else if creation["ephemeral_1h_input_tokens"].as_u64().unwrap_or(0) > 0 {
        Some(3600)
    } else {
        None
    }
}

pub fn deadline(start: Instant, ttl: u64, margin: u64) -> Deadline {
    let ttl = std::time::Duration::from_secs(ttl);
    // If usage reveals a shorter TTL, preserve some scheduling window.
    let requested_margin = std::time::Duration::from_secs(margin);
    let margin = if requested_margin >= ttl {
        ttl / 2
    } else {
        requested_margin
    };
    Deadline {
        compact_at: start + ttl - margin,
        expires_at: start + ttl,
    }
}

#[derive(Default)]
struct State {
    backend: Option<Backend>,
    deadline: Option<Deadline>,
    ttl: Option<u64>,
    session_id: Option<String>,
    resume_id: Option<String>,
    failure: Option<String>,
    live: Option<OwnedSemaphorePermit>,
    last_used: Option<Instant>,
}

pub struct Session {
    pub config: AgentConfig,
    pub name: String,
    state: Mutex<State>,
    changed: Notify,
    resources: Resources,
}

impl Session {
    pub fn new(name: String, config: AgentConfig, resources: Resources) -> Arc<Self> {
        Arc::new(Self {
            name,
            config,
            state: Mutex::new(State::default()),
            changed: Notify::new(),
            resources,
        })
    }

    pub async fn call(&self, prompt: &str) -> Result<Outcome> {
        let mut queued = Some(
            self.resources
                .queued
                .clone()
                .try_acquire_owned()
                .map_err(|_| anyhow::anyhow!("queue capacity reached"))?,
        );
        let mut state = self.state.lock().await;
        if let Some(error) = &state.failure {
            bail!("{error}");
        }
        state.deadline = None;
        self.changed.notify_one();
        // Move ownership out of state. Cancellation, timeout and protocol failures
        // drop the process rather than reusing a half-consumed event stream.
        let mut backend = state.backend.take();
        let mut live = state.live.take();
        let start = Instant::now();
        let result = timeout(self.config.timeout(), async {
            let _active = self.resources.active.clone().acquire_owned().await?;
            queued.take();
            if backend.is_none() {
                live = Some(
                    self.resources
                        .live
                        .clone()
                        .try_acquire_owned()
                        .map_err(|_| anyhow::anyhow!("session capacity reached"))?,
                );
                backend = Some(match state.resume_id.as_deref() {
                    Some(id) => Backend::resume(&self.config, false, id).await?,
                    None => Backend::start(&self.config).await?,
                });
            }
            backend.as_mut().unwrap().run(prompt, false).await
        })
        .await;
        let outcome = match result {
            Ok(result) => result?,
            Err(_) => bail!(
                "{}: harness request timed out; process discarded",
                self.name
            ),
        };
        state.session_id = Some(outcome.session_id.clone());
        state.resume_id = None;
        state.backend = backend;
        state.live = live;
        state.last_used = Some(Instant::now());
        if self.config.cache.use_usage_ttl {
            state.ttl = observed_ttl(&outcome.usage).or(state.ttl);
        }
        if self.config.cache.enabled {
            let ttl = state.ttl.unwrap_or(self.config.cache.ttl_seconds);
            state.deadline = Some(deadline(
                start,
                ttl,
                self.config.cache.compact_margin_seconds,
            ));
        }
        log(
            &self.name,
            "task_completed",
            json!({"session_id":outcome.session_id,"usage":outcome.usage,"total_cost_usd":outcome.total_cost_usd}),
        );
        self.changed.notify_one();
        Ok(outcome)
    }

    pub fn spawn_timer(self: &Arc<Self>, mut shutdown: watch::Receiver<bool>) -> JoinHandle<()> {
        let session = self.clone();
        tokio::spawn(async move {
            loop {
                let (due, idle) = {
                    let state = session.state.lock().await;
                    (
                        state.deadline,
                        state
                            .last_used
                            .filter(|_| state.backend.is_some())
                            .map(|time| time + session.resources.idle),
                    )
                };
                tokio::select! {
                    _ = shutdown.changed() => break,
                    _ = session.changed.notified() => {},
                    _=async {match idle {Some(time)=>sleep_until(time).await,None=>std::future::pending().await}}=>{
                        let mut state=session.state.lock().await;
                        if state.last_used.map(|time|time+session.resources.idle)!=idle {continue;}
                        let Some(mut backend) = state.backend.take() else { continue; };
                        let suspended = timeout(std::time::Duration::from_secs(10), backend.suspend()).await;
                        match suspended {
                            Ok(Ok(id))=>state.resume_id=Some(id),
                            _=>{
                                state.failure=Some("native session suspension failed; context cannot be safely resumed".into());
                                log(&session.name,"session_suspend_failed",json!({"session_id":state.session_id}));
                            }
                        }
                        drop(backend);
                        state.live=None;state.deadline=None;state.last_used=None;
                        log(&session.name,"session_idle_reaped",json!({"session_id":state.session_id}));
                    },
                    _ = async { match due { Some(d) => sleep_until(d.compact_at).await, None => std::future::pending().await } } => {
                        let mut state = session.state.lock().await;
                        // A task may have refreshed the deadline while the timer waited.
                        if state.deadline != due { continue; }
                        let Some(due) = state.deadline.take() else { continue; };
                        let Ok(_active)=session.resources.active.clone().try_acquire_owned() else {continue};
                        // Never intentionally submit an already-cold compaction.
                        if Instant::now() >= due.expires_at {
                            log(&session.name, "compact_window_missed", json!({"session_id":state.session_id}));
                            continue;
                        }
                        let Some(mut backend) = state.backend.take() else { continue; };
                        let live=state.live.take();
                        log(&session.name, "compact_started", json!({"session_id":state.session_id}));
                        let result = timeout(session.config.timeout(), backend.run("", true)).await;
                        match result {
                            Ok(Ok(outcome)) => {
                                log(&session.name, if outcome.compacted {"compact_completed"} else {"compact_no_history"}, json!({"session_id":outcome.session_id,"usage":outcome.usage,"total_cost_usd":outcome.total_cost_usd}));
                                state.backend = Some(backend);
                                state.live=live;
                            }
                            Ok(Err(error)) => log(&session.name, "compact_failed", json!({"error":format!("{error:#}")})),
                            Err(_) => log(&session.name, "compact_failed", json!({"error":"request timeout"})),
                        }
                        // One compaction per user activity interval. Do not rearm here.
                    }
                }
            }
        })
    }

    pub async fn close(&self) {
        let mut state = self.state.lock().await;
        state.backend = None;
        state.live = None;
    }
}

pub fn log(tool: &str, action: &str, data: Value) {
    eprintln!(
        "{}",
        json!({"tool":tool,"action":action,"unix_time_ms":std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap_or_default().as_millis(),"data":data})
    );
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn shortest_usage_bucket_wins_and_read_hits_do_not_invent_ttl() {
        assert_eq!(
            observed_ttl(&json!({"cache_creation":{"ephemeral_1h_input_tokens":10}})),
            Some(3600)
        );
        assert_eq!(
            observed_ttl(
                &json!({"cache_creation":{"ephemeral_1h_input_tokens":10,"ephemeral_5m_input_tokens":1}})
            ),
            Some(300)
        );
        assert_eq!(observed_ttl(&json!({"cache_read_input_tokens":100})), None);
    }
    #[test]
    fn deadline_is_based_on_start_not_completion() {
        let start = Instant::now();
        let d = deadline(start, 300, 60);
        assert_eq!(d.compact_at - start, std::time::Duration::from_secs(240));
        assert_eq!(d.expires_at - start, std::time::Duration::from_secs(300));
        assert!(deadline(start, 300, 480).compact_at > start);
        assert_eq!(
            deadline(start, 300, 200).compact_at - start,
            std::time::Duration::from_secs(100)
        );
    }
}
