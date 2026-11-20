//! picoclaw 适配器（对照 adapters/picoclaw.py）
//! 同步 CPA 模型到 ~/.picoclaw/config.json 的 model_list
use crate::core::ir::{ModelIR, ProviderIR};
use crate::core::remote::RemoteIO;
use crate::adapters::{Adapter, AdapterInstance, Rendered, base_diff};
use serde_json::{json, Value};

const CPA_PROVIDER: &str = "cpa";

pub struct PicoclawAdapter { pub path: String, pub io: RemoteIO }

impl PicoclawAdapter {
    pub fn new(inst: &AdapterInstance) -> Self {
        let path = inst.path.clone().unwrap_or_else(|| Self::default_path());
        PicoclawAdapter { path, io: RemoteIO::new(inst.host.as_deref(), inst.platform.as_deref()) }
    }
    fn default_path() -> String {
        if let Ok(p) = std::env::var("PICOCLAW_CONFIG") { return p; }
        let home = std::env::var("USERPROFILE").ok().or_else(|| std::env::var("HOME").ok()).unwrap_or_default();
        format!("{}/.picoclaw/config.json", home)
    }
    fn read(&self) -> Value {
        let text = self.io.read(&self.path).unwrap_or_default();
        if text.trim().is_empty() { return Value::Object(Default::default()); }
        serde_json::from_str(&text).unwrap_or(Value::Object(Default::default()))
    }
    fn iter(&self, instances: &[AdapterInstance]) -> Vec<(String, Box<dyn Adapter>)> {
        if instances.is_empty() {
            let i = AdapterInstance { name: "default".into(), ..Default::default() };
            vec![("default".into(), Box::new(PicoclawAdapter::new(&i)))]
        } else {
            instances.iter().map(|i| (i.name.clone(), Box::new(PicoclawAdapter::new(i)) as Box<dyn Adapter>)).collect()
        }
    }

    /// 对应 load_current()：从 model_list 里提取 cpa 条目
    fn load_impl(&self) -> Option<ProviderIR> {
        let data = self.read();
        let list: Vec<Value> = match data.get("model_list").and_then(|v| v.as_array()) {
            Some(l) => l.clone(),
            None => return None,
        };
        let cpa_entries: Vec<&Value> = list.iter().filter(|m| {
            m.get("provider").and_then(|v| v.as_str()) == Some(CPA_PROVIDER)
        }).collect();
        if cpa_entries.is_empty() { return None; }
        let base = cpa_entries[0].get("api_base").and_then(|v| v.as_str()).unwrap_or("").to_string();
        let key = cpa_entries[0].get("api_keys").and_then(|v| v.as_array())
            .and_then(|a| a.first()).and_then(|k| k.as_str()).unwrap_or("").to_string();
        let mut models = Vec::new();
        for e in cpa_entries {
            models.push(ModelIR {
                id: e.get("model_name").and_then(|v| v.as_str()).unwrap_or("").into(),
                context_window: 128000,
                max_tokens: 16384,
                input_modes: vec!["text".into()],
                reasoning: false,
                extra: Default::default(),
            });
        }
        Some(ProviderIR {
            provider_id: CPA_PROVIDER.into(),
            api: "openai-completions".into(),
            base_url: base,
            api_key: key,
            models,
            meta: Default::default(),
        })
    }

    /// 对应 render()：清旧 cpa 条目 + 追加新模型
    fn render_impl(&self, ir: &ProviderIR) -> Value {
        let mut data = self.read();
        let d = data.as_object_mut().unwrap();
        let old: Vec<Value> = d.get("model_list").and_then(|v| v.as_array())
            .cloned().unwrap_or_default();
        let kept: Vec<Value> = old.into_iter().filter(|m| {
            m.get("provider").and_then(|v| v.as_str()) != Some(CPA_PROVIDER)
        }).collect();
        let mut model_list = kept;
        for m in &ir.models {
            model_list.push(json!({
                "model_name": m.id,
                "provider": CPA_PROVIDER,
                "model": m.id,
                "api_base": ir.base_url,
                "api_keys": [ir.api_key],
            }));
        }
        d.insert("model_list".into(), Value::Array(model_list));
        data
    }

}

impl Adapter for PicoclawAdapter {
    fn name(&self) -> &str { "picoclaw" }
    fn iter_instances(&self, instances: &[AdapterInstance]) -> Vec<(String, Box<dyn Adapter>)> { self.iter(instances) }
    fn load_current(&self) -> Option<ProviderIR> { self.load_impl() }
    fn diff(&self, current: &Option<ProviderIR>, target: &ProviderIR) -> Vec<String> { base_diff(current, target, "picoclaw") }
    fn render(&self, ir: &ProviderIR) -> Rendered { Rendered::Json(self.render_impl(ir)) }
    fn write(&self, rendered: &Rendered, backup: bool) -> Result<(), String> {
        if backup { self.io.backup(&self.path); }
        self.io.write(&self.path, &rendered.to_text()).map_err(|e| e.to_string())
    }
}
