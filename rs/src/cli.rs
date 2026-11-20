//! 同步核心逻辑（对照 cpa-sync CLI 的 check / apply）
use crate::adapters::{self, Adapter, Rendered};
use crate::core::config::{build_provider_ir, filter_models, SyncConfig};
use crate::core::cpa_client::CPAClient;
use crate::core::ir::ProviderIR;
use serde_json::{json, Value};
use std::path::PathBuf;

fn config_path(explicit: String) -> Option<String> {
    if explicit.is_empty() { None } else { Some(explicit) }
}

/// 连 CPA 拉模型 + 构建目标 IR（对照 _pull_models + build_provider_ir）
async fn pull_target(cfg: &SyncConfig) -> Result<(ProviderIR, usize), String> {
    let client = CPAClient::new(&cfg.cpa.base_url, &cfg.cpa.api_key, 10);
    let models = client.list_models().await.map_err(|e| e.to_string())?;
    let total = models.len();
    let kept = filter_models(models, &cfg.cpa.include_models, &cfg.cpa.exclude_models);
    let ir = build_provider_ir(cfg, kept);
    Ok((ir, total))
}

/// 对应 cpa-sync check
pub async fn check(cfg_path: &str) -> Value {
    let cfg = match SyncConfig::load(config_path(cfg_path.to_string()).as_deref()) {
        Ok(c) => c,
        Err(e) => return json!({"ok": false, "error": e.to_string()}),
    };
    let (target, total) = match pull_target(&cfg).await {
        Ok(v) => v,
        Err(e) => return json!({"ok": false, "error": format!("连 CPA 失败: {}", e)}),
    };
    let mut diffs = Vec::new();
    let mut agents = 0usize;
    for agent in adapters::available_names() {
        let insts = adapters::factory::build(&cfg, &agent);
        for (name, adapter) in insts {
            if adapter.diff(&None, &target).is_empty() { continue; }
            let cur = adapter.load_current();
            let lines = adapter.diff(&cur, &target);
            if lines.is_empty() { continue; }
            agents += 1;
            diffs.push(json!({"agent": agent, "instance": name, "lines": lines}));
        }
    }
    json!({
        "ok": true,
        "models_total": total,
        "models_kept": target.models.len(),
        "changed_agents": agents,
        "diffs": diffs,
        "summary": format!("共 {} 个模型（{} 个命中过滤器），{} 个 agent 需更新", total, target.models.len(), agents),
    })
}

/// 对应 cpa-sync apply
pub async fn apply(cfg_path: &str, dry_run: bool, sync_only: &[String]) -> Value {
    let cfg = match SyncConfig::load(config_path(cfg_path.to_string()).as_deref()) {
        Ok(c) => c,
        Err(e) => return json!({"ok": false, "error": e.to_string()}),
    };
    let (target, total) = match pull_target(&cfg).await {
        Ok(v) => v,
        Err(e) => return json!({"ok": false, "error": format!("连 CPA 失败: {}", e)}),
    };
    let mut results = Vec::new();
    let mut ok = 0usize;
    let mut fail = 0usize;
    for agent in adapters::available_names() {
        if !sync_only.is_empty() && !sync_only.contains(&agent) { continue; }
        let insts = adapters::factory::build(&cfg, &agent);
        for (name, adapter) in insts {
            let cur = adapter.load_current();
            let diff_lines = adapter.diff(&cur, &target);
            if diff_lines.is_empty() {
                results.push(json!({"agent": agent, "instance": name, "status": "已同步", "lines": []}));
                continue;
            }
            if dry_run {
                results.push(json!({"agent": agent, "instance": name, "status": "待应用（dry-run）", "lines": diff_lines}));
                continue;
            }
            let rendered = adapter.render(&target);
            match adapter.write(&rendered, true) {
                Ok(()) => { ok += 1; results.push(json!({"agent": agent, "instance": name, "status": "已应用", "lines": diff_lines})); }
                Err(e) => { fail += 1; results.push(json!({"agent": agent, "instance": name, "status": "失败", "error": e, "lines": diff_lines})); }
            }
        }
    }
    json!({
        "ok": fail == 0,
        "dry_run": dry_run,
        "models_total": total,
        "models_kept": target.models.len(),
        "applied": ok,
        "failed": fail,
        "results": results,
        "summary": if dry_run { format!("dry-run：{} 个模型待同步", target.models.len()) } else { format!("已同步 {} 个 agent，失败 {} 个（共 {} 个模型）", ok, fail, target.models.len()) },
    })
}

/// 对应 cpa-sync models：只打印会同步的模型清单
pub async fn models(cfg_path: &str) -> Value {
    let cfg = match SyncConfig::load(config_path(cfg_path.to_string()).as_deref()) {
        Ok(c) => c,
        Err(e) => return json!({"ok": false, "error": e.to_string()}),
    };
    let (target, total) = match pull_target(&cfg).await {
        Ok(v) => v,
        Err(e) => return json!({"ok": false, "error": format!("连 CPA 失败: {}", e)}),
    };
    let list: Vec<Value> = target.models.iter().map(|m| json!({
        "id": m.id,
        "input": m.input_modes.join("/"),
        "context": m.context_window,
        "max_tokens": m.max_tokens,
        "reasoning": m.reasoning,
    })).collect();
    json!({"ok": true, "models_total": total, "kept": list.len(), "models": list})
}

/// 对应 cpa-sync list：列出全部 agent 的实例（不发网络）
pub fn list(cfg_path: &str) -> Value {
    let cfg = match SyncConfig::load(config_path(cfg_path.to_string()).as_deref()) {
        Ok(c) => c,
        Err(e) => return json!({"ok": false, "error": e.to_string()}),
    };
    let mut out = Vec::new();
    for agent in adapters::available_names() {
        for (name, adapter) in adapters::factory::build(&cfg, &agent) {
            out.push(json!({"agent": adapter.name(), "instance": name}));
        }
        let _ = &Rendered::Text(String::new());
    }
    json!({"ok": true, "instances": out})
}

/// 对应 cpa-sync CLI 入口（面板「同步」按钮走这里）
pub async fn run_central(cmd: &str) -> (i32, String) {
    let arg = if cmd.is_empty() { "check" } else { cmd };
    let v = if arg.starts_with("apply") {
        cli_apply("").await
    } else if arg.starts_with("models") {
        cli_models("").await
    } else {
        check("").await
    };
    if v.get("ok").and_then(|x| x.as_bool()).unwrap_or(false) { (0, serde_json::to_string_pretty(&v).unwrap_or_default()) }
    else { (1, serde_json::to_string_pretty(&v).unwrap_or_default()) }
}

/// 对应 agent-sync CLI 入口
pub async fn run_client(cmd: &str) -> (i32, String) {
    let arg = if cmd.is_empty() { "check" } else { cmd };
    let v = if arg.starts_with("models") { cli_models("").await } else { check("").await };
    if v.get("ok").and_then(|x| x.as_bool()).unwrap_or(false) { (0, serde_json::to_string_pretty(&v).unwrap_or_default()) }
    else { (1, serde_json::to_string_pretty(&v).unwrap_or_default()) }
}

async fn cli_apply(_p: &str) -> Value { apply("", false, &[]).await }
async fn cli_models(_p: &str) -> Value { models("").await }
