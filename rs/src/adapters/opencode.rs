//! opencode 适配器（对照 adapters/opencode.py）
//! 同步 CPA provider 配置到 opencode.json 的 provider.cpa
//! 通用分发：path 由 AdapterInstance 指定，缺省展开到 opencode 标准配置位置；
//! host 非空则经 RemoteIO 走 ssh/scp 远程读写（对照 core/remote.py）。
use crate::core::ir::{ModelIR, ProviderIR};
use crate::core::remote::RemoteIO;
use crate::adapters::{Adapter, AdapterInstance, Rendered, base_diff};
use serde_json::{json, Value};

pub struct OpencodeAdapter {
    pub path: String,
    pub io: RemoteIO,
}

impl OpencodeAdapter {
    pub fn new(inst: &AdapterInstance) -> Self {
        let path = inst.path.clone().unwrap_or_else(|| Self::default_path());
        OpencodeAdapter { path, io: RemoteIO::new(inst.host.as_deref(), inst.platform.as_deref()) }
    }
    fn default_path() -> String {
        if let Ok(p) = std::env::var("OPENCODE_JSON") { return p; }
        let home = std::env::var("USERPROFILE").ok().and_then(|v| Some(v))
            .or_else(|| std::env::var("HOME").ok()).unwrap_or_default();
        format!("{}/.config/opencode/opencode.json", home)
    }

    fn read(&self) -> Value {
        let text = self.io.read(&self.path).unwrap_or_default();
        if text.trim().is_empty() { return Value::Object(Default::default()); }
        serde_json::from_str(&text).unwrap_or(Value::Object(Default::default()))
    }

    fn find_cpa(data: &Value) -> Value {
        let providers = data.get("provider").cloned().unwrap_or(Value::Object(Default::default()));
        if let Some(c) = providers.get("cpa") { if !c.is_null() { return c.clone(); } }
        if let Some(obj) = providers.as_object() {
            for v in obj.values() {
                let base = v.get("options").and_then(|o| o.get("baseURL")).and_then(|b| b.as_str()).unwrap_or("");
                if base.contains(":8317") { return v.clone(); }
            }
        }
        Value::Null
    }

    fn provider_to_ir(provider: &Value) -> ProviderIR {
        let options = provider.get("options").cloned().unwrap_or(Value::Object(Default::default()));
        let mut models = Vec::new();
        if let Some(ms) = provider.get("models").and_then(|v| v.as_object()) {
            for (mid, m) in ms {
                let m = if m.is_null() { Value::Object(Default::default()) } else { m.clone() };
                let caps = m.get("capabilities").cloned().unwrap_or(Value::Object(Default::default()));
                let limit = m.get("limit").cloned().unwrap_or(Value::Object(Default::default()));
                let input_modes = caps.get("input").cloned().filter(|v| !v.is_null())
                    .or_else(|| m.get("input").cloned().filter(|v| !v.is_null()))
                    .unwrap_or_else(|| Value::Array(vec![Value::String("text".into())]));
                let input: Vec<String> = input_modes.as_array().map(|a| a.iter()
                    .filter_map(|x| x.as_str().map(String::from)).collect()).unwrap_or_else(|| vec!["text".into()]);
                let mut reasoning = m.get("reasoning").and_then(|v| v.as_bool()).unwrap_or(false);
                if let Some(r) = caps.get("reasoning").and_then(|v| v.as_object()) {
                    reasoning = r.get("supported").and_then(|v| v.as_bool()).unwrap_or(false);
                }
                let ctx = limit.get("context").and_then(|v| v.as_u64())
                    .or_else(|| m.get("contextWindow").and_then(|v| v.as_u64())).unwrap_or(128000);
                let out = limit.get("output").and_then(|v| v.as_u64())
                    .or_else(|| m.get("maxTokens").and_then(|v| v.as_u64())).unwrap_or(16384);
                models.push(ModelIR { id: mid.clone(), context_window: ctx, max_tokens: out as u32,
                    input_modes: input, reasoning, extra: Default::default() });
            }
        }
        ProviderIR {
            provider_id: "cpa".into(),
            api: "openai-completions".into(),
            base_url: options.get("baseURL").and_then(|v| v.as_str()).unwrap_or("").into(),
            api_key: options.get("apiKey").and_then(|v| v.as_str()).unwrap_or("").into(),
            models,
            meta: Default::default(),
        }
    }
}

impl Adapter for OpencodeAdapter {
    fn name(&self) -> &str { "opencode" }

    fn iter_instances(&self, instances: &[AdapterInstance]) -> Vec<(String, Box<dyn Adapter>)> {
        if instances.is_empty() {
            let i = AdapterInstance { name: "default".into(), ..Default::default() };
            vec![("default".into(), Box::new(OpencodeAdapter::new(&i)))]
        } else {
            instances.iter().map(|i| (i.name.clone(), Box::new(OpencodeAdapter::new(i)) as Box<dyn Adapter>)).collect()
        }
    }

    fn load_current(&self) -> Option<ProviderIR> {
        let data = self.read();
        let cpa = Self::find_cpa(&data);
        if cpa.is_null() { return None; }
        Some(Self::provider_to_ir(&cpa))
    }

    fn diff(&self, current: &Option<ProviderIR>, target: &ProviderIR) -> Vec<String> {
        let mut lines = base_diff(current, target, self.name());
        let data = self.read();
        let cpa = Self::find_cpa(&data);
        if let Some(ms) = cpa.get("models").and_then(|v| v.as_object()) {
            if let Some(sample) = ms.values().next() {
                if let Some(o) = sample.as_object() {
                    if o.contains_key("input") || o.contains_key("contextWindow") || o.contains_key("maxTokens") {
                        lines.push(format!("  [{}] schema 迁移: 顶层 input/contextWindow/maxTokens -> capabilities/limit", self.name()));
                    }
                }
            }
        }
        lines
    }

    fn render(&self, ir: &ProviderIR) -> Rendered {
        let mut data = self.read();
        let d = data.as_object_mut().unwrap();
        if !d.contains_key("provider") { d.insert("provider".into(), Value::Object(Default::default())); }
        let providers = d.get_mut("provider").unwrap();
        let p = providers.as_object_mut().unwrap();
        if !p.contains_key("cpa") { p.insert("cpa".into(), Value::Object(Default::default())); }
        let c = p.get_mut("cpa").unwrap().as_object_mut().unwrap();
        if !c.contains_key("npm") { c.insert("npm".into(), Value::String("@ai-sdk/openai-compatible".into())); }
        if !c.contains_key("options") { c.insert("options".into(), Value::Object(Default::default())); }
        let o = c.get_mut("options").unwrap().as_object_mut().unwrap();
        o.insert("baseURL".into(), Value::String(ir.base_url.clone()));
        o.insert("apiKey".into(), Value::String(ir.api_key.clone()));
        if !o.contains_key("setCacheKey") { o.insert("setCacheKey".into(), Value::Bool(true)); }
        let mut models = serde_json::Map::new();
        for m in &ir.models {
            models.insert(m.id.clone(), json!({
                "name": m.id,
                "capabilities": { "tools": true, "input": m.input_modes, "output": ["text"],
                                  "reasoning": { "supported": m.reasoning } },
                "limit": { "context": m.context_window, "output": m.max_tokens }
            }));
        }
        c.insert("models".into(), Value::Object(models));
        Rendered::Json(data)
    }

    fn write(&self, rendered: &Rendered, backup: bool) -> Result<(), String> {
        if backup { self.io.backup(&self.path); }
        self.io.write(&self.path, &rendered.to_text()).map_err(|e| e.to_string())
    }
}
