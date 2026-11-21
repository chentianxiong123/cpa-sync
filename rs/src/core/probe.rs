//! 探测（probe）：选中 host → 自动嗅探该机装了哪些 agent、配置在哪。
//!
//! 机制：对每台选中 host，逐个 agent 用其「候选路径集」跑 RemoteIO::exists，
//! 命中即认为该 agent 已安装。路径候选来源：
//!   1. config 里 adapter_instances 显式声明的 path（最准，优先）
//!   2. 各 adapter 的 default_path（按目标机平台推导 HOME）
//! 不做任何猜测式硬编码主机名——host/路径全由 config 声明驱动。

use crate::adapters;
use crate::core::config::SyncConfig;
use crate::core::remote::RemoteIO;
use serde_json::{json, Value};

/// 各 adapter 在「指定平台 + HOME」下的候选配置路径（相对 HOME）。
/// 返回相对路径列表；probe 用目标机 HOME 拼绝对路径。
/// claude/codex 走 cc-switch db，不在文件层探测（单独标注）。
fn candidate_rel_paths(agent: &str, _platform: &str) -> Vec<String> {
    // Windows 用反斜杠无所谓，RemoteIO.exists 走 ssh 时按平台选 test 语法；
    // 这里统一用正斜杠，HOME 由目标机 shell 展开。
    match agent {
        "omp" => vec![
            ".omp/agent/models.yml".into(),
            ".omp/agents.yaml".into(),
        ],
        "dsh" => vec![".dsh/profiles/web/cordis.patch.yml".into()],
        "picoclaw" => vec![".picoclaw/config.json".into()],
        "crush" => vec![".config/crush/crushrc".into()],
        "openclaw" => vec![".openclaw/openclaw.json".into()],
        "hermes" => vec![
            ".hermes/config.yaml".into(),
            "AppData/Local/hermes/config.yaml".into(), // win
        ],
        "pi" => vec![".pi/agent/models.json".into()],
        "opencode" => vec![".config/opencode/opencode.json".into()],
        // cc-switch 系：db 层，不在文件探测范围
        "claude" | "codex" => vec![],
        _ => vec![],
    }
}

/// 探测单台 host：返回该机各 agent 的安装情况。
fn probe_host(host: &str, platform: &str, cfg: &SyncConfig) -> Value {
    let io = RemoteIO::new(Some(host), Some(platform));
    let is_win = io.platform() == "win";
    let mut found: Vec<Value> = Vec::new();

    for agent in adapters::available_names() {
        // 1) config 显式 path（优先，最准）
        let explicit: Vec<String> = cfg
            .adapter_instances
            .get(&agent)
            .cloned()
            .unwrap_or_default()
            .into_iter()
            .filter(|i| i.host == host && !i.path.is_empty())
            .map(|i| i.path)
            .collect();

        // 2) default 候选（相对 HOME，让远端 shell 展开）
        let rels = candidate_rel_paths(&agent, platform);
        let home_var = if is_win { "%USERPROFILE%" } else { "$HOME" };
        let sep = if is_win { "\\" } else { "/" };
        let defaults: Vec<String> = rels
            .iter()
            .map(|r| format!("{}{}{}", home_var, sep, r.replace('/', sep)))
            .collect();

        // 逐个候选探测（去重保序）
        let mut hit_path: Option<String> = None;
        let mut checked: Vec<String> = Vec::new();
        for p in explicit.iter().chain(defaults.iter()) {
            if checked.contains(p) { continue; }
            checked.push(p.clone());
            // RemoteIO.exists 对含 $HOME/%USERPROFILE% 的路径：win 的 if exist 不展开 %VAR% 于引号内，
            // unix 的 [ -f "$HOME/x" ] 会展开。win 显式 path 是绝对路径可直接测。
            let ok = io.exists(p);
            if ok && hit_path.is_none() {
                hit_path = Some(p.clone());
            }
        }

        // cc-switch 系单独标注（不文件探测）
        if agent == "claude" || agent == "codex" {
            found.push(json!({"agent": agent, "installed": null, "note": "cc-switch db 层，文件探测不适用"}));
            continue;
        }

        match &hit_path {
            Some(p) => {
                found.push(json!({"agent": agent, "installed": true, "path": p}));
            }
            None => {
                found.push(json!({"agent": agent, "installed": false, "checked": checked}));
            }
        }
    }

    let installed_agents: Vec<&str> = found.iter()
        .filter(|f| f.get("installed").and_then(|v| v.as_bool()) == Some(true))
        .filter_map(|f| f.get("agent").and_then(|v| v.as_str()))
        .collect();

    json!({
        "host": host,
        "platform": io.platform(),
        "reachable": true,
        "installed_agents": installed_agents,
        "agents": found,
    })
}

/// 对应 cpa-sync probe：探测选中 host（逗号分隔）上的 agent 安装情况。
/// hosts 为空 = 探测 config.hosts 里全部 host。
pub fn probe(cfg_path: &str, hosts_csv: &str) -> Value {
    let cfg = match SyncConfig::load(if cfg_path.is_empty() { None } else { Some(cfg_path) }) {
        Ok(c) => c,
        Err(e) => return json!({"ok": false, "error": e.to_string()}),
    };

    // 目标 host 列表：命令行 > config.hosts 全部
    let targets: Vec<String> = if !hosts_csv.is_empty() {
        hosts_csv.split(',').map(|s| s.trim().to_string()).filter(|s| !s.is_empty()).collect()
    } else {
        cfg.hosts.keys().cloned().collect()
    };

    let mut results = Vec::new();
    for h in &targets {
        let platform = cfg.hosts.get(h).map(|d| d.platform.clone()).unwrap_or_else(|| "unix".into());
        // 可达性先探：连不上就标 reachable=false（用平台对应根目录探活）
        let io = RemoteIO::new(Some(h.as_str()), Some(platform.as_str()));
        let probe_root = if platform == "win" { "C:\\Windows" } else { "/etc" };
        let reachable = io.exists(probe_root);
        if !reachable {
            results.push(json!({"host": h, "reachable": false, "error": "SSH 不可达或认证失败"}));
            continue;
        }
        results.push(probe_host(h, &platform, &cfg));
    }

    json!({"ok": true, "probed": results.len(), "hosts": results})
}