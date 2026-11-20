//! Web API（对应 web/app.py）：CPA 节点管理 + 配置读写
use serde_json::Value;
use serde_json::json;
use std::collections::HashMap;
use std::path::PathBuf;

pub fn root() -> PathBuf { PathBuf::from(env!("CARGO_MANIFEST_DIR")) }
pub fn config_path() -> PathBuf { root().join("config").join("cpa-config.yaml") }

#[derive(Debug, Clone)]
pub struct GatewayCfg {
    pub listen: u16,
    pub panel_port: u16,
    pub key: String,
    pub upstreams: Vec<Value>,
}

/// 对应 _gateway_cfg()：读 config/gateway.yaml
/// yaml Value → json Value（配置层转换）
pub fn yaml_to_json(v: &serde_yaml::Value) -> Value {
    serde_json::to_value(v).unwrap_or(Value::Null)
}

/// json Value → yaml Value（写配置文件用）
pub fn json_to_yaml(v: &Value) -> serde_yaml::Value {
    serde_yaml::to_value(v).unwrap_or(serde_yaml::Value::Null)
}

/// 读 yaml 文件为 json Value（配置层统一入口）
pub fn read_yaml_json(path: &PathBuf) -> Value {
    std::fs::read_to_string(path).ok().and_then(|t| {
        serde_yaml::from_str::<serde_yaml::Value>(&t).ok().map(|v| yaml_to_json(&v))
    }).unwrap_or(Value::Null)
}

/// 写 json Value 为 yaml 文件
pub fn write_yaml_json(path: &PathBuf, v: &Value) -> std::io::Result<()> {
    let y = json_to_yaml(v);
    let text = serde_yaml::to_string(&y).map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, e.to_string()))?;
    std::fs::write(path, text)
}


pub fn gateway_cfg() -> GatewayCfg {
    let p = root().join("config").join("gateway.yaml");
    let raw = read_yaml_json(&p);
    let g = raw.get("gateway").cloned().unwrap_or(Value::Object(Default::default()));
    let mgmt = g.get("management").cloned().unwrap_or(Value::Object(Default::default()));
    GatewayCfg {
        listen: g.get("listen").and_then(|v| v.as_u64()).unwrap_or(8380) as u16,
        panel_port: g.get("panel_port").and_then(|v| v.as_u64()).unwrap_or(17834) as u16,
        key: mgmt.get("key").and_then(|v| v.as_str()).unwrap_or("").into(),
        upstreams: g.get("upstreams").and_then(|v| v.as_array())
            .map(|s| s.iter().cloned().collect()).unwrap_or_default(),
    }
}

/// 对应 _panel_cfg()
pub fn panel_cfg() -> (String, String) {
    let p = root().join("config").join("gateway.yaml");
    let raw = read_yaml_json(&p);
    let pl = raw.get("gateway").and_then(|g| g.get("panel")).cloned().unwrap_or(Value::Object(Default::default()));
    (
        pl.get("auth_key").and_then(|v| v.as_str()).unwrap_or("").into(),
        pl.get("bind").and_then(|v| v.as_str()).unwrap_or("127.0.0.1").into(),
    )
}

pub fn get_panel_config() -> Value {
    let (_, bind) = panel_cfg();
    let (auth, _) = panel_cfg();
    json!({"ok": true, "auth_set": !auth.is_empty(), "bind": bind})
}

/// 对应 set_panel_config()：写 gateway.yaml panel 段
pub fn set_panel_config(payload: &Value) -> Value {
    let path = root().join("config").join("gateway.yaml");
    let raw = read_yaml_json(&path);
    let mut m = raw.as_object().cloned().unwrap_or_default();
    let mut gw = m.get("gateway").cloned().unwrap_or(Value::Object(Default::default()));
    let mut pl = gw.get("panel").cloned().unwrap_or(Value::Object(Default::default()));
    if let Some(a) = pl.as_object_mut() {
        if payload.get("auth_key").is_some() {
            a.insert("auth_key".into(), Value::String(payload.get("auth_key").and_then(|v| v.as_str()).unwrap_or("").into()));
        }
        if payload.get("bind").is_some() {
            a.insert("bind".into(), Value::String(payload.get("bind").and_then(|v| v.as_str()).unwrap_or("127.0.0.1").into()));
        }
    }
    if let Some(g) = gw.as_object_mut() { g.insert("panel".into(), pl); }
    m.insert("gateway".into(), gw);
    match write_yaml_json(&path, &Value::Object(m)) {
        Ok(_) => json!({"ok": true}),
        Err(e) => json!({"ok": false, "error": e.to_string()}),
    }
}


/// 对应 _mgmt()：调 CPA 管理 API /v8/management/<path>
pub async fn mgmt(node: &Value, path: &str, method: &str, data: Option<&Value>) -> (u16, Value) {
    let key = gateway_cfg().key;
    let host = node.get("host").and_then(|v| v.as_str()).unwrap_or("");
    let port = node.get("port").and_then(|v| v.as_u64()).unwrap_or(8317) as u16;
    let url = format!("http://{}:{}/v8/management/{}", host, port, path);
    let mut req = reqwest::Client::new()
        .request(reqwest::Method::from_bytes(method.as_bytes()).unwrap_or(reqwest::Method::GET), &url)
        .header("Authorization", format!("Bearer {}", key))
        .header("Accept", "application/json");
    if let Some(d) = data {
        req = req.header("Content-Type", "application/json").json(d);
    }
    let resp = match req.send().await {
        Ok(r) => r,
        Err(e) => return (0, Value::String(format!("{}", e))),
    };
    let code = resp.status().as_u16();
    let body = resp.text().await.unwrap_or_default();
    match serde_json::from_str::<serde_json::Value>(&body) {
        Ok(v) => (code, Value::from(v)),
        Err(_) => (code, Value::String(body)),
    }
}

/// 对应 _nodes_from_req()
fn nodes_from_req(payload: &Value) -> Vec<Value> {
    let cfg = gateway_cfg();
    let wanted: Vec<String> = payload.get("hosts").and_then(|v| v.as_array())
        .map(|s| s.iter().filter_map(|x| x.as_str().map(String::from)).collect())
        .unwrap_or_default();
    if wanted.is_empty() { return cfg.upstreams; }
    cfg.upstreams.into_iter()
        .filter(|u| wanted.iter().any(|w| u.get("host").and_then(|h| h.as_str()) == Some(w.as_str())))
        .collect()
}

/// 对应 get_nodes()：状态监测
pub async fn get_nodes() -> Value {
    let cfg = gateway_cfg();
    let mut nodes = Vec::new();
    for u in cfg.upstreams {
        let mut node = json!({"host": u.get("host").and_then(|v| v.as_str()).unwrap_or(""),
                             "port": u.get("port").and_then(|v| v.as_u64()).unwrap_or(8317)});
        let host = u.get("host").and_then(|v| v.as_str()).unwrap_or("");
        let port = u.get("port").and_then(|v| v.as_u64()).unwrap_or(8317);
        let base = format!("http://{}:{}/v1", host, port);
        let cli = crate::core::cpa_client::CPAClient::new(&base, &cfg.key, 6);
        match cli.list_models().await {
            Ok(ms) => { node["ok"] = Value::Bool(true); node["models"] = serde_json::to_value(ms.iter().map(|m| m.id.clone()).collect::<Vec<_>>()).unwrap_or_default(); node["model_count"] = Value::from(ms.len()); }
            Err(e) => { node["ok"] = Value::Bool(false); node["error"] = Value::String(e.to_string()); node["models"] = Value::Array(vec![]); }
        }
        let (st, v) = mgmt(&u, "server/latest-version", "GET", None).await;
        node["version"] = if st == 200 { Value::from(v) } else { Value::Null };
        let (st, usage) = mgmt(&u, "observability/usage/api-keys", "GET", None).await;
        node["usage"] = if st == 200 { Value::from(usage) } else { Value::Null };
        nodes.push(Value::from(node));
    }
    json!({"ok": true, "nodes": nodes})
}

/// 对应 node_config()
pub async fn node_config(payload: &Value) -> Value {
    let nodes = nodes_from_req(payload);
    if nodes.is_empty() { return json!({"ok": false, "error": "无匹配节点"}); }
    let mut out = HashMap::new();
    for u in nodes {
        let host = u.get("host").and_then(|v| v.as_str()).unwrap_or("").to_string();
        let (st, cfg) = mgmt(&u, "config", "GET", None).await;
        out.insert(host, json!({"ok": st == 200, "config": if st == 200 { Some(cfg.clone()) } else { None },
                               "error": if st == 200 { None } else { Some(cfg.to_string()) }}));
    }
    json!({"ok": true, "configs": out})
}

/// 对应 node_diff()：多节点 providers 对比
pub async fn node_diff(payload: &Value) -> Value {
    let r = node_config(payload).await;
    let configs = r.get("configs").cloned().unwrap_or_default();
    let mut summary: HashMap<String, Value> = HashMap::new();
    let cmap = configs.as_object().cloned().unwrap_or_default();
    for (host, info) in cmap.iter() {
        let cfg = info.get("config").cloned().unwrap_or(Value::Null);
        let mut provs: HashMap<String, Value> = HashMap::new();
        if let Some(aks) = cfg.get("api-keys").and_then(|v| v.as_object()) {
            for (brand, items) in aks {
                if let Some(list) = items.as_array() {
                    for p in list {
                        if let Some(name) = p.get("name").and_then(|v| v.as_str()) {
                            let models: Vec<Value> = p.get("models").and_then(|v| v.as_array())
                                .map(|s| s.iter().map(|m| Value::from(m.get("name").cloned().unwrap_or_default())).collect())
                                .unwrap_or_default();
                            provs.insert(name.into(), json!({"brand": brand, "base": p.get("base-url").cloned().unwrap_or_default(),
                                                             "disabled": p.get("disabled").and_then(|v| v.as_bool()).unwrap_or(false),
                                                             "models": models}));
                        }
                    }
                }
            }
        }
        summary.insert(host.clone(), json!({"ok": info.get("ok").and_then(|v| v.as_bool()).unwrap_or(false), "providers": provs}));
    }
    let hosts: Vec<String> = summary.keys().cloned().collect();
    let mut only: HashMap<String, Value> = HashMap::new();
    if hosts.len() >= 2 {
        let base_names: Vec<String> = summary.get(&hosts[0]).and_then(|v| v.get("providers"))
            .and_then(|v| v.as_object()).map(|m| m.keys().cloned().collect()).unwrap_or_default();
        for h in &hosts[1..] {
            let names: Vec<String> = summary.get(h).and_then(|v| v.get("providers"))
                .and_then(|v| v.as_object()).map(|m| m.keys().cloned().collect()).unwrap_or_default();
            let only_here: Vec<String> = names.iter().filter(|n| !base_names.contains(n)).cloned().collect();
            let missing: Vec<String> = base_names.iter().filter(|n| !names.contains(n)).cloned().collect();
            only.insert(h.clone(), json!({"only": only_here, "missing": missing}));
        }
    }
    json!({"ok": true, "summary": summary, "only": only})
}

/// 对应 node_sync()：权威源→目标全量同步
pub async fn node_sync(payload: &Value) -> Value {
    let cfg = gateway_cfg();
    let mut nodes: HashMap<String, Value> = HashMap::new();
    for u in cfg.upstreams {
        let h = u.get("host").and_then(|v| v.as_str()).unwrap_or("");
        nodes.insert(h.into(), u);
    }
    let src = payload.get("source").and_then(|v| v.as_str()).unwrap_or("");
    let dst = payload.get("target").and_then(|v| v.as_str()).unwrap_or("");
    if !nodes.contains_key(src) || !nodes.contains_key(dst) {
        return json!({"ok": false, "error": format!("源/目标不在 upstreams: {}->{}", src, dst)});
    }
    let (st, src_cfg) = mgmt(nodes.get(src).unwrap(), "config", "GET", None).await;
    if st != 200 { return json!({"ok": false, "error": format!("读源 {} 失败: {}", src, src_cfg.to_string())}); }
    let (st, resp) = mgmt(nodes.get(dst).unwrap(), "config", "PUT", Some(&src_cfg)).await;
    if !(200..=204).contains(&st) { return json!({"ok": false, "error": format!("写目标 {} 失败: {}", dst, resp.to_string())}); }
    let _ = mgmt(nodes.get(dst).unwrap(), "routing/model-definitions/refresh", "POST", None).await;
    json!({"ok": true, "source": src, "target": dst, "status": st})
}

/// 对应 node_call()：嗅探（api-call / ping）
pub async fn node_call(payload: &Value) -> Value {
    let cfg = gateway_cfg();
    let mut nodes: HashMap<String, Value> = HashMap::new();
    for u in cfg.upstreams {
        let h = u.get("host").and_then(|v| v.as_str()).unwrap_or("");
        nodes.insert(h.into(), u);
    }
    let host = payload.get("host").and_then(|v| v.as_str()).unwrap_or("");
    let node = match nodes.get(host) { Some(n) => n.clone(), None => return json!({"ok": false, "error": "host 不在 upstreams"}) };
    if payload.get("mode").and_then(|v| v.as_str()) == Some("ping") {
        let port = node.get("port").and_then(|v| v.as_u64()).unwrap_or(8317);
        let base = format!("http://{}:{}/v1", host, port);
        let cli = crate::core::cpa_client::CPAClient::new(&base, &cfg.key, 6);
        return match cli.list_models().await {
            Ok(ms) => json!({"ok": true, "mode": "ping", "host": host, "models": ms.iter().map(|m| m.id.clone()).collect::<Vec<_>>()}),
            Err(e) => json!({"ok": false, "mode": "ping", "host": host, "error": e.to_string()}),
        };
    }
    let body = payload.get("body").cloned().unwrap_or(Value::Null);
    let (st, resp) = mgmt(&node, "requests/api-call", "POST", Some(&body)).await;
    json!({"ok": st == 200 || st == 201, "host": host, "status": st, "resp": resp})
}

/// 对应 node_refresh()
pub async fn node_refresh(payload: &Value) -> Value {
    let cfg = gateway_cfg();
    let mut nodes: HashMap<String, Value> = HashMap::new();
    for u in cfg.upstreams {
        let h = u.get("host").and_then(|v| v.as_str()).unwrap_or("");
        nodes.insert(h.into(), u);
    }
    let hosts: Vec<String> = payload.get("hosts").and_then(|v| v.as_array())
        .map(|s| s.iter().filter_map(|x| x.as_str().map(String::from)).collect()).unwrap_or_else(|| nodes.keys().cloned().collect());
    let mut done = Vec::new();
    for host in hosts {
        if let Some(node) = nodes.get(&host) {
            let (mut st, mut resp) = mgmt(node, "routing/model-definitions/refresh", "POST", None).await;
            if st == 404 { (st, resp) = mgmt(node, "models/refresh", "POST", None).await; }
            done.push(json!({"host": host, "status": st, "resp": if st == 200 { Value::String("ok".into()) } else { resp.clone() }}));
        }
    }
    json!({"ok": true, "results": done})
}

/// 对应 node_credentials()
pub async fn node_credentials(payload: &Value) -> Value { list_mgmt(payload, "credentials", "GET").await }
/// 对应 node_plugins()
pub async fn node_plugins(payload: &Value) -> Value { list_mgmt(payload, "plugins", "GET").await }
/// 对应 node_logs()
pub async fn node_logs(payload: &Value) -> Value {
    list_mgmt(payload, "observability/logs", "GET").await
}
/// 对应 node_oauth()
pub async fn node_oauth(payload: &Value) -> Value { list_mgmt(payload, "oauth/status", "GET").await }

async fn list_mgmt(payload: &Value, path: &str, _method: &str) -> Value {
    let mut out: HashMap<String, Value> = HashMap::new();
    for u in nodes_from_req(payload) {
        let host = u.get("host").and_then(|v| v.as_str()).unwrap_or("").to_string();
        let (st, data) = mgmt(&u, path, "GET", None).await;
        out.insert(host, json!({"ok": st == 200, "status": st, "data": if st == 200 { Some(data.clone()) } else { None }}));
    }
    json!({"ok": true, "nodes": out})
}

/// 对应 node_cred_delete()
pub async fn node_cred_delete(payload: &Value) -> Value {
    let host = payload.get("host").and_then(|v| v.as_str()).unwrap_or("");
    let name = payload.get("name").and_then(|v| v.as_str()).unwrap_or("");
    let cfg = gateway_cfg();
    let mut nodes: HashMap<String, Value> = HashMap::new();
    for u in cfg.upstreams {
        let h = u.get("host").and_then(|v| v.as_str()).unwrap_or("");
        nodes.insert(h.into(), u);
    }
    if !nodes.contains_key(host) { return json!({"ok": false, "error": "host 不在 upstreams"}); }
    if name.is_empty() { return json!({"ok": false, "error": "需 name 参数（凭据文件名）"}); }
    let encoded = name.replace('%', "%25");
    let (st, resp) = mgmt(nodes.get(host).unwrap(), &format!("credentials?name={}", encoded), "DELETE", None).await;
    json!({"ok": st == 200 || st == 204, "status": st, "resp": resp})
}

/// 对应 node_plugin_delete()
pub async fn node_plugin_delete(payload: &Value) -> Value {
    let host = payload.get("host").and_then(|v| v.as_str()).unwrap_or("");
    let pid = payload.get("id").and_then(|v| v.as_str()).unwrap_or("");
    let cfg = gateway_cfg();
    let mut nodes: HashMap<String, Value> = HashMap::new();
    for u in cfg.upstreams {
        let h = u.get("host").and_then(|v| v.as_str()).unwrap_or("");
        nodes.insert(h.into(), u);
    }
    if !nodes.contains_key(host) { return json!({"ok": false, "error": "host 不在 upstreams"}); }
    if pid.is_empty() { return json!({"ok": false, "error": "需 id 参数（插件 id）"}); }
    let (st, resp) = mgmt(nodes.get(host).unwrap(), &format!("plugins/{}", pid), "DELETE", None).await;
    json!({"ok": st == 200 || st == 204, "status": st, "resp": resp})
}

/// 对应 node_log_clear()
pub async fn node_log_clear(payload: &Value) -> Value {
    let mut out: HashMap<String, Value> = HashMap::new();
    for u in nodes_from_req(payload) {
        let host = u.get("host").and_then(|v| v.as_str()).unwrap_or("").to_string();
        let (st, resp) = mgmt(&u, "observability/logs", "DELETE", None).await;
        out.insert(host, json!({"ok": st == 200 || st == 204, "status": st, "resp": if st != 200 { resp.to_string() } else { String::new() }}));
    }
    json!({"ok": true, "nodes": out})
}

/// 对应 node_log_toggle()：logging-to-file 开关
pub async fn node_log_toggle(payload: &Value) -> Value {
    let on = payload.get("on").and_then(|v| v.as_bool()).unwrap_or(true);
    let mut out: HashMap<String, Value> = HashMap::new();
    for u in nodes_from_req(payload) {
        let host = u.get("host").and_then(|v| v.as_str()).unwrap_or("").to_string();
        let (st, mut cfgv) = mgmt(&u, "config", "GET", None).await;
        if st != 200 { out.insert(host, json!({"ok": false, "status": st, "error": "读 config 失败"})); continue; }
        if let Some(obj) = cfgv.as_object_mut() {
            obj.entry("observability").or_insert_with(|| Value::Object(Default::default()));
            if let Some(obs) = obj.get_mut("observability").and_then(|v| v.as_object_mut()) {
                obs.entry("logs").or_insert_with(|| Value::Object(Default::default()));
                if let Some(logs) = obs.get_mut("logs").and_then(|v| v.as_object_mut()) {
                    logs.insert("logging-to-file".into(), Value::Bool(on));
                }
            }
        }
        let (st, resp) = mgmt(&u, "config", "PUT", Some(&cfgv)).await;
        out.insert(host, json!({"ok": st == 200 || st == 204, "status": st, "on": on, "resp": if st == 200 { String::new() } else { resp.to_string() }}));
    }
    json!({"ok": true, "nodes": out})
}

/// 对应 get_models()：CPA 模型 + 覆盖
pub async fn get_models() -> Value {
    let cfg = match crate::core::config::SyncConfig::load(None) {
        Ok(c) => c, Err(e) => return json!({"ok": false, "error": e, "models": [], "cpa": {}}),
    };
    let cli = crate::core::cpa_client::CPAClient::new(&cfg.cpa.base_url, &cfg.cpa.api_key, 8);
    let models = match cli.list_models().await {
        Ok(m) => m, Err(e) => return json!({"ok": false, "error": e.to_string(), "models": [], "cpa": {}}),
    };
    let cpa = json!({"context_window": cfg.cpa.context_window, "max_tokens": cfg.cpa.max_tokens,
                     "auto_compact_token_limit": cfg.cpa.auto_compact_token_limit,
                     "default_input_modes": cfg.cpa.default_input_modes, "image_models": cfg.cpa.image_models});
    let mut list = Vec::new();
    for m in models {
        let ov = cfg.cpa.model_overrides.get(&m.id).cloned().unwrap_or(serde_yaml::Value::Null);
        list.push(json!({"id": m.id, "override": ov}));
    }
    json!({"ok": true, "cpa": cpa, "models": list})
}

/// 对应 get_adapters_info()
pub fn get_adapters_info() -> Value {
    let names = crate::adapters::available_names();
    let cfg = read_yaml_json(&config_path());
    json!({"names": names, "hosts": cfg.get("hosts").cloned().unwrap_or_default(),
           "adapter_instances": cfg.get("adapter_instances").cloned().unwrap_or_default(),
           "targets": cfg.get("targets").cloned().unwrap_or_default(),
           "strategies": ["replace", "merge"]})
}

/// 对应 get_version()
pub fn get_version() -> Value {
    let mut ver = String::from("?");
    if let Ok(text) = std::fs::read_to_string(root().join("cpa-sync")) {
        for line in text.lines() {
            if let Some(rest) = line.strip_prefix("VERSION=") {
                let v: String = rest.chars().filter(|c| *c != '"' && *c != ' ').collect();
                if !v.is_empty() { ver = v; break; }
            }
        }
    }
    json!({"cpa_sync": ver, "agent_sync": "1.0.0", "adapters": crate::adapters::available_names().len()})
}

/// 命令执行（对应 run_central/run_client/run_script）
pub async fn run_central(name: &str) -> Value {
    match name {
        "models" | "list" | "check" | "apply" | "doctor" => {
            let code = crate::cli::run_central(name).await;
            json!({"code": code.0, "output": code.1})
        }
        _ => json!({"code": 2, "output": format!("未知中心命令: {}", name)}),
    }
}

pub async fn run_client(name: &str) -> Value {
    match name {
        "list" | "check" | "apply" | "doctor" => {
            let code = crate::cli::run_client(name).await;
            json!({"code": code.0, "output": code.1})
        }
        _ => json!({"code": 2, "output": format!("未知客户端命令: {}", name)}),
    }
}
