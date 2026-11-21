#!/usr/bin/env python3
"""cpa-sync GUI —— PySide6 图形界面。

作为 CLI 的图形外壳：复用 cpa-sync 的 cmd_* 全部逻辑，把输出导到界面。
内置图形化配置编辑器（不用手写 YAML）+ agent 接入点一览。

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

import yaml
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

# 复用 CLI（cpa-sync 无 .py 后缀，用 SourceFileLoader 加载）
_cli = SourceFileLoader("cpa_sync_cli", str(ROOT / "cpa-sync")).load_module()

CONFIG_PATH = str(ROOT / "config" / "cpa-config.yaml")

# 各 agent 接入点（配置文件位置）
AGENT_PATHS = [
    ("pi", "本机 ~/.pi/agent/models.json"),
    ("opencode", "本机 ~/.config/opencode/opencode.json"),
    ("claude", "本机 ~/.cc-switch/cc-switch.db（providers 表 app_type=claude）"),
    ("codex", "本机 ~/.cc-switch/cc-switch.db（providers 表 app_type=codex）"),
    ("dsh", "win11 a1@192.168.31.66:C:\\Users\\a1\\.dsh\\profiles\\web\\cordis.patch.yml"),
    ("omp", "win11 a1@192.168.31.66:C:\\Users\\a1\\.omp\\agent\\models.yml"),
]

ALL_ADAPTERS = [a for a, _ in AGENT_PATHS]


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


class ConfigEditor(QWidget):
    """图形化配置编辑器：读/写 cpa-config.yaml，不手写 YAML。"""

    def __init__(self, path: str, log_fn, parent=None):
        super().__init__(parent)
        self.path = path
        self.log = log_fn
        self._data: dict = {}
        self._build()
        # load() 由 MainWindow 在 UI 全部建好后调用（log_view 需就绪）

    def _build(self):
        layout = QVBoxLayout(self)

        # --- CPA 连接 ---
        conn = QGroupBox("CPA 连接")
        cf = QFormLayout(conn)
        self.base_url = QLineEdit()
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        cf.addRow("Base URL", self.base_url)
        cf.addRow("API Key", self.api_key)
        layout.addWidget(conn)

        # --- 模型 ---
        mdl = QGroupBox("模型 / 上下文")
        mf = QFormLayout(mdl)
        self.context_window = QSpinBox()
        self.context_window.setRange(1024, 10_000_000)
        self.context_window.setSingleStep(1024)
        self.max_tokens = QSpinBox()
        self.max_tokens.setRange(256, 1_000_000)
        self.max_tokens.setSingleStep(1024)
        self.auto_compact = QSpinBox()
        self.auto_compact.setRange(1024, 10_000_000)
        self.auto_compact.setSingleStep(1024)
        self.include = QLineEdit()
        self.include.setPlaceholderText("空 = 动态扫 CPA /v1/models")
        self.exclude = QLineEdit()
        self.exclude.setPlaceholderText("支持通配符，如 *.flash")
        mf.addRow("上下文窗口", self.context_window)
        mf.addRow("最大输出", self.max_tokens)
        mf.addRow("codex 自动压缩", self.auto_compact)
        mf.addRow("白名单(逗号)", self.include)
        mf.addRow("黑名单(逗号)", self.exclude)
        layout.addWidget(mdl)

        # --- 同步策略 + targets ---
        syn = QGroupBox("同步")
        sv = QVBoxLayout(syn)
        strat_row = QHBoxLayout()
        strat_row.addWidget(QLabel("策略:"))
        self.strategy = QComboBox()
        self.strategy.addItems(["replace", "merge"])
        strat_row.addWidget(self.strategy)
        strat_row.addStretch()
        sv.addLayout(strat_row)
        tgt_row = QHBoxLayout()
        tgt_row.addWidget(QLabel("目标:"))
        self.target_checks: dict[str, QCheckBox] = {}
        for name in ALL_ADAPTERS:
            cb = QCheckBox(name)
            self.target_checks[name] = cb
            tgt_row.addWidget(cb)
        tgt_row.addStretch()
        sv.addLayout(tgt_row)
        layout.addWidget(syn)

        # --- agent 接入点一览 ---
        ap = QGroupBox("Agent 接入点（配置文件位置）")
        av = QVBoxLayout(ap)
        table = QTableWidget(len(AGENT_PATHS), 2)
        table.setHorizontalHeaderLabels(["agent", "配置文件"])
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        for r, (name, path) in enumerate(AGENT_PATHS):
            table.setItem(r, 0, QTableWidgetItem(name))
            table.setItem(r, 1, QTableWidgetItem(path))
        table.setMaximumHeight(140)
        av.addWidget(table)
        layout.addWidget(ap)

        # --- 保存 ---
        btn_row = QHBoxLayout()
        self.btn_save = QPushButton("💾 保存配置")
        self.btn_save.clicked.connect(self.save)
        self.btn_reload = QPushButton("🔄 重新读取")
        self.btn_reload.clicked.connect(self.load)
        btn_row.addWidget(self.btn_save)
        btn_row.addWidget(self.btn_reload)
        btn_row.addStretch()
        layout.addLayout(btn_row)
        layout.addStretch()

    # ---- 读/写 ----
    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self._data = yaml.safe_load(f) or {}
        except Exception as e:  # noqa: BLE001
            self.log(f"✗ 读取配置失败: {e}\n")
            self._data = {}
            return
        cpa = self._data.get("cpa", {}) or {}
        self.base_url.setText(str(cpa.get("base_url", "")))
        self.api_key.setText(str(cpa.get("api_key", "")))
        self.context_window.setValue(int(cpa.get("context_window", 256000)))
        self.max_tokens.setValue(int(cpa.get("max_tokens", 16384)))
        self.auto_compact.setValue(int(cpa.get("auto_compact_token_limit", 200000)))
        self.include.setText(", ".join(cpa.get("include_models", []) or []))
        self.exclude.setText(", ".join(cpa.get("exclude_models", []) or []))
        self.strategy.setCurrentText(str(self._data.get("strategy", "replace")))
        targets = self._data.get("targets", []) or []
        for name, cb in self.target_checks.items():
            cb.setChecked(name in targets)
        self.log("✓ 配置已加载到表单\n")

    def save(self):
        cpa = self._data.setdefault("cpa", {})
        cpa["base_url"] = self.base_url.text().strip()
        cpa["api_key"] = self.api_key.text().strip()
        cpa["context_window"] = self.context_window.value()
        cpa["max_tokens"] = self.max_tokens.value()
        cpa["auto_compact_token_limit"] = self.auto_compact.value()
        cpa["include_models"] = [x.strip() for x in self.include.text().split(",") if x.strip()]
        cpa["exclude_models"] = [x.strip() for x in self.exclude.text().split(",") if x.strip()]
        self._data["strategy"] = self.strategy.currentText()
        self._data["targets"] = [n for n, c in self.target_checks.items() if c.isChecked()]
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                yaml.safe_dump(self._data, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
            self.log(f"✓ 配置已保存: {self.path}\n")
        except Exception as e:  # noqa: BLE001
            self.log(f"✗ 保存失败: {e}\n")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("cpa-sync · CPA 配置同步")
        self.resize(980, 680)
        self._worker: Worker | None = None
        self._build_ui()
        self.log("cpa-sync GUI 就绪。配置: " + CONFIG_PATH + "\n")

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
        for b in (self.btn_models, self.btn_list, self.btn_check, self.btn_apply, self.btn_doctor):
            btns.addWidget(b)
        btns.addStretch()
        root.addLayout(btns)

        # 配置路径提示
        self.cfg_label = QLabel("")
        root.addWidget(self.cfg_label)

        # 中间分栏：左=配置编辑器，右=日志
        split = QSplitter(Qt.Orientation.Horizontal)
        self.editor = ConfigEditor(CONFIG_PATH, self.log)
        split.addWidget(self.editor)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.addWidget(QLabel("输出日志"))
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.log_view.setStyleSheet(
            "font-family: monospace; font-size: 12px; background:#1e1e1e; color:#d4d4d4;"
        )
        rv.addWidget(self.log_view, 1)
        split.addWidget(right)
        split.setSizes([430, 550])
        root.addWidget(split, 1)

        # UI 全部建好后加载配置（此时 log_view 已就绪）
        self.editor.load()

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
            self.btn_apply, self.btn_doctor,
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
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
