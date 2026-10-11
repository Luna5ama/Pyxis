use anyhow::{Context, Result, bail, ensure};
use serde::{Deserialize, Serialize};
use std::{
    collections::BTreeMap,
    path::{Path, PathBuf},
    time::Duration,
};

pub type Config = BTreeMap<String, AgentConfig>;

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum Harness {
    Claude,
    Codex,
    Dsh,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum Mode {
    Plan,
    Auto,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AgentConfig {
    pub executable: PathBuf,
    pub harness: Harness,
    pub mode: Mode,
    pub model: String,
    pub reasoning_effort: Option<String>,
    pub description: String,
    #[serde(default)]
    pub prefix_prompt: String,
    #[serde(default)]
    pub args: Vec<String>,
    pub cwd: Option<PathBuf>,
    pub provider: Option<String>,
    /// Anchor for resolving the installed dsh packages used by our bridge.
    pub dsh_module_root: Option<PathBuf>,
    #[serde(default)]
    pub cache: CacheConfig,
    #[serde(default = "default_timeout")]
    pub request_timeout_seconds: u64,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct CacheConfig {
    pub enabled: bool,
    pub ttl_seconds: u64,
    pub compact_margin_seconds: u64,
    pub use_usage_ttl: bool,
}

impl Default for CacheConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            ttl_seconds: 300,
            compact_margin_seconds: 60,
            use_usage_ttl: true,
        }
    }
}

fn default_timeout() -> u64 {
    3600
}

impl AgentConfig {
    pub fn timeout(&self) -> Duration {
        Duration::from_secs(self.request_timeout_seconds)
    }
}

pub fn load(path: &Path) -> Result<Config> {
    let bytes = std::fs::read(path).with_context(|| format!("read config {}", path.display()))?;
    let bytes = bytes.strip_prefix(&[0xef, 0xbb, 0xbf]).unwrap_or(&bytes);
    let mut config: Config = serde_json::from_slice(bytes).context("parse agent config")?;
    ensure!(!config.is_empty(), "config must contain at least one tool");
    let base = std::fs::canonicalize(path)?.parent().unwrap().to_owned();
    let inherited_cwd = std::env::current_dir()?;
    for (name, agent) in &mut config {
        ensure!(
            !name.is_empty()
                && name.len() <= 128
                && name
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"_-.".contains(&b)),
            "invalid tool name {name:?}"
        );
        for (field, value) in [("model", &agent.model), ("description", &agent.description)] {
            ensure!(!value.trim().is_empty(), "{name}: {field} is empty");
        }
        ensure!(
            agent
                .reasoning_effort
                .as_ref()
                .is_none_or(|s| !s.trim().is_empty()),
            "{name}: reasoning_effort is empty; use null for provider default"
        );
        ensure!(
            !agent.executable.as_os_str().is_empty(),
            "{name}: executable is empty"
        );
        // A bare name uses PATH; paths are relative to the configuration file.
        if agent.executable.is_relative() && agent.executable.components().count() > 1 {
            agent.executable = base.join(&agent.executable);
        }
        let cwd = agent
            .cwd
            .take()
            .map(|p| if p.is_absolute() { p } else { base.join(p) })
            .unwrap_or_else(|| inherited_cwd.clone());
        agent.cwd = Some(
            std::fs::canonicalize(&cwd)
                .with_context(|| format!("{name}: cwd {}", cwd.display()))?,
        );
        ensure!(
            agent.cwd.as_ref().unwrap().is_dir(),
            "{name}: cwd is not a directory"
        );
        ensure!(
            agent.request_timeout_seconds > 0 && agent.request_timeout_seconds <= 86400,
            "{name}: request_timeout_seconds must be 1..86400"
        );
        ensure!(
            agent.cache.ttl_seconds > agent.cache.compact_margin_seconds
                && agent.cache.compact_margin_seconds > 0
                && agent.cache.ttl_seconds <= 604800,
            "{name}: require 0 < compact_margin_seconds < ttl_seconds <= 604800"
        );
        if agent.harness == Harness::Dsh {
            ensure!(
                agent
                    .provider
                    .as_ref()
                    .is_some_and(|p| !p.trim().is_empty()),
                "{name}: dsh requires provider"
            );
            let Some(root) = agent.dsh_module_root.take() else {
                bail!("{name}: dsh requires dsh_module_root (directory containing node_modules)")
            };
            agent.dsh_module_root = Some(if root.is_absolute() {
                root
            } else {
                base.join(root)
            });
        }
    }
    Ok(config)
}
