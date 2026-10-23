use crate::{
    config::{AgentConfig, Harness, Mode},
    transport::{Process, reject_request, response},
};
use anyhow::{Context, Result, ensure};
use serde::Serialize;
use serde_json::{Value, json};
use std::{
    collections::HashMap,
    path::PathBuf,
    sync::atomic::{AtomicU64, Ordering},
};
use tokio::process::Command;
use tokio::sync::{mpsc, oneshot};

pub enum Control {
    Steer {
        task_id: String,
        prompt: String,
        reply: oneshot::Sender<Result<Value>>,
    },
    Respond {
        interaction_id: String,
        response: Value,
        reply: oneshot::Sender<Result<Value>>,
    },
    Cancel {
        task_id: String,
        reply: oneshot::Sender<Result<Value>>,
    },
}

pub struct DynamicEvent(pub Value);
impl DynamicEvent {
    pub fn into_value(self) -> Value {
        self.0
    }
}

#[derive(Debug, Default, Serialize)]
pub struct Outcome {
    pub result: String,
    pub session_id: String,
    pub usage: Value,
    pub total_cost_usd: Option<f64>,
    pub compacted: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub cost_scope: Option<&'static str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub usage_scope: Option<&'static str>,
    #[serde(skip_serializing_if = "std::ops::Not::not")]
    pub cancelled: bool,
}

pub struct Backend {
    process: Process,
    kind: Harness,
    session_id: String,
    model: String,
    effort: Option<String>,
    mode: Mode,
    pub effective_config: Value,
    metadata: Value,
    _bridge: Option<BridgeFiles>,
}

impl Backend {
    pub async fn start(config: &AgentConfig) -> Result<Self> {
        Self::start_impl(config, false, false, None).await
    }
    pub async fn start_dynamic(config: &AgentConfig) -> Result<Self> {
        Self::start_impl(config, true, false, None).await
    }
    pub async fn resume(config: &AgentConfig, interactive: bool, session_id: &str) -> Result<Self> {
        Self::start_impl(config, interactive, false, Some(session_id)).await
    }
    pub async fn suspend(&mut self) -> Result<String> {
        ensure!(
            !self.session_id.is_empty(),
            "cannot suspend without native session identity"
        );
        if self.kind == Harness::Dsh {
            self.rpc(
                "pyxis/session/suspend",
                json!({"sessionId":self.session_id}),
            )
            .await?;
        }
        Ok(self.session_id.clone())
    }
    async fn start_impl(
        config: &AgentConfig,
        interactive: bool,
        discovery: bool,
        resume: Option<&str>,
    ) -> Result<Self> {
        let mut command = Command::new(&config.executable);
        command
            .args(&config.args)
            .current_dir(config.cwd.as_ref().unwrap())
            .env("PYXIS_SUBAGENT_NESTED", "1");
        let mut bridge = None;
        match config.harness {
            Harness::Claude => {
                if let Some(id) = resume {
                    command.args(["--resume", id]);
                }
                if interactive {
                    command.env("CLAUDE_CODE_SDK_READS_SESSION_STATE", "1");
                    // Use the SDK host identity supported by the installed CLI
                    // together with its documented stdio permission callback.
                    command.env("CLAUDE_CODE_ENTRYPOINT", "sdk-py");
                    command.args(["--permission-prompt-tool", "stdio"]);
                    command.args(["--disallowedTools", "EnterPlanMode", "ExitPlanMode"]);
                }
                command.args([
                    "-p",
                    "--input-format",
                    "stream-json",
                    "--output-format",
                    "stream-json",
                    "--verbose",
                    "--permission-prompts",
                    if interactive { "host" } else { "none" },
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
                // Inherit MCP configuration. Nested Pyxis connections suppress
                // their own execution tools through PYXIS_SUBAGENT_NESTED.
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
            session_id: resume.unwrap_or_default().to_owned(),
            model: config.model.clone(),
            effort: config.reasoning_effort.clone(),
            mode: config.mode,
            effective_config: json!({"harness":config.harness,"model":config.model,"effort":config.reasoning_effort,"provider":config.provider,"cwd":config.cwd,"mode":config.mode}),
            metadata: Value::Null,
            _bridge: bridge,
        };
        let initialized = tokio::time::timeout(std::time::Duration::from_secs(30), async {
        match config.harness {
            Harness::Claude => {
                if interactive {
                    backend.process.send(json!({"type":"control_request","request_id":"initialize","request":{"subtype":"initialize","hooks":null}})).await?;
                    loop {
                        let event=backend.process.recv().await?;
                        if event["type"]=="control_response" && event["response"]["request_id"]=="initialize" {
                            ensure!(event["response"]["subtype"]=="success","Claude initialization failed: {event}");
                            backend.metadata=event["response"]["response"].clone();
                            if !discovery {
                                if let Some(model)=backend.metadata["models"].as_array().and_then(|models|models.iter().find(|m|m["value"]==config.model || m["resolvedModel"]==config.model)) {
                                    if let Some(effort)=&config.reasoning_effort {
                                        if let Some(levels)=model["supportedEffortLevels"].as_array() {ensure!(levels.iter().any(|v|v==effort),"Claude model {} does not support effort {effort}",config.model);}
                                        ensure!(model["supportsEffort"]!=false,"Claude model does not support effort");
                                    }
                                    ensure!(config.mode!=Mode::Auto || model["supportsAutoMode"]!=false,"Claude model does not support auto mode");
                                }
                            }
                            break;
                        }
                    }
                }
            },
            Harness::Codex => {
                backend.rpc("initialize", json!({"clientInfo":{"name":"pyxis-subagent-mcp","title":"Pyxis subagent MCP","version":env!("CARGO_PKG_VERSION")},"capabilities":{"experimentalApi":true}})).await?;
                backend.process.send(json!({"method":"initialized"})).await?;
                if !discovery {
                    if interactive {
                        if let Some(effort)=&config.reasoning_effort {
                            let catalog=backend.rpc("model/list",json!({"limit":100})).await?;
                            if let Some(model)=catalog["data"].as_array().and_then(|models|models.iter().find(|m|m["model"]==config.model || m["id"]==config.model)) {
                                if let Some(levels)=model["supportedReasoningEfforts"].as_array(){ensure!(levels.iter().any(|v|v["reasoningEffort"]==*effort),"Codex model {} does not support effort {effort}",config.model);}
                            }
                        }
                    }
                    let mut params=json!({"model":config.model,"cwd":config.cwd,"developerInstructions":config.prefix_prompt});
                    if !interactive {params["approvalPolicy"]=json!("never");params["sandbox"]=json!(if config.mode==Mode::Plan {"read-only"} else {"workspace-write"});}
                    else {
                        if let Some(policy)=&config.approval_policy {params["approvalPolicy"]=policy.clone();}
                        if let Some(sandbox)=&config.sandbox {params["sandbox"]=json!(sandbox);}
                        if config.mode==Mode::Plan {params["sandbox"]=json!("read-only");}
                    }
                    let method = if let Some(id) = resume {
                        params["threadId"] = json!(id);
                        "thread/resume"
                    } else { "thread/start" };
                    let thread = backend.rpc(method,params).await?;
                    backend.session_id = thread["thread"]["id"].as_str().context("thread/start missing thread.id")?.to_owned();
                    ensure!(resume.is_none_or(|id| id == backend.session_id), "resumed a different Codex thread");
                    let mut native=thread; native.as_object_mut().unwrap().remove("thread");backend.effective_config["native"]=native;
                }
            }
            Harness::Dsh => {
                backend.rpc("initialize", json!({"cwd":config.cwd,"provider":config.provider,"model":config.model,"reasoningEffort":config.reasoning_effort,"discoveryOnly":discovery})).await?;
                if !discovery {
                    let session = backend.rpc("pyxis/session/start", json!({"mode":config.mode,"prefixPrompt":config.prefix_prompt,"interactive":interactive,"resumeSessionId":resume})).await?;
                    backend.session_id = session["sessionId"].as_str().context("dsh missing sessionId")?.to_owned();
                    ensure!(resume.is_none_or(|id| id == backend.session_id), "resumed a different DSH session");
                    backend.effective_config["native"]=session["effectiveConfig"].clone();
                }
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
            if let Some(result) =
                response(&event, id).with_context(|| self.process.diagnostics())?
            {
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

    pub async fn models(config: &AgentConfig, provider_filter: Option<&str>) -> Result<Value> {
        match config.harness {
            Harness::Codex => {
                let mut backend = Self::start_impl(config, true, true, None).await?;
                let mut models = Vec::new();
                let mut cursor = Value::Null;
                loop {
                    let page = backend
                        .rpc("model/list", json!({"limit":100,"cursor":cursor}))
                        .await?;
                    if let Some(data) = page["data"].as_array() {
                        models.extend(data.iter().cloned());
                    }
                    cursor = page["nextCursor"].clone();
                    if cursor.is_null() {
                        break;
                    }
                    ensure!(models.len() < 4096, "model catalog exceeds 4096 entries");
                }
                Ok(
                    json!({"harness":"codex","default":{"model":config.model,"effort":config.reasoning_effort},"models":models,"capabilities":{"steer":"native","compact":"native","interactive":true},"catalog_is_entitlement_check":false}),
                )
            }
            Harness::Dsh => {
                let mut backend = Self::start_impl(config, true, true, None).await?;
                let models = backend
                    .rpc("pyxis/models/list", json!({"provider":provider_filter}))
                    .await?;
                Ok(
                    json!({"harness":"dsh","default":{"provider":config.provider,"model":config.model,"effort":config.reasoning_effort},"models":models,"capabilities":{"steer":"native","compact":"native","interactive":true,"plan_mode":"soft_guidance"},"catalog_is_entitlement_check":false}),
                )
            }
            Harness::Claude => {
                let backend = Self::start_impl(config, true, true, None).await?;
                Ok(
                    json!({"harness":"claude","default":{"model":config.model,"effort":config.reasoning_effort},"models":backend.metadata["models"],"cli_version":backend.metadata["claude_code_version"],"capabilities":{"steer":"interrupt_and_continue","compact":"native","interactive":true},"catalog_is_entitlement_check":false}),
                )
            }
        }
    }

    pub async fn idle_event(&mut self) -> Result<Value> {
        self.process.recv().await
    }

    async fn user(&mut self, text: &str) -> Result<()> {
        self.process
            .send(
                json!({"type":"user","session_id":self.session_id,"parent_tool_use_id":null,
            "message":{"role":"user","content":text}}),
            )
            .await
    }

    pub async fn run_dynamic<F>(
        &mut self,
        prompt: &str,
        compact: bool,
        task_id: &str,
        controls: &mut mpsc::Receiver<Control>,
        mut emit: F,
    ) -> Result<Outcome>
    where
        F: FnMut(DynamicEvent),
    {
        let mut outcome = Outcome {
            session_id: self.session_id.clone(),
            ..Outcome::default()
        };
        let mut pending: HashMap<String, Value> = HashMap::new();
        let mut replies: HashMap<u64, oneshot::Sender<Result<Value>>> = HashMap::new();
        let mut interaction_seq = 0_u64;
        let mut turn_id: Option<String> = None;
        let mut accepted = self.kind == Harness::Claude;
        let mut finished = false;
        let mut interrupted = false;
        let mut interrupt_id: Option<Value> = None;
        let mut interrupt_ack = false;
        let mut cancel_reply: Option<oneshot::Sender<Result<Value>>> = None;
        let mut followup: Option<(String, oneshot::Sender<Result<Value>>)> = None;
        let mut native_round = 1_u64;
        let mut session_state: Option<String> = None;
        let mut result_seen = false;
        let mut tasks = std::collections::HashSet::<String>::new();
        let mut raw_usage = Vec::new();
        let mut command_items = std::collections::HashSet::<String>::new();
        let start_id = match self.kind {
            Harness::Claude => {
                self.user(if compact { "/compact" } else { prompt }).await?;
                None
            }
            Harness::Codex => Some(if compact {
                self.process
                    .request("thread/compact/start", json!({"threadId":self.session_id}))
                    .await?
            } else {
                self.process.request("turn/start",json!({"threadId":self.session_id,
                    "input":[{"type":"text","text":prompt}],
                    "collaborationMode":{"mode":if self.mode==Mode::Plan{"plan"}else{"default"},
                        "settings":{"model":self.model,"reasoning_effort":self.effort,"developer_instructions":null}}})).await?
            }),
            Harness::Dsh => Some(
                self.process
                    .request(
                        if compact {
                            "pyxis/session/compact"
                        } else {
                            "pyxis/session/run"
                        },
                        if compact {
                            json!({"sessionId":self.session_id})
                        } else {
                            json!({"sessionId":self.session_id,"prompt":prompt})
                        },
                    )
                    .await?,
            ),
        };
        emit(DynamicEvent(
            json!({"type":"native_task_started","harness":self.kind,"native_round":native_round}),
        ));
        loop {
            // Prefer an already-delivered completion to a racing steer.
            tokio::select! {
                biased;
                event=self.process.recv()=>{
                    let event=event?;
                    if event["type"]=="control_response" {
                        if interrupt_id.as_ref()==Some(&event["response"]["request_id"]) {
                            ensure!(event["response"]["subtype"]=="success","Claude interrupt failed: {event}");
                            interrupt_ack=true;
                        }
                    } else if let Some(id)=event.get("id").and_then(Value::as_u64).filter(|_|event.get("method").is_none()) {
                        if let Some(reply)=replies.remove(&id) {
                            match response(&event,id) {
                                Ok(Some(result))=>{
                                    let applied=result.get("applied").and_then(Value::as_bool).unwrap_or(true);
                                    let _=reply.send(Ok(json!({"applied":applied,"native_result":result})));
                                    emit(DynamicEvent(json!({"type":"steer","applied":applied,"method":"native"})));
                                },
                                Err(error)=>{let _=reply.send(Ok(json!({"applied":false,"reason":format!("{error:#}")})));},
                                _=>{let _=reply.send(Ok(json!({"applied":false,"reason":"malformed acknowledgement"})));}
                            }
                        } else if Some(id)==start_id {
                            let result=response(&event,id).with_context(||self.process.diagnostics())?.context("missing start response")?;
                            accepted=true;
                            match self.kind {
                                Harness::Codex=>{
                                    if let Some(turn)=result["turn"]["id"].as_str(){
                                        if let Some(expected)=&turn_id {ensure!(expected==turn,"Codex turn identity changed");}
                                        turn_id=Some(turn.into());
                                    }
                                    if interrupted && interrupt_id.is_none() {
                                        let turn=turn_id.as_ref().context("Codex turn ID unavailable for interrupt")?;
                                        interrupt_id=Some(json!(self.process.request("turn/interrupt",json!({"threadId":self.session_id,"turnId":turn})).await?));
                                    }
                                },
                                Harness::Dsh=>{
                                    if compact {ensure!(result["compacted"].is_boolean(),"invalid DSH compaction result");outcome.compacted=result["compacted"]==true;}
                                    else{
                                        ensure!(result["stopReason"]=="completed" || interrupted,"DSH turn failed: {result}");
                                        outcome.result=result["text"].as_str().context("DSH missing final text")?.into();
                                        outcome.usage=result["usage"].clone();outcome.usage_scope=Some("task");
                                    }
                                    finished=true;
                                },
                                _=>{}
                            }
                        } else if interrupt_id.as_ref()==Some(&json!(id)) {
                            response(&event,id)?.context("invalid interrupt acknowledgement")?;interrupt_ack=true;
                        }
                    } else if event["type"]=="control_cancel_request" || event["method"]=="serverRequest/resolved" || event["method"]=="pyxis/interaction/resolved" {
                        let native_id=if event["type"]=="control_cancel_request"{&event["request_id"]}else{&event["params"]["requestId"]};
                        let key=&event["params"]["interactionKey"];
                        let expired=pending.iter().filter(|(_,request)|{
                            request.get("request_id")==Some(native_id) || request.get("id")==Some(native_id) ||
                                (!key.is_null() && request["params"]["interactionKey"]==*key)
                        }).map(|(id,_)|id.clone()).collect::<Vec<_>>();
                        for id in expired {pending.remove(&id);emit(DynamicEvent(json!({"type":"interaction_resolved","interaction_id":id,"reason":"native request cleared"})));}
                    } else if (event.get("method").is_some() && event.get("id").is_some()) || event["type"]=="control_request" {
                        if self.kind==Harness::Codex {
                            let p=&event["params"];
                            if p["threadId"].as_str().is_some_and(|id|id!=self.session_id)
                                || p["turnId"].as_str().is_some_and(|id|turn_id.as_deref().is_some_and(|expected|id!=expected)) {
                                self.process.send(json!({"jsonrpc":"2.0","id":event["id"],"error":{"code":-32602,"message":"Request does not belong to the active task"}})).await?;continue;
                            }
                        }
                        let method=if event["type"]=="control_request"{event["request"]["subtype"].as_str().unwrap_or("")}else{event["method"].as_str().unwrap_or("")};
                        let supported=matches!(method,"can_use_tool"|"item/commandExecution/requestApproval"|"item/fileChange/requestApproval"|
                            "item/permissions/requestApproval"|"item/tool/requestUserInput"|"mcpServer/elicitation/request"|
                            "pyxis/interaction/approval"|"pyxis/interaction/questions");
                        if !supported {
                            if self.kind==Harness::Claude {
                                self.process.send(json!({"type":"control_response","response":{"subtype":"error","request_id":event["request_id"],"error":"Unsupported host control request"}})).await?;
                            } else {
                                self.process.send(json!({"jsonrpc":"2.0","id":event["id"],"error":{"code":-32601,"message":"Unsupported host control request"}})).await?;
                            }
                            emit(DynamicEvent(json!({"type":"unsupported_control","method":method})));continue;
                        }
                        ensure!(pending.len()<64 && pending.values().map(|v|v.to_string().len()).sum::<usize>()+event.to_string().len()<256*1024,"interactive request capacity exceeded (256 KiB per task)");
                        interaction_seq+=1;let id=format!("{task_id}:{interaction_seq}");
                        pending.insert(id.clone(),event.clone());
                        emit(DynamicEvent(json!({"type":"interaction","interaction_id":id,"method":method,"request":event})));
                    } else {
                        match self.kind {
                            Harness::Claude=>{
                                if let Some(id)=event["session_id"].as_str(){
                                    if self.session_id.is_empty(){self.session_id=id.into();outcome.session_id=id.into();}
                                    else{ensure!(self.session_id==id,"Claude session changed unexpectedly");}
                                }
                                if event["type"]=="system" {
                                    match event["subtype"].as_str(){
                                        Some("init")=>{
                                            self.effective_config["native"]=json!({"model":event["model"],"permissionMode":event["permissionMode"],
                                                "mcp_servers":event["mcp_servers"],"version":event["claude_code_version"]});
                                            emit(DynamicEvent(json!({"type":"effective_config","config":self.effective_config})));
                                        },
                                        Some("session_state_changed")=>session_state=event["state"].as_str().map(str::to_owned),
                                        Some("compact_boundary")=>outcome.compacted=true,
                                        Some("task_started") if matches!(event["task_type"].as_str(),Some("local_agent"|"local_workflow"))=>{
                                            if let Some(id)=event["task_id"].as_str(){tasks.insert(id.into());}
                                        },
                                        Some("task_notification")=>{if let Some(id)=event["task_id"].as_str(){tasks.remove(id);}},
                                        Some("task_updated") if matches!(event["patch"]["status"].as_str(),Some("completed"|"failed"|"killed"|"stopped"))=>{
                                            if let Some(id)=event["task_id"].as_str(){tasks.remove(id);}
                                        },
                                        _=>{}
                                    }
                                }
                                if matches!(event["type"].as_str(),Some("assistant"|"stream_event")) && event["parent_tool_use_id"].is_null(){result_seen=false;}
                                if event["type"]=="result" {
                                    ensure!(event["is_error"]!=true || interrupted || followup.is_some(),"Claude failed: {event}");
                                    result_seen=true;
                                    outcome.result=event["result"].as_str().unwrap_or("").into();
                                    raw_usage.push(event["usage"].clone());
                                    outcome.total_cost_usd=event["total_cost_usd"].as_f64();
                                    outcome.cost_scope=outcome.total_cost_usd.map(|_|"session_cumulative");
                                    outcome.usage=json!({"native_turns":raw_usage});
                                    outcome.usage_scope=Some("native_turns");
                                }
                            },
                            Harness::Codex=>{
                                let p=&event["params"];
                                if p["threadId"].as_str()!=Some(&self.session_id){continue;}
                                if let Some(actual)=p["turnId"].as_str(){if turn_id.as_deref().is_some_and(|expected|actual!=expected){continue;}}
                                match event["method"].as_str(){
                                    Some("item/started"|"item/completed") if p["item"]["type"]=="commandExecution"=>{
                                        if let Some(id)=p["item"]["id"].as_str(){command_items.insert(id.into());}
                                    },
                                    Some("thread/settings/updated")=>{
                                        let settings=&p["threadSettings"];
                                        for (target,source) in [("reasoningEffort","effort"),("model","model"),("cwd","cwd"),("approvalPolicy","approvalPolicy"),("approvalsReviewer","approvalsReviewer"),("sandbox","sandboxPolicy")] {
                                            if let Some(value)=settings.get(source){self.effective_config["native"][target]=value.clone();}
                                        }
                                        emit(DynamicEvent(json!({"type":"effective_config","config":self.effective_config})));
                                    },
                                    Some("turn/started")=>{
                                        if let Some(id)=p["turn"]["id"].as_str(){turn_id=Some(id.into());}
                                    },
                                    Some("item/completed") if p["item"]["type"]=="agentMessage" && p["item"]["phase"]!="commentary"=>{
                                        if let Some(text)=p["item"]["text"].as_str(){outcome.result=text.into();}
                                    },
                                    Some("item/completed") if p["item"]["type"]=="contextCompaction"=>outcome.compacted=true,
                                    Some("thread/tokenUsage/updated")=>{outcome.usage=p["tokenUsage"].clone();outcome.usage_scope=Some("native_last_and_session_total");},
                                    Some("turn/completed")=>{
                                        let id=p["turn"]["id"].as_str().context("Codex completion missing turn ID")?;
                                        if turn_id.as_deref().is_some_and(|expected|id!=expected){continue;}
                                        turn_id=Some(id.into());
                                        ensure!(p["turn"]["status"]=="completed" || p["turn"]["status"]=="interrupted","Codex turn failed: {}",p["turn"]);
                                        if p["turn"]["status"]=="interrupted" {interrupted=true;}
                                        finished=true;
                                    },
                                    _=>{}
                                }
                            },
                            Harness::Dsh=>{}
                        }
                        emit(DynamicEvent(json!({"type":"progress","native":event})));
                    }
                },
                control=controls.recv()=>{
                    let Some(control)=control else{anyhow::bail!("session control channel closed")};
                    match control {
                        Control::Steer {task_id:target,prompt,reply}=>{
                            if target!=task_id || finished || interrupted || compact || followup.is_some() {
                                let _=reply.send(Ok(json!({"applied":false,"reason":"task is not currently steerable"})));continue;
                            }
                            match self.kind {
                                Harness::Claude=>{
                                    let id=format!("steer-{task_id}-{native_round}");
                                    self.process.send(json!({"type":"control_request","request_id":id,"request":{"subtype":"interrupt"}})).await?;
                                    interrupt_id=Some(json!(id));interrupt_ack=false;followup=Some((prompt,reply));
                                },
                                Harness::Codex=>{
                                    if let Some(turn)=&turn_id {
                                        let id=self.process.request("turn/steer",json!({"threadId":self.session_id,"expectedTurnId":turn,
                                            "input":[{"type":"text","text":prompt}]})).await?;replies.insert(id,reply);
                                    }else{let _=reply.send(Ok(json!({"applied":false,"reason":"native turn has not started"})));}
                                },
                                Harness::Dsh=>{
                                    let id=self.process.request("pyxis/session/steer",json!({"sessionId":self.session_id,"prompt":prompt})).await?;replies.insert(id,reply);
                                }
                            }
                        },
                        Control::Respond {interaction_id,response:answer,reply}=>{
                            if let Some(request)=pending.get(&interaction_id) {
                                match crate::interaction::validate(self.kind,request,answer) {
                                    Ok(answer)=>{
                                        if self.kind==Harness::Claude {
                                            self.process.send(json!({"type":"control_response","response":{"subtype":"success",
                                                "request_id":request["request_id"],"response":answer}})).await?;
                                        }else{self.process.send(json!({"jsonrpc":"2.0","id":request["id"],"result":answer})).await?;}
                                        pending.remove(&interaction_id);
                                        emit(DynamicEvent(json!({"type":"interaction_resolved","interaction_id":interaction_id,"reason":"answered"})));
                                        let _=reply.send(Ok(json!({"accepted":true})));
                                    },
                                    Err(error)=>{let _=reply.send(Err(error));}
                                }
                            }else{let _=reply.send(Ok(json!({"accepted":false,"reason":"interaction is stale or already answered"})));}
                        },
                        Control::Cancel {task_id:target,reply}=>{
                            if target!=task_id || finished || interrupted {let _=reply.send(Ok(json!({"cancelled":false,"reason":"task is no longer cancellable"})));continue;}
                            interrupted=true;cancel_reply=Some(reply);
                            if let Some((_,reply))=followup.take(){let _=reply.send(Ok(json!({"applied":false,"reason":"task cancelled"})));}
                            match self.kind {
                                Harness::Claude=>{
                                    let id=format!("cancel-{task_id}");
                                    self.process.send(json!({"type":"control_request","request_id":id,"request":{"subtype":"interrupt"}})).await?;
                                    for child in &tasks {
                                        self.process.send(json!({"type":"control_request","request_id":format!("stop-{child}"),
                                            "request":{"subtype":"stop_task","task_id":child}})).await?;
                                    }
                                    interrupt_id=Some(json!(id));interrupt_ack=false;
                                },
                                Harness::Codex=>{
                                    if let Some(turn)=&turn_id{interrupt_id=Some(json!(self.process.request("turn/interrupt",
                                        json!({"threadId":self.session_id,"turnId":turn})).await?));}
                                },
                                Harness::Dsh=>{interrupt_id=Some(json!(self.process.request("pyxis/session/cancel",json!({"sessionId":self.session_id})).await?));}
                            }
                            emit(DynamicEvent(json!({"type":"stop_requested"})));
                        }
                    }
                }
            }
            if self.kind == Harness::Claude
                && session_state.as_deref() == Some("idle")
                && tasks.is_empty()
                && pending.is_empty()
                && (result_seen || interrupt_ack)
            {
                if let Some((text, reply)) = followup.take() {
                    self.user(&text).await?;
                    native_round += 1;
                    session_state = None;
                    result_seen = false;
                    interrupt_ack = false;
                    interrupt_id = None;
                    emit(DynamicEvent(
                        json!({"type":"steer","method":"interrupt_and_continue","native_turn_changed":true,"native_round":native_round}),
                    ));
                    let _=reply.send(Ok(json!({"applied":true,"method":"interrupt_and_continue","native_round":native_round})));
                } else {
                    finished = true;
                }
            }
            if finished && accepted {
                if interrupted && self.kind == Harness::Codex && !command_items.is_empty() {
                    // Native turn interruption can leave unified-exec terminals
                    // alive. Stop only commands attributed to this public task;
                    // prior turns' background services belong to their owners.
                    let mut cursor = Value::Null;
                    let mut processes = Vec::new();
                    loop {
                        let page = self
                            .rpc(
                                "thread/backgroundTerminals/list",
                                json!({"threadId":self.session_id,"limit":100,"cursor":cursor}),
                            )
                            .await?;
                        for terminal in page["data"]
                            .as_array()
                            .context("missing background terminal list")?
                        {
                            if terminal["itemId"]
                                .as_str()
                                .is_some_and(|id| command_items.contains(id))
                            {
                                processes.push(
                                    terminal["processId"]
                                        .as_str()
                                        .context("missing terminal processId")?
                                        .to_owned(),
                                );
                            }
                        }
                        let next = page["nextCursor"].clone();
                        if next.is_null() {
                            break;
                        }
                        ensure!(
                            next != cursor && processes.len() <= 4096,
                            "invalid background terminal pagination"
                        );
                        cursor = next;
                    }
                    for process in &processes {
                        self.rpc(
                            "thread/backgroundTerminals/terminate",
                            json!({"threadId":self.session_id,"processId":process}),
                        )
                        .await?;
                    }
                    emit(DynamicEvent(
                        json!({"type":"native_terminal_cleanup","process_ids":processes}),
                    ));
                }
                outcome.cancelled = interrupted;
                ensure!(
                    interrupted || !compact || outcome.compacted || self.kind == Harness::Dsh,
                    "native compaction was not confirmed"
                );
                for (id, _) in pending.drain() {
                    emit(DynamicEvent(
                        json!({"type":"interaction_resolved","interaction_id":id,"reason":"task ended"}),
                    ));
                }
                for (_, reply) in replies.drain() {
                    let _=reply.send(Ok(json!({"applied":false,"reason":"task ended before native acknowledgement"})));
                }
                if let Some(reply) = cancel_reply.take() {
                    let _ = reply.send(Ok(json!({"cancelled":true,"native_stop_confirmed":true})));
                }
                return Ok(outcome);
            }
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
                        cost_scope: Some("session_cumulative"),
                        usage_scope: Some("native_turn"),
                        cancelled: false,
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
