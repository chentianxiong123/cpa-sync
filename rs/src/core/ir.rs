//! IR: 中间表示（对应 core/ir.py）
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

fn d_ctx() -> u64 { 512000 }
fn d_max() -> u32 { 16384 }

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct ModelIR {
    pub id: String,
    #[serde(default = "d_ctx")]
    pub context_window: u64,
    #[serde(default = "d_max")]
    pub max_tokens: u32,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub input_modes: Vec<String>,
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub reasoning: bool,
    #[serde(default, skip_serializing_if = "HashMap::is_empty")]
    pub extra: HashMap<String, serde_json::Value>,
}

fn d_api() -> String { "openai-completions".into() }

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct ProviderIR {
    pub provider_id: String,
    #[serde(default = "d_api")]
    pub api: String,
    pub base_url: String,
    pub api_key: String,
    pub models: Vec<ModelIR>,
    #[serde(default, skip_serializing_if = "HashMap::is_empty")]
    pub meta: HashMap<String, serde_json::Value>,
}
