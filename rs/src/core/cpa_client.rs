//! CPA 客户端（对应 core/cpa_client.py）：拉 /v1/models
use super::ir::ModelIR;
use serde_json::Value;
use std::collections::HashMap;

#[derive(Debug, thiserror::Error)]
pub enum CPAError {
    #[error("{0}")]
    Msg(String),
}

/// OpenAI 兼容的 CPA 网关客户端
pub struct CPAClient {
    base_url: String,
    api_key: String,
    timeout: u64,
    client: reqwest::Client,
}

impl CPAClient {
    pub fn new(base_url: &str, api_key: &str, timeout: u64) -> Self {
        let client = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(timeout))
            .build()
            .unwrap_or_default();
        CPAClient {
            base_url: base_url.trim_end_matches('/').to_string(),
            api_key: api_key.into(),
            timeout,
            client,
        }
    }

    fn endpoint(&self, path: &str) -> String {
        if self.base_url.ends_with("/v1") {
            format!("{}{}", self.base_url, path)
        } else {
            format!("{}/v1{}", self.base_url, path)
        }
    }

    async fn get(&self, path: &str) -> Result<serde_json::Value, CPAError> {
        let url = self.endpoint(path);
        let resp = self.client
            .get(&url)
            .header("Authorization", format!("Bearer {}", self.api_key))
            .header("Accept", "application/json")
            .send()
            .await
            .map_err(|e| CPAError::Msg(format!("GET {} 失败: {}", url, e)))?;
        let code = resp.status().as_u16();
        let ok = resp.status().is_success();
        let body = resp.text().await.map_err(|e| CPAError::Msg(e.to_string()))?;
        if !ok {
            return Err(CPAError::Msg(format!("GET {} 失败: HTTP {} {}", url, code, &body[..body.len().min(300)])));
        }
        serde_json::from_str(&body).map_err(|e| CPAError::Msg(format!("GET {} 返回非 JSON: {} ({})", url, e, &body[..body.len().min(200)])))
    }

    /// 拉取 CPA 实际暴露的模型列表（/v1/models）
    pub async fn list_models(&self) -> Result<Vec<ModelIR>, CPAError> {
        let payload = self.get("/models").await?;
        let data = payload.get("data").and_then(|v| v.as_array()).cloned().unwrap_or_default();
        let mut models = Vec::new();
        for item in data {
            let mid = item.get("id").and_then(|v| v.as_str()).unwrap_or("").to_string();
            if mid.is_empty() { continue; }
            let extra: HashMap<String, serde_json::Value> = item.as_object().map(|o| {
                o.iter().filter(|(k, _)| k.as_str() != "id" && k.as_str() != "object")
                 .map(|(k, v)| (k.clone(), v.clone()))
                 .collect()
            }).unwrap_or_default();
            models.push(ModelIR { id: mid, extra, ..Default::default() });
        }
        Ok(models)
    }

    /// 健康检查
    pub async fn ping(&self) -> bool {
        self.list_models().await.is_ok()
    }
}
