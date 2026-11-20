//! adapter 模块（对照 adapters/ 包）
//! 通用抽象：Adapter trait + AdapterInstance（host/path 参数化，不写死用户目录）
pub mod base;
pub mod claude;
pub mod ccswitch;
pub mod codex;
pub mod crush;
pub mod dsh;
pub mod hermes;
pub mod omp;
pub mod openclaw;
pub mod opencode;
pub mod picoclaw;
pub mod pi;
pub mod factory;

use crate::core::ir::ProviderIR;
use serde_json::Value;

/// 渲染产物（对照 Python render() 返回 dict/str 的 Any）
pub enum Rendered {
    Json(Value),
    Yaml(serde_yaml::Value),
    Text(String),
}

impl Rendered {
    pub fn to_text(&self) -> String {
        match self {
            Rendered::Json(v) => { let mut t = serde_json::to_string_pretty(v).unwrap_or_default(); t.push('\n'); t }
            Rendered::Yaml(v) => serde_yaml::to_string(v).unwrap_or_default(),
            Rendered::Text(s) => s.clone(),
        }
    }
}

/// 对应 config.AdapterInstance：{name, host, path, platform}
#[derive(Debug, Clone, Default)]
pub struct AdapterInstance {
    pub name: String,
    pub host: Option<String>,
    pub path: Option<String>,
    pub platform: Option<String>,
}

/// 对应 adapters.base.Adapter
pub trait Adapter: Send {
    fn name(&self) -> &str;
    /// 对应 iter_instances()：从 adapter_instances 展开多实例
    fn iter_instances(&self, instances: &[AdapterInstance]) -> Vec<(String, Box<dyn Adapter>)>;
    /// 对应 load_current()
    fn load_current(&self) -> Option<ProviderIR>;
    /// 对应 diff()
    fn diff(&self, current: &Option<ProviderIR>, target: &ProviderIR) -> Vec<String>;
    /// 对应 render()
    fn render(&self, ir: &ProviderIR) -> Rendered;
    /// 对应 write()
    fn write(&self, rendered: &Rendered, backup: bool) -> Result<(), String>;
}

/// 对应 adapters.available_names()
pub fn available_names() -> Vec<String> {
    ["opencode", "pi", "omp", "dsh", "codex", "claude", "crush", "hermes", "picoclaw", "openclaw"]
        .into_iter().map(String::from).collect()
}

pub fn base_diff(current: &Option<ProviderIR>, target: &ProviderIR, name: &str) -> Vec<String> {
    base::base_diff(current, target, name)
}
