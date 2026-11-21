//! 跨主机文件读写（对照 core/remote.py）
//! host=None → 本机文件；host=Some → 调系统 ssh/scp 命令（BatchMode）
//! 写文件统一走临时文件 + 原子替换（先备份），失败不清空原文件。
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::time::{SystemTime, UNIX_EPOCH};

fn secs() -> u64 { SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0) }

fn rand8() -> String {
    let n = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0);
    format!("{:08x}", (n % 0xffffffff) as u32)
}

/// 对应 RemoteIO
pub struct RemoteIO {
    pub host: Option<String>,
    pub platform_hint: Option<String>,
}

impl RemoteIO {
    pub fn new(host: Option<&str>, platform: Option<&str>) -> Self {
        RemoteIO { host: host.map(String::from), platform_hint: platform.map(String::from) }
    }
    pub fn is_local(&self) -> bool { self.host.is_none() }

    /// 对应 platform 属性：本机按当前 OS；显式优先；否则 ssh uname 自动检测
    pub fn platform(&self) -> String {
        if self.is_local() { return if cfg!(windows) { "win".into() } else { "unix".into() }; }
        if let Some(p) = &self.platform_hint { return p.clone(); }
        let host = self.host.clone().unwrap_or_default();
        let out = self.run(&["ssh", "-o", "BatchMode=yes", &host, "uname -s"]);
        let out = out.map(|o| o.trim().to_lowercase()).unwrap_or_default();
        if out.contains("darwin") || out.contains("linux") { "unix".into() } else { "win".into() }
    }

    /// 对应 _run()
    fn run(&self, args: &[&str]) -> Option<String> {
        let mut c = Command::new(args[0]);
        c.args(&args[1..]).stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped())
            .env("NO_PROXY", "*").env("no_proxy", "*");
        let out = c.output().ok()?;
        if !out.status.success() { return None; }
        Some(String::from_utf8_lossy(&out.stdout).to_string())
    }

    /// 对应 tmp_dir 属性（临时函数，不缓存）
    fn tmp_dir(&self) -> String {
        let host = self.host.clone().unwrap_or_default();
        let p = self.platform();
        let dir = if p == "unix" {
            self.run(&["ssh", "-o", "BatchMode=yes", &host, "echo ${TMPDIR:-/tmp}"]).unwrap_or_default()
        } else {
            self.run(&["ssh", "-o", "BatchMode=yes", &host, r#"powershell -NoProfile -Command "$env:TEMP""#]).unwrap_or_default()
        };
        let dir = dir.trim().to_string();
        if dir.is_empty() {
            if p == "unix" { "/tmp".into() } else { std::env::temp_dir().to_string_lossy().into() }
        } else { dir }
    }

    /// 对应 read()
    pub fn read(&self, path: &str) -> Option<String> {
        if self.is_local() { return std::fs::read_to_string(path).ok(); }
        let host = self.host.clone().unwrap_or_default();
        let cmd = if self.platform() == "unix" {
            format!("cat \"{}\"", path)
        } else {
            format!("type \"{}\"", path)
        };
        self.run(&["ssh", "-o", "BatchMode=yes", &host, &cmd])
    }

    /// 对应 exists()
    pub fn exists(&self, path: &str) -> bool {
        if self.is_local() { return PathBuf::from(path).exists(); }
        let host = self.host.clone().unwrap_or_default();
        let cmd = if self.platform() == "unix" {
            // unix: [ -f "$HOME/x" ]，ssh 传过去由远端 shell 展开 $HOME
            format!("[ -f \"{}\" ] && echo YES || echo NO", path)
        } else {
            // win: %USERPROFILE% 在引号内 cmd 不展开，改去引号让远端 cmd 展开
            let p = path.replace('"', "");
            format!("if exist {} (echo YES) else (echo NO)", p)
        };
        self.run(&["ssh", "-o", "BatchMode=yes", &host, &cmd]).map(|o| o.contains("YES")).unwrap_or(false)
    }

    /// 对应 backup()
    pub fn backup(&self, path: &str) -> Option<String> {
        if !self.exists(path) { return None; }
        let s = secs().to_string();
        if self.is_local() {
            let dest = format!("{}.bak.{}", path, s);
            if std::fs::copy(path, &dest).is_ok() { return Some(dest); }
            return None;
        }
        let host = self.host.clone().unwrap_or_default();
        let cmd = if self.platform() == "unix" {
            format!("cp \"{}\" \"{}.bak.{}\" && echo \"{}.bak.{}\"", path, path, s, path, s)
        } else {
            format!("powershell -NoProfile -Command \"$p='{path}'; $b=$p+'.bak.{s}'; Copy-Item $p $b -Force; Write-Output $b\"")
        };
        self.run(&["ssh", "-o", "BatchMode=yes", &host, &cmd]).map(|o| o.trim().to_string()).filter(|o| !o.is_empty())
    }

    /// 对应 write()：临时文件 + 原子替换（先备份）
    pub fn write(&self, path: &str, content: &str) -> std::io::Result<()> {
        if self.is_local() { return self.write_local(path, content); }
        let host = self.host.clone().unwrap_or_default();
        let rt = self.tmp_dir();
        let remote_tmp = format!("{}/cpa_sync_{}.tmp", rt, rand8());
        let local_tmp = std::env::temp_dir().join(format!("cpa_sync_{}.tmp", rand8()));
        std::fs::write(&local_tmp, content)?;
        let scp_dst = format!("{}:{}", host, remote_tmp);
        let mut c = Command::new("scp");
        c.args(["-o", "BatchMode=yes"]).arg(&local_tmp).arg(&scp_dst)
            .stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped())
            .env("NO_PROXY", "*").env("no_proxy", "*");
        let out = c.output().map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, e.to_string()))?;
        std::fs::remove_file(&local_tmp).ok();
        if !out.status.success() {
            let err = String::from_utf8_lossy(&out.stderr);
            return Err(std::io::Error::new(std::io::ErrorKind::Other, format!("scp 上传失败: {}", err.trim())));
        }
        let move_cmd = if self.platform() == "unix" {
            format!("cp \"{}\" \"{}.bak.{}\" 2>/dev/null; mv -f \"{}\" \"{}\" && echo \"{}\"", path, path, secs(), remote_tmp, path, path)
        } else {
            format!("powershell -NoProfile -Command \"$t='{rt}'; $d='{p}'; if (Test-Path $d) {{ Copy-Item $d ($d+'.bak.'+[DateTime]::Now.ToString('yyyyMMdd-HHmmss')) -Force }}; Move-Item $t $d -Force; Write-Output $d\"", rt = remote_tmp, p = path)
        };
        let _ = self.run(&["ssh", "-o", "BatchMode=yes", &host, &move_cmd]);
        Ok(())
    }

    /// 对应 _write_local()
    fn write_local(&self, path: &str, content: &str) -> std::io::Result<()> {
        let p = PathBuf::from(path);
        if p.exists() {
            let _ = std::fs::copy(&p, format!("{}.bak.{}", path, secs()));
        }
        if let Some(parent) = p.parent() { std::fs::create_dir_all(parent).ok(); }
        let tmp = format!("{}.cpa_tmp", path);
        std::fs::write(&tmp, content)?;
        std::fs::rename(&tmp, p)
    }
}
