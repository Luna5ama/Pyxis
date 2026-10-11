use anyhow::{Context, Result, bail, ensure};
use serde_json::{Value, json};
use std::{
    process::Stdio,
    sync::{Arc, Mutex},
};
use tokio::{
    io::{AsyncBufRead, AsyncBufReadExt, AsyncRead, AsyncWriteExt, BufReader},
    process::{Child, ChildStdin, ChildStdout, Command},
    task::JoinHandle,
};

pub const MAX_FRAME: usize = 8 * 1024 * 1024;

/// Bounded, cancellation-safe JSONL framing. The partial frame lives in self.
pub struct Frames<R> {
    reader: R,
    partial: Vec<u8>,
}
impl<R: AsyncBufRead + Unpin> Frames<R> {
    pub fn new(reader: R) -> Self {
        Self {
            reader,
            partial: Vec::new(),
        }
    }
    pub async fn next(&mut self) -> Result<Option<Value>> {
        loop {
            let buf = self.reader.fill_buf().await?;
            if buf.is_empty() {
                ensure!(self.partial.is_empty(), "EOF inside JSON frame");
                return Ok(None);
            }
            let newline = buf.iter().position(|b| *b == b'\n');
            let count = newline.map_or(buf.len(), |n| n + 1);
            ensure!(
                self.partial.len() + count <= MAX_FRAME,
                "JSON frame exceeds {MAX_FRAME} bytes"
            );
            self.partial.extend_from_slice(&buf[..count]);
            self.reader.consume(count);
            if newline.is_some() {
                let parsed = serde_json::from_slice(&self.partial).context("invalid JSON frame");
                self.partial.clear();
                return parsed.map(Some);
            }
        }
    }
}

pub struct Process {
    child: Child,
    stdin: ChildStdin,
    frames: Frames<BufReader<ChildStdout>>,
    stderr: Arc<Mutex<Vec<u8>>>,
    stderr_task: JoinHandle<()>,
    next_id: u64,
    #[cfg(windows)]
    _tree: ProcessTree,
}

impl Process {
    pub fn spawn(mut command: Command) -> Result<Self> {
        command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true);
        #[cfg(windows)]
        command.creation_flags(0x08000040); // CREATE_NO_WINDOW | IDLE_PRIORITY_CLASS
        #[cfg(unix)]
        command.process_group(0);
        let mut child = command.spawn().context("start harness process")?;
        #[cfg(windows)]
        let tree = ProcessTree::new(&child)?;
        let stdin = child.stdin.take().unwrap();
        let stdout = child.stdout.take().unwrap();
        let mut error_pipe = child.stderr.take().unwrap();
        let stderr = Arc::new(Mutex::new(Vec::new()));
        let tail = stderr.clone();
        let stderr_task = tokio::spawn(async move {
            use tokio::io::AsyncReadExt;
            let mut chunk = [0; 2048];
            while let Ok(n) = error_pipe.read(&mut chunk).await {
                if n == 0 {
                    break;
                }
                let mut tail = tail.lock().unwrap();
                tail.extend_from_slice(&chunk[..n]);
                let excess = tail.len().saturating_sub(8192);
                tail.drain(..excess);
            }
        });
        Ok(Self {
            child,
            stdin,
            frames: Frames::new(BufReader::new(stdout)),
            stderr,
            stderr_task,
            next_id: 0,
            #[cfg(windows)]
            _tree: tree,
        })
    }

    pub async fn send(&mut self, value: Value) -> Result<()> {
        let mut bytes = serde_json::to_vec(&value)?;
        ensure!(bytes.len() < MAX_FRAME, "outgoing frame is too large");
        bytes.push(b'\n');
        self.stdin
            .write_all(&bytes)
            .await
            .context("write harness stdin")?;
        self.stdin.flush().await?;
        Ok(())
    }

    pub async fn recv(&mut self) -> Result<Value> {
        match self.frames.next().await {
            Ok(Some(value)) => Ok(value),
            result => {
                let status = self.child.try_wait()?;
                let tail = String::from_utf8_lossy(&self.stderr.lock().unwrap()).into_owned();
                match result {
                    Err(error) => Err(error.context(format!(
                        "harness protocol error; status={status:?}; stderr={tail}"
                    ))),
                    _ => bail!("harness stdout closed; status={status:?}; stderr={tail}"),
                }
            }
        }
    }

    pub async fn request(&mut self, method: &str, params: Value) -> Result<u64> {
        self.next_id += 1;
        let id = self.next_id;
        self.send(json!({"jsonrpc":"2.0", "id":id, "method":method,"params":params}))
            .await?;
        Ok(id)
    }

    pub fn diagnostics(&self) -> String {
        format!(
            "stderr={}",
            String::from_utf8_lossy(&self.stderr.lock().unwrap())
        )
    }
}

impl Drop for Process {
    fn drop(&mut self) {
        self.stderr_task.abort();
        #[cfg(unix)]
        if let Some(pid) = self.child.id() {
            // The child is group leader; never target the MCP's own group.
            unsafe {
                libc::kill(-(pid as i32), libc::SIGKILL);
            }
        }
    }
}

#[cfg(windows)]
struct ProcessTree(usize);
#[cfg(windows)]
impl ProcessTree {
    fn new(child: &Child) -> Result<Self> {
        use windows_sys::Win32::{Foundation::CloseHandle, System::JobObjects::*};
        // The job owns all descendants, including commands spawned by the harness.
        unsafe {
            let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            ensure!(
                !job.is_null(),
                "CreateJobObjectW: {}",
                std::io::Error::last_os_error()
            );
            let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            let configured = SetInformationJobObject(
                job,
                JobObjectExtendedLimitInformation,
                &limits as *const _ as *const _,
                std::mem::size_of_val(&limits) as u32,
            );
            let assigned = configured != 0
                && AssignProcessToJobObject(
                    job,
                    child.raw_handle().context("child has no process handle")? as _,
                ) != 0;
            if !assigned {
                let error = std::io::Error::last_os_error();
                CloseHandle(job);
                bail!("attach child process tree: {error}");
            }
            Ok(Self(job as usize))
        }
    }
}
#[cfg(windows)]
impl Drop for ProcessTree {
    fn drop(&mut self) {
        unsafe {
            windows_sys::Win32::Foundation::CloseHandle(self.0 as _);
        }
    }
}

pub fn response(value: &Value, id: u64) -> Result<Option<Value>> {
    if value.get("method").is_some() || value.get("id").and_then(Value::as_u64) != Some(id) {
        return Ok(None);
    }
    if let Some(error) = value.get("error") {
        bail!("harness RPC failed: {error}");
    }
    Ok(Some(
        value
            .get("result")
            .context("RPC response missing result")?
            .clone(),
    ))
}

pub async fn reject_request(process: &mut Process, value: &Value) -> Result<bool> {
    if value.get("method").is_none() || value.get("id").is_none() {
        return Ok(false);
    }
    let method = value["method"].as_str().unwrap_or("");
    let result = match method {
        "item/commandExecution/requestApproval" | "item/fileChange/requestApproval" => {
            json!({"decision":"decline"})
        }
        "item/permissions/requestApproval" => json!({"permissions":{}}),
        "item/tool/requestUserInput" => json!({"answers":{}}),
        "mcpServer/elicitation/request" => json!({"action":"decline","content":null}),
        _ => {
            process.send(json!({"jsonrpc":"2.0","id":value["id"],"error":{"code":-32601,"message":"No interactive handler available in pyxis-subagent-mcp"}})).await?;
            return Ok(true);
        }
    };
    process
        .send(json!({"jsonrpc":"2.0","id":value["id"],"result":result}))
        .await?;
    Ok(true)
}

pub fn frames<R: AsyncRead + Unpin>(read: R) -> Frames<BufReader<R>> {
    Frames::new(BufReader::new(read))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn partial_frame_survives_cancellation() {
        let (mut writer, reader) = tokio::io::duplex(128);
        let mut input = frames(reader);
        writer.write_all(b"{\"value\":").await.unwrap();
        assert!(
            tokio::time::timeout(std::time::Duration::from_millis(5), input.next())
                .await
                .is_err()
        );
        writer.write_all(b"1}\n").await.unwrap();
        assert_eq!(input.next().await.unwrap(), Some(json!({"value":1})));
        drop(writer);
        assert!(input.next().await.unwrap().is_none());
    }
    #[tokio::test]
    async fn oversized_or_truncated_frames_fail() {
        let bytes = vec![b'x'; MAX_FRAME + 1];
        assert!(
            frames(bytes.as_slice())
                .next()
                .await
                .unwrap_err()
                .to_string()
                .contains("exceeds")
        );
        assert!(frames(b"{\"value\":1}".as_slice()).next().await.is_err());
    }
    #[test]
    fn responses_do_not_accept_unrelated_notifications_or_errors() {
        assert!(
            response(&json!({"id":1,"method":"approval"}), 1)
                .unwrap()
                .is_none()
        );
        assert!(response(&json!({"id":2,"result":{}}), 1).unwrap().is_none());
        assert!(response(&json!({"id":1,"error":{"message":"failed"}}), 1).is_err());
    }
}
