//! 探测（probe）：选中 host → 自动嗅探该机装了哪些 agent、配置在哪。
//!
//! 两种方法（用户拍板）：
//!   1. **自动探测**：按 cc-switch 源码的真实「家目录+点目录」路径表，逐 host 判断目录/文件存不存在。
//!      路径表与 cc-switch src-tauri 完全对齐（见 agent_probe_paths），不瞎猜。
//!   2. **手动兜底**：自动探测覆盖不到的（cc-switch db 系、特殊安装），用 config 显式声明的
//!      path 或 probe.manual 自定义探针命令去测。
//!
//! 路径解析同 cc-switch：get_home_dir() = dirs::home_dir()，再 join 点目录。
//! win 的 hermes/claude-desktop 走 %LOCALAPPDATA%，其余家目录点目录。

use crate::adapters;
use crate::core::config::SyncConfig;
use crate::core::remote::RemoteIO;
use serde_json::{json, Value};

/// 一个 agent 的探测目标：目录或文件（相对 HOME，或用 %LOCALAPPDATA% 等变量）。
struct ProbeTarget {
    /// 相对 HOME 的路径（unix）；win 用反斜杠由调用方转换
    rel: &'static str,
    /// 是否是「目录存在即算装」（true）还是「文件存在」（false）
    is_dir: bool,
    /// win 专用：从 %LOCALAPPDATA% 起算（None = 从 HOME 起算）
    from_localappdata: bool,
}

/// 对齐 cc-switch 的真实路径表（src-tauri/src/{config,codex_config,gemini_config,
/// opencode_config,hermes_config,openclaw_config,grok_config,mcode_config,pi_config,
/// claude_desktop_config}.rs）。
///
/// 判定策略：能探目录就探目录（更稳，文件可能被改名/迁移）；cc-switch db 系单独标注。
fn agent_probe_paths(agent: &str) -> Vec<ProbeTarget> {
    let t = |rel, is_dir, from_localappdata| ProbeTarget { rel, is_dir, from_localappdata };
    match agent {
        // ~/.claude（config.rs:91 get_claude_config_dir）
        "claude" => vec![t(".claude", true, false), t(".claude.json", false, false)],
        // ~/.codex（codex_config.rs:270）
        "codex" => vec![t(".codex", true, false)],
        // ~/.gemini（gemini_config.rs:9）；agy 也在这（~/.gemini/antigravity-cli）
        "gemini" => vec![t(".gemini", true, false)],
        // ~/.config/opencode（opencode_config.rs:49，无平台分支）
        "opencode" => vec![t(".config/opencode", true, false)],
        // ~/.openclaw（openclaw_config.rs:35，无平台分支）
        "openclaw" => vec![t(".openclaw", true, false)],
        // hermes：unix ~/.hermes；win %LOCALAPPDATA%\hermes（hermes_config.rs:53 平台分支）
        "hermes" => vec![
            t(".hermes", true, false),
            t("hermes", true, true), // win: %LOCALAPPDATA%\hermes
        ],
        // pi：~/.pi/agent（pi_config/mod.rs:47）
        "pi" => vec![t(".pi", true, false)],
        // grokbuild：~/.grok（grok_config.rs:25）
        "grokbuild" => vec![t(".grok", true, false)],
        // mcode：~/.minimax（mcode_config.rs:14）
        "mcode" => vec![t(".minimax", true, false)],
        // claude-desktop：win %LOCALAPPDATA%\Claude；mac ~/Library/Application Support/Claude；
        //   linux ~/.config/Claude（claude_desktop_config.rs:1469 平台分支）
        "claude-desktop" => vec![
            t("Claude", true, true),                                  // win
            t("Library/Application Support/Claude", true, false),     // mac
            t(".config/Claude", true, false),                         // linux
        ],
        // crush / dsh / picoclaw：非 cc-switch 管理的 agent，用各自约定路径
        "crush" => vec![t(".config/crush", true, false)],
        "dsh" => vec![t(".dsh", true, false)],
        "picoclaw" => vec![t(".picoclaw", true, false)],
        // omp：~/.omp（agent 子目录放 models.yml/config.yml）
        "omp" => vec![t(".omp", true, false)],
        _ => vec![],
    }
}

/// 把相对路径拼成目标机可测的路径（unix: $HOME/rel；win: %USERPROFILE%\rel 或 %LOCALAPPDATA%\rel）。
fn build_probe_path(rel: &str, is_win: bool, from_localappdata: bool) -> String {
    let (base, sep) = if from_localappdata {
        ("%LOCALAPPDATA%", if is_win { "\\" } else { "/" })
    } else if is_win {
        ("%USERPROFILE%", "\\")
    } else {
        ("$HOME", "/")
    };
    let rel_conv = if is_win { rel.replace('/', "\\") } else { rel.to_string() };
    // localappdata 在 unix 上无意义，跳过（只 win 用）
    if from_localappdata && !is_win {
        return String::new();
    }
    format!("{}{}{}", base, sep, rel_conv)
}

/// 用 RemoteIO 测一条路径存不存在（目录或文件）。
fn test_path(io: &RemoteIO, path: &str, is_dir: bool) -> bool {
    if path.is_empty() {
        return false;
    }
    // RemoteIO.exists 用 [ -f ]（unix）/ if exist（win，目录文件都真）。
    // unix 要区分目录：用私有 run 不便，这里对目录额外用 [ -d ] 走 exists 不行，
    // 改：目录用 exists（[ -f ] 对目录返回 false）→ 需要目录专用判断。
    // 简化：probe 一律用「存在性」，目录用 dir_exists 逻辑内联（ssh test -d / win if exist）。
    if is_dir {
        io.path_exists_any(path)
    } else {
        io.exists(path)
    }
}

/// 探测单台 host。
fn probe_host(host: &str, platform: &str, cfg: &SyncConfig) -> Value {
    let io = RemoteIO::new(Some(host), Some(platform));
    let is_win = io.platform() == "win";
    let mut found: Vec<Value> = Vec::new();

    // 合并「cc-switch 系 + 其它 agent」的全集：adapters 列表 ∪ cc-switch AppType 列表
    let mut agents: Vec<String> = adapters::available_names();
    for extra in ["gemini", "grokbuild", "mcode", "claude-desktop", "agy"] {
        if !agents.contains(&extra.to_string()) {
            agents.push(extra.to_string());
        }
    }

    for agent in &agents {
        // 1) 自动探测：按 cc-switch 路径表
        let targets = agent_probe_paths(agent);
        let mut hit: Option<String> = None;
        let mut checked: Vec<String> = Vec::new();

        for tg in &targets {
            let p = build_probe_path(tg.rel, is_win, tg.from_localappdata);
            if p.is_empty() { continue; }
            if checked.contains(&p) { continue; }
            checked.push(p.clone());
            if test_path(&io, &p, tg.is_dir) && hit.is_none() {
                hit = Some(p.clone());
            }
        }

        // 2) 手动兜底：config 里该 agent+host 显式声明的 path（自动没命中才用，但始终记录）
        let explicit: Vec<String> = cfg
            .adapter_instances
            .get(agent.as_str())
            .cloned()
            .unwrap_or_default()
            .into_iter()
            .filter(|i| i.host == host && !i.path.is_empty())
            .map(|i| i.path)
            .collect();
        for p in &explicit {
            if checked.contains(p) { continue; }
            checked.push(p.clone());
            if hit.is_none() && io.exists(p) {
                hit = Some(p.clone());
            }
        }

        // cc-switch db 系标注（claude/codex 的实际 provider 在 ~/.cc-switch/cc-switch.db）
        let is_ccswitch_db = matches!(agent.as_str(), "claude" | "codex");
        let note = if is_ccswitch_db {
            Some("provider 存 ~/.cc-switch/cc-switch.db（sqlite），此处仅探配置目录")
        } else {
            None
        };

        match &hit {
            Some(p) => found.push(json!({
                "agent": agent, "installed": true, "path": p, "note": note,
            })),
            None => found.push(json!({
                "agent": agent, "installed": false, "checked": checked, "note": note,
            })),
        }
    }

    let installed: Vec<&str> = found.iter()
        .filter(|f| f.get("installed").and_then(|v| v.as_bool()) == Some(true))
        .filter_map(|f| f.get("agent").and_then(|v| v.as_str()))
        .collect();

    json!({
        "host": host,
        "platform": io.platform(),
        "reachable": true,
        "installed_agents": installed,
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

    let targets: Vec<String> = if !hosts_csv.is_empty() {
        hosts_csv.split(',').map(|s| s.trim().to_string()).filter(|s| !s.is_empty()).collect()
    } else {
        cfg.hosts.keys().cloned().collect()
    };

    let mut results = Vec::new();
    for h in &targets {
        let platform = cfg.hosts.get(h).map(|d| d.platform.clone()).unwrap_or_else(|| "unix".into());
        let io = RemoteIO::new(Some(h.as_str()), Some(platform.as_str()));
        // 探活：unix [ -d /etc ]；win if exist C:\Windows
        let alive = if platform == "win" {
            io.exists("C:\\Windows")
        } else {
            io.path_exists_any("/etc")
        };
        if !alive {
            results.push(json!({"host": h, "reachable": false, "error": "SSH 不可达或认证失败"}));
            continue;
        }
        results.push(probe_host(h, &platform, &cfg));
    }

    json!({"ok": true, "probed": results.len(), "hosts": results})
}