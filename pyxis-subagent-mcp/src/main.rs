mod agent;
mod config;
mod harness;
mod interaction;
mod session;
mod transport;

use anyhow::{Context, Result, ensure};
use serde::Deserialize;
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, HashMap},
    path::PathBuf,
    sync::Arc,
};
use tokio::{
    io::AsyncWriteExt,
    sync::{mpsc, watch},
    task::{AbortHandle, JoinSet},
};

type Output = mpsc::Sender<Value>;
const VERSIONS: &[&str] = &["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"];

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Arguments {
    prompt: String,
}

fn error(id: Value, code: i64, message: &str) -> Value {
    json!({"jsonrpc":"2.0","id":id,"error":{"code":code,"message":message}})
}
fn success(id: Value, result: Value) -> Value {
    json!({"jsonrpc":"2.0","id":id,"result":result})
}

fn managed_reply(id: Value, result: Result<Value>) -> Value {
    match result {
        Ok(value) => {
            let encoded = value.to_string();
            if encoded.len() >= transport::MAX_FRAME / 2 {
                return success(
                    id,
                    json!({"content":[{"type":"text","text":"Result exceeds the response budget; query individual tasks and page their events."}],"isError":true}),
                );
            }
            let text = if encoded.len() < 512 * 1024 {
                encoded
            } else {
                "Structured result attached. Query individual tasks to page their events.".into()
            };
            success(
                id,
                json!({"content":[{"type":"text","text":text}],"structuredContent":value,"isError":false}),
            )
        }
        Err(error) => success(
            id,
            json!({"content":[{"type":"text","text":format!("{error:#}")}],"isError":true}),
        ),
    }
}

fn tool_list(sessions: &BTreeMap<String, Arc<session::Session>>, dynamic: bool) -> Value {
    let mut tools = sessions.iter().map(|(name,session)| json!({
        "name":name,"description":session.config.description,
        "inputSchema":{"type":"object","properties":{"prompt":{"type":"string","minLength":1,"description":"Task or follow-up for this specialist. The server manages its session."}},"required":["prompt"],"additionalProperties":false}
    })).collect::<Vec<_>>();
    if dynamic {
        tools.extend(agent::definitions());
    }
    json!({"tools":tools})
}

async fn execute(
    session: Arc<session::Session>,
    prompt: String,
    id: Value,
    progress: Option<Value>,
    output: Output,
) -> Value {
    let work = session.call(&prompt);
    tokio::pin!(work);
    let mut count = 0_u64;
    let mut heartbeat = tokio::time::interval(std::time::Duration::from_secs(10));
    let result = loop {
        tokio::select! {
            result = &mut work => break result,
            _ = heartbeat.tick(), if progress.is_some() => {
                count += 1;
                let _ = output.send(json!({"jsonrpc":"2.0","method":"notifications/progress","params":{"progressToken":progress,"progress":count,"message":format!("{} is working",session.name)}})).await;
            }
        }
    };
    match result {
        Ok(outcome) => success(
            id,
            json!({"content":[{"type":"text","text":outcome.result}],"structuredContent":outcome,"isError":false}),
        ),
        Err(error) => {
            session::log(
                &session.name,
                "task_failed",
                json!({"error":format!("{error:#}")}),
            );
            success(
                id,
                json!({"content":[{"type":"text","text":format!("{error:#}")}],"isError":true}),
            )
        }
    }
}

async fn serve(config: config::LoadedConfig) -> Result<()> {
    let nested = std::env::var_os("PYXIS_SUBAGENT_NESTED").is_some();
    let dynamic = !nested && !config.runtimes.is_empty();
    let resources = session::Resources::new(&config.limits);
    let manager = agent::Manager::new(config.runtimes, config.limits, resources.clone());
    let reaper = manager.spawn_reaper();
    let sessions: BTreeMap<_, _> = config
        .specialists
        .into_iter()
        .map(|(name, config)| {
            (
                name.clone(),
                session::Session::new(name, config, resources.clone()),
            )
        })
        .collect();
    let (shutdown, shutdown_rx) = watch::channel(false);
    let timers: Vec<_> = sessions
        .values()
        .map(|s| s.spawn_timer(shutdown_rx.clone()))
        .collect();
    let (output, mut outgoing) = mpsc::channel::<Value>(32);
    let mut writer = tokio::spawn(async move {
        let mut stdout = tokio::io::stdout();
        while let Some(value) = outgoing.recv().await {
            let mut bytes = serde_json::to_vec(&value)?;
            ensure!(
                bytes.len() < transport::MAX_FRAME,
                "MCP response exceeds frame limit"
            );
            bytes.push(b'\n');
            stdout.write_all(&bytes).await?;
            stdout.flush().await?;
        }
        Ok::<_, anyhow::Error>(())
    });
    let mut input = transport::frames(tokio::io::stdin());
    let mut initialized = false;
    let mut calls = JoinSet::new();
    let mut running: HashMap<String, AbortHandle> = HashMap::new();
    let stop = tokio::signal::ctrl_c();
    tokio::pin!(stop);
    let mut writer_finished = false;
    let result = async {
        loop {
            tokio::select! {
                _ = &mut stop => break,
                result = &mut writer => {
                    writer_finished = true;
                    result??;
                    break;
                }
                completed = calls.join_next_with_id(), if !calls.is_empty() => {
                    if let Some(Ok((task_id,(key,reply)))) = completed {
                        if running.get(&key).is_some_and(|handle| handle.id() == task_id) {
                            running.remove(&key);
                            output.send(reply).await?;
                        }
                    }
                }
                request = input.next() => {
                    let request = match request {
                        Ok(Some(value)) => value,
                        Ok(None) => break,
                        Err(parse_error) => {
                            output.send(error(Value::Null,-32700,&format!("{parse_error:#}"))).await?;
                            // The bounded parser cannot recover from an oversized frame or EOF.
                            break;
                        }
                    };
                    let id = request.get("id").cloned();
                    let valid_id = id.as_ref().is_none_or(|id| id.is_string() || id.is_i64() || id.is_u64());
                    if !request.is_object() || request["jsonrpc"] != "2.0" || !request["method"].is_string() || !valid_id {
                        output.send(error(Value::Null,-32600,"Invalid JSON-RPC request")).await?;
                        continue;
                    }
                    let method = request["method"].as_str().unwrap();
                    if id.is_none() {
                        if method == "notifications/cancelled" {
                            let cancel_id = &request["params"]["requestId"];
                            if let Some(handle) = running.remove(&cancel_id.to_string()) {
                                handle.abort();
                                output.send(error(cancel_id.clone(),-32800,"Request cancelled")).await?;
                            }
                        }
                        continue;
                    }
                    let id = id.unwrap();
                    let key = id.to_string();
                    if running.contains_key(&key) {
                        output.send(error(id,-32600,"Duplicate in-flight request ID")).await?;
                        continue;
                    }
                    let params = &request["params"];
                    let reply = match method {
                        "initialize" if !initialized => {
                            let version = params["protocolVersion"].as_str().unwrap_or("");
                            if version.is_empty() || !params["capabilities"].is_object() || !params["clientInfo"].is_object() {
                                error(id,-32602,"initialize requires protocolVersion, capabilities and clientInfo")
                            } else {
                                initialized = true;
                                success(id,json!({"protocolVersion":if VERSIONS.contains(&version) {version} else {VERSIONS.last().unwrap()},"capabilities":{"tools":{"listChanged":false}},"serverInfo":{"name":"pyxis-subagent-mcp","version":env!("CARGO_PKG_VERSION")}}))
                            }
                        }
                        "ping" => success(id,json!({})),
                        _ if !initialized => error(id,-32002,"Server is not initialized"),
                        "initialize" => error(id,-32600,"Already initialized"),
                        "tools/list" => {
                            if params.get("cursor").is_some() { error(id,-32602,"No pagination cursor is supported") }
                            else { success(id,if nested {json!({"tools":[]})} else {tool_list(&sessions,dynamic)}) }
                        }
                        "tools/call" => {
                            if nested { output.send(error(id,-32602,"Execution tools are disabled in nested Pyxis processes")).await?;continue; }
                            if running.len() >= 128 { output.send(error(id,-32000,"Too many in-flight tool calls (limit 128)")).await?;continue; }
                            let name = params["name"].as_str().unwrap_or("");
                            if dynamic && name.starts_with("agent_") {
                                let manager = manager.clone(); let task_key=key.clone(); let args=params["arguments"].clone(); let tool_name=name.to_owned();
                                let handle=calls.spawn(async move {
                                    let response=managed_reply(id,manager.call(&tool_name,args).await);(task_key,response)
                                });running.insert(key,handle);continue;
                            }
                            let arguments = serde_json::from_value::<Arguments>(params["arguments"].clone());
                            match (sessions.get(name), arguments) {
                                (None, _) => error(id,-32602,"Unknown specialist tool"),
                                (_, Err(e)) => error(id,-32602,&format!("Invalid tool arguments: {e}")),
                                (_, Ok(args)) if args.prompt.trim().is_empty() => error(id,-32602,"prompt must not be blank"),
                                (Some(session), Ok(args)) => {
                                    if running.len() >= 128 {
                                        error(id,-32000,"Too many in-flight tool calls (limit 128)")
                                    } else {
                                        let token = params["_meta"]["progressToken"].clone();
                                        let token = if token.is_string() || token.is_number() { Some(token) } else { None };
                                        let output = output.clone();
                                        let session = session.clone();
                                        let task_key = key.clone();
                                        let handle = calls.spawn(async move {
                                            let response = execute(session,args.prompt,id,token,output.clone()).await;
                                            (task_key,response)
                                        });
                                        running.insert(key,handle);
                                        continue;
                                    }
                                }
                            }
                        }
                        _ => error(id,-32601,"Method not found"),
                    };
                    output.send(reply).await?;
                }
            }
        }
        Ok::<_,anyhow::Error>(())
    }.await;
    shutdown.send_replace(true);
    calls.abort_all();
    while calls.join_next().await.is_some() {}
    for timer in timers {
        timer.abort();
        let _ = timer.await;
    }
    for session in sessions.values() {
        session.close().await;
    }
    manager.close_all().await;
    reaper.abort();
    let _ = reaper.await;
    drop(output);
    if !writer_finished {
        writer.await??;
    }
    result
}

fn main() {
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .expect("create Tokio runtime");
    let result = runtime.block_on(run());
    // Tokio stdin uses an uncancellable blocking read. After Ctrl-C, do not
    // wait forever for the caller to close its pipe; serve has closed children.
    runtime.shutdown_timeout(std::time::Duration::from_millis(200));
    if let Err(error) = result {
        eprintln!("pyxis-subagent-mcp: {error:#}");
        std::process::exit(1);
    }
}
async fn run() -> Result<()> {
    let mut args = std::env::args_os().skip(1);
    let path = PathBuf::from(
        args.next()
            .context("Usage: pyxis-subagent-mcp <config.json>")?,
    );
    ensure!(
        args.next().is_none(),
        "Usage: pyxis-subagent-mcp <config.json> (exactly one positional argument)"
    );
    serve(config::load(&path)?).await
}
