//! 适配器公共逻辑（对照 adapters/base.py 的 diff）
use crate::core::ir::ProviderIR;

pub fn base_diff(current: &Option<ProviderIR>, target: &ProviderIR, name: &str) -> Vec<String> {
    let mut lines = Vec::new();
    match current {
        None => lines.push(format!("  [{}] 当前无配置，将新建", name)),
        Some(cur) => {
            if cur.base_url != target.base_url {
                lines.push(format!("  [{}] base_url: {} -> {}", name, cur.base_url, target.base_url));
            }
            if cur.api_key != target.api_key {
                let ck = if cur.api_key.is_empty() { "(空)" } else { "****" };
                let tk = if target.api_key.is_empty() { "(空)" } else { "****" };
                lines.push(format!("  [{}] api_key: {} -> {}", name, ck, tk));
            }
            let cur_ids: std::collections::BTreeSet<String> = cur.models.iter().map(|m| m.id.clone()).collect();
            let tgt_ids: std::collections::BTreeSet<String> = target.models.iter().map(|m| m.id.clone()).collect();
            let added: Vec<&str> = tgt_ids.difference(&cur_ids).into_iter().map(|s| s.as_str()).collect();
            let removed: Vec<&str> = cur_ids.difference(&tgt_ids).into_iter().map(|s| s.as_str()).collect();
            if !added.is_empty() {
                lines.push(format!("  [{}] 新增模型: {}", name, added.join(", ")));
            }
            if !removed.is_empty() {
                lines.push(format!("  [{}] 移除模型: {}", name, removed.join(", ")));
            }
            let common: Vec<&str> = tgt_ids.intersection(&cur_ids).into_iter().map(|s| s.as_str()).collect();
            for mid in common {
                let cm = cur.models.iter().find(|m| m.id == *mid);
                let tm = target.models.iter().find(|m| m.id == *mid);
                if let (Some(cm), Some(tm)) = (cm, tm) {
                    if cm.context_window != tm.context_window {
                        lines.push(format!("  [{}] 模型 {}: contextWindow {} -> {}", name, mid, cm.context_window, tm.context_window));
                    }
                    if cm.max_tokens != tm.max_tokens {
                        lines.push(format!("  [{}] 模型 {}: maxTokens {} -> {}", name, mid, cm.max_tokens, tm.max_tokens));
                    }
                    if cm.input_modes != tm.input_modes {
                        lines.push(format!("  [{}] 模型 {}: input {} -> {}", name, mid, cm.input_modes.join("/"), tm.input_modes.join("/")));
                    }
                    if cm.reasoning != tm.reasoning {
                        lines.push(format!("  [{}] 模型 {}: reasoning {} -> {}", name, mid, cm.reasoning, tm.reasoning));
                    }
                }
            }
        }
    }
    lines
}
