//! pi 适配器（对照 adapters/pi.py）
//! 同步 CPA provider 配置到 models.json（含历史 UUID 供应商标识迁移）
use crate::core::ir::{ModelIR, ProviderIR};
use crate::core::remote::RemoteIO;
use crate::adapters::{Adapter, AdapterInstance, Rendered, base_diff};
use serde_json::{json, Value};

pub struct PiAdapter { pub path: String, pub io: RemoteIO }

impl PiAdapter {
    pub fn new(inst: &AdapterInstance) -> Self {
        let path = inst.path.clone().unwrap_or_else(|| Self::default_path());
        PiAdapter { path, io: RemoteIO::new(inst.host.as_deref(), inst.platform.as_deref()) }
    }
    fn default_path() -> String {
        if let Ok(p) = std::env::var("PI_MODELS_JSON") { return p; }
        let home = std::env::var("USERPROFILE").ok().or_else(|| std::env::var("HOME").ok()).unwrap_or_default();
        format!("{}/.pi/agent/models.json", home)
    }

    fn read(&self) -> Value {
        let text = self.io.read(&self.path).unwrap_or_default();
        if text.trim().is_empty() { return Value::Object(Default::default()); }
        serde_json::from_str(&text).unwrap_or(Value::Object(Default::default()))
    }

    /// 对应 _find_cpa_key()：name=="cpa" 优先，兜底 baseUrl 含 :8317
    fn find_cpa_key(data: &Value) -> Option<String> {
        let providers = data.get("providers")?.as_object()?;
        for (k, v) in providers {
            if v.get("name").and_then(|n| n.as_str()) == Some("cpa") { return Some(k.clone()); }
        }
        for (k, v) in providers {
            if v.get("baseUrl").and_then(|b| b.as_str()).unwrap_or("").contains(":8317") { return Some(k.clone()); }
        }
        None
    }

    fn provider_to_ir(provider: &Value) -> ProviderIR {
        let mut models = Vec::new();
        if let Some(list) = provider.get("models").and_then(|v| v.as_array()) {
            for m in list {
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
            provider_id: "cpa".into(),
            api: provider.get("api").and_then(|v| v.as_str()).unwrap_or("openai-completions").into(),
            base_url: provider.get("baseUrl").and_then(|v| v.as_str()).unwrap_or("").into(),
            api_key: provider.get("apiKey").and_then(|v| v.as_str()).unwrap_or("").into(),
            models, meta: Default::default(),
        }
    }
}

impl Adapter for PiAdapter {
    fn name(&self) -> &str { "pi" }

    fn iter_instances(&self, instances: &[AdapterInstance]) -> Vec<(String, Box<dyn Adapter>)> {
        if instances.is_empty() {
            let i = AdapterInstance { name: "default".into(), ..Default::default() };
            vec![("default".into(), Box::new(PiAdapter::new(&i)))]
        } else {
            instances.iter().map(|i| (i.name.clone(), Box::new(PiAdapter::new(i)) as Box<dyn Adapter>)).collect()
        }
    }

    fn load_current(&self) -> Option<ProviderIR> {
        let data = self.read();
        let key = Self::find_cpa_key(&data)?;
        let provider = data.get("providers")?.get(&key)?;
        let mut ir = Self::provider_to_ir(provider);
        ir.meta.insert("_current_provider_id".into(), Value::String(key));
        Some(ir)
    }

    fn diff(&self, current: &Option<ProviderIR>, target: &ProviderIR) -> Vec<String> {
        let mut lines = base_diff(current, target, self.name());
        if let Some(cur) = current {
            if let Some(k) = cur.meta.get("_current_provider_id").and_then(|v| v.as_str()) {
                if !k.is_empty() && k != target.provider_id {
                    lines.insert(0, format!("  [{}] 供应商标识: '{}' -> '{}'（历史 UUID 迁移）", self.name(), k, target.provider_id));
                }
            }
        }
        lines
    }

    fn render(&self, ir: &ProviderIR) -> Rendered {
        let data = self.read();
        let mut providers: Value = data.get("providers").cloned().unwrap_or(Value::Object(Default::default()));
        let target_key = if ir.provider_id.is_empty() { "cpa".into() } else { ir.provider_id.clone() };
        // 历史 UUID key 迁移
        if let Some(old_key) = Self::find_cpa_key(&data) {
            if old_key != target_key {
                let p = providers.as_object_mut().unwrap();
                if let Some(v) = p.remove(&old_key) { p.insert(target_key.clone(), v); }
            }
        }
        {
            let p = providers.as_object_mut().unwrap();
            if !p.contains_key(&target_key) { p.insert(target_key.clone(), Value::Object(Default::default())); }
            let c = p.get_mut(&target_key).unwrap().as_object_mut().unwrap();
            c.insert("name".into(), Value::String("cpa".into()));
            c.insert("baseUrl".into(), Value::String(ir.base_url.clone()));
            c.insert("api".into(), Value::String(if ir.api.is_empty() { "openai-completions".into() } else { ir.api.clone() }));
            c.insert("apiKey".into(), Value::String(ir.api_key.clone()));
            let models: Vec<Value> = ir.models.iter().map(|m| json!({
                "id": m.id, "name": m.id, "reasoning": m.reasoning,
                "input": m.input_modes, "contextWindow": m.context_window, "maxTokens": m.max_tokens
            })).collect();
            c.insert("models".into(), Value::Array(models));
        }
        let mut out = data;
        if let Some(o) = out.as_object_mut() { o.insert("providers".into(), providers); }
        Rendered::Json(out)
    }

    fn write(&self, rendered: &Rendered, backup: bool) -> Result<(), String> {
        if backup { self.io.backup(&self.path); }
        self.io.write(&self.path, &rendered.to_text()).map_err(|e| e.to_string())
    }
}
