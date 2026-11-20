//! claude 适配器（对照 adapters/claude.py）
//! 同步 CPA 到 cc-switch.db（app_type='claude'），每个模型一条 provider
use crate::core::ir::ProviderIR;
use crate::core::remote::RemoteIO;
use crate::adapters::ccswitch;
use crate::adapters::{Adapter, AdapterInstance, Rendered, base_diff};
use serde_json::{json, Value};

const APP_TYPE: &str = "claude";

/// 对应 _ccswitch.CLAUDE_MODEL_KEYS
pub const CLAUDE_MODEL_KEYS: [&str; 9] = [
    "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME",
    "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME",
    "ANTHROPIC_DEFAULT_FABLE_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL_NAME",
    "ANTHROPIC_MODEL",
];

pub struct ClaudeAdapter { pub io: RemoteIO }

impl ClaudeAdapter {
    pub fn new(_inst: &AdapterInstance) -> Self { ClaudeAdapter { io: RemoteIO::new(None, None) } }
    fn iter(&self, _instances: &[AdapterInstance]) -> Vec<(String, Box<dyn Adapter>)> {
        vec![("default".into(), Box::new(ClaudeAdapter::new(&AdapterInstance::default())))]
    }
}

impl Adapter for ClaudeAdapter {
    fn name(&self) -> &str { "claude" }
    fn iter_instances(&self, instances: &[AdapterInstance]) -> Vec<(String, Box<dyn Adapter>)> { self.iter(instances) }

    fn load_current(&self) -> Option<ProviderIR> {
        let recs = ccswitch::get_cpa_providers(APP_TYPE);
        if recs.is_empty() { return None; }
        let mut base = String::new();
        let mut key = String::new();
        let mut ids: Vec<String> = Vec::new();
        for rec in &recs {
            let cfg = ccswitch::parse_settings(rec);
            let env = cfg.get("env").cloned().unwrap_or(Value::Object(Default::default()));
            if base.is_empty() { base = env.get("ANTHROPIC_BASE_URL").and_then(|v| v.as_str()).unwrap_or("").to_string(); }
            if key.is_empty() { key = env.get("ANTHROPIC_AUTH_TOKEN").and_then(|v| v.as_str()).unwrap_or("").to_string(); }
            let m = env.get("ANTHROPIC_MODEL").and_then(|v| v.as_str()).unwrap_or("").to_string();
            if !m.is_empty() && !ids.contains(&m) { ids.push(m); }
        }
        if ids.is_empty() { return None; }
        let models = ids.into_iter().map(|id| crate::core::ir::ModelIR {
            id, context_window: 512000, max_tokens: 16384,
            input_modes: vec!["text".into()], reasoning: false, extra: Default::default(),
        }).collect();
        Some(ProviderIR {
            provider_id: "cpa".into(), api: "anthropic-messages".into(),
            base_url: base, api_key: key, models, meta: Default::default(),
        })
    }

    fn diff(&self, _current: &Option<ProviderIR>, _target: &ProviderIR) -> Vec<String> {
        vec![format!("  [{}] 每次同步全盘重建（cc-switch 切换式，不增量）", self.name())]
    }

    /// 对应 render()：每个模型一条 provider（env 多档）
    fn render(&self, ir: &ProviderIR) -> Rendered {
        let root = {
            let r = ir.base_url.trim_end_matches('/');
            if r.ends_with("/v1") { &r[..r.len() - 3] } else { r }
        };
        let mut providers: Vec<Value> = Vec::new();
        for m in &ir.models {
            let mut env = serde_json::Map::new();
            env.insert("ANTHROPIC_BASE_URL".into(), Value::String(root.to_string()));
            env.insert("ANTHROPIC_AUTH_TOKEN".into(), Value::String(ir.api_key.clone()));
            for k in CLAUDE_MODEL_KEYS {
                env.insert(k.into(), Value::String(m.id.clone()));
            }
            env.insert("DISABLE_AUTOUPDATER".into(), Value::String("1".into()));
            env.insert("CLAUDE_CODE_SUBAGENT_MODEL".into(), Value::String(m.id.clone()));
            providers.push(json!({
                "name": m.id,
                "notes": "cpa",
                "settings_config": {
                    "env": Value::Object(env),
                    "attribution": { "commit": "", "pr": "" }
                },
                "meta": {
                    "commonConfigEnabled": false,
                    "endpointAutoSelect": true,
                    "apiFormat": "openai_chat",
                    "customUserAgent": "claude-cli/2.1.161 (external, cli)"
                }
            }));
        }
        Rendered::Json(Value::Array(providers))
    }

    /// 对应 write()：cc-switch.db 事务同步
    fn write(&self, rendered: &Rendered, backup: bool) -> Result<(), String> {
        let providers = match rendered {
            Rendered::Json(Value::Array(a)) => a.clone(),
            _ => return Err("claude 需要数组渲染".into()),
        };
        if providers.is_empty() { return Err("没有可用模型".into()); }
        if backup { ccswitch::backup_db(); }
        let cur = providers[0].get("name").and_then(|v| v.as_str()).unwrap_or("");
        ccswitch::sync_providers(APP_TYPE, &providers, Some(cur)).map_err(|e| e.to_string())?;
        Ok(())
    }
}

/// 避免 unused 警告（base_diff 由其他 adapter 使用）
#[allow(dead_code)]
fn _keep() { let _ = base_diff; }
