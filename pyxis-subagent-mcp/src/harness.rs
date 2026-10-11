use crate::{
    config::{AgentConfig, Harness, Mode},
    transport::{Process, reject_request, response},
};
use anyhow::{Context, Result, ensure};
use serde::Serialize;
use serde_json::{Value, json};
use std::{
    path::PathBuf,
    sync::atomic::{AtomicU64, Ordering},
};
use tokio::process::Command;

#[derive(Debug, Default, Serialize)]
pub struct Outcome {
    pub result: String,
    pub session_id: String,
    pub usage: Value,
    pub total_cost_usd: Option<f64>,
    pub compacted: bool,
}

pub struct Backend {
    process: Process,
    kind: Harness,
    session_id: String,
    model: String,
    effort: Option<String>,
    mode: Mode,
    _bridge: Option<BridgeFiles>,
}

impl Backend {
    pub async fn start(config: &AgentConfig) -> Result<Self> {
        let mut command = Command::new(&config.executable);
        command
            .args(&config.args)
            .current_dir(config.cwd.as_ref().unwrap());
        let mut bridge = None;
        match config.harness {
            Harness::Claude => {
                command.args([
                    "-p",
                    "--input-format",
                    "stream-json",
                    "--output-format",
                    "stream-json",
                    "--verbose",
                    "--permission-prompts",
                    "none",
                    "--model",
                    &config.model,
                    "--permission-mode",
                    if config.mode == Mode::Plan {
                        "plan"
                    } else {
                        "auto"
                    },
                ]);
                if let Some(effort) = &config.reasoning_effort {
                    command.args(["--effort", effort]);
                }
                if !config.prefix_prompt.is_empty() {
                    command.args(["--append-system-prompt", &config.prefix_prompt]);
                }
                // Avoid a recursive connection back to this MCP via project settings.
                command.args(["--strict-mcp-config", "--mcp-config", "{\"mcpServers\":{}}"]);
            }
            Harness::Codex => {
                command.args(["app-server", "--listen", "stdio://"]);
            }
            Harness::Dsh => {
                let files = BridgeFiles::new()?;
                command
                    .args(["--profile", "sdk", "--patch"])
                    .arg(&files.patch);
                command.env(
                    "PYXIS_DSH_MODULE_ROOT",
                    config.dsh_module_root.as_ref().unwrap(),
                );
                bridge = Some(files);
            }
        }
        let mut backend = Self {
            process: Process::spawn(command)?,
            kind: config.harness,
            session_id: String::new(),
            model: config.model.clone(),
            effort: config.reasoning_effort.clone(),
            mode: config.mode,
            _bridge: bridge,
        };
        let initialized = tokio::time::timeout(std::time::Duration::from_secs(30), async {
        match config.harness {
            Harness::Claude => {}, // Claude's stream input has no mandatory handshake.
            Harness::Codex => {
                backend.rpc("initialize", json!({"clientInfo":{"name":"pyxis-subagent-mcp","title":"Pyxis subagent MCP","version":env!("CARGO_PKG_VERSION")},"capabilities":{"experimentalApi":true}})).await?;
                backend.process.send(json!({"method":"initialized"})).await?;
                let thread = backend.rpc("thread/start", json!({"model":config.model,"cwd":config.cwd,"approvalPolicy":"never","sandbox":if config.mode == Mode::Plan {"read-only"} else {"workspace-write"},"developerInstructions":config.prefix_prompt})).await?;
                backend.session_id = thread["thread"]["id"].as_str().context("thread/start missing thread.id")?.to_owned();
            }
            Harness::Dsh => {
                backend.rpc("initialize", json!({"cwd":config.cwd,"provider":config.provider,"model":config.model,"reasoningEffort":config.reasoning_effort})).await?;
                let session = backend.rpc("pyxis/session/start", json!({"mode":config.mode,"prefixPrompt":config.prefix_prompt})).await?;
                backend.session_id = session["sessionId"].as_str().context("dsh missing sessionId")?.to_owned();
            }
        }
        Ok::<_,anyhow::Error>(())
        }).await;
        match initialized {
            Ok(result) => result?,
            Err(_) => anyhow::bail!(
                "Harness initialization timed out after 30s; {}",
                backend.process.diagnostics()
            ),
        }
        Ok(backend)
    }

    async fn rpc(&mut self, method: &str, params: Value) -> Result<Value> {
        let id = self.process.request(method, params).await?;
        loop {
            let event = self.process.recv().await?;
            if let Some(result) = response(&event, id)? {
                return Ok(result);
            }
            reject_request(&mut self.process, &event).await?;
        }
    }

    pub async fn run(&mut self, prompt: &str, compact: bool) -> Result<Outcome> {
        match self.kind {
            Harness::Claude => self.claude(prompt, compact).await,
            Harness::Codex => self.codex(prompt, compact).await,
            Harness::Dsh => self.dsh(prompt, compact).await,
        }
    }

    async fn claude(&mut self, prompt: &str, compact: bool) -> Result<Outcome> {
        self.process.send(json!({"type":"user","session_id":self.session_id,"parent_tool_use_id":null,"message":{"role":"user","content":if compact {"/compact"} else {prompt}}})).await?;
        let mut compacted = false;
        loop {
            let event = self.process.recv().await?;
            if let Some(id) = event["session_id"].as_str() {
                if self.session_id.is_empty() {
                    self.session_id = id.to_owned();
                }
                ensure!(
                    id == self.session_id,
                    "Claude unexpectedly changed session ID"
                );
            }
            match event["type"].as_str() {
                Some("system") if event["subtype"] == "compact_boundary" => {
                    compacted = true;
                }
                Some("control_request") => {
                    // Do not implement undocumented control APIs. Explicitly reject callbacks.
                    self.process.send(json!({"type":"control_response","response":{"subtype":"error","request_id":event["request_id"],"error":"Interactive callbacks are unavailable"}})).await?;
                }
                Some("result") => {
                    ensure!(
                        event["is_error"] != true && event["subtype"] == "success",
                        "Claude failed: {event}"
                    );
                    ensure!(!self.session_id.is_empty(), "Claude returned no session ID");
                    ensure!(
                        !compact || compacted,
                        "Claude did not emit compact_boundary: {}",
                        event["result"]
                    );
                    return Ok(Outcome {
                        result: event["result"].as_str().unwrap_or("").to_owned(),
                        session_id: self.session_id.clone(),
                        usage: event["usage"].clone(),
                        total_cost_usd: event["total_cost_usd"].as_f64(),
                        compacted,
                    });
                }
                _ => {}
            }
        }
    }

    async fn codex(&mut self, prompt: &str, compact: bool) -> Result<Outcome> {
        let id = if compact {
            self.process
                .request("thread/compact/start", json!({"threadId":self.session_id}))
                .await?
        } else {
            self.process.request("turn/start", json!({"threadId":self.session_id,"input":[{"type":"text","text":prompt}],"collaborationMode":{"mode":if self.mode == Mode::Plan {"plan"} else {"default"},"settings":{"model":self.model,"reasoning_effort":self.effort,"developer_instructions":null}}})).await?
        };
        let mut outcome = Outcome {
            session_id: self.session_id.clone(),
            ..Outcome::default()
        };
        let mut turn_id = None;
        let mut accepted = false;
        let mut finished = false;
        loop {
            let event = self.process.recv().await?;
            if let Some(result) = response(&event, id)? {
                accepted = true;
                turn_id = result["turn"]["id"].as_str().map(str::to_owned);
                if finished {
                    ensure!(
                        !compact || outcome.compacted,
                        "Codex completed without a contextCompaction item"
                    );
                    return Ok(outcome);
                }
                continue;
            }
            if reject_request(&mut self.process, &event).await? {
                continue;
            }
            let p = &event["params"];
            if p["threadId"].as_str() != Some(&self.session_id) {
                continue;
            }
            if let (Some(expected), Some(actual)) =
                (turn_id.as_deref(), p.get("turnId").and_then(Value::as_str))
            {
                if actual != expected {
                    continue;
                }
            }
            match event["method"].as_str() {
                Some("item/completed") if p["item"]["type"] == "agentMessage" => {
                    if p["item"]["phase"] != "commentary" {
                        if let Some(text) = p["item"]["text"].as_str() {
                            outcome.result = text.to_owned();
                        }
                    }
                }
                Some("item/completed") if p["item"]["type"] == "contextCompaction" => {
                    outcome.compacted = true;
                }
                Some("thread/tokenUsage/updated") => {
                    outcome.usage = p["tokenUsage"].clone();
                }
                Some("turn/completed") => {
                    if let Some(expected) = turn_id.as_deref() {
                        if p["turn"]["id"].as_str() != Some(expected) {
                            continue;
                        }
                    }
                    ensure!(
                        p["turn"]["status"] == "completed",
                        "Codex turn failed: {}",
                        p["turn"]
                    );
                    finished = true;
                    if accepted {
                        ensure!(
                            !compact || outcome.compacted,
                            "Codex completed without a contextCompaction item"
                        );
                        return Ok(outcome);
                    }
                }
                _ => {}
            }
        }
    }

    async fn dsh(&mut self, prompt: &str, compact: bool) -> Result<Outcome> {
        if compact {
            let result = self
                .rpc(
                    "pyxis/session/compact",
                    json!({"sessionId":self.session_id}),
                )
                .await?;
            ensure!(
                result["compacted"].is_boolean(),
                "invalid dsh compact result"
            );
            // A null native result means there was no compactable history.
            return Ok(Outcome {
                session_id: self.session_id.clone(),
                compacted: result["compacted"] == true,
                ..Outcome::default()
            });
        }
        let result = self
            .rpc(
                "pyxis/session/run",
                json!({"sessionId":self.session_id,"prompt":prompt}),
            )
            .await?;
        ensure!(
            result["stopReason"] == "completed",
            "dsh turn failed: {result}"
        );
        Ok(Outcome {
            result: result["text"]
                .as_str()
                .context("dsh missing final text")?
                .to_owned(),
            session_id: self.session_id.clone(),
            usage: result["usage"].clone(),
            ..Outcome::default()
        })
    }
}

struct BridgeFiles {
    directory: PathBuf,
    patch: PathBuf,
}
impl BridgeFiles {
    fn new() -> Result<Self> {
        static NEXT: AtomicU64 = AtomicU64::new(0);
        let directory = std::env::temp_dir().join(format!(
            "pyxis-dsh-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir(&directory).context("create private dsh bridge directory")?;
        let files = Self {
            patch: directory.join("bridge.json"),
            directory,
        };
        let module = files.directory.join("bridge.mjs");
        std::fs::write(&module, include_str!("../dsh/bridge.mjs"))?;
        let patch = json!([
            {"id":"sdk-jsonrpc-server","disabled":true},
            {"insert":[{"id":"pyxis-jsonrpc-server","name":module,"inject":["sdkAppStartup","loader"]}]}
        ]);
        std::fs::write(&files.patch, serde_json::to_vec(&patch)?)?;
        Ok(files)
    }
}
impl Drop for BridgeFiles {
    fn drop(&mut self) {
        // Remove only our two files and empty directory, never a recursive path.
        let _ = std::fs::remove_file(&self.patch);
        let _ = std::fs::remove_file(self.directory.join("bridge.mjs"));
        let _ = std::fs::remove_dir(&self.directory);
    }
}
