#!/usr/bin/env python3
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from PyQt6.QtCore import QProcess, Qt, QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout,
    QFrame, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit,
    QPushButton, QStackedWidget, QVBoxLayout, QWidget
)

TITLE = "PQC Firmware Signing Tool V4"
HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "project_config.json"


class PathField(QWidget):
    def __init__(self, mode="open", file_filter="All files (*)", changed=None):
        super().__init__()
        self.mode = mode
        self.file_filter = file_filter
        self.changed = changed
        self.edit = QLineEdit()
        self.button = QPushButton("瀏覽…")
        self.button.clicked.connect(self.pick)
        self.edit.textChanged.connect(lambda: self.changed() if self.changed else None)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.edit)
        layout.addWidget(self.button)

    def pick(self):
        initial = self.text() or str(HERE)
        if self.mode == "directory":
            selected = QFileDialog.getExistingDirectory(self, "選擇目錄", initial)
        else:
            selected, _ = QFileDialog.getOpenFileName(
                self, "選擇檔案", initial, self.file_filter
            )
        if selected:
            self.edit.setText(selected)

    def text(self):
        return self.edit.text().strip()

    def set_text(self, value):
        self.edit.setText(value)


class StatusCard(QFrame):
    def __init__(self, number, title, description):
        super().__init__()
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.badge = QLabel(number)
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.badge.setFixedSize(38, 38)
        self.title = QLabel(title)
        font = self.title.font()
        font.setBold(True)
        self.title.setFont(font)
        self.description = QLabel(description)
        self.description.setWordWrap(True)
        self.status = QLabel("尚未執行")

        text = QVBoxLayout()
        text.addWidget(self.title)
        text.addWidget(self.description)
        text.addWidget(self.status)

        layout = QHBoxLayout(self)
        layout.addWidget(self.badge)
        layout.addLayout(text, 1)
        self.set_state("pending", "尚未執行")

    def set_state(self, state, text):
        colors = {
            "pending": ("#d8d8d8", "#222"),
            "running": ("#ffd37a", "#222"),
            "ok": ("#9fdfa8", "#173d1e"),
            "error": ("#ffaaaa", "#610000"),
        }
        background, foreground = colors[state]
        self.badge.setStyleSheet(
            f"font-weight:bold;border-radius:19px;background:{background};color:{foreground};"
        )
        self.status.setText(text)
        self.status.setStyleSheet(f"font-weight:bold;color:{foreground};")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(TITLE)
        self.resize(1120, 780)

        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.readyReadStandardOutput.connect(self.read_output)
        self.process.finished.connect(self.finished)

        self.active_step = None
        self.active_action = None

        self.project_dir = PathField(mode="directory", changed=self.project_changed)
        self.openssl_path = PathField(
            file_filter="Executable (*.exe);;All files (*)",
            changed=self.save_config,
        )
        self.algorithm = QComboBox()
        self.algorithm.addItems(["ML-DSA-44", "ML-DSA-65", "ML-DSA-87"])
        self.algorithm.currentTextChanged.connect(self.settings_changed)

        self.input_hex = PathField(
            file_filter="Intel HEX (*.hex);;All files (*)",
            changed=self.settings_changed,
        )

        self.private_key = QLabel("-")
        self.public_key = QLabel("-")
        self.output_hex = QLabel("-")

        self.image_start = QLineEdit("0x08000000")
        self.image_end = QLineEdit("0x0803E000")
        self.metadata_address = QLineEdit("0x0803E000")
        self.metadata_size = QLineEdit("0x2000")
        self.build_info = QLineEdit("release")
        self.use_trusted_key = QCheckBox("使用專案公鑰作為可信根")
        self.use_trusted_key.setChecked(True)

        for widget in [
            self.image_start, self.image_end,
            self.metadata_address, self.metadata_size,
            self.build_info,
        ]:
            widget.textChanged.connect(self.save_config)

        self.cards = [
            StatusCard("1", "Environment", "自動偵測 OpenSSL 3.5+ 與 ML-DSA。"),
            StatusCard("2", "Key Generation", "在 project/keys 建立 ML-DSA 金鑰。"),
            StatusCard("3", "Firmware Signing", "建立 TLV Metadata 與 Signed HEX。"),
            StatusCard("4", "Firmware Verification", "驗證 Hash、可信公鑰與簽章。"),
        ]

        self.navigation = QListWidget()
        for item in [
            "專案總覽", "① Environment", "② Key Generation",
            "③ Firmware Signing", "④ Firmware Verification"
        ]:
            self.navigation.addItem(QListWidgetItem(item))
        self.navigation.setFixedWidth(210)

        self.pages = QStackedWidget()
        self.pages.addWidget(self.overview_page())
        self.pages.addWidget(self.environment_page())
        self.pages.addWidget(self.key_page())
        self.pages.addWidget(self.sign_page())
        self.pages.addWidget(self.verify_page())
        self.navigation.currentRowChanged.connect(self.pages.setCurrentIndex)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QFont("Consolas"))
        self.log.setMaximumBlockCount(8000)

        top = QWidget()
        top_form = QFormLayout(top)
        top_form.addRow("Project Folder", self.project_dir)
        top_form.addRow("OpenSSL executable", self.openssl_path)
        top_form.addRow("Algorithm", self.algorithm)

        middle = QHBoxLayout()
        middle.addWidget(self.navigation)
        middle.addWidget(self.pages, 1)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(top)
        layout.addLayout(middle, 4)
        layout.addWidget(QLabel("執行紀錄"))
        layout.addWidget(self.log, 2)
        self.setCentralWidget(central)

        self.load_config()
        self.ensure_project()
        self.refresh_paths()
        self.navigation.setCurrentRow(0)
        QTimer.singleShot(400, self.auto_detect_openssl)

    def overview_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        title = QLabel("PQC Secure Firmware Workflow")
        font = title.font()
        font.setBold(True)
        font.setPointSize(font.pointSize() + 4)
        title.setFont(font)
        layout.addWidget(title)
        layout.addWidget(QLabel(
            "V4 使用 dataclass Manifest Header 與 TLV Metadata，"
            "並自動管理金鑰、輸出檔及紀錄。"
        ))
        for card in self.cards:
            layout.addWidget(card)
        test = QPushButton("執行格式 Self-Test")
        test.clicked.connect(self.selftest)
        layout.addWidget(test, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addStretch()
        return page

    def environment_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        group = QGroupBox("OpenSSL Environment")
        form = QFormLayout(group)
        form.addRow("Executable", self.openssl_path)
        detect = QPushButton("自動偵測")
        detect.clicked.connect(self.auto_detect_openssl)
        check = QPushButton("檢查 OpenSSL 與 ML-DSA")
        check.clicked.connect(self.check_environment)
        buttons = QHBoxLayout()
        buttons.addWidget(detect)
        buttons.addWidget(check)
        form.addRow(buttons)
        layout.addWidget(group)
        layout.addStretch()
        return page

    def key_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        group = QGroupBox("ML-DSA Key Generation")
        form = QFormLayout(group)
        form.addRow("Algorithm", self.algorithm)
        form.addRow("Private key", self.private_key)
        form.addRow("Public key", self.public_key)
        button = QPushButton("Generate Key")
        button.clicked.connect(self.generate_key)
        form.addRow(button)
        layout.addWidget(group)
        note = QLabel(
            "若金鑰已存在，GUI 不會自動覆寫。正式產品請將私鑰移到受控簽章環境。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch()
        return page

    def sign_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        group = QGroupBox("Firmware Signing")
        form = QFormLayout(group)
        form.addRow("Input HEX", self.input_hex)
        form.addRow("Private key", self.private_key)
        form.addRow("Public key", self.public_key)
        form.addRow("Output HEX", self.output_hex)
        form.addRow("Image start", self.image_start)
        form.addRow("Image end", self.image_end)
        form.addRow("Metadata address", self.metadata_address)
        form.addRow("Metadata size", self.metadata_size)
        form.addRow("Build info", self.build_info)
        button = QPushButton("Sign Firmware")
        button.clicked.connect(self.sign_firmware)
        form.addRow(button)
        layout.addWidget(group)
        layout.addStretch()
        return page

    def verify_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        group = QGroupBox("Firmware Verification")
        form = QFormLayout(group)
        form.addRow("Signed HEX", self.output_hex)
        form.addRow("Metadata address", self.metadata_address)
        form.addRow(self.use_trusted_key)
        form.addRow("Trusted public key", self.public_key)
        button = QPushButton("Verify Firmware")
        button.clicked.connect(self.verify_firmware)
        form.addRow(button)
        layout.addWidget(group)
        layout.addStretch()
        return page

    def project(self):
        return Path(self.project_dir.text()) if self.project_dir.text() else None

    def ensure_project(self):
        project = self.project()
        if not project:
            return
        for folder in ["input", "keys", "output", "log"]:
            (project / folder).mkdir(parents=True, exist_ok=True)

    def key_paths(self):
        project = self.project()
        if not project:
            return Path(), Path()
        token = self.algorithm.currentText().lower().replace("-", "")
        return (
            project / "keys" / f"{token}_private.pem",
            project / "keys" / f"{token}_public.pem",
        )

    def output_path(self):
        project = self.project()
        if not project:
            return Path()
        stem = Path(self.input_hex.text()).stem if self.input_hex.text() else "firmware"
        return project / "output" / f"{stem}_signed.hex"

    def log_path(self):
        project = self.project()
        return project / "log" / "pqc_v4.log" if project else Path()

    def refresh_paths(self):
        private_key, public_key = self.key_paths()
        output = self.output_path()
        self.private_key.setText(str(private_key) if str(private_key) != "." else "-")
        self.public_key.setText(str(public_key) if str(public_key) != "." else "-")
        self.output_hex.setText(str(output) if str(output) != "." else "-")

        keys_ok = private_key.exists() and public_key.exists()
        self.cards[1].set_state("ok" if keys_ok else "pending",
                                "金鑰已存在" if keys_ok else "等待產生金鑰")
        self.cards[2].set_state("ok" if output.exists() else "pending",
                                "Signed HEX 已存在" if output.exists() else "等待簽章")

    def project_changed(self):
        self.ensure_project()
        self.refresh_paths()
        self.save_config()

    def settings_changed(self):
        self.refresh_paths()
        self.save_config()

    def validate(self, require_project=True):
        if require_project and not self.project():
            QMessageBox.warning(self, TITLE, "請先選擇 Project Folder。")
            return False
        if require_project:
            self.ensure_project()
        return True

    def command_base(self):
        command = [sys.executable, str(HERE / "pqc_openssl_hex_v4.py")]
        if self.openssl_path.text():
            command += ["--openssl", self.openssl_path.text()]
        return command

    def auto_detect_openssl(self):
        candidates = [
            shutil.which("openssl"),
            r"C:\Program Files\OpenSSL-Win64\bin\openssl.exe",
            r"C:\Program Files\OpenSSL-Win32\bin\openssl.exe",
            r"C:\Program Files\Git\usr\bin\openssl.exe",
        ]
        for candidate in candidates:
            if candidate and Path(candidate).exists():
                self.openssl_path.set_text(str(candidate))
                break
        self.check_environment()

    def selftest(self):
        self.start(self.command_base() + ["selftest"], 0, "selftest")

    def check_environment(self):
        self.start(self.command_base() + ["check"], 0, "check")

    def generate_key(self):
        if not self.validate():
            return
        private_key, public_key = self.key_paths()
        if private_key.exists() or public_key.exists():
            QMessageBox.warning(
                self, TITLE,
                "金鑰已存在。為避免誤覆寫，請先手動備份或刪除舊金鑰。"
            )
            return
        self.start(
            self.command_base() + [
                "keygen",
                "--algorithm", self.algorithm.currentText(),
                "--private-key", str(private_key),
                "--public-key", str(public_key),
            ],
            1, "keygen"
        )

    def sign_firmware(self):
        if not self.validate():
            return
        if not self.input_hex.text() or not Path(self.input_hex.text()).exists():
            QMessageBox.warning(self, TITLE, "請選擇有效的 Input HEX。")
            return
        private_key, public_key = self.key_paths()
        if not private_key.exists() or not public_key.exists():
            QMessageBox.warning(self, TITLE, "請先執行 Generate Key。")
            return
        self.start(
            self.command_base() + [
                "sign",
                "--algorithm", self.algorithm.currentText(),
                "--input", self.input_hex.text(),
                "--output", str(self.output_path()),
                "--private-key", str(private_key),
                "--public-key", str(public_key),
                "--image-start", self.image_start.text(),
                "--image-end", self.image_end.text(),
                "--metadata-address", self.metadata_address.text(),
                "--metadata-size", self.metadata_size.text(),
                "--build-info", self.build_info.text(),
            ],
            2, "sign"
        )

    def verify_firmware(self):
        if not self.validate():
            return
        output = self.output_path()
        if not output.exists():
            QMessageBox.warning(self, TITLE, "找不到 Signed HEX。")
            return
        command = self.command_base() + [
            "verify",
            "--input", str(output),
            "--metadata-address", self.metadata_address.text(),
        ]
        if self.use_trusted_key.isChecked():
            _, public_key = self.key_paths()
            if not public_key.exists():
                QMessageBox.warning(self, TITLE, "找不到可信公鑰。")
                return
            command += ["--trusted-public-key", str(public_key)]
        self.start(command, 3, "verify")

    def start(self, command, step, action):
        if self.process.state() != QProcess.ProcessState.NotRunning:
            QMessageBox.warning(self, TITLE, "已有操作正在執行。")
            return
        self.active_step = step
        self.active_action = action
        self.cards[step].set_state("running", "執行中…")
        self.navigation.setEnabled(False)
        self.pages.setEnabled(False)
        self.append_log("\n" + "=" * 90 + "\n")
        self.append_log("$ " + subprocess.list2cmdline(command) + "\n\n")
        self.process.start(command[0], command[1:])

    def read_output(self):
        self.append_log(bytes(self.process.readAllStandardOutput()).decode(errors="replace"))

    def append_log(self, text):
        self.log.insertPlainText(text)
        self.log.ensureCursorVisible()
        path = self.log_path()
        if str(path) != ".":
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(text)

    def finished(self, exit_code, _status):
        self.read_output()
        self.navigation.setEnabled(True)
        self.pages.setEnabled(True)

        if exit_code == 0:
            labels = {
                "selftest": "格式 Self-Test 通過",
                "check": "OpenSSL 與 ML-DSA 可用",
                "keygen": "金鑰產生完成",
                "sign": "Firmware 簽章完成",
                "verify": "Firmware 驗證通過",
            }
            self.cards[self.active_step].set_state(
                "ok", labels.get(self.active_action, "操作完成")
            )
            next_page = {
                "check": 2,
                "keygen": 3,
                "sign": 4,
                "verify": 0,
            }.get(self.active_action)
            if next_page is not None:
                self.navigation.setCurrentRow(next_page)
            QMessageBox.information(self, TITLE, "操作完成。")
        else:
            self.cards[self.active_step].set_state(
                "error", f"操作失敗，Exit code={exit_code}"
            )
            QMessageBox.critical(self, TITLE, f"操作失敗，Exit code={exit_code}。")

        self.refresh_paths()
        self.save_config()

    def save_config(self):
        config = {
            "project_dir": self.project_dir.text(),
            "openssl_path": self.openssl_path.text(),
            "algorithm": self.algorithm.currentText(),
            "input_hex": self.input_hex.text(),
            "image_start": self.image_start.text(),
            "image_end": self.image_end.text(),
            "metadata_address": self.metadata_address.text(),
            "metadata_size": self.metadata_size.text(),
            "build_info": self.build_info.text(),
            "use_trusted_key": self.use_trusted_key.isChecked(),
        }
        try:
            CONFIG_FILE.write_text(
                json.dumps(config, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except OSError:
            pass

    def load_config(self):
        config = {
            "project_dir": str(HERE / "demo_project"),
            "openssl_path": "",
            "algorithm": "ML-DSA-65",
            "input_hex": str(HERE / "sample_application.hex"),
            "image_start": "0x08000000",
            "image_end": "0x0803E000",
            "metadata_address": "0x0803E000",
            "metadata_size": "0x2000",
            "build_info": "release",
            "use_trusted_key": True,
        }
        try:
            config.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass

        self.project_dir.set_text(config["project_dir"])
        self.openssl_path.set_text(config["openssl_path"])
        self.algorithm.setCurrentText(config["algorithm"])
        self.input_hex.set_text(config["input_hex"])
        self.image_start.setText(config["image_start"])
        self.image_end.setText(config["image_end"])
        self.metadata_address.setText(config["metadata_address"])
        self.metadata_size.setText(config["metadata_size"])
        self.build_info.setText(config["build_info"])
        self.use_trusted_key.setChecked(config["use_trusted_key"])


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
