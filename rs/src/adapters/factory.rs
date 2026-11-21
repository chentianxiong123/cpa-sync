//! adapter 工厂（对照 adapters 包的 iter_instances 分发）
use crate::core::config::{AdapterInstance, SyncConfig};
use super::{Adapter, AdapterInstance as AI};
use super::claude::ClaudeAdapter;
use super::codex::CodexAdapter;
use super::crush::CrushAdapter;
use super::dsh::DshAdapter;
use super::hermes::HermesAdapter;
use super::omp::OmpAdapter;
use super::openclaw::OpenclawAdapter;
use super::opencode::OpencodeAdapter;
use super::picoclaw::PicoclawAdapter;
use super::pi::PiAdapter;

/// 从 SyncConfig 的 adapter_instances 构造某 agent 的所有实例
pub fn build(cfg: &SyncConfig, agent: &str) -> Vec<(String, Box<dyn Adapter>)> {
    let instances: Vec<AI> = cfg.adapter_instances.get(agent).cloned().unwrap_or_default().into_iter()
        .map(|i| AI {
            name: i.name,
            // 通用语义：host 为空 = 本机模式（对照 Python 的 `self.host or None`）
            host: if i.host.is_empty() { None } else { Some(i.host) },
            path: if i.path.is_empty() { None } else { Some(i.path) },
            platform: i.platform,
            enabled_models: i.enabled_models,
            config_path: i.config_path,
        }).collect();
    match agent {
        "opencode" => OpencodeAdapter::new(&AI::default()).iter_instances(&instances),
        "pi" => PiAdapter::new(&AI::default()).iter_instances(&instances),
        "openclaw" => OpenclawAdapter::new(&AI::default()).iter_instances(&instances),
        "picoclaw" => PicoclawAdapter::new(&AI::default()).iter_instances(&instances),
        "crush" => CrushAdapter::new(&AI::default()).iter_instances(&instances),
        "dsh" => DshAdapter::new(&AI::default()).iter_instances(&instances),
        "hermes" => HermesAdapter::new(&AI::default()).iter_instances(&instances),
        "omp" => OmpAdapter::new(&AI::default()).iter_instances(&instances),
        "claude" => ClaudeAdapter::new(&AI::default()).iter_instances(&instances),
        "codex" => CodexAdapter::new(&AI::default()).iter_instances(&instances),
        _ => Vec::new(),
    }
}
