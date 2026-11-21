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

/// 确定本次要处理哪些 agent（对照 Python 的 pick_adapters）。
///
/// 优先级：命令行显式指定 > 配置里的 targets > 全部 adapter。
/// 这是同步范围的关键阀门：写错会把所有 agent 全量改写。
pub fn pick_agents(cfg: &SyncConfig, explicit: &[String]) -> Vec<String> {
    let all = adapters::available_names();
    // 先 clone 成 owned，避免引用临时值的生命周期错误
    let non_empty: Vec<String> = explicit.iter().filter(|s| !s.is_empty()).cloned().collect();
    if !non_empty.is_empty() {
        for n in &non_empty {
            if !all.contains(n) {
                eprintln!("未知 adapter: {}（可用: {}）", n, all.join(", "));
                std::process::exit(2);
            }
        }
        return non_empty;
    }
    if !cfg.targets.is_empty() {
        return cfg.targets.iter().filter(|n| all.contains(n)).cloned().collect();
    }
    adapters::available_names()
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
    for agent in pick_agents(&cfg, &[String::new()]) {
        let insts = adapters::factory::build(&cfg, agent.as_str());
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
    for agent in pick_agents(&cfg, sync_only) {
        let insts = adapters::factory::build(&cfg, agent.as_str());
        for (name, adapter) in insts {
            let cur = adapter.load_current();
            let diff_lines = adapter.diff(&cur, &target);
            if diff_lines.is_empty() {
                // 无 diff：仍同步 side_effects（如 omp enabledModels），确保深度裁剪始终下发
                let se_ok = if dry_run { true } else { adapter.side_effects().is_ok() };
                results.push(json!({"agent": agent, "instance": name, "status": "已同步", "lines": [], "side_effects": se_ok}));
                continue;
            }
            if dry_run {
                results.push(json!({"agent": agent, "instance": name, "status": "待应用（dry-run）", "lines": diff_lines}));
                continue;
            }
            let rendered = adapter.render(&target);
            match adapter.write(&rendered, true) {
                Ok(()) => { ok += 1; let se_ok = adapter.side_effects().is_ok(); results.push(json!({"agent": agent, "instance": name, "status": "已应用", "lines": diff_lines, "side_effects": se_ok})); }
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
    for agent in pick_agents(&cfg, &[]) {
        for (name, adapter) in adapters::factory::build(&cfg, agent.as_str()) {
            out.push(json!({"agent": adapter.name(), "instance": name}));
        }
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

/// 对应 cpa-sync doctor：连接检查 + 白名单校验 + adapter 健康（纯读诊断）
pub async fn doctor(cfg_path: &str) -> Value {
    let cfg = match SyncConfig::load(config_path(cfg_path.to_string()).as_deref()) {
        Ok(c) => c,
        Err(e) => return json!({"ok": false, "error": e.to_string()}),
    };
    let mut checks: Vec<Value> = Vec::new();

    // 1) CPA 连接检查
    let (target, total) = match pull_target(&cfg).await {
        Ok(v) => v,
        Err(e) => {
            checks.push(json!({"kind": "conn", "ok": false, "line": format!("✗ 连 CPA 失败: {}", e)}));
            return json!({"ok": false, "checks": checks});
        }
    };
    checks.push(json!({"kind": "conn", "ok": true,
        "line": format!("✓ {} 可达，暴露 {} 个模型", cfg.cpa.base_url, total)}));

    // 2) 模型白名单校验
    let avail: Vec<&str> = target.models.iter().map(|m| m.id.as_str()).collect();
    for mid in &cfg.cpa.include_models {
        let ok = avail.contains(&mid.as_str())
            || avail.iter().any(|a| !a.contains('/') && mid.ends_with(&format!("/{}", a)));
        checks.push(json!({"kind": "whitelist", "ok": ok, "model": mid,
            "line": format!("{} {}", if ok { "✓" } else { "⚠" }, mid)}));
    }

    // 3) adapter 健康（幽灵模型检测）
    for agent in pick_agents(&cfg, &[]) {
        let insts = adapters::factory::build(&cfg, agent.as_str());
        for (name, adapter) in insts {
            let cur = adapter.load_current();
            match cur {
                None => checks.push(json!({"kind": "adapter", "ok": true, "agent": agent,
                    "line": format!("  {}: 未配置", name)})),
                Some(c) => {
                    let ghosts: Vec<&String> = c.models.iter()
                        .filter(|m| !avail.contains(&m.id.as_str()))
                        .map(|m| &m.id).collect();
                    let ghost_s = ghosts.iter().map(|s| s.as_str())
                        .collect::<Vec<_>>().join(", ");
                    let line = format!("  {}: {}  models={}  {}",
                        name, c.base_url, c.models.len(),
                        if ghosts.is_empty() { String::new() } else {
                            format!("⚠ 幽灵模型: {}", ghost_s)
                        });
                    checks.push(json!({"kind": "adapter", "ok": ghosts.is_empty(),
                        "agent": agent, "ghosts": ghosts, "line": line}));
                }
            }
        }
    }
    let bad = checks.iter().filter(|c| c.get("ok").and_then(|v| v.as_bool()) == Some(false)).count();
    json!({"ok": true, "checks": checks, "summary": format!("共 {} 项检查，{} 项需注意", checks.len(), bad)})
}
