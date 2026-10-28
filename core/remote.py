"""跨主机文件读写辅助（经 ssh / scp）。

win11 上的 omp/dsh 配置通过 ssh/scp 读写。本模块封装：
  - read_remote(path, host)          : ssh type
  - write_remote(path, content, host): scp 上传临时文件 + ssh 原子替换（先备份）
  - backup_remote(path, host)        : ssh 备份带时间戳
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
import uuid


class RemoteIO:
    """通过 ssh/scp 访问远程主机文件的封装。

    写文件统一走 scp（原始字节传输，避免 ssh+PowerShell stdin 编码问题），
    再在远端用 Move-Item 原子替换目标（先备份）。失败不会清空原文件。
    """

    def __init__(self, host: str):
        self.host = host
        self._tmp_dir: str | None = None

    @property
    def tmp_dir(self) -> str:
        """远程临时目录（真实路径，首次查询后缓存）。"""
        if self._tmp_dir is None:
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
        return self._run(["ssh", "-o", "BatchMode=yes", self.host, f"type \"{path}\""])

    def exists(self, path: str) -> bool:
        out = self._run(
            ["ssh", "-o", "BatchMode=yes", self.host, f"if exist \"{path}\" (echo YES) else (echo NO)"]
        )
        return "YES" in out

    # ---- 备份 ----
    def backup(self, path: str, backup_dir: str | None = None) -> str | None:
        if not self.exists(path):
            return None
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup_cmd = (
            f'powershell -NoProfile -Command "'
            f'$p=\'{path}\'; $b=$p+\'.bak.{stamp}\'; Copy-Item $p $b -Force; Write-Output $b"'
        )
        return self._run(["ssh", "-o", "BatchMode=yes", self.host, backup_cmd]).strip()

    # ---- 写入（scp 原子替换，最可靠）----
    def write(self, path: str, content: str) -> None:
        remote_tmp = f"{self.tmp_dir}\\cpa_sync_{uuid.uuid4().hex[:8]}.tmp"
        remote_tmp_scp = remote_tmp.replace("\\", "/")  # scp 目标用正斜杠
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
            # 远程：备份旧文件 -> 用临时文件替换目标
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
