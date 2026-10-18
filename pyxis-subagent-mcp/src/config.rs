use anyhow::{Context, Result, bail, ensure};
use serde::{Deserialize, Serialize};
use std::{
    collections::BTreeMap,
    path::{Path, PathBuf},
    time::Duration,
};

pub type Config = BTreeMap<String, AgentConfig>;

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq, PartialOrd, Ord, Hash)]
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
#[serde(default, deny_unknown_fields)]
pub struct Limits {
    pub active_tasks: usize,
    pub sessions: usize,
    pub queued_tasks: usize,
    pub idle_seconds: u64,
    pub retained_tasks: usize,
    pub retained_events: usize,
}

impl Default for Limits {
    fn default() -> Self {
        Self {
            active_tasks: 8,
            sessions: 16,
            queued_tasks: 128,
            idle_seconds: 7200,
            retained_tasks: 256,
            retained_events: 1024,
        }
    }
}

pub struct LoadedConfig {
    pub specialists: Config,
    pub runtimes: BTreeMap<Harness, AgentConfig>,
    pub limits: Limits,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct VersionedConfig {
    version: u32,
    #[serde(default)]
    specialists: BTreeMap<String, serde_json::Value>,
    runtimes: BTreeMap<Harness, serde_json::Value>,
    #[serde(default)]
    limits: Limits,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
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
    /// Trusted local Codex runtime overrides. Omitted values inherit CLI policy.
    pub approval_policy: Option<serde_json::Value>,
    pub sandbox: Option<String>,
    /// Anchor for resolving the installed dsh packages used by our bridge.
    pub dsh_module_root: Option<PathBuf>,
    #[serde(default)]
    pub cache: CacheConfig,
    #[serde(default = "default_timeout")]
    pub request_timeout_seconds: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
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

/// Rust canonicalization returns Windows verbatim paths. Native CLI path
/// walkers do not consistently understand the device prefix (notably drive
/// ancestors); supply ordinary absolute drive/UNC paths over their protocols.
pub fn native_path(path: PathBuf) -> PathBuf {
    #[cfg(windows)]
    if let Some(text) = path.to_str().and_then(|p| p.strip_prefix(r"\\?\")) {
        if let Some(unc) = text.strip_prefix(r"UNC\") {
            return PathBuf::from(format!(r"\\{unc}"));
        }
        if text.as_bytes().get(1) == Some(&b':') {
            return PathBuf::from(text);
        }
    }
    path
}

pub fn load(path: &Path) -> Result<LoadedConfig> {
    let bytes = std::fs::read(path).with_context(|| format!("read config {}", path.display()))?;
    let bytes = bytes.strip_prefix(&[0xef, 0xbb, 0xbf]).unwrap_or(&bytes);
    let value: serde_json::Value = serde_json::from_slice(bytes).context("parse agent config")?;
    let (mut config, mut runtimes, limits) = if value.get("version").is_some() {
        let parsed: VersionedConfig =
            serde_json::from_value(value).context("parse versioned config")?;
        ensure!(
            parsed.version == 2,
            "unsupported config version {}",
            parsed.version
        );
        ensure!(!parsed.runtimes.is_empty(), "v2 config requires runtimes");
        let mut runtimes = BTreeMap::new();
        for (kind, mut value) in parsed.runtimes {
            let fields = value.as_object_mut().context("runtime must be an object")?;
            fields
                .entry("harness")
                .or_insert(serde_json::to_value(kind)?);
            fields
                .entry("description")
                .or_insert(serde_json::json!("Dynamic agent runtime"));
            fields.entry("mode").or_insert(serde_json::json!("auto"));
            runtimes.insert(
                kind,
                serde_json::from_value::<AgentConfig>(value).context("invalid runtime")?,
            );
        }
        let mut specialists = BTreeMap::new();
        for (name, value) in parsed.specialists {
            let fields = value.as_object().context("specialist must be an object")?;
            let kind: Harness = serde_json::from_value(value["harness"].clone())
                .context("specialist requires harness")?;
            let mut merged = if fields.contains_key("executable") {
                serde_json::json!({})
            } else {
                serde_json::to_value(
                    runtimes
                        .get(&kind)
                        .context("specialist references unconfigured runtime")?,
                )?
            };
            merged
                .as_object_mut()
                .unwrap()
                .extend(fields.iter().map(|(k, v)| (k.clone(), v.clone())));
            specialists.insert(
                name,
                serde_json::from_value::<AgentConfig>(merged)
                    .context("invalid specialist preset")?,
            );
        }
        (specialists, runtimes, parsed.limits)
    } else {
        let legacy: Config = serde_json::from_value(value).context("parse legacy agent config")?;
        ensure!(!legacy.is_empty(), "config must contain at least one tool");
        (legacy, BTreeMap::new(), Limits::default())
    };
    ensure!(
        limits.active_tasks > 0
            && limits.sessions > 0
            && limits.queued_tasks > 0
            && limits.idle_seconds > 0
            && limits.retained_tasks > 0
            && limits.retained_events > 0,
        "all resource limits must be positive"
    );
    let base = native_path(std::fs::canonicalize(path)?)
        .parent()
        .unwrap()
        .to_owned();
    let inherited_cwd = std::env::current_dir()?;
    for (name, agent) in config
        .iter_mut()
        .map(|(name, agent)| (name.clone(), agent))
        .chain(runtimes.iter_mut().map(|(kind, agent)| {
            (
                match kind {
                    Harness::Claude => "claude",
                    Harness::Codex => "codex",
                    Harness::Dsh => "dsh",
                }
                .to_owned(),
                agent,
            )
        }))
    {
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
        agent.cwd = Some(native_path(
            std::fs::canonicalize(&cwd)
                .with_context(|| format!("{name}: cwd {}", cwd.display()))?,
        ));
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
    for (kind, agent) in &runtimes {
        ensure!(
            *kind == agent.harness,
            "runtime key {kind:?} disagrees with harness field"
        );
    }
    for name in config.keys() {
        ensure!(
            !name.starts_with("agent_"),
            "specialist tool name {name} uses the reserved agent_ prefix"
        );
    }
    Ok(LoadedConfig {
        specialists: config,
        runtimes,
        limits,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn config(value: serde_json::Value) -> Result<LoadedConfig> {
        use std::sync::atomic::{AtomicU64, Ordering};
        static NEXT: AtomicU64 = AtomicU64::new(0);
        let root = std::env::current_dir()?.join("target/test-fixtures");
        std::fs::create_dir_all(&root)?;
        let path = root.join(format!(
            "config-{}-{}.json",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::write(&path, serde_json::to_vec(&value)?)?;
        let result = load(&path);
        std::fs::remove_file(path)?;
        result
    }

    #[test]
    fn versioned_presets_inherit_runtime_without_changing_legacy_shape() {
        let loaded=config(json!({"version":2,"runtimes":{"claude":{"executable":"claude","model":"exact-model","reasoning_effort":"max","prefix_prompt":"runtime"}},
            "specialists":{"planner":{"harness":"claude","mode":"plan","description":"plan","prefix_prompt":"preset"}}})).unwrap();
        assert_eq!(loaded.runtimes[&Harness::Claude].mode, Mode::Auto);
        let preset = &loaded.specialists["planner"];
        assert_eq!(preset.model, "exact-model");
        assert_eq!(preset.reasoning_effort.as_deref(), Some("max"));
        assert_eq!(preset.prefix_prompt, "preset");
        assert_eq!(preset.mode, Mode::Plan);
        let legacy = config(json!({"planner":serde_json::to_value(preset).unwrap()})).unwrap();
        assert!(legacy.runtimes.is_empty());
        assert_eq!(legacy.specialists.len(), 1);
    }

    #[test]
    fn config_rejects_unknown_fields_versions_and_reserved_names() {
        let valid =
            json!({"version":2,"runtimes":{"codex":{"executable":"codex","model":"exact"}}});
        let mut value = valid.clone();
        value["version"] = json!(3);
        assert!(config(value).is_err());
        let mut value = valid.clone();
        value["limits"] = json!({"sessions":0});
        assert!(config(value).is_err());
        let mut value = valid.clone();
        value["runtimes"]["codex"]["arbitrary"] = json!(true);
        assert!(config(value).is_err());
        let mut value = valid;
        value["specialists"] = json!({"agent_start":{"harness":"codex","description":"collision"}});
        assert!(config(value).is_err());
    }

    #[cfg(windows)]
    #[test]
    fn canonical_windows_cwd_is_usable_by_native_directory_walkers() {
        assert_eq!(
            native_path(PathBuf::from(r"\\?\J:\work\folder")),
            PathBuf::from(r"J:\work\folder")
        );
        assert_eq!(
            native_path(PathBuf::from(r"\\?\UNC\server\share\folder")),
            PathBuf::from(r"\\server\share\folder")
        );
        assert_eq!(
            native_path(PathBuf::from(r"J:\work")),
            PathBuf::from(r"J:\work")
        );
    }
}
