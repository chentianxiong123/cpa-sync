//! codex 适配器（对照 adapters/codex.py）
//! 同步 CPA 到 cc-switch.db（app_type='codex'），每模型一条 provider
//! settings_config = auth + config(TOML) + modelCatalog
use crate::core::ir::ModelIR;
use crate::core::ir::ProviderIR;
use crate::core::remote::RemoteIO;
use crate::adapters::ccswitch;
use crate::adapters::{Adapter, AdapterInstance, Rendered, base_diff};
use serde_json::{json, Value};

const APP_TYPE: &str = "codex";

pub struct CodexAdapter { pub io: RemoteIO }

impl CodexAdapter {
    pub fn new(_inst: &AdapterInstance) -> Self { CodexAdapter { io: RemoteIO::new(None, None) } }
    fn iter(&self, _instances: &[AdapterInstance]) -> Vec<(String, Box<dyn Adapter>)> {
        vec![("default".into(), Box::new(CodexAdapter::new(&AdapterInstance::default())))]
    }

    /// 从 config 文本里取 base_url（对照 re.search(r'base_url\s*=\s*"([^"]+)"')）
    fn parse_base_url(cfg: &str) -> String {
        let key = "base_url";
        let Some(i) = cfg.find(key) else { return String::new() };
        let rest = &cfg[i..];
        let Some(q1) = rest.find('"') else { return String::new() };
        let after = &rest[q1 + 1..];
        match after.find('"') { Some(q2) => after[..q2].to_string(), None => String::new() }
    }
}

impl Adapter for CodexAdapter {
    fn name(&self) -> &str { "codex" }
    fn iter_instances(&self, instances: &[AdapterInstance]) -> Vec<(String, Box<dyn Adapter>)> { self.iter(instances) }

    fn load_current(&self) -> Option<ProviderIR> {
        let recs = ccswitch::get_cpa_providers(APP_TYPE);
        if recs.is_empty() { return None; }
        let mut base = String::new();
        let mut key = String::new();
        let mut seen: Vec<String> = Vec::new();
        for rec in &recs {
            let cfg = ccswitch::parse_settings(rec);
            if base.is_empty() { base = Self::parse_base_url(cfg.get("config").and_then(|v| v.as_str()).unwrap_or("")); }
            if key.is_empty() {
                key = cfg.get("auth").and_then(|a| a.get("OPENAI_API_KEY")).and_then(|v| v.as_str()).unwrap_or("").to_string();
            }
            if let Some(list) = cfg.get("modelCatalog").and_then(|c| c.get("models")).and_then(|v| v.as_array()) {
                for m in list {
                    let mid = m.get("model").and_then(|v| v.as_str()).or_else(|| m.get("displayName").and_then(|v| v.as_str())).unwrap_or("");
                    if !mid.is_empty() && !seen.contains(&mid.to_string()) { seen.push(mid.to_string()); }
                }
            }
        }
        if seen.is_empty() { return None; }
        let models = seen.into_iter().map(|id| ModelIR {
            id, context_window: 512000, max_tokens: 16384,
            input_modes: vec!["text".into()], reasoning: true, extra: Default::default(),
        }).collect();
        Some(ProviderIR {
            provider_id: "cpa".into(), api: "responses".into(),
            base_url: base, api_key: key, models, meta: Default::default(),
        })
    }

    fn diff(&self, _current: &Option<ProviderIR>, _target: &ProviderIR) -> Vec<String> {
        vec![format!("  [{}] 每次同步全盘重建（cc-switch 切换式，不增量）", self.name())]
    }

    /// 对应 render()：每模型一条 provider（含 modelCatalog）
    fn render(&self, ir: &ProviderIR) -> Rendered {
        if ir.models.is_empty() { return Rendered::Text(String::new()); }
        let base = ir.base_url.trim_end_matches('/');
        let ctx_win = ir.meta.get("context_window").and_then(|v| v.as_u64()).unwrap_or(512000);
        let auto_compact = ir.meta.get("auto_compact_token_limit").and_then(|v| v.as_u64()).unwrap_or(200000);
        let mut providers: Vec<Value> = Vec::new();
        for m in &ir.models {
            let config = format!(
                "model_provider = \"custom\"\nmodel = \"{}\"\nreview_model = \"{}\"\nmodel_context_window = {}\nmodel_auto_compact_token_limit = {}\n\n[model_providers]\n[model_providers.custom]\nname = \"custom\"\nwire_api = \"responses\"\nrequires_openai_auth = true\nbase_url = \"{}\"\n",
                m.id, m.id, ctx_win, auto_compact, base);
            providers.push(json!({
                "name": m.id,
                "notes": "cpa",
                "settings_config": {
                    "auth": { "OPENAI_API_KEY": ir.api_key },
                    "config": config,
                    "modelCatalog": { "models": [{ "model": m.id, "displayName": m.id }] }
                },
                "meta": {
                    "commonConfigEnabled": false,
                    "endpointAutoSelect": true,
                    "apiFormat": "openai_chat",
                    "codexChatReasoning": {
                        "supportsThinking": true, "supportsEffort": false,
                        "thinkingParam": "thinking", "effortParam": "none", "outputFormat": "auto"
                    },
                    "customUserAgent": "claude-cli/2.1.161 (external, cli)"
                }
            }));
        }
        Rendered::Json(Value::Array(providers))
    }

    fn write(&self, rendered: &Rendered, backup: bool) -> Result<(), String> {
        let providers = match rendered {
            Rendered::Json(Value::Array(a)) => a.clone(),
            _ => return Err("codex 需要数组渲染".into()),
        };
        if providers.is_empty() { return Err("没有可用模型".into()); }
        if backup { ccswitch::backup_db(); }
        let cur = providers[0].get("name").and_then(|v| v.as_str()).unwrap_or("");
        ccswitch::sync_providers(APP_TYPE, &providers, Some(cur)).map_err(|e| e.to_string())?;
        Ok(())
    }
}

#[allow(dead_code)]
fn _keep() { let _ = base_diff; }
