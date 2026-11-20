//! hermes 适配器（对照 adapters/hermes.py）
//! 同步 CPA 配置到 ~/.hermes/config.yaml（model 段 + providers.cpa）
use crate::core::ir::{ModelIR, ProviderIR};
use crate::core::remote::RemoteIO;
use crate::adapters::{Adapter, AdapterInstance, Rendered, base_diff};
use serde_yaml::{Mapping, Value as YV};

const CPA_ID: &str = "cpa";

pub struct HermesAdapter { pub path: String, pub io: RemoteIO }

impl HermesAdapter {
    pub fn new(inst: &AdapterInstance) -> Self {
        let path = inst.path.clone().unwrap_or_else(|| Self::default_path());
        HermesAdapter { path, io: RemoteIO::new(inst.host.as_deref(), inst.platform.as_deref()) }
    }
    fn default_path() -> String {
        if let Ok(p) = std::env::var("HERMES_CONFIG_YAML") { return p; }
        let home = std::env::var("USERPROFILE").ok().or_else(|| std::env::var("HOME").ok()).unwrap_or_default();
        format!("{}/.hermes/config.yaml", home)
    }
    fn iter(&self, instances: &[AdapterInstance]) -> Vec<(String, Box<dyn Adapter>)> {
        if instances.is_empty() {
            let i = AdapterInstance { name: "default".into(), ..Default::default() };
            vec![("default".into(), Box::new(HermesAdapter::new(&i)))]
        } else {
            instances.iter().map(|i| (i.name.clone(), Box::new(HermesAdapter::new(i)) as Box<dyn Adapter>)).collect()
        }
    }
    fn read(&self) -> YV {
        let text = self.io.read(&self.path).unwrap_or_default();
        if text.trim().is_empty() { return YV::Mapping(Default::default()); }
        serde_yaml::from_str(&text).unwrap_or(YV::Null)
    }

    /// 对应 _find_cpa_provider()
    fn find_cpa(data: &YV) -> YV {
        let providers = match data.get("providers") { Some(v) => v.clone(), None => YV::Null };
        if let Some(c) = providers.get(CPA_ID) { return c.clone(); }
        if let Some(obj) = providers.as_mapping() {
            for v in obj.values() {
                let base = v.get("base_url").and_then(|b| b.as_str()).unwrap_or("");
                if base.contains("8317") { return v.clone(); }
            }
        }
        YV::Null
    }

    fn provider_to_ir(provider: &YV) -> ProviderIR {
        let mut models = Vec::new();
        if let Some(ms) = provider.get("models") {
            if let Some(obj) = ms.as_mapping() {
                for (k, v) in obj {
                    let id = k.as_str().unwrap_or("").to_string();
                    let ctx = v.get("context_window").and_then(|c| c.as_u64()).unwrap_or(256000);
                    models.push(ModelIR {
                        id, context_window: ctx, max_tokens: 16384,
                        input_modes: vec!["text".into()], reasoning: false, extra: Default::default(),
                    });
                }
            }
        }
        ProviderIR {
            provider_id: CPA_ID.into(),
            api: "openai-completions".into(),
            base_url: provider.get("base_url").and_then(|v| v.as_str()).unwrap_or("").into(),
            api_key: provider.get("api_key").and_then(|v| v.as_str()).unwrap_or("").into(),
            models,
            meta: Default::default(),
        }
    }

    fn load_impl(&self) -> Option<ProviderIR> {
        let data = self.read();
        let p = Self::find_cpa(&data);
        if p.is_null() { return None; }
        Some(Self::provider_to_ir(&p))
    }

    /// 对应 render()：model 主端点 + providers.cpa
    /// 对应 render()：model 主端点 + providers.cpa（作用域分离避免借用冲突）
    fn render_impl(&self, ir: &ProviderIR) -> YV {
        let mut data = self.read();
        // 1) model 段
        {
            let d = data.as_mapping_mut().unwrap();
            let mk = YV::String("model".into());
            if !d.contains_key(&mk) { d.insert(YV::String("model".into()), YV::Mapping(Default::default())); }
            let mc = d.get_mut(&mk).unwrap().as_mapping_mut().unwrap();
            if !mc.contains_key(&YV::String("provider".into())) {
                mc.insert(YV::String("provider".into()), YV::String("custom".into()));
            }
            mc.insert(YV::String("base_url".into()), YV::String(ir.base_url.clone()));
            mc.insert(YV::String("api_key".into()), YV::String(ir.api_key.clone()));
            if !mc.contains_key(&YV::String("api_mode".into())) {
                mc.insert(YV::String("api_mode".into()), YV::String("chat_completions".into()));
            }
        }
        // 2) providers.cpa
        {
            let d = data.as_mapping_mut().unwrap();
            let pk = YV::String("providers".into());
            if !d.contains_key(&pk) { d.insert(YV::String("providers".into()), YV::Mapping(Default::default())); }
            let pm = d.get_mut(&pk).unwrap().as_mapping_mut().unwrap();
            let ck = YV::String(CPA_ID.into());
            if !pm.contains_key(&ck) { pm.insert(YV::String(CPA_ID.into()), YV::Mapping(Default::default())); }
            let c = pm.get_mut(&ck).unwrap().as_mapping_mut().unwrap();
            c.insert(YV::String("name".into()), YV::String("cpa".into()));
            c.insert(YV::String("base_url".into()), YV::String(ir.base_url.clone()));
            c.insert(YV::String("api_key".into()), YV::String(ir.api_key.clone()));
            let mut models_map = Mapping::new();
            for m in &ir.models { models_map.insert(YV::String(m.id.clone()), YV::Mapping(Default::default())); }
            c.insert(YV::String("models".into()), YV::Mapping(models_map));
        }
        // 3) model.default（保留原值，若不在 cpa 清单内则取第一个）
        {
            let d = data.as_mapping_mut().unwrap();
            let mk = YV::String("model".into());
            let mc = d.get_mut(&mk).unwrap().as_mapping_mut().unwrap();
            let default = mc.get(&YV::String("default".into())).and_then(|v| v.as_str()).unwrap_or("").to_string();
            let has = ir.models.iter().any(|m| m.id == default);
            if !has && !ir.models.is_empty() {
                mc.insert(YV::String("default".into()), YV::String(ir.models[0].id.clone()));
            }
        }
        data
    }

}

impl Adapter for HermesAdapter {
    fn name(&self) -> &str { "hermes" }
    fn iter_instances(&self, instances: &[AdapterInstance]) -> Vec<(String, Box<dyn Adapter>)> { self.iter(instances) }
    fn load_current(&self) -> Option<ProviderIR> { self.load_impl() }
    fn diff(&self, current: &Option<ProviderIR>, target: &ProviderIR) -> Vec<String> { base_diff(current, target, "hermes") }
    fn render(&self, ir: &ProviderIR) -> Rendered { Rendered::Yaml(self.render_impl(ir)) }
    fn write(&self, rendered: &Rendered, backup: bool) -> Result<(), String> {
        if backup { self.io.backup(&self.path); }
        self.io.write(&self.path, &rendered.to_text()).map_err(|e| e.to_string())
    }
}
