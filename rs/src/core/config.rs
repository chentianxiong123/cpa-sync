//! 中央配置（对应 core/config.py）：加载 cpa-config.yaml + 默认值
use super::ir::{ModelIR, ProviderIR};
use serde::{Deserialize, Serialize};
use serde_yaml::Value as YValue;
use serde_json::Value;
use std::collections::HashMap;
use std::path::{Path, PathBuf};

/// 项目根：crate 可能在子目录（如 rs/），config/scripts/web 在项目根。
/// 探测顺序：环境变量 CPA_SYNC_ROOT > CARGO_MANIFEST_DIR 祖先链 >
/// 可执行文件旁（分发形态：二进制与 config/scripts 同目录）> 回退 CARGO_MANIFEST_DIR。
pub fn project_root() -> PathBuf {
    if let Ok(p) = std::env::var("CPA_SYNC_ROOT") {
        let pb = PathBuf::from(p);
        if pb.exists() {
            return pb;
        }
    }
    let m = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let mut cands: Vec<PathBuf> = vec![m.clone()];
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            cands.push(dir.to_path_buf());
        }
    }
    for c in cands {
        for d in c.ancestors() {
            if d.join("config").join("cpa-config.yaml").exists() || d.join("scripts").is_dir() {
                return d.to_path_buf();
            }
        }
    }
    m
}
pub fn default_config_path() -> PathBuf {
    if let Ok(p) = std::env::var("CPA_SYNC_CONFIG") {
        PathBuf::from(p)
    } else {
        project_root().join("config").join("cpa-config.yaml")
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct CPASettings {
    #[serde(default = "def_base_url")]
    pub base_url: String,
    #[serde(default = "def_api_key")]
    pub api_key: String,
    #[serde(default = "def_provider_id")]
    pub provider_id: String,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub include_models: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub exclude_models: Vec<String>,
    #[serde(default = "def_ctx")]
    pub context_window: u64,
    #[serde(default = "def_max")]
    pub max_tokens: u32,
    #[serde(default = "def_auto")]
    pub auto_compact_token_limit: u64,
    #[serde(default, skip_serializing_if = "HashMap::is_empty")]
    pub model_overrides: HashMap<String, YValue>,
    #[serde(default = "def_input_modes")]
    pub default_input_modes: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub image_models: Vec<String>,
}
fn def_base_url() -> String { "http://YOUR_CPA_IP:8317/v1".into() }
fn def_api_key() -> String { "YOUR_CPA_API_KEY".into() }
fn def_provider_id() -> String { "cpa".into() }
fn def_ctx() -> u64 { 512000 }
fn def_max() -> u32 { 16384 }
fn def_auto() -> u64 { 300000 }
fn def_input_modes() -> Vec<String> { vec!["text".into()] }

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HostDef {
    pub ssh: String,
    #[serde(default = "def_platform")]
    pub platform: String,
}
fn def_platform() -> String { "win".into() }

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AdapterInstance {
    #[serde(default)]
    pub name: String,
    #[serde(default)]
    pub host: String,
    #[serde(default)]
    pub path: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub platform: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SyncConfig {
    #[serde(default)]
    pub cpa: CPASettings,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub targets: Vec<String>,
    #[serde(default = "def_strategy")]
    pub strategy: String,
    #[serde(default, skip_serializing_if = "HashMap::is_empty")]
    pub hosts: HashMap<String, HostDef>,
    #[serde(default, skip_serializing_if = "HashMap::is_empty")]
    pub adapter_instances: HashMap<String, Vec<AdapterInstance>>,
    #[serde(skip)]
    pub path: String,
}
fn def_strategy() -> String { "merge".into() }

impl SyncConfig {
    pub fn instance_for(&self, adapter: &str) -> Vec<AdapterInstance> {
        self.adapter_instances.get(adapter).cloned().unwrap_or_default()
    }

    /// 对应 SyncConfig.load()
    pub fn load(path: Option<&str>) -> Result<Self, String> {
        let cfg_path = path.map(PathBuf::from).unwrap_or_else(default_config_path);
        if !cfg_path.exists() {
            return Err(format!("中央配置不存在: {}", cfg_path.display()));
        }
        let text = std::fs::read_to_string(&cfg_path)
            .map_err(|e| format!("读配置失败: {}", e))?;
        let raw: HashMap<String, YValue> = serde_yaml::from_str(&text).unwrap_or_default();

        let cpa_raw = raw.get("cpa").cloned().unwrap_or(YValue::Mapping(Default::default()));
        let cpa: CPASettings = serde_yaml::from_value(cpa_raw).unwrap_or_default();

        let hosts: HashMap<String, HostDef> = raw
            .get("hosts")
            .and_then(|v| serde_yaml::from_value(v.clone()).ok())
            .unwrap_or_default();

        let mut adapter_instances: HashMap<String, Vec<AdapterInstance>> = HashMap::new();
        if let Some(ai) = raw.get("adapter_instances") {
            if let Some(list) = ai.as_mapping() {
                for (k, v) in list {
                    if let Some(s) = k.as_str() {
                        let items: Vec<AdapterInstance> = serde_yaml::from_value(v.clone()).unwrap_or_default();
                        let fixed: Vec<AdapterInstance> = items.into_iter().map(|i| {
                            let platform = i.platform.clone().or_else(|| {
                                hosts.get(&i.host).map(|h| h.platform.clone())
                            });
                            AdapterInstance { name: i.name, host: i.host, path: i.path, platform }
                        }).collect();
                        adapter_instances.insert(s.into(), fixed);
                    }
                }
            }
        }
        let targets: Vec<String> = raw.get("targets").and_then(|v| serde_yaml::from_value(v.clone()).ok()).unwrap_or_default();
        let strategy: String = raw.get("strategy").and_then(|v| serde_yaml::from_value(v.clone()).ok()).unwrap_or_else(|| "merge".into());

        Ok(SyncConfig {
            cpa, targets, strategy, hosts, adapter_instances,
            path: cfg_path.to_string_lossy().into(),
        })
    }
}

/// 对应 filter_models()
pub fn filter_models(models: Vec<ModelIR>, include: &[String], exclude: &[String]) -> Vec<ModelIR> {
    let excluded = |mid: &str| exclude.iter().any(|p| wildcard_match(mid, p));
    let mut models = models;
    if !include.is_empty() {
        let hit: Vec<ModelIR> = models.into_iter().filter(|m| {
            include.iter().any(|inc| {
                if m.id == *inc { return true; }
                if inc.contains('/') {
                    let prefix = inc.split('/').next().unwrap_or("");
                    m.id.starts_with(prefix)
                } else {
                    m.id.ends_with(inc)
                }
            })
        }).collect();
        models = hit;
    }
    models.into_iter().filter(|m| !excluded(&m.id)).collect()
}

/// 简单通配符匹配（对应 fnmatch.fnmatch）
pub fn wildcard_match(s: &str, pat: &str) -> bool {
    glob::Pattern::new(pat)
        .map(|p| p.matches(s))
        .unwrap_or(false)
}

/// 对应 resolve_input_modes()
pub fn resolve_input_modes(mid: &str, ov: &YValue, cfg: &SyncConfig) -> Vec<String> {
    if let Some(input) = ov.get("input") {
        if let Some(list) = input.as_sequence() {
            let modes: Vec<String> = list.iter()
                .filter_map(|v| v.as_str().map(String::from))
                .collect();
            if !modes.is_empty() { return modes; }
        }
    }
    let mut base: Vec<String> = cfg.cpa.default_input_modes.clone();
    if !base.contains(&"image".into()) &&
       cfg.cpa.image_models.iter().any(|p| wildcard_match(mid, p)) {
        base.push("image".into());
    }
    base
}

/// 对应 build_provider_ir()
pub fn build_provider_ir(cfg: &SyncConfig, cpa_models: Vec<ModelIR>) -> ProviderIR {
    let final_models: Vec<ModelIR> = filter_models(cpa_models, &cfg.cpa.include_models, &cfg.cpa.exclude_models)
        .into_iter()
        .map(|m| {
            let mid = m.id;
            let ov = cfg.cpa.model_overrides.get(&mid).cloned().unwrap_or(YValue::Mapping(Default::default()));
            let ctx = ov.get("contextWindow").and_then(|v| v.as_u64()).unwrap_or(cfg.cpa.context_window);
            let maxt = ov.get("maxTokens").and_then(|v| v.as_u64()).unwrap_or(cfg.cpa.max_tokens as u64) as u32;
            ModelIR {
                id: mid.clone(),
                context_window: ctx,
                max_tokens: maxt,
                input_modes: resolve_input_modes(&mid, &ov, cfg),
                reasoning: ov.get("reasoning").and_then(|v| v.as_bool()).unwrap_or(false),
                extra: m.extra,
            }
        })
        .collect();
    let mut meta = HashMap::new();
    meta.insert("context_window".into(), Value::from(cfg.cpa.context_window));
    meta.insert("auto_compact_token_limit".into(), Value::from(cfg.cpa.auto_compact_token_limit));
    ProviderIR {
        provider_id: cfg.cpa.provider_id.clone(),
        api: "openai-completions".into(),
        base_url: cfg.cpa.base_url.clone(),
        api_key: cfg.cpa.api_key.clone(),
        models: final_models,
        meta,
    }
}
