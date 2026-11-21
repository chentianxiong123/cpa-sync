//! omp 适配器（对照 adapters/omp.py）
//! 同步 CPA provider 到 omp 的 agents.yaml（providers.cpa + models 数组）
use crate::core::ir::{ModelIR, ProviderIR};
use crate::core::remote::RemoteIO;
use crate::adapters::{Adapter, AdapterInstance, Rendered, base_diff};
use serde_yaml::{Mapping, Value as YV};

pub struct OmpAdapter { pub path: String, pub io: RemoteIO, pub enabled_models: Vec<String>, pub config_path: String }

impl OmpAdapter {
    pub fn new(inst: &AdapterInstance) -> Self {
        let path = inst.path.clone().unwrap_or_else(|| Self::default_path());
        let config_path = inst.config_path.clone().unwrap_or_else(|| Self::default_config_path(&path));
        OmpAdapter {
            path,
            io: RemoteIO::new(inst.host.as_deref(), inst.platform.as_deref()),
            enabled_models: inst.enabled_models.clone().unwrap_or_default(),
            config_path,
        }
    }
    fn default_config_path(path: &str) -> String {
        // omp 18.x：~/.omp/agent/config.yml（agent 子目录）；旧版退化根 config.yml
        let p = std::path::Path::new(path);
        let dir = p.parent().unwrap_or(std::path::Path::new("."));
        dir.join("agent").join("config.yml").to_string_lossy().into_owned()
    }
    /// 深度裁剪：把 enabled_models 白名单写进 omp config.yml（保留其它字段）
    fn write_enabled_models(&self) -> Result<(), String> {
        let cfg = if self.config_path.is_empty() { Self::default_config_path(&self.path) } else { self.config_path.clone() };
        let mut data: YV = self.io.read(&cfg)
            .and_then(|t| serde_yaml::from_str(&t).ok())
            .unwrap_or_else(|| YV::Mapping(Default::default()));
        if let Some(m) = data.as_mapping_mut() {
            m.insert(
                YV::String("enabledModels".into()),
                YV::Sequence(self.enabled_models.iter().map(|s| YV::String(s.clone())).collect()),
            );
        }
        let txt = serde_yaml::to_string(&data).map_err(|e| e.to_string())?;
        self.io.write(&cfg, &txt).map_err(|e| e.to_string())
    }
    fn default_path() -> String {
        if let Ok(p) = std::env::var("OMP_AGENTS_YAML") { return p; }
        let home = std::env::var("USERPROFILE").ok().or_else(|| std::env::var("HOME").ok()).unwrap_or_default();
        format!("{}/.omp/agents.yaml", home)
    }
    fn iter(&self, instances: &[AdapterInstance]) -> Vec<(String, Box<dyn Adapter>)> {
        if instances.is_empty() {
            let i = AdapterInstance { name: "default".into(), ..Default::default() };
            vec![("default".into(), Box::new(OmpAdapter::new(&i)))]
        } else {
            instances.iter().map(|i| (i.name.clone(), Box::new(OmpAdapter::new(i)) as Box<dyn Adapter>)).collect()
        }
    }
    fn read(&self) -> YV {
        let text = self.io.read(&self.path).unwrap_or_default();
        if text.trim().is_empty() { return YV::Mapping(Default::default()); }
        serde_yaml::from_str(&text).unwrap_or(YV::Mapping(Default::default()))
    }

    /// 对应 _find_cpa_provider_key()：优先 key=cpa，否则任意 name=cpa
    fn find_cpa_key(data: &YV) -> Option<String> {
        let providers = data.get("providers")?.as_mapping()?;
        for (k, v) in providers {
            if k.as_str() == Some("cpa") { return Some("cpa".into()); }
        }
        for (k, v) in providers {
            if v.get("name").and_then(|n| n.as_str()) == Some("cpa") {
                return Some(k.as_str().unwrap_or("").to_string());
            }
        }
        None
    }

    fn provider_to_ir(provider: &YV) -> ProviderIR {
        let mut models = Vec::new();
        if let Some(list) = provider.get("models").and_then(|v| v.as_sequence()) {
            for m in list {
                models.push(ModelIR {
                    id: m.get("id").and_then(|v| v.as_str()).unwrap_or("").into(),
                    context_window: m.get("contextWindow").and_then(|v| v.as_u64()).unwrap_or(256000),
                    max_tokens: m.get("maxTokens").and_then(|v| v.as_u64()).unwrap_or(16384) as u32,
                    input_modes: m.get("input").and_then(|v| v.as_sequence())
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

impl Adapter for OmpAdapter {
    fn name(&self) -> &str { "omp" }
    fn iter_instances(&self, instances: &[AdapterInstance]) -> Vec<(String, Box<dyn Adapter>)> { self.iter(instances) }

    fn load_current(&self) -> Option<ProviderIR> {
        let data = self.read();
        let key = Self::find_cpa_key(&data)?;
        let p = data.get("providers")?.get(&YV::String(key.clone()))?;
        Some(Self::provider_to_ir(p))
    }

    fn diff(&self, current: &Option<ProviderIR>, target: &ProviderIR) -> Vec<String> { base_diff(current, target, "omp") }

    /// 对应 render()：providers.cpa（含清旧 cpa key 收拢）
    fn render(&self, ir: &ProviderIR) -> Rendered {
        let mut data = self.read();
        // 清掉 name=cpa 但 key != cpa 的旧条目
        if let Some(providers) = data.get_mut("providers").and_then(|v| v.as_mapping_mut()) {
            let stale: Vec<YV> = providers.iter().filter(|(k, v)| {
                k.as_str() != Some("cpa") && v.get("name").and_then(|n| n.as_str()) == Some("cpa")
            }).map(|(k, _)| k.clone()).collect();
            for k in stale { providers.remove(&k); }
        }
        {
            let d = data.as_mapping_mut().unwrap();
            if !d.contains_key(&YV::String("providers".into())) {
                d.insert(YV::String("providers".into()), YV::Mapping(Default::default()));
            }
            let pm = d.get_mut(&YV::String("providers".into())).unwrap().as_mapping_mut().unwrap();
            let ck = YV::String("cpa".into());
            if !pm.contains_key(&ck) { pm.insert(YV::String("cpa".into()), YV::Mapping(Default::default())); }
            let c = pm.get_mut(&ck).unwrap().as_mapping_mut().unwrap();
            c.insert(YV::String("name".into()), YV::String("cpa".into()));
            c.insert(YV::String("baseUrl".into()), YV::String(ir.base_url.clone()));
            c.insert(YV::String("api".into()), YV::String(if ir.api.is_empty() { "openai-completions".into() } else { ir.api.clone() }));
            c.insert(YV::String("apiKey".into()), YV::String(ir.api_key.clone()));
            let mut models: Vec<YV> = Vec::new();
            for m in &ir.models {
                let mut mm = Mapping::new();
                mm.insert(YV::String("id".into()), YV::String(m.id.clone()));
                mm.insert(YV::String("name".into()), YV::String(m.id.clone()));
                mm.insert(YV::String("reasoning".into()), YV::from(m.reasoning));
                mm.insert(YV::String("input".into()), YV::Sequence(m.input_modes.iter().map(|s| YV::String(s.clone())).collect()));
                mm.insert(YV::String("contextWindow".into()), YV::from(m.context_window));
                mm.insert(YV::String("maxTokens".into()), YV::from(m.max_tokens));
                models.push(YV::Mapping(mm));
            }
            c.insert(YV::String("models".into()), YV::Sequence(models));
        }
        Rendered::Yaml(data)
    }

    fn write(&self, rendered: &Rendered, backup: bool) -> Result<(), String> {
        if backup { self.io.backup(&self.path); }
        self.io.write(&self.path, &rendered.to_text()).map_err(|e| e.to_string())?;
        if !self.enabled_models.is_empty() {
            self.write_enabled_models()?;
        }
        Ok(())
    }
}
