#!/usr/bin/env python3
"""cpa-sync GUI —— PySide6 图形界面。

作为 CLI 的图形外壳：复用 cpa-sync 的 cmd_* 全部逻辑，
把输出导到界面日志区。后台任务用 QThread，不阻塞 UI。

用法: .venv/bin/python gui/app.py
"""
from __future__ import annotations

import argparse
import contextlib
import io
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

# 项目根加入 path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

# 复用 CLI（cpa-sync 无 .py 后缀，用 SourceFileLoader 加载）
_cli = SourceFileLoader("cpa_sync_cli", str(ROOT / "cpa-sync")).load_module()

CONFIG_PATH = str(ROOT / "config" / "cpa-config.yaml")


class Worker(QThread):
    """后台跑一个 CLI 命令函数，输出经信号回主线程。"""

    output = Signal(str)
    finished_ok = Signal(bool)

    def __init__(self, fn, args, parent=None):
        super().__init__(parent)
        self._fn = fn
        self._args = args

    def run(self):
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                code = self._fn(self._args)
        except SystemExit as e:  # argparse / SystemExit
            code = int(e.code or 0)
        except Exception as e:  # noqa: BLE001
            buf.write(f"\n✗ 异常: {e}\n")
            code = 1
        self.output.emit(buf.getvalue())
        self.finished_ok.emit(code == 0)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("cpa-sync · CPA 配置同步")
        self.resize(860, 600)
        self._worker: Worker | None = None
        self._build_ui()
        self.log("cpa-sync GUI 就绪。配置: " + CONFIG_PATH)

    # ---- UI ----
    def _build_ui(self):
        central = QWidget()
        root = QVBoxLayout(central)

        # 顶部按钮行
        btns = QHBoxLayout()
        self.btn_models = self._mk_btn("📡 模型", self.run_models)
        self.btn_list = self._mk_btn("📋 状态", self.run_list)
        self.btn_check = self._mk_btn("🔍 检查(干跑)", self.run_check)
        self.btn_apply = self._mk_btn("🚀 同步", self.run_apply)
        self.btn_doctor = self._mk_btn("🩺 诊断", self.run_doctor)
        self.btn_reload = self._mk_btn("🔄 重载配置", self.reload_config)
        for b in (
            self.btn_models, self.btn_list, self.btn_check,
            self.btn_apply, self.btn_doctor, self.btn_reload,
        ):
            btns.addWidget(b)
        root.addLayout(btns)

        # 配置路径提示
        self.cfg_label = QLabel("")
        root.addWidget(self.cfg_label)

        # 日志区
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.log_view.setStyleSheet(
            "font-family: monospace; font-size: 12px; background:#1e1e1e; color:#d4d4d4;"
        )
        root.addWidget(self.log_view, 1)

        # 状态栏
        self.status = self.statusBar()
        self.status.showMessage("就绪")

        self.setCentralWidget(central)

    def _mk_btn(self, text, slot):
        b = QPushButton(text)
        b.clicked.connect(slot)
        return b

    # ---- 后台任务 ----
    def _run_cmd(self, fn, busy_text):
        if self._worker is not None and self._worker.isRunning():
            self.log("⚠ 上一个任务还在跑，忽略。\n")
            return
        args = argparse.Namespace(config=CONFIG_PATH, targets=[], yes=True, backup=False)
        self._set_busy(True, busy_text)
        self._worker = Worker(fn, args)
        self._worker.output.connect(self._append)
        self._worker.finished_ok.connect(self._on_done)
        self._worker.start()

    def _on_done(self, ok: bool):
        self._set_busy(False)
        self.status.showMessage("完成 ✓" if ok else "失败 ✗")
        self.log("\n" + ("── 完成 ──" if ok else "── 失败 ──") + "\n")

    def _set_busy(self, busy: bool, msg: str = ""):
        for b in (
            self.btn_models, self.btn_list, self.btn_check,
            self.btn_apply, self.btn_doctor, self.btn_reload,
        ):
            b.setEnabled(not busy)
        if msg:
            self.status.showMessage(msg)

    # ---- 动作 ----
    def run_models(self):
        self._run_cmd(_cli.cmd_models, "扫描 CPA 模型…")

    def run_list(self):
        self._run_cmd(_cli.cmd_list, "读取各 agent 状态…")

    def run_check(self):
        self._run_cmd(_cli.cmd_check, "干跑检查…")

    def run_apply(self):
        self._run_cmd(_cli.cmd_apply, "同步中…")

    def run_doctor(self):
        self._run_cmd(_cli.cmd_doctor, "诊断…")

    def reload_config(self):
        try:
            from core.config import SyncConfig
            c = SyncConfig.load(CONFIG_PATH)
            self.cfg_label.setText(
                f"CPA: {c.cpa.base_url} | 上下文 {c.cpa.context_window} | 策略 {c.strategy}"
            )
            self.log("✓ 配置已重载\n")
        except Exception as e:  # noqa: BLE001
            self.log(f"✗ 配置读取失败: {e}\n")

    # ---- 日志 ----
    def _append(self, text: str):
        self.log_view.moveCursor(self.log_view.textCursor().MoveOperation.End)
        self.log_view.insertPlainText(text)

    def log(self, text: str):
        self._append(text)


def main() -> int:
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    win.reload_config()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
