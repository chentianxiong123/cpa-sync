//! cc-switch 数据库公共读写（对照 adapters/_ccswitch.py）
//! 用 rusqlite bundled 操作 ~/.cc-switch/cc-switch.db
use rusqlite::{params, Connection};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::path::PathBuf;

pub fn db_path() -> PathBuf {
    std::env::var("CC_SWITCH_DB").map(PathBuf::from).unwrap_or_else(|_| {
        let home = std::env::var("USERPROFILE").or_else(|_| std::env::var("HOME")).unwrap_or_default();
        PathBuf::from(home).join(".cc-switch").join("cc-switch.db")
    })
}

fn connect() -> Result<Connection, rusqlite::Error> {
    Connection::open(db_path())
}

fn row_to_value(row: &rusqlite::Row) -> rusqlite::Result<Value> {
    // providers 表字段
    let mut m = serde_json::Map::new();
    for (i, name) in ["id","app_type","name","settings_config","notes","meta","is_current","sort_index","created_at"].iter().enumerate() {
        let v: rusqlite::types::Value = row.get(i)?;
        m.insert(name.to_string(), match v {
            rusqlite::types::Value::Null => Value::Null,
            rusqlite::types::Value::Integer(n) => Value::from(n),
            rusqlite::types::Value::Real(f) => Value::from(f),
            rusqlite::types::Value::Text(t) => Value::String(t),
            rusqlite::types::Value::Blob(_) => Value::Null,
        });
    }
    Ok(Value::Object(m))
}

/// 对应 list_providers(app_type)
pub fn list_providers(app_type: &str) -> Vec<Value> {
    let Ok(con) = connect() else { return vec![] };
    let Ok(mut stmt) = con.prepare("SELECT id, app_type, name, settings_config, notes, meta, is_current, sort_index, created_at FROM providers WHERE app_type=? ORDER BY sort_index, name") else { return vec![] };
    let Ok(rows) = stmt.query_map(params![app_type], row_to_value) else { return vec![] };
    rows.filter_map(|r| r.ok()).collect()
}

/// 对应 get_cpa_providers(app_type)：notes='cpa' 或 name='cpa' 或 name LIKE 'cpa-%'
pub fn get_cpa_providers(app_type: &str) -> Vec<Value> {
    let Ok(con) = connect() else { return vec![] };
    let Ok(mut stmt) = con.prepare("SELECT id, app_type, name, settings_config, notes, meta, is_current, sort_index, created_at FROM providers WHERE app_type=? AND (notes='cpa' OR name='cpa' OR name LIKE 'cpa-%') ORDER BY sort_index, name") else { return vec![] };
    let Ok(rows) = stmt.query_map(params![app_type], row_to_value) else { return vec![] };
    rows.filter_map(|r| r.ok()).collect()
}

/// 对应 backup_db()：写库前快照
pub fn backup_db() -> Option<PathBuf> {
    let p = db_path();
    if !p.exists() { return None; }
    let dir = p.parent().unwrap_or(std::path::Path::new(".")).to_path_buf();
    let stamp = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0);
    let dest = dir.join(format!("cc-switch.db.bak.{}", stamp));
    let _ = std::fs::copy(&p, &dest);
    Some(dest)
}

/// 对应 sync_providers()：整体对齐（删多余 + 增改 + 设当前），事务回滚
pub fn sync_providers(app_type: &str, providers: &[Value], current_name: Option<&str>) -> Result<Vec<Value>, String> {
    if providers.is_empty() { return Err("providers 不能为空".into()); }
    let con = connect().map_err(|e| e.to_string())?;
    let tx = con.unchecked_transaction().map_err(|e| e.to_string())?;
    // 现有 name -> id
    let mut existing: HashMap<String, String> = HashMap::new();
    {
        let mut stmt = tx.prepare("SELECT id, name FROM providers WHERE app_type=?").map_err(|e| e.to_string())?;
        let rows = stmt.query_map(params![app_type], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?))).map_err(|e| e.to_string())?;
        for r in rows { if let Ok((_id, name)) = r { existing.insert(name, "-".into()); } }
    }
    let wanted: std::collections::HashSet<String> = providers.iter().filter_map(|p| p.get("name").and_then(|v| v.as_str()).map(String::from)).collect();
    // 1) 删除不在 wanted 里的
    for name in existing.keys() {
        if !wanted.contains(name) {
            tx.execute("DELETE FROM providers WHERE app_type=? AND name=?", params![app_type, name]).map_err(|e| e.to_string())?;
        }
    }
    // 2) 插入或更新
    for p in providers {
        let name = p.get("name").and_then(|v| v.as_str()).unwrap_or("").to_string();
        let cfg_json = p.get("settings_config").cloned().unwrap_or(Value::Null).to_string();
        let notes = p.get("notes").and_then(|v| v.as_str()).map(String::from);
        let meta_json = p.get("meta").filter(|v| !v.is_null()).map(|v| v.to_string());
        if existing.contains_key(&name) {
            tx.execute("UPDATE providers SET settings_config=?, notes=?, meta=? WHERE app_type=? AND name=?",
                params![cfg_json, notes, meta_json, app_type, name]).map_err(|e| e.to_string())?;
        } else {
            let pid = format!("{}-{}-{}", app_type, name, std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_millis()).unwrap_or(0));
            tx.execute("INSERT INTO providers (id, app_type, name, settings_config, notes, meta, created_at) VALUES (?,?,?,?,?,?,?)",
                params![pid, app_type, name, cfg_json, notes, meta_json, std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0)]).map_err(|e| e.to_string())?;
        }
    }
    // 3) 设当前
    let cur = current_name.unwrap_or_else(|| providers[0].get("name").and_then(|v| v.as_str()).unwrap_or(""));
    tx.execute("UPDATE providers SET is_current=0 WHERE app_type=?", params![app_type]).map_err(|e| e.to_string())?;
    tx.execute("UPDATE providers SET is_current=1 WHERE app_type=? AND name=?", params![app_type, cur]).map_err(|e| e.to_string())?;
    tx.commit().map_err(|e| e.to_string())?;
    Ok(list_providers(app_type))
}

/// 工具：解析 settings_config 里的 JSON 文本
pub fn parse_settings(cfg: &Value) -> Value {
    cfg.get("settings_config").and_then(|v| v.as_str())
        .and_then(|s| serde_json::from_str(s).ok())
        .unwrap_or(Value::Object(Default::default()))
}
