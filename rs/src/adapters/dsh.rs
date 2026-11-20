//! dsh 适配器（对照 adapters/dsh.py）
//! 同步 CPA 配置到 ~/.dsh/profiles/web/cordis.patch.yml 的 llm-pi-ai.providers.cpa
use crate::core::ir::{ModelIR, ProviderIR};
use crate::core::remote::RemoteIO;
use crate::adapters::{Adapter, AdapterInstance, Rendered, base_diff};
use serde_yaml::{Mapping, Value as YV};

pub struct DshAdapter { pub path: String, pub io: RemoteIO }

impl DshAdapter {
    pub fn new(inst: &AdapterInstance) -> Self {
        let path = inst.path.clone().unwrap_or_else(|| Self::default_path());
        DshAdapter { path, io: RemoteIO::new(inst.host.as_deref(), inst.platform.as_deref()) }
    }
    fn default_path() -> String {
        if let Ok(p) = std::env::var("DSH_PATCH_YML") { return p; }
        let home = std::env::var("USERPROFILE").ok().or_else(|| std::env::var("HOME").ok()).unwrap_or_default();
        format!("{}/.dsh/profiles/web/cordis.patch.yml", home)
    }
    fn iter(&self, instances: &[AdapterInstance]) -> Vec<(String, Box<dyn Adapter>)> {
        if instances.is_empty() {
            let i = AdapterInstance { name: "default".into(), ..Default::default() };
            vec![("default".into(), Box::new(DshAdapter::new(&i)))]
        } else {
            instances.iter().map(|i| (i.name.clone(), Box::new(DshAdapter::new(i)) as Box<dyn Adapter>)).collect()
        }
    }
    fn read_seq(&self) -> Vec<YV> {
        let text = self.io.read(&self.path).unwrap_or_default();
        if text.trim().is_empty() { return vec![]; }
        match serde_yaml::from_str::<YV>(&text) {
            Ok(YV::Sequence(vs)) => vs,
            _ => vec![],
        }
    }

    /// 对应 _find_llm_pi_ai()：数组里 id=="llm-pi-ai" 的条目
    fn find_llm_pi_ai(entries: &[YV]) -> Option<usize> {
        entries.iter().position(|e| e.get("id").and_then(|v| v.as_str()) == Some("llm-pi-ai"))
    }

    /// 对应 _provider_to_ir()（模态：条目 input → defaultInput 路由 → [text]）
    fn provider_to_ir(provider: &YV) -> ProviderIR {
        let route_default: Vec<String> = provider.get("defaultInput").and_then(|v| v.as_sequence())
            .map(|a| a.iter().filter_map(|x| x.as_str().map(String::from)).collect())
            .unwrap_or_else(|| vec!["text".into()]);
        let mut models = Vec::new();
        if let Some(list) = provider.get("models").and_then(|v| v.as_sequence()) {
            for m in list {
                let input: Vec<String> = m.get("input").and_then(|v| v.as_sequence())
                    .map(|a| a.iter().filter_map(|x| x.as_str().map(String::from)).collect())
                    .filter(|a: &Vec<String>| !a.is_empty())
                    .unwrap_or_else(|| route_default.clone());
                models.push(ModelIR {
                    id: m.get("id").and_then(|v| v.as_str()).unwrap_or("").into(),
                    context_window: m.get("contextWindow").and_then(|v| v.as_u64()).unwrap_or(128000),
                    max_tokens: m.get("maxTokens").and_then(|v| v.as_u64()).unwrap_or(16384) as u32,
                    input_modes: input,
                    reasoning: false,
                    extra: Default::default(),
                });
            }
        }
        ProviderIR {
            provider_id: "cpa".into(),
            api: provider.get("api").and_then(|v| v.as_str()).unwrap_or("openai-completions").into(),
            base_url: provider.get("baseURL").and_then(|v| v.as_str()).unwrap_or("").into(),
            api_key: String::new(),
            models,
            meta: Default::default(),
        }
    }

    fn load_impl(&self) -> Option<ProviderIR> {
        let entries = self.read_seq();
        let idx = Self::find_llm_pi_ai(&entries)?;
        let entry = &entries[idx];
        let cpa = match entry.get("config").and_then(|c| c.get("providers")).and_then(|p| p.get("cpa")) {
            Some(c) => c.clone(),
            None => return None,
        };
        Some(Self::provider_to_ir(&cpa))
    }

    /// 对应 render()：只更新 llm-pi-ai.providers.cpa（保留其它条目）
    fn render_impl(&self, ir: &ProviderIR) -> YV {
        let mut entries: Vec<YV> = self.read_seq();
        let idx = if let Some(i) = Self::find_llm_pi_ai(&entries) { i } else {
            let mut e = Mapping::new();
            e.insert(YV::String("id".into()), YV::String("llm-pi-ai".into()));
            let mut cfg = Mapping::new();
            cfg.insert(YV::String("providers".into()), YV::Mapping(Default::default()));
            e.insert(YV::String("config".into()), YV::Mapping(cfg));
            entries.push(YV::Mapping(e));
            entries.len() - 1
        };
        // 保留 apiKeyEnv / displayName / retryPolicy，只更新 baseURL/api/models
        let mut cpa = if let Some(c) = entries[idx].get("config").and_then(|v| v.get("providers")).and_then(|v| v.get("cpa")) {
            c.clone()
        } else {
            YV::Mapping(Default::default())
        };
        let c = cpa.as_mapping_mut().unwrap();
        c.insert(YV::String("baseURL".into()), YV::String(ir.base_url.clone()));
        c.insert(YV::String("api".into()), YV::String(if ir.api.is_empty() { "openai-completions".into() } else { ir.api.clone() }));
        if !c.contains_key(&YV::String("displayName".into())) {
            c.insert(YV::String("displayName".into()), YV::String("cpa".into()));
        }
        c.entry(YV::String("apiKeyEnv".into())).or_insert(YV::String("DSH_CPA_API_KEY".into()));
        let mut models: Vec<YV> = Vec::new();
        for m in &ir.models {
            let mut mm = Mapping::new();
            mm.insert(YV::String("id".into()), YV::String(m.id.clone()));
            mm.insert(YV::String("contextWindow".into()), YV::from(m.context_window));
            mm.insert(YV::String("maxTokens".into()), YV::from(m.max_tokens));
            mm.insert(YV::String("input".into()), YV::Sequence(m.input_modes.iter().map(|s| YV::String(s.clone())).collect()));
            models.push(YV::Mapping(mm));
        }
        c.insert(YV::String("models".into()), YV::Sequence(models));
        // 写回 entry.config.providers.cpa
        let entry = &mut entries[idx];
        let mut entry_m = Mapping::new();
        if let Some(e) = entry.as_mapping() { for (k, v) in e { entry_m.insert(k.clone(), v.clone()); } }
        if !entry_m.contains_key(&YV::String("config".into())) {
            entry_m.insert(YV::String("config".into()), YV::Mapping(Default::default()));
        }
        let cfg = entry_m.get_mut(&YV::String("config".into())).unwrap().as_mapping_mut().unwrap();
        if !cfg.contains_key(&YV::String("providers".into())) {
            cfg.insert(YV::String("providers".into()), YV::Mapping(Default::default()));
        }
        let prov = cfg.get_mut(&YV::String("providers".into())).unwrap().as_mapping_mut().unwrap();
        prov.insert(YV::String("cpa".into()), cpa);
        *entry = YV::Mapping(entry_m);
        YV::Sequence(entries)
    }

}

impl Adapter for DshAdapter {
    fn name(&self) -> &str { "dsh" }
    fn iter_instances(&self, instances: &[AdapterInstance]) -> Vec<(String, Box<dyn Adapter>)> { self.iter(instances) }
    fn load_current(&self) -> Option<ProviderIR> { self.load_impl() }
    fn diff(&self, current: &Option<ProviderIR>, target: &ProviderIR) -> Vec<String> { base_diff(current, target, "dsh") }
    fn render(&self, ir: &ProviderIR) -> Rendered { Rendered::Yaml(self.render_impl(ir)) }
    fn write(&self, rendered: &Rendered, backup: bool) -> Result<(), String> {
        if backup { self.io.backup(&self.path); }
        self.io.write(&self.path, &rendered.to_text()).map_err(|e| e.to_string())
    }
}
