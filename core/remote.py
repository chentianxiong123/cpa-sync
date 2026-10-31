"""跨主机文件读写辅助（经 ssh / scp）。

本模块封装跨平台（Windows / macOS / Linux）的远程文件读写：
  - read_remote(path, host)          : ssh type / cat
  - write_remote(path, content, host): scp 上传临时文件 + ssh 原子替换（先备份）
  - backup_remote(path, host)        : ssh 备份带时间戳

平台自动检测：ssh 跑 `uname -s`，Darwin/Linux => unix，其余 => win。
unix 用 sh 命令（cat/mv/cp），win 用 PowerShell（type/Move-Item/Copy-Item）。
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
import uuid


class RemoteIO:
    """通过 ssh/scp 访问远程主机文件的封装（自动检测 win/unix）。

    写文件统一走 scp（原始字节传输，避免 ssh+PowerShell stdin 编码问题），
    再在远端原子替换目标（先备份）。失败不会清空原文件。
    """

    def __init__(self, host: str, platform: str | None = None):
        self.host = host
        self._platform = platform  # None=自动检测; "win"|"unix"
        self._tmp_dir: str | None = None

    @property
    def platform(self) -> str:
        if self._platform is None:
            try:
                out = self._run(
                    ["ssh", "-o", "BatchMode=yes", self.host, "uname -s"]
                ).strip().lower()
            except RuntimeError:
                self._platform = "win"
            else:
                self._platform = "unix" if ("darwin" in out or "linux" in out) else "win"
        return self._platform

    @property
    def tmp_dir(self) -> str:
        """远程临时目录（真实路径，首次查询后缓存）。"""
        if self._tmp_dir is None:
            if self.platform == "unix":
                out = self._run(
                    ["ssh", "-o", "BatchMode=yes", self.host, 'echo ${TMPDIR:-/tmp}']
                ).strip()
                self._tmp_dir = out or "/tmp"
            else:
                out = self._run(
                    ["ssh", "-o", "BatchMode=yes", self.host,
                     'powershell -NoProfile -Command "$env:TEMP"']
                ).strip()
                self._tmp_dir = out or r"C:\Users\a1\AppData\Local\Temp"
        return self._tmp_dir

    def _run(self, cmd: list[str], input_text: str | None = None) -> str:
        proc = subprocess.run(
            cmd,
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,  # 不让 ssh 吃掉父进程 stdin
            timeout=90,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"命令失败: {' '.join(cmd)}\n{proc.stderr.strip()}")
        return proc.stdout

    # ---- 读取 ----
    def read(self, path: str) -> str:
        if self.platform == "unix":
            return self._run(["ssh", "-o", "BatchMode=yes", self.host, f"cat \"{path}\""])
        return self._run(["ssh", "-o", "BatchMode=yes", self.host, f"type \"{path}\""])

    def exists(self, path: str) -> bool:
        if self.platform == "unix":
            out = self._run(
                ["ssh", "-o", "BatchMode=yes", self.host, f'[ -f "{path}" ] && echo YES || echo NO']
            )
            return "YES" in out
        out = self._run(
            ["ssh", "-o", "BatchMode=yes", self.host, f'if exist "{path}" (echo YES) else (echo NO)']
        )
        return "YES" in out

    # ---- 备份 ----
    def backup(self, path: str, backup_dir: str | None = None) -> str | None:
        if not self.exists(path):
            return None
        stamp = time.strftime("%Y%m%d-%H%M%S")
        if self.platform == "unix":
            backup_cmd = f'cp "{path}" "{path}.bak.{stamp}" && echo "{path}.bak.{stamp}"'
        else:
            backup_cmd = (
                f'powershell -NoProfile -Command "'
                f'$p=\'{path}\'; $b=$p+\'.bak.{stamp}\'; Copy-Item $p $b -Force; Write-Output $b"'
            )
        return self._run(["ssh", "-o", "BatchMode=yes", self.host, backup_cmd]).strip()

    # ---- 写入（scp 原子替换，最可靠）----
    def write(self, path: str, content: str) -> None:
        sep = "/" if self.platform == "unix" else "\\"
        remote_tmp = f"{self.tmp_dir}/cpa_sync_{uuid.uuid4().hex[:8]}.tmp"
        remote_tmp_scp = remote_tmp  # scp 目标统一正斜杠
        local_tmp = os.path.join(tempfile.gettempdir(), f"cpa_sync_{uuid.uuid4().hex[:8]}.tmp")
        try:
            with open(local_tmp, "w", encoding="utf-8") as f:
                f.write(content)
            scp_proc = subprocess.run(
                ["scp", "-o", "BatchMode=yes", local_tmp, f"{self.host}:{remote_tmp_scp}"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdin=subprocess.DEVNULL,
                timeout=90,
            )
            if scp_proc.returncode != 0:
                raise RuntimeError(f"scp 上传失败: {scp_proc.stderr.strip()}")
            if self.platform == "unix":
                move_cmd = (
                    f'cp "{path}" "{path}.bak.{time.strftime("%Y%m%d-%H%M%S")}" 2>/dev/null; '
                    f'mv -f "{remote_tmp}" "{path}" && echo "{path}"'
                )
            else:
                move_cmd = (
                    f'powershell -NoProfile -Command "'
                    f'$t=\'{remote_tmp}\'; $d=\'{path}\';'
                    f'if (Test-Path $d) {{ Copy-Item $d ($d+\'.bak.\'+[DateTime]::Now.ToString(\'yyyyMMdd-HHmmss\')) -Force }};'
                    f'Move-Item $t $d -Force; Write-Output $d"'
                )
            self._run(["ssh", "-o", "BatchMode=yes", self.host, move_cmd])
        finally:
            try:
                os.remove(local_tmp)
            except OSError:
                pass
