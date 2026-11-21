//! cpa-sync Rust 版（对照 web/app.py：单进程面板 + API + 静态）
mod api;
mod cli;
mod adapters;
pub mod core {
    pub mod ir;
    pub mod config;
    pub mod cpa_client;
    pub mod remote;
    pub mod probe;
}

use axum::{extract::State, response::IntoResponse, routing::{get, post}, Json, Router};
use serde_json::Value;
use std::path::PathBuf;
use tower_http::services::ServeDir;

#[derive(Clone)]
struct S(PathBuf);

fn root() -> PathBuf { PathBuf::from(env!("CARGO_MANIFEST_DIR")) }

// ---- GET handlers ----
async fn get_index(State(_s): State<S>) -> axum::response::Response {
    let p = root().join("web").join("static").join("index.html");
    let body = std::fs::read(&p).unwrap_or_default();
    axum::response::Response::builder()
        .header("content-type", "text/html; charset=utf-8")
        .body(axum::body::Body::from(body)).unwrap()
}
async fn get_config(State(s): State<S>) -> axum::response::Response {
    let path = s.0.join("config").join("cpa-config.yaml");
    match std::fs::read_to_string(&path) {
        Ok(_t) => axum::Json(serde_json::json!({"ok": true, "config": api::read_yaml_json(&path)})).into_response(),
        Err(e) => axum::Json(serde_json::json!({"ok": false, "error": e.to_string()})).into_response(),
    }
}
async fn post_config(State(s): State<S>, Json(payload): Json<Value>) -> axum::response::Response {
    let cfg = payload.get("config").cloned().unwrap_or_default();
    let path = s.0.join("config").join("cpa-config.yaml");
    match api::write_yaml_json(&path, &cfg) {
        Ok(_) => axum::Json(serde_json::json!({"ok": true})).into_response(),
        Err(e) => axum::Json(serde_json::json!({"ok": false, "error": e.to_string()})).into_response(),
    }
}
// ---- POST 节点管理 handlers（对照 Handler.do_POST）----
macro_rules! node_handler {
    ($name:ident, $f:expr) => {
        async fn $name(Json(p): Json<Value>) -> axum::Json<Value> { axum::Json($f(&p).await) }
    };
}
node_handler!(post_nodes_config, api::node_config);
node_handler!(post_nodes_diff, api::node_diff);
node_handler!(post_nodes_sync, api::node_sync);
node_handler!(post_nodes_call, api::node_call);
node_handler!(post_nodes_refresh, api::node_refresh);
node_handler!(post_nodes_credentials, api::node_credentials);
node_handler!(post_nodes_plugins, api::node_plugins);
node_handler!(post_nodes_logs, api::node_logs);
node_handler!(post_nodes_oauth, api::node_oauth);
node_handler!(post_nodes_cred_delete, api::node_cred_delete);
node_handler!(post_nodes_plugin_delete, api::node_plugin_delete);
node_handler!(post_nodes_log_clear, api::node_log_clear);
node_handler!(post_nodes_log_toggle, api::node_log_toggle);

// POST /api/scripts：执行项目辅助脚本（白名单，探测 python3）
async fn post_scripts(Json(p): Json<Value>) -> axum::Json<Value> {
    let name = p.get("name").and_then(|v| v.as_str()).unwrap_or("");
    let params = p.get("params").cloned().unwrap_or(Value::Null);
    axum::Json(api::run_script(name, &params).await)
}

async fn post_panel_config(Json(p): Json<Value>) -> axum::Json<Value> { axum::Json(api::set_panel_config(&p)) }

async fn post_probe(Json(p): Json<Value>) -> axum::Json<Value> {
    let hosts = p.get("hosts").and_then(|v| v.as_str()).unwrap_or("");
    axum::Json(api::probe_hosts(hosts))
}

async fn get_probe_cache() -> axum::Json<Value> { axum::Json(api::get_probe_cache()) }
async fn post_probe_cache(Json(p): Json<Value>) -> axum::Json<Value> { axum::Json(api::set_probe_cache(&p)) }

async fn post_run(Json(p): Json<Value>) -> axum::Json<Value> {
    let mode = p.get("mode").and_then(|v| v.as_str()).unwrap_or("central");
    let cmd = p.get("cmd").and_then(|v| v.as_str()).unwrap_or("");
    let central = ["models", "list", "check", "apply", "doctor", "probe"];
    let client = ["list", "check", "apply", "doctor"];
    if mode == "client" {
        if !client.contains(&cmd) {
            return axum::Json(serde_json::json!({"ok": false, "error": format!("未知客户端命令: {}", cmd)}));
        }
        let r = api::run_client(cmd).await;
        return axum::Json(serde_json::json!({"ok": true, "code": r.get("code").cloned().unwrap_or_default(), "output": r.get("output").cloned().unwrap_or_default()}));
    }
    if !central.contains(&cmd) {
        return axum::Json(serde_json::json!({"ok": false, "error": format!("未知中心命令: {}", cmd)}));
    }
    let r = api::run_central(cmd).await;
    axum::Json(serde_json::json!({"ok": true, "code": r.get("code").cloned().unwrap_or_default(), "output": r.get("output").cloned().unwrap_or_default()}))
}

/// 命令行入口（对照 cpa-sync / agent-sync CLI）：
/// cpa-sync check|models|list|apply [--config PATH]
/// 不带子命令时按 web 面板启动（向后兼容）
async fn maybe_cli(args: &[String]) -> bool {
    let first = args.iter().find(|a| !a.starts_with('-'));
    let cmd = match first { Some(v) => v.as_str(), None => return false };
    if !matches!(cmd, "check" | "models" | "list" | "apply" | "doctor" | "sync" | "probe") { return false; }
    let cfg = args.iter().position(|a| a == "--config")
        .and_then(|i| args.get(i + 1)).cloned().unwrap_or_default();
    // --host h1,h2：中心下发选目标（只处理这些 host 的实例）
    let hosts_csv = args.iter().position(|a| a == "--host")
        .and_then(|i| args.get(i + 1)).cloned().unwrap_or_default();
    let only_hosts: Vec<String> = if hosts_csv.is_empty() { vec![] }
        else { hosts_csv.split(',').map(|s| s.trim().to_string()).filter(|s| !s.is_empty()).collect() };
    let out = if cmd == "check" { crate::cli::check_hosts(&cfg, &only_hosts).await }
        else if cmd == "models" { crate::cli::models(&cfg).await }
        else if cmd == "apply" { crate::cli::apply_hosts(&cfg, false, &[], &only_hosts).await }
        else if cmd == "doctor" { crate::cli::doctor(&cfg).await }
        else if cmd == "probe" { crate::cli::probe(&cfg, &hosts_csv) }
        else if cmd == "sync" {
            // 交互式 TUI 向导在 Rust 版由 web 面板承担；CLI sync 输出诊断+预览，执行用 apply
            let doc = crate::cli::doctor(&cfg).await;
            let chk = crate::cli::check(&cfg).await;
            serde_json::json!({"ok": true,
                "note": "Rust 版的交互式同步入口是 web 面板（无子命令启动）。CLI sync 给出诊断与预览，确认后请执行 apply。",
                "doctor": doc, "preview": chk})
        }
        else { crate::cli::list(&cfg) };
    println!("{}", serde_json::to_string_pretty(&out).unwrap_or_default());
    true
}

#[tokio::main]
async fn main() {
    if maybe_cli(&std::env::args().skip(1).collect::<Vec<_>>()).await { return; }
    let state = S(root());
    let app = Router::new()
        .route("/api/config", get(get_config).post(post_config))
        .route("/api/adapters", get(|| async { axum::Json(serde_json::json!({"ok": true, "names": api::get_adapters_info().get("names").cloned().unwrap_or_default(), "hosts": api::get_adapters_info().get("hosts").cloned().unwrap_or_default(), "adapter_instances": api::get_adapters_info().get("adapter_instances").cloned().unwrap_or_default(), "targets": api::get_adapters_info().get("targets").cloned().unwrap_or_default(), "strategies": api::get_adapters_info().get("strategies").cloned().unwrap_or_default()})) }))
        .route("/api/models", get(|| async { axum::Json(api::get_models().await) }))
        .route("/api/nodes", get(|| async { axum::Json(api::get_nodes().await) }))
        .route("/api/probe", post(post_probe))
        .route("/api/probe/cache", get(get_probe_cache).post(post_probe_cache))
        .route("/api/panel/config", get(|| async { axum::Json(api::get_panel_config()) }).post(post_panel_config))
        .route("/api/version", get(|| async { let v = api::get_version(); axum::Json(serde_json::json!({"ok": true, "cpa_sync": v.get("cpa_sync").cloned().unwrap_or_default(), "agent_sync": v.get("agent_sync").cloned().unwrap_or_default(), "adapters": v.get("adapters").cloned().unwrap_or_default()})) }))
        .route("/api/nodes/config", post(post_nodes_config))
        .route("/api/nodes/diff", post(post_nodes_diff))
        .route("/api/nodes/sync", post(post_nodes_sync))
        .route("/api/nodes/call", post(post_nodes_call))
        .route("/api/nodes/refresh", post(post_nodes_refresh))
        .route("/api/nodes/credentials", post(post_nodes_credentials))
        .route("/api/nodes/plugins", post(post_nodes_plugins))
        .route("/api/nodes/logs", post(post_nodes_logs))
        .route("/api/nodes/oauth", post(post_nodes_oauth))
        .route("/api/nodes/cred-delete", post(post_nodes_cred_delete))
        .route("/api/nodes/plugin-delete", post(post_nodes_plugin_delete))
        .route("/api/nodes/log-clear", post(post_nodes_log_clear))
        .route("/api/nodes/log-toggle", post(post_nodes_log_toggle))
        .route("/api/run", post(post_run))
        .route("/api/scripts", post(post_scripts))
        .fallback_service(ServeDir::new(root().join("web").join("static")))
        .with_state(state);
    let addr = format!("127.0.0.1:{}", api::gateway_cfg().panel_port);
    println!("cpa-sync-rs → http://{addr}");
    axum::serve(tokio::net::TcpListener::bind(&addr).await.expect("bind 失败"), app)
        .await.expect("serve");
}
