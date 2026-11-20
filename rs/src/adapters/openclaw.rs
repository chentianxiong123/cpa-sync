//! openclaw 适配器（对照 adapters/openclaw.py）
//! 同步 CPA provider 配置到 ~/.openclaw/openclaw.json 的 models.providers.cpa
use crate::core::ir::{ModelIR, ProviderIR};
use crate::core::remote::RemoteIO;
use crate::adapters::{Adapter, AdapterInstance, Rendered, base_diff};
use serde_json::{json, Value};

const CPA_ID: &str = "cpa";

pub struct OpenclawAdapter { pub path: String, pub io: RemoteIO }

impl OpenclawAdapter {
    pub fn new(inst: &AdapterInstance) -> Self {
        let path = inst.path.clone().unwrap_or_else(|| Self::default_path());
        OpenclawAdapter { path, io: RemoteIO::new(inst.host.as_deref(), inst.platform.as_deref()) }
    }
    fn default_path() -> String {
        if let Ok(p) = std::env::var("OPENCLAW_JSON") { return p; }
        let home = std::env::var("USERPROFILE").ok().or_else(|| std::env::var("HOME").ok()).unwrap_or_default();
        format!("{}/.openclaw/openclaw.json", home)
    }
    fn read(&self) -> Value {
        let text = self.io.read(&self.path).unwrap_or_default();
        if text.trim().is_empty() { return Value::Object(Default::default()); }
        serde_json::from_str(&text).unwrap_or(Value::Object(Default::default()))
    }
    fn iter(&self, instances: &[AdapterInstance]) -> Vec<(String, Box<dyn Adapter>)> {
        if instances.is_empty() {
            let i = AdapterInstance { name: "default".into(), ..Default::default() };
            vec![("default".into(), Box::new(OpenclawAdapter::new(&i)))]
        } else {
            instances.iter().map(|i| (i.name.clone(), Box::new(OpenclawAdapter::new(i)) as Box<dyn Adapter>)).collect()
        }
    }

    fn find_cpa_provider(data: &Value) -> Value {
        let providers = data.get("models").and_then(|m| m.get("providers")).cloned()
            .unwrap_or(Value::Object(Default::default()));
        if let Some(c) = providers.get(CPA_ID) {
            if c.is_object() { return c.clone(); }
        }
        if let Some(obj) = providers.as_object() {
            for (k, v) in obj {
                if v.is_object() {
                    let base = v.get("baseUrl").and_then(|b| b.as_str()).unwrap_or("");
                    if !base.is_empty() && (k == CPA_ID || base.contains("8317")) { return v.clone(); }
                }
            }
        }
        Value::Null
    }

    fn provider_to_ir(provider: &Value) -> ProviderIR {
        let mut models = Vec::new();
        if let Some(list) = provider.get("models").and_then(|v| v.as_array()) {
            for m in list {
                if !m.is_object() { continue; }
                models.push(ModelIR {
                    id: m.get("id").and_then(|v| v.as_str()).unwrap_or("").into(),
                    context_window: m.get("contextWindow").and_then(|v| v.as_u64()).unwrap_or(128000),
                    max_tokens: m.get("maxTokens").and_then(|v| v.as_u64()).unwrap_or(16384) as u32,
                    input_modes: m.get("input").and_then(|v| v.as_array())
                        .map(|a| a.iter().filter_map(|x| x.as_str().map(String::from)).collect())
                        .unwrap_or_else(|| vec!["text".into()]),
                    reasoning: m.get("reasoning").and_then(|v| v.as_bool()).unwrap_or(false),
                    extra: Default::default(),
                });
            }
        }
        ProviderIR {
            provider_id: CPA_ID.into(),
            api: provider.get("api").and_then(|v| v.as_str()).unwrap_or("openai-completions").into(),
            base_url: provider.get("baseUrl").and_then(|v| v.as_str()).unwrap_or("").into(),
            api_key: provider.get("apiKey").and_then(|v| v.as_str()).unwrap_or("").into(),
            models,
            meta: Default::default(),
        }
    }

    pub fn load_current(&self) -> Option<ProviderIR> {
        let data = self.read();
        let p = Self::find_cpa_provider(&data);
        if p.is_null() { return None; }
        Some(Self::provider_to_ir(&p))
    }

    /// 对应 render()：IR → openclaw.json 的 models.providers.cpa
    pub fn render_json(&self, ir: &ProviderIR) -> Value {
        let mut data = self.read();
        let d = data.as_object_mut().unwrap();
        if !d.contains_key("models") { d.insert("models".into(), Value::Object(Default::default())); }
        let models_cfg = d.get_mut("models").unwrap();
        let mc = models_cfg.as_object_mut().unwrap();
        if !mc.contains_key("mode") { mc.insert("mode".into(), Value::String("merge".into())); }
        if !mc.contains_key("providers") { mc.insert("providers".into(), Value::Object(Default::default())); }
        let providers = mc.get_mut("providers").unwrap();
        let p = providers.as_object_mut().unwrap();
        if !p.contains_key(CPA_ID) { p.insert(CPA_ID.into(), Value::Object(Default::default())); }
        let cpa = p.get_mut(CPA_ID).unwrap();
        let c = cpa.as_object_mut().unwrap();
        c.insert("baseUrl".into(), Value::String(ir.base_url.clone()));
        c.insert("apiKey".into(), Value::String(ir.api_key.clone()));
        c.insert("api".into(), Value::String(if ir.api.is_empty() { "openai-completions".into() } else { ir.api.clone() }));
        c.insert("auth".into(), Value::String("api-key".into()));
        if !c.contains_key("timeoutSeconds") { c.insert("timeoutSeconds".into(), json!(300)); }
        let models: Vec<Value> = ir.models.iter().map(|m| json!({
            "id": m.id, "name": m.id, "reasoning": m.reasoning,
            "input": m.input_modes, "contextWindow": m.context_window, "maxTokens": m.max_tokens
        })).collect();
        c.insert("models".into(), Value::Array(models));
        data
    }
}

impl Adapter for OpenclawAdapter {
    fn name(&self) -> &str { "openclaw" }
    fn iter_instances(&self, instances: &[AdapterInstance]) -> Vec<(String, Box<dyn Adapter>)> { self.iter(instances) }
    fn load_current(&self) -> Option<ProviderIR> {
        let data = self.read();
        let p = Self::find_cpa_provider(&data);
        if p.is_null() { return None; }
        Some(Self::provider_to_ir(&p))
    }
    fn diff(&self, current: &Option<ProviderIR>, target: &ProviderIR) -> Vec<String> { base_diff(current, target, "openclaw") }
    fn render(&self, ir: &ProviderIR) -> Rendered { Rendered::Json(self.render_json(ir)) }
    fn write(&self, rendered: &Rendered, backup: bool) -> Result<(), String> {
        if backup { self.io.backup(&self.path); }
        self.io.write(&self.path, &rendered.to_text()).map_err(|e| e.to_string())
    }
}
