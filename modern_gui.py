"""
bnet-session-switcher - 现代化 PyQt5 无边框图形界面
支持暴雪战网国服（网易通行证）与国际服多账号隔离保存与快速切换
"""
import ctypes
import json
import os
import subprocess
import sys
from datetime import datetime

from PyQt5.QtCore import QPoint, Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QIcon, QPixmap
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import (
    APP_DIR,
    APP_NAME,
    APP_TITLE,
    APP_VERSION,
    LOGS_DIR,
    PROJECT_REPO,
    SETTINGS_FILE,
    UPSTREAM_REPO,
    SingleInstanceLock,
)
from isolated_switcher import IsolatedSwitcher, find_battlenet_exe, is_admin


COLORS = {
    "bg": "#f5f7fa",
    "sidebar": "#ffffff",
    "card": "#e8f4f8",
    "card_hover": "#d4eef5",
    "card_active": "#dff5fc",
    "primary": "#00b4d8",
    "primary_dark": "#0096c7",
    "secondary": "#0ea5e9",
    "global": "#9333ea",
    "global_dark": "#7e22ce",
    "success": "#38a169",
    "warning": "#dd6b20",
    "danger": "#e53e3e",
    "text": "#2d3748",
    "text_light": "#718096",
    "white": "#ffffff",
    "border": "#e2e8f0",
}

STYLESHEET = f"""
QMainWindow {{
    background-color: {COLORS['bg']};
}}

QWidget#sidebar {{
    background-color: {COLORS['sidebar']};
    border-right: 1px solid {COLORS['border']};
    border-top-left-radius: 15px;
    border-bottom-left-radius: 15px;
}}

QLabel#title {{
    color: {COLORS['text']};
    font-size: 19px;
    font-weight: bold;
}}

QLabel#subtitle {{
    color: {COLORS['text_light']};
    font-size: 11px;
}}

QPushButton#secondaryBtn {{
    background-color: {COLORS['white']};
    color: {COLORS['text']};
    border: 1px solid {COLORS['border']};
    border-radius: 8px;
    padding: 8px 16px;
    font-size: 13px;
}}

QPushButton#secondaryBtn:hover {{
    background-color: {COLORS['bg']};
}}

QScrollArea {{
    border: none;
    background-color: transparent;
}}

QWidget#scrollContent {{
    background-color: transparent;
}}
"""


class TaskThread(QThread):
    """通用后台任务线程，严格以 QThread.finished 作为生命周期释放边界"""

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.outcome_ok = False
        self.outcome_value = None
        self.outcome_error = ""

    def run(self):
        try:
            self.outcome_value = self.fn(*self.args, **self.kwargs)
            self.outcome_ok = True
        except Exception as e:
            self.outcome_ok = False
            self.outcome_error = str(e)


class BaseFramelessDialog(QDialog):
    """带圆角阴影、自适应高度、支持拖拽及回车/Esc 快捷键的无边框对话框基类"""

    def __init__(self, parent=None, width: int = 480):
        super().__init__(parent, Qt.FramelessWindowHint | Qt.Dialog)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setModal(True)
        self._drag_pos = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 14)

        self.container = QFrame(self)
        self.container.setObjectName("dialogContainer")
        self.container.setFixedWidth(width - 28)
        self.container.setStyleSheet(
            f"""
            QFrame#dialogContainer {{
                background-color: white;
                border-radius: 14px;
                border: 1px solid {COLORS['border']};
            }}
            """
        )

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(22)
        shadow.setColor(QColor(0, 0, 0, 55))
        shadow.setOffset(0, 5)
        self.container.setGraphicsEffect(shadow)

        outer.addWidget(self.container)

        self.body_layout = QVBoxLayout(self.container)
        self.body_layout.setContentsMargins(26, 22, 26, 22)
        self.body_layout.setSpacing(14)

    def showEvent(self, event):
        super().showEvent(event)
        self.adjustSize()
        parent_win = self.parent().window() if self.parent() else None
        if parent_win:
            center = parent_win.frameGeometry().center()
            self.move(center.x() - self.width() // 2, center.y() - self.height() // 2)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPos() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.LeftButton and self._drag_pos is not None:
            self.move(event.globalPos() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, _event):
        self._drag_pos = None


class ModernDialog(BaseFramelessDialog):
    """自适应多行文本高度的提示/确认对话框"""

    def __init__(self, parent, title: str, message: str, dialog_type: str = "info", buttons=None):
        super().__init__(parent, width=500)
        self._setup_content(title, message, dialog_type, buttons or ["确定"])

    def _setup_content(self, title: str, message: str, dialog_type: str, buttons: list):
        header = QHBoxLayout()
        header.setSpacing(12)

        icon_colors = {
            "info": COLORS["primary"],
            "success": COLORS["success"],
            "warning": COLORS["warning"],
            "error": COLORS["danger"],
        }
        icon_texts = {"info": "ℹ", "success": "✓", "warning": "⚠", "error": "✕"}

        icon = QLabel(icon_texts.get(dialog_type, "ℹ"))
        icon.setFixedSize(38, 38)
        icon.setAlignment(Qt.AlignCenter)
        icon.setStyleSheet(
            f"""
            background-color: {icon_colors.get(dialog_type, COLORS['primary'])};
            color: white;
            border-radius: 19px;
            font-size: 18px;
            font-weight: bold;
            """
        )
        header.addWidget(icon)

        title_label = QLabel(title)
        title_label.setFont(QFont("微软雅黑", 13, QFont.Bold))
        title_label.setStyleSheet(f"color: {COLORS['text']};")
        header.addWidget(title_label, 1)
        self.body_layout.addLayout(header)

        msg_label = QLabel(message)
        msg_label.setFont(QFont("微软雅黑", 10))
        msg_label.setStyleSheet(f"color: {COLORS['text_light']}; line-height: 1.5;")
        msg_label.setWordWrap(True)
        msg_label.setMinimumHeight(44)
        self.body_layout.addWidget(msg_label)

        self.body_layout.addSpacing(4)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        if len(buttons) == 2:
            cancel_btn = QPushButton(buttons[1])
            cancel_btn.setCursor(Qt.PointingHandCursor)
            cancel_btn.setStyleSheet(
                f"""
                QPushButton {{
                    background-color: white;
                    color: {COLORS['text']};
                    border: 1px solid {COLORS['border']};
                    border-radius: 8px;
                    padding: 9px 24px;
                    font-size: 13px;
                }}
                QPushButton:hover {{
                    background-color: {COLORS['bg']};
                }}
                """
            )
            cancel_btn.clicked.connect(self.reject)
            btn_layout.addWidget(cancel_btn)

        ok_btn = QPushButton(buttons[0])
        ok_btn.setCursor(Qt.PointingHandCursor)
        ok_btn.setDefault(True)
        ok_btn.setStyleSheet(
            f"""
            QPushButton {{
                background-color: {COLORS['primary']};
                color: white;
                border: none;
                border-radius: 8px;
                padding: 9px 26px;
                font-size: 13px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background-color: {COLORS['primary_dark']};
            }}
            """
        )
        ok_btn.clicked.connect(self.accept)
        btn_layout.addWidget(ok_btn)

        self.body_layout.addLayout(btn_layout)

    @staticmethod
    def show_info(parent, title: str, message: str):
        ModernDialog(parent, title, message, "info").exec_()

    @staticmethod
    def show_success(parent, title: str, message: str):
        ModernDialog(parent, title, message, "success").exec_()

    @staticmethod
    def show_warning(parent, title: str, message: str):
        ModernDialog(parent, title, message, "warning").exec_()

    @staticmethod
    def show_error(parent, title: str, message: str):
        ModernDialog(parent, title, message, "error").exec_()

    @staticmethod
    def show_question(parent, title: str, message: str) -> bool:
        return ModernDialog(parent, title, message, "info", ["确定", "取消"]).exec_() == QDialog.Accepted


class ModernInputDialog(BaseFramelessDialog):
    """文本输入对话框（支持回车确认与空白校验）"""

    def __init__(self, parent, title: str, message: str, default_text: str = ""):
        super().__init__(parent, width=460)
        self.result_text = ""
        self._setup_content(title, message, default_text)

    def _setup_content(self, title: str, message: str, default_text: str):
        title_label = QLabel(title)
        title_label.setFont(QFont("微软雅黑", 14, QFont.Bold))
        title_label.setStyleSheet(f"color: {COLORS['primary']};")
        self.body_layout.addWidget(title_label)

        msg_label = QLabel(message)
        msg_label.setFont(QFont("微软雅黑", 10))
        msg_label.setStyleSheet(f"color: {COLORS['text_light']};")
        msg_label.setWordWrap(True)
        self.body_layout.addWidget(msg_label)

        self.input_field = QLineEdit()
        self.input_field.setText(default_text)
        self.input_field.setFont(QFont("微软雅黑", 11))
        self.input_field.setStyleSheet(
            f"""
            QLineEdit {{
                border: 2px solid {COLORS['border']};
                border-radius: 8px;
                padding: 10px 14px;
                background-color: {COLORS['bg']};
                color: {COLORS['text']};
            }}
            QLineEdit:focus {{
                border-color: {COLORS['primary']};
            }}
            """
        )
        self.input_field.returnPressed.connect(self._on_confirm)
        self.body_layout.addWidget(self.input_field)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        cancel_btn = QPushButton("取消")
        cancel_btn.setCursor(Qt.PointingHandCursor)
        cancel_btn.setStyleSheet(
            f"""
            QPushButton {{
                background-color: white;
                color: {COLORS['text']};
                border: 1px solid {COLORS['border']};
                border-radius: 8px;
                padding: 9px 24px;
                font-size: 13px;
            }}
            QPushButton:hover {{
                background-color: {COLORS['bg']};
            }}
            """
        )
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        ok_btn = QPushButton("确定")
        ok_btn.setCursor(Qt.PointingHandCursor)
        ok_btn.setDefault(True)
        ok_btn.setStyleSheet(
            f"""
            QPushButton {{
                background-color: {COLORS['primary']};
                color: white;
                border: none;
                border-radius: 8px;
                padding: 9px 26px;
                font-size: 13px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background-color: {COLORS['primary_dark']};
            }}
            """
        )
        ok_btn.clicked.connect(self._on_confirm)
        btn_layout.addWidget(ok_btn)

        self.body_layout.addLayout(btn_layout)
        self.input_field.setFocus()
        self.input_field.selectAll()

    def _on_confirm(self):
        val = self.input_field.text().strip()
        if not val:
            self.input_field.setFocus()
            return
        self.result_text = val
        self.accept()

    @staticmethod
    def get_text(parent, title: str, message: str, default_text: str = ""):
        dialog = ModernInputDialog(parent, title, message, default_text)
        ok = dialog.exec_() == QDialog.Accepted
        return dialog.result_text if ok else "", ok


class SaveAccountDialog(BaseFramelessDialog):
    """保存账号对话框（统一支持国服与国际服主题样式）"""

    def __init__(self, parent, email: str, default_name: str, is_global: bool = False):
        super().__init__(parent, width=460)
        self.result_nickname = ""
        self.is_global = is_global
        self._setup_content(email, default_name)

    def _setup_content(self, email: str, default_name: str):
        accent = COLORS["global"] if self.is_global else COLORS["primary"]
        accent_dark = COLORS["global_dark"] if self.is_global else COLORS["primary_dark"]
        header_text = "🌐 保存国际服账号" if self.is_global else "🎮 保存国服账号"

        title = QLabel(header_text)
        title.setFont(QFont("微软雅黑", 14, QFont.Bold))
        title.setStyleSheet(f"color: {accent if self.is_global else COLORS['text']};")
        self.body_layout.addWidget(title)

        account_display = email if email else "未自动识别记忆账号名（保存后仍可正常隔离切换）"
        email_label = QLabel(f"识别标识: {account_display}")
        email_label.setStyleSheet(f"color: {COLORS['text_light']}; font-size: 12px;")
        email_label.setWordWrap(True)
        self.body_layout.addWidget(email_label)

        self.nickname_input = QLineEdit()
        self.nickname_input.setText(default_name)
        self.nickname_input.setPlaceholderText("请输入便于区分的账号备注昵称")
        self.nickname_input.setStyleSheet(
            f"""
            QLineEdit {{
                border: 2px solid {COLORS['border']};
                border-radius: 8px;
                padding: 10px 12px;
                background-color: {COLORS['bg']};
                color: {COLORS['text']};
                font-size: 13px;
            }}
            QLineEdit:focus {{
                border-color: {accent};
            }}
            """
        )
        self.nickname_input.returnPressed.connect(self._on_confirm)
        self.body_layout.addWidget(self.nickname_input)

        hint_text = (
            "🌐 此账号将标记为国际服（启动参数使用设置中的国际服大区）"
            if self.is_global
            else "🇨🇳 此账号将标记为国服（切走前自动回写最新滚动凭证）"
        )
        hint = QLabel(hint_text)
        hint.setStyleSheet(f"color: {accent}; font-size: 11px;")
        self.body_layout.addWidget(hint)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        cancel_btn = QPushButton("取消")
        cancel_btn.setCursor(Qt.PointingHandCursor)
        cancel_btn.setStyleSheet(
            f"""
            QPushButton {{
                background-color: white;
                color: {COLORS['text']};
                border: 1px solid {COLORS['border']};
                border-radius: 8px;
                padding: 9px 26px;
                font-size: 13px;
            }}
            QPushButton:hover {{
                background-color: {COLORS['bg']};
            }}
            """
        )
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        ok_btn = QPushButton("确定")
        ok_btn.setCursor(Qt.PointingHandCursor)
        ok_btn.setDefault(True)
        ok_btn.setStyleSheet(
            f"""
            QPushButton {{
                background-color: {accent};
                color: white;
                border: none;
                border-radius: 8px;
                padding: 9px 26px;
                font-size: 13px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background-color: {accent_dark};
            }}
            """
        )
        ok_btn.clicked.connect(self._on_confirm)
        btn_layout.addWidget(ok_btn)

        self.body_layout.addLayout(btn_layout)
        self.nickname_input.setFocus()
        self.nickname_input.selectAll()

    def _on_confirm(self):
        val = self.nickname_input.text().strip()
        if not val:
            self.nickname_input.setFocus()
            return
        self.result_nickname = val
        self.accept()


class SettingsDialog(BaseFramelessDialog):
    """设置对话框（支持路径自动探测与国际服大区选择）"""

    REGION_OPTIONS = [
        ("KR", "亚服 / 台服 (KR)"),
        ("US", "美服 (US)"),
        ("EU", "欧服 (EU)"),
    ]

    def __init__(self, parent, settings: dict):
        super().__init__(parent, width=520)
        self.settings = settings.copy()
        self._setup_content()

    def _setup_content(self):
        title = QLabel("⚙️ 设置")
        title.setFont(QFont("微软雅黑", 14, QFont.Bold))
        title.setStyleSheet(f"color: {COLORS['text']};")
        self.body_layout.addWidget(title)

        path_label = QLabel("战网启动程序路径 (Battle.net Launcher.exe):")
        path_label.setStyleSheet(f"color: {COLORS['text']}; font-size: 12px;")
        self.body_layout.addWidget(path_label)

        path_layout = QHBoxLayout()
        path_layout.setSpacing(8)
        self.path_input = QLineEdit()
        self.path_input.setText(self.settings.get("battlenet_path") or find_battlenet_exe())
        self.path_input.setStyleSheet(
            f"""
            QLineEdit {{
                border: 2px solid {COLORS['border']};
                border-radius: 8px;
                padding: 8px 12px;
                background-color: {COLORS['bg']};
                color: {COLORS['text']};
                font-size: 12px;
            }}
            QLineEdit:focus {{
                border-color: {COLORS['primary']};
            }}
            """
        )
        path_layout.addWidget(self.path_input, 1)

        detect_btn = QPushButton("自动探测")
        detect_btn.setCursor(Qt.PointingHandCursor)
        detect_btn.setStyleSheet(
            f"""
            QPushButton {{
                background-color: white;
                color: {COLORS['text']};
                border: 1px solid {COLORS['border']};
                border-radius: 8px;
                padding: 8px 12px;
                font-size: 12px;
            }}
            QPushButton:hover {{
                background-color: {COLORS['bg']};
            }}
            """
        )
        detect_btn.clicked.connect(self._auto_detect_path)
        path_layout.addWidget(detect_btn)

        browse_btn = QPushButton("浏览")
        browse_btn.setCursor(Qt.PointingHandCursor)
        browse_btn.setStyleSheet(
            f"""
            QPushButton {{
                background-color: {COLORS['primary']};
                color: white;
                border: none;
                border-radius: 8px;
                padding: 8px 14px;
                font-size: 12px;
            }}
            QPushButton:hover {{
                background-color: {COLORS['primary_dark']};
            }}
            """
        )
        browse_btn.clicked.connect(self._browse_path)
        path_layout.addWidget(browse_btn)
        self.body_layout.addLayout(path_layout)

        region_layout = QHBoxLayout()
        region_label = QLabel("国际服默认启动大区:")
        region_label.setStyleSheet(f"color: {COLORS['text']}; font-size: 12px;")
        region_layout.addWidget(region_label)

        self.region_combo = QComboBox()
        for code, label in self.REGION_OPTIONS:
            self.region_combo.addItem(label, code)
        current_region = self.settings.get("global_region", "KR")
        idx = self.region_combo.findData(current_region)
        if idx >= 0:
            self.region_combo.setCurrentIndex(idx)
        self.region_combo.setStyleSheet(
            f"""
            QComboBox {{
                border: 2px solid {COLORS['border']};
                border-radius: 8px;
                padding: 6px 12px;
                background-color: {COLORS['bg']};
                color: {COLORS['text']};
                font-size: 12px;
                min-width: 160px;
            }}
            QComboBox:focus {{
                border-color: {COLORS['primary']};
            }}
            """
        )
        region_layout.addWidget(self.region_combo)
        region_layout.addStretch()
        self.body_layout.addLayout(region_layout)

        self.hide_email_checkbox = QCheckBox("列表中隐藏账号邮箱 / 手机号中间位")
        self.hide_email_checkbox.setChecked(self.settings.get("hide_email", False))
        self.hide_email_checkbox.setStyleSheet(
            f"""
            QCheckBox {{
                color: {COLORS['text']};
                font-size: 13px;
                spacing: 8px;
            }}
            QCheckBox::indicator {{
                width: 18px;
                height: 18px;
                border-radius: 4px;
                border: 2px solid {COLORS['border']};
            }}
            QCheckBox::indicator:checked {{
                background-color: {COLORS['primary']};
                border-color: {COLORS['primary']};
            }}
            """
        )
        self.body_layout.addWidget(self.hide_email_checkbox)

        about_frame = QFrame()
        about_frame.setStyleSheet(
            f"""
            QFrame {{
                background-color: {COLORS['bg']};
                border-radius: 8px;
                border: 1px solid {COLORS['border']};
            }}
            """
        )
        about_layout = QVBoxLayout(about_frame)
        about_layout.setContentsMargins(12, 10, 12, 10)
        about_layout.setSpacing(4)

        about_label = QLabel(
            f'<div style="line-height:1.5;">'
            f'<b>项目开源地址：</b><a href="{PROJECT_REPO}" style="color:{COLORS["primary_dark"]};">{PROJECT_REPO}</a><br/>'
            f'<span style="color:{COLORS["text_light"]}; font-size:11px;">'
            f'当前版本 v{APP_VERSION} · 基于 <a href="{UPSTREAM_REPO}" style="color:{COLORS["text_light"]};">54006/cnbxqhdl</a> 修复与优化'
            f'</span></div>'
        )
        about_label.setOpenExternalLinks(True)
        about_label.setTextInteractionFlags(Qt.LinksAccessibleByMouse | Qt.TextSelectableByMouse)
        about_label.setWordWrap(True)
        about_layout.addWidget(about_label)
        self.body_layout.addWidget(about_frame)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        cancel_btn = QPushButton("取消")
        cancel_btn.setCursor(Qt.PointingHandCursor)
        cancel_btn.setStyleSheet(
            f"""
            QPushButton {{
                background-color: white;
                color: {COLORS['text']};
                border: 1px solid {COLORS['border']};
                border-radius: 8px;
                padding: 9px 26px;
                font-size: 13px;
            }}
            QPushButton:hover {{
                background-color: {COLORS['bg']};
            }}
            """
        )
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        save_btn = QPushButton("保存")
        save_btn.setCursor(Qt.PointingHandCursor)
        save_btn.setDefault(True)
        save_btn.setStyleSheet(
            f"""
            QPushButton {{
                background-color: {COLORS['primary']};
                color: white;
                border: none;
                border-radius: 8px;
                padding: 9px 26px;
                font-size: 13px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background-color: {COLORS['primary_dark']};
            }}
            """
        )
        save_btn.clicked.connect(self._on_save)
        btn_layout.addWidget(save_btn)

        self.body_layout.addLayout(btn_layout)

    def _auto_detect_path(self):
        self.path_input.setText(find_battlenet_exe())

    def _browse_path(self):
        start_dir = os.path.dirname(self.path_input.text().strip()) or r"C:\Program Files (x86)\Battle.net"
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择战网启动程序",
            start_dir,
            "可执行文件 (*.exe)",
        )
        if path:
            self.path_input.setText(path)

    def _on_save(self):
        self.settings["battlenet_path"] = self.path_input.text().strip()
        self.settings["global_region"] = self.region_combo.currentData() or "KR"
        self.settings["hide_email"] = self.hide_email_checkbox.isChecked()
        self.accept()

    def get_settings(self) -> dict:
        return self.settings


class AccountCard(QFrame):
    """单个账号卡片组件"""

    switch_clicked = pyqtSignal(str)
    delete_clicked = pyqtSignal(str)
    rename_clicked = pyqtSignal(str)
    toggle_version_clicked = pyqtSignal(str)
    update_clicked = pyqtSignal(str)

    def __init__(
        self,
        account_id: str,
        nickname: str,
        has_data: bool,
        is_current: bool = False,
        email: str = "",
        last_login: str = "",
        version: str = "国服",
        hide_email: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self.account_id = account_id
        self.nickname = nickname or "未命名"
        self.has_data = has_data
        self.is_current = is_current
        self.version = version
        self.hide_email = hide_email
        display_email = self.mask_email(email) if (hide_email and email) else email
        self._setup_ui(display_email, last_login)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self.show_context_menu)

    @staticmethod
    def mask_email(value: str) -> str:
        """安全隐藏邮箱、手机号或短用户名中间部分"""
        if not value:
            return ""
        if "@" in value:
            name, domain = value.split("@", 1)
            if len(name) <= 1:
                masked = "*"
            elif len(name) == 2:
                masked = name[0] + "*"
            else:
                masked = name[0] + "*" * min(len(name) - 2, 6) + name[-1]
            return f"{masked}@{domain}"

        n = len(value)
        if n >= 11:
            return value[:3] + "****" + value[-4:]
        if n >= 7:
            return value[:2] + "*" * (n - 4) + value[-2:]
        if n >= 3:
            return value[0] + "*" * (n - 2) + value[-1]
        if n == 2:
            return value[0] + "*"
        return "*"

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.switch_clicked.emit(self.account_id)

    def show_context_menu(self, pos: QPoint):
        menu = QMenu(self)
        menu.setStyleSheet(
            f"""
            QMenu {{
                background-color: white;
                border: 1px solid {COLORS['border']};
                border-radius: 8px;
                padding: 5px;
            }}
            QMenu::item {{
                padding: 8px 18px;
                border-radius: 4px;
                color: {COLORS['text']};
            }}
            QMenu::item:selected {{
                background-color: {COLORS['bg']};
            }}
            """
        )

        primary_label = "🚀 启动并同步最新备份" if self.is_current else "🔄 切换到此账号"
        switch_action = menu.addAction(primary_label)
        update_action = menu.addAction("💾 用当前战网状态更新备份")
        rename_action = menu.addAction("✏️ 重命名")
        toggle_label = "🇨🇳 标记为国服" if self.version == "国际服" else "🌐 标记为国际服"
        toggle_version_action = menu.addAction(toggle_label)

        menu.addSeparator()
        delete_action = menu.addAction("🗑️ 删除账号")

        action = menu.exec_(self.mapToGlobal(pos))
        if action == switch_action:
            self.switch_clicked.emit(self.account_id)
        elif action == update_action:
            self.update_clicked.emit(self.account_id)
        elif action == rename_action:
            self.rename_clicked.emit(self.account_id)
        elif action == toggle_version_action:
            self.toggle_version_clicked.emit(self.account_id)
        elif action == delete_action:
            self.delete_clicked.emit(self.account_id)

    def _setup_ui(self, email: str, last_login: str):
        self.setObjectName("accountCard")
        self.setFixedHeight(120)
        self.setCursor(Qt.PointingHandCursor)

        bg_color = COLORS["card_active"] if self.is_current else COLORS["card"]
        border_rule = f"2px solid {COLORS['primary']}" if self.is_current else "1px solid transparent"
        self.setStyleSheet(
            f"""
            QFrame#accountCard {{
                background-color: {bg_color};
                border-radius: 12px;
                border: {border_rule};
            }}
            QFrame#accountCard:hover {{
                background-color: {COLORS['card_hover']};
            }}
            """
        )

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(10)
        shadow.setColor(QColor(0, 0, 0, 28))
        shadow.setOffset(0, 2)
        self.setGraphicsEffect(shadow)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 14, 20, 14)
        layout.setSpacing(15)

        avatar_bg = COLORS["global"] if self.version == "国际服" else COLORS["primary"]
        avatar = QLabel()
        avatar.setFixedSize(56, 56)
        avatar.setStyleSheet(
            f"""
            background-color: {avatar_bg};
            border-radius: 28px;
            color: white;
            font-size: 22px;
            font-weight: bold;
            """
        )
        avatar.setAlignment(Qt.AlignCenter)
        first_char = self.nickname.strip()[0].upper() if self.nickname.strip() else "?"
        avatar.setText(first_char)
        layout.addWidget(avatar)

        info_layout = QVBoxLayout()
        info_layout.setSpacing(4)

        name_label = QLabel(self.nickname)
        name_label.setStyleSheet(f"color: {COLORS['text']}; font-size: 16px; font-weight: bold;")
        info_layout.addWidget(name_label)

        email_label = QLabel(email or "未记录账号名")
        email_label.setStyleSheet(f"color: {COLORS['text_light']}; font-size: 12px;")
        info_layout.addWidget(email_label)

        time_label = QLabel(f"最后活跃: {last_login}" if last_login else "")
        time_label.setStyleSheet(f"color: {COLORS['text_light']}; font-size: 11px;")
        info_layout.addWidget(time_label)

        layout.addLayout(info_layout, 1)

        tags_layout = QVBoxLayout()
        tags_layout.setSpacing(5)
        tags_layout.setAlignment(Qt.AlignVCenter)

        version_color = COLORS["global"] if self.version == "国际服" else COLORS["primary"]
        version_label = QLabel(self.version)
        version_label.setAlignment(Qt.AlignCenter)
        version_label.setStyleSheet(
            f"""
            color: white;
            background-color: {version_color};
            padding: 3px 9px;
            border-radius: 8px;
            font-size: 10px;
            font-weight: bold;
            """
        )
        version_label.setFixedHeight(22)
        tags_layout.addWidget(version_label)

        if self.is_current and self.has_data:
            status_text = "⚡ 当前归属"
            status_color = COLORS["success"]
        elif self.has_data:
            status_text = "已备份"
            status_color = "#64748b"
        else:
            status_text = "⚠️ 备份不完整"
            status_color = COLORS["warning"]

        status_label = QLabel(status_text)
        status_label.setAlignment(Qt.AlignCenter)
        status_label.setStyleSheet(
            f"""
            color: white;
            background-color: {status_color};
            padding: 3px 9px;
            border-radius: 8px;
            font-size: 10px;
            font-weight: bold;
            """
        )
        status_label.setFixedHeight(22)
        tags_layout.addWidget(status_label)

        layout.addLayout(tags_layout)

        btn_layout = QVBoxLayout()
        btn_layout.setSpacing(5)

        action_btn_text = "启动" if self.is_current else "切换"
        switch_btn = QPushButton(action_btn_text)
        switch_btn.setFixedSize(72, 32)
        if self.is_current:
            switch_btn.setToolTip("同步当前最新滚动凭证并启动战网")
        switch_btn.setStyleSheet(
            f"""
            QPushButton {{
                background-color: {COLORS['success'] if self.is_current else COLORS['primary']};
                color: white;
                border: none;
                border-radius: 6px;
                font-size: 12px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background-color: {'#2f855a' if self.is_current else COLORS['primary_dark']};
            }}
            """
        )
        switch_btn.clicked.connect(lambda: self.switch_clicked.emit(self.account_id))
        btn_layout.addWidget(switch_btn)

        rename_btn = QPushButton("重命名")
        rename_btn.setFixedSize(72, 27)
        rename_btn.setStyleSheet(
            f"""
            QPushButton {{
                background-color: transparent;
                color: {COLORS['text_light']};
                border: 1px solid {COLORS['border']};
                border-radius: 6px;
                font-size: 11px;
            }}
            QPushButton:hover {{
                background-color: {COLORS['border']};
                color: {COLORS['text']};
            }}
            """
        )
        rename_btn.clicked.connect(lambda: self.rename_clicked.emit(self.account_id))
        btn_layout.addWidget(rename_btn)

        delete_btn = QPushButton("删除")
        delete_btn.setFixedSize(72, 27)
        delete_btn.setStyleSheet(
            f"""
            QPushButton {{
                background-color: transparent;
                color: {COLORS['danger']};
                border: 1px solid {COLORS['danger']};
                border-radius: 6px;
                font-size: 11px;
            }}
            QPushButton:hover {{
                background-color: {COLORS['danger']};
                color: white;
            }}
            """
        )
        delete_btn.clicked.connect(lambda: self.delete_clicked.emit(self.account_id))
        btn_layout.addWidget(delete_btn)

        layout.addLayout(btn_layout)


class ModernGUI(QMainWindow):
    """战网会话切换器主窗口"""

    def __init__(self):
        super().__init__()
        self.switcher = IsolatedSwitcher()
        self.active_worker = None
        self._pending_close = False
        self.drag_pos = None
        self.settings = self.load_settings()
        self.apply_settings()
        self.setStyleSheet(STYLESHEET)
        self.setup_ui()
        self.refresh_accounts()
        QTimer.singleShot(0, self._check_startup_health)

    # ------------------------------------------------------------------
    # 启动健康自检、设置持久化与窗口关闭守卫
    # ------------------------------------------------------------------

    def _check_startup_health(self):
        if not self.switcher.dir_writable:
            ModernDialog.show_error(self, "目录不可写", self.switcher.writable_error)
            return
        if self.switcher.index_corrupted:
            ModernDialog.show_error(
                self,
                "账号索引损坏保护",
                f"检测到 {self.switcher.accounts_file} 无法解析，已自动生成 .corrupt 备份并启用只读保护。\n"
                "在修复索引前不会覆盖原有文件，已有账号备份目录均完好保留。",
            )
            return
        if self.switcher.recovery_error:
            ModernDialog.show_warning(
                self,
                "存在待恢复会话材料",
                f"{self.switcher.recovery_error}\n\n请先确保战网客户端完全关闭后重新打开本工具。",
            )
            return
        if self.switcher.sid_mismatch_warning:
            rebind = ModernDialog.show_question(
                self,
                "检测到更换了 Windows 用户或电脑",
                f"{self.switcher.sid_mismatch_warning}\n\n"
                "如果你确认刚刚把旧电脑/旧目录的 data 文件夹迁移到了当前用户下，是否将其重新绑定到当前用户？",
            )
            if rebind:
                ok, msg = self.switcher.rebind_current_user_sid()
                if ok:
                    self.refresh_accounts()
                    ModernDialog.show_success(self, "绑定完成", msg)
                else:
                    ModernDialog.show_error(self, "绑定失败", msg)

    def closeEvent(self, event):
        """后台写事务运行中拦截 Alt+F4 / 系统关闭，等待安全提交或回滚结束后自动关闭"""
        if self.active_worker is not None and self.active_worker.isRunning():
            self._pending_close = True
            self.busy_label.setText("⏳ 正在等待当前会话事务安全完成后退出...")
            event.ignore()
            return
        event.accept()

    def load_settings(self) -> dict:
        defaults = {
            "battlenet_path": self.switcher.BATTLENET_EXE,
            "global_region": "KR",
            "hide_email": False,
        }
        legacy_settings = os.path.join(APP_DIR, "settings.json")
        for path in (SETTINGS_FILE, legacy_settings):
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8-sig") as f:
                        loaded = json.load(f)
                    if isinstance(loaded, dict):
                        raw_path = loaded.get("battlenet_path")
                        if isinstance(raw_path, str):
                            defaults["battlenet_path"] = raw_path.strip()
                        raw_reg = loaded.get("global_region")
                        if raw_reg in ("KR", "US", "EU"):
                            defaults["global_region"] = raw_reg
                        if "hide_email" in loaded:
                            defaults["hide_email"] = bool(loaded.get("hide_email"))
                        if path == legacy_settings and not os.path.exists(SETTINGS_FILE) and self.switcher.dir_writable:
                            self.settings = defaults
                            self.save_settings()
                        break
                except Exception:
                    pass
        return defaults

    def save_settings(self) -> tuple:
        """原子写入设置并返回 (success, error_message)"""
        try:
            IsolatedSwitcher._atomic_write_json(SETTINGS_FILE, self.settings)
            return True, ""
        except Exception as e:
            return False, str(e)

    def apply_settings(self):
        raw_path = self.settings.get("battlenet_path")
        custom_path = raw_path.strip() if isinstance(raw_path, str) else ""
        if custom_path and os.path.isfile(custom_path):
            self.switcher.BATTLENET_EXE = custom_path
        else:
            detected = find_battlenet_exe()
            self.switcher.BATTLENET_EXE = custom_path or detected
        reg = self.settings.get("global_region")
        self.switcher.global_region = reg if reg in ("KR", "US", "EU") else "KR"

    @staticmethod
    def get_icon_path():
        search_dirs = []
        if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
            search_dirs.append(sys._MEIPASS)
        search_dirs.append(APP_DIR)

        for folder in search_dirs:
            for name in ("icon.ico", "006.ico"):
                candidate = os.path.join(folder, name)
                if os.path.exists(candidate):
                    return candidate
        return None

    # ------------------------------------------------------------------
    # 界面构建与无边框拖拽
    # ------------------------------------------------------------------

    def setup_ui(self):
        self.setWindowTitle(f"{APP_TITLE} v{APP_VERSION}")
        self.setWindowFlags(Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMinimumSize(860, 640)

        icon_path = self.get_icon_path()
        if icon_path:
            self.setWindowIcon(QIcon(icon_path))

        container = QWidget()
        self.setCentralWidget(container)
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(10, 10, 10, 10)

        self.main_frame = QFrame()
        self.main_frame.setObjectName("mainFrame")
        self.main_frame.setStyleSheet(
            f"""
            QFrame#mainFrame {{
                background-color: {COLORS['bg']};
                border-radius: 15px;
            }}
            """
        )

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(20)
        shadow.setColor(QColor(0, 0, 0, 60))
        shadow.setOffset(0, 5)
        self.main_frame.setGraphicsEffect(shadow)

        container_layout.addWidget(self.main_frame)

        main_layout = QHBoxLayout(self.main_frame)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        main_layout.addWidget(self.create_sidebar())
        main_layout.addWidget(self.create_content(), 1)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.drag_pos = event.globalPos() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.LeftButton and self.drag_pos is not None:
            self.move(event.globalPos() - self.drag_pos)
            event.accept()

    def mouseReleaseEvent(self, _event):
        self.drag_pos = None

    def create_sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(232)

        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(20, 15, 20, 20)
        layout.setSpacing(12)

        ctrl_layout = QHBoxLayout()
        ctrl_layout.setSpacing(8)

        close_btn = QPushButton("●")
        close_btn.setFixedSize(16, 16)
        close_btn.setToolTip("关闭窗口")
        close_btn.setStyleSheet(
            """
            QPushButton {
                background-color: #ff5f57;
                border: none;
                border-radius: 8px;
                font-size: 8px;
                color: transparent;
            }
            QPushButton:hover {
                color: #800000;
            }
            """
        )
        close_btn.clicked.connect(self.close)
        ctrl_layout.addWidget(close_btn)

        min_btn = QPushButton("●")
        min_btn.setFixedSize(16, 16)
        min_btn.setToolTip("最小化")
        min_btn.setStyleSheet(
            """
            QPushButton {
                background-color: #ffbd2e;
                border: none;
                border-radius: 8px;
                font-size: 8px;
                color: transparent;
            }
            QPushButton:hover {
                color: #805500;
            }
            """
        )
        min_btn.clicked.connect(self.showMinimized)
        ctrl_layout.addWidget(min_btn)
        ctrl_layout.addStretch()
        layout.addLayout(ctrl_layout)

        layout.addSpacing(4)

        logo_label = QLabel()
        icon_path = self.get_icon_path()
        if icon_path:
            logo_pixmap = QPixmap(icon_path).scaled(58, 58, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            logo_label.setPixmap(logo_pixmap)
        else:
            logo_label.setText("🎮")
            logo_label.setStyleSheet("font-size: 44px;")
        logo_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(logo_label)

        title = QLabel(APP_TITLE)
        title.setObjectName("title")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        subtitle = QLabel(f"v{APP_VERSION} · 国服 & 国际服切换")
        subtitle.setObjectName("subtitle")
        subtitle.setAlignment(Qt.AlignCenter)
        layout.addWidget(subtitle)

        layout.addSpacing(14)

        save_btn = QPushButton("💾 保存当前登录")
        save_btn.setFixedHeight(43)
        save_btn.setCursor(Qt.PointingHandCursor)
        save_btn.setStyleSheet(
            f"""
            QPushButton {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 {COLORS['primary']}, stop:1 #6366f1);
                color: white;
                border: none;
                border-radius: 10px;
                font-size: 13px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 {COLORS['primary_dark']}, stop:1 #4f46e5);
            }}
            """
        )
        save_btn.clicked.connect(self.save_current_login)
        layout.addWidget(save_btn)

        save_global_btn = QPushButton("🌐 保存当前登录 (国际服)")
        save_global_btn.setFixedHeight(43)
        save_global_btn.setCursor(Qt.PointingHandCursor)
        save_global_btn.setStyleSheet(
            """
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #9333ea, stop:1 #7c3aed);
                color: white;
                border: none;
                border-radius: 10px;
                font-size: 13px;
                font-weight: bold;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #7e22ce, stop:1 #6d28d9);
            }
            """
        )
        save_global_btn.clicked.connect(self.save_current_login_global)
        layout.addWidget(save_global_btn)

        layout.addSpacing(6)

        new_login_btn = QPushButton("🔄 登录国服新账号")
        new_login_btn.setFixedHeight(38)
        new_login_btn.setCursor(Qt.PointingHandCursor)
        new_login_btn.setStyleSheet(
            f"""
            QPushButton {{
                background-color: {COLORS['secondary']};
                color: white;
                border: none;
                border-radius: 8px;
                font-size: 13px;
                font-weight: 500;
            }}
            QPushButton:hover {{
                background-color: #0284c7;
            }}
            """
        )
        new_login_btn.clicked.connect(lambda: self.prepare_clean_login(is_global=False))
        layout.addWidget(new_login_btn)

        global_login_btn = QPushButton("🌐 登录国际服新账号")
        global_login_btn.setFixedHeight(38)
        global_login_btn.setCursor(Qt.PointingHandCursor)
        global_login_btn.setStyleSheet(
            f"""
            QPushButton {{
                background-color: {COLORS['global']};
                color: white;
                border: none;
                border-radius: 8px;
                font-size: 13px;
                font-weight: 500;
            }}
            QPushButton:hover {{
                background-color: {COLORS['global_dark']};
            }}
            """
        )
        global_login_btn.clicked.connect(lambda: self.prepare_clean_login(is_global=True))
        layout.addWidget(global_login_btn)

        layout.addSpacing(6)

        start_btn = QPushButton("🚀 启动战网")
        start_btn.setFixedHeight(38)
        start_btn.setCursor(Qt.PointingHandCursor)
        start_btn.setStyleSheet(
            f"""
            QPushButton {{
                background-color: {COLORS['success']};
                color: white;
                border: none;
                border-radius: 8px;
                font-size: 13px;
                font-weight: 500;
            }}
            QPushButton:hover {{
                background-color: #2f855a;
            }}
            """
        )
        start_btn.clicked.connect(self.start_battlenet)
        layout.addWidget(start_btn)

        close_battlenet_btn = QPushButton("⏹️ 关闭战网")
        close_battlenet_btn.setFixedHeight(38)
        close_battlenet_btn.setCursor(Qt.PointingHandCursor)
        close_battlenet_btn.setStyleSheet(
            """
            QPushButton {
                background-color: #ef4444;
                color: white;
                border: none;
                border-radius: 8px;
                font-size: 13px;
                font-weight: 500;
            }
            QPushButton:hover {
                background-color: #dc2626;
            }
            """
        )
        close_battlenet_btn.clicked.connect(self.close_battlenet)
        layout.addWidget(close_battlenet_btn)

        settings_btn = QPushButton("⚙️ 设置")
        settings_btn.setFixedHeight(38)
        settings_btn.setCursor(Qt.PointingHandCursor)
        settings_btn.setStyleSheet(
            f"""
            QPushButton {{
                background-color: {COLORS['bg']};
                color: {COLORS['text']};
                border: 1px solid {COLORS['border']};
                border-radius: 8px;
                font-size: 13px;
                font-weight: 500;
            }}
            QPushButton:hover {{
                background-color: {COLORS['border']};
            }}
            """
        )
        settings_btn.clicked.connect(self.open_settings)
        layout.addWidget(settings_btn)

        layout.addStretch()

        admin_ok = is_admin()
        priv_text = "🛡️ 管理员权限" if admin_ok else "🛡️ 当前用户权限 (推荐)"
        priv_color = COLORS["primary_dark"] if admin_ok else COLORS["success"]
        priv_label = QLabel(priv_text)
        priv_label.setStyleSheet(f"color: {priv_color}; font-size: 11px;")
        priv_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(priv_label)

        footer = QLabel(
            f'<div style="line-height:1.45;">'
            f'<a href="{PROJECT_REPO}" style="color:{COLORS["primary_dark"]}; text-decoration:none; font-weight:bold;">'
            f'🔗 GitHub 项目主页</a><br/>'
            f'<span>TRAAACER/Bnet-session-switcher</span><br/>'
            f'<span>v{APP_VERSION} · 基于 54006/cnbxqhdl 优化</span>'
            f'</div>'
        )
        footer.setStyleSheet(f"color: {COLORS['text_light']}; font-size: 10px;")
        footer.setAlignment(Qt.AlignCenter)
        footer.setOpenExternalLinks(True)
        footer.setCursor(Qt.PointingHandCursor)
        footer.setToolTip(f"点击打开项目主页：{PROJECT_REPO}")
        footer.setWordWrap(True)
        layout.addWidget(footer)

        return sidebar

    def create_content(self) -> QWidget:
        content = QWidget()
        content.setObjectName("contentArea")
        content.setStyleSheet(
            f"""
            QWidget#contentArea {{
                background-color: {COLORS['bg']};
                border-top-right-radius: 15px;
                border-bottom-right-radius: 15px;
            }}
            """
        )
        layout = QVBoxLayout(content)
        layout.setContentsMargins(28, 26, 28, 24)
        layout.setSpacing(16)

        header = QHBoxLayout()
        title = QLabel("账号列表")
        title.setStyleSheet(f"color: {COLORS['text']}; font-size: 22px; font-weight: bold;")
        header.addWidget(title)

        self.busy_label = QLabel("")
        self.busy_label.setStyleSheet(f"color: {COLORS['primary_dark']}; font-size: 12px; font-weight: bold;")
        header.addWidget(self.busy_label)

        header.addStretch()

        self.count_label = QLabel("共 0 个账号")
        self.count_label.setStyleSheet(f"color: {COLORS['text_light']}; font-size: 13px;")
        header.addWidget(self.count_label)

        refresh_btn = QPushButton("🔄 刷新")
        refresh_btn.setObjectName("secondaryBtn")
        refresh_btn.setCursor(Qt.PointingHandCursor)
        refresh_btn.clicked.connect(self.refresh_accounts)
        header.addWidget(refresh_btn)

        layout.addLayout(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self.scroll_content = QWidget()
        self.scroll_content.setObjectName("scrollContent")
        self.cards_layout = QVBoxLayout(self.scroll_content)
        self.cards_layout.setContentsMargins(0, 0, 8, 0)
        self.cards_layout.setSpacing(14)
        self.cards_layout.addStretch()

        scroll.setWidget(self.scroll_content)
        layout.addWidget(scroll, 1)

        help_frame = QFrame()
        help_frame.setStyleSheet(
            f"""
            QFrame {{
                background-color: {COLORS['white']};
                border-radius: 10px;
                border: 1px solid {COLORS['border']};
            }}
            """
        )
        help_layout = QVBoxLayout(help_frame)
        help_layout.setContentsMargins(15, 10, 15, 10)
        help_layout.setSpacing(4)

        help_title = QLabel("💡 使用说明")
        help_title.setStyleSheet(f"color: {COLORS['text']}; font-size: 12px; font-weight: bold;")
        help_layout.addWidget(help_title)

        help_text = QLabel(
            "添加新号：点击【登录国服/国际服新账号】登录战网（勾选保持登录）后点击【保存当前登录】\n"
            "日常切号：双击卡片或点击【切换/启动】（切走前会自动同步已识别活跃账号的最新凭证；右键卡片可修改区服标记或更新备份）"
        )
        help_text.setStyleSheet(f"color: {COLORS['text_light']}; font-size: 11px;")
        help_text.setWordWrap(True)
        help_layout.addWidget(help_text)

        layout.addWidget(help_frame)
        return content

    # ------------------------------------------------------------------
    # 异步任务调度（严格等待 QThread.finished 后释放并响应挂起关闭）
    # ------------------------------------------------------------------

    def _run_background_task(self, status_text: str, fn, on_done, *args, **kwargs):
        if self.active_worker is not None and self.active_worker.isRunning():
            return
        self.busy_label.setText(f"⏳ {status_text}")
        self.main_frame.setEnabled(False)
        QApplication.setOverrideCursor(Qt.WaitCursor)

        worker = TaskThread(fn, *args, **kwargs)
        self.active_worker = worker

        def _on_thread_finished():
            self.busy_label.setText("")
            self.main_frame.setEnabled(True)
            QApplication.restoreOverrideCursor()
            self.active_worker = None
            worker.deleteLater()

            if self._pending_close:
                self._pending_close = False
                self.close()
                return

            if worker.outcome_ok:
                on_done(worker.outcome_value)
            else:
                ModernDialog.show_error(self, "操作异常", f"执行过程中出现异常：\n{worker.outcome_error}")
                self.refresh_accounts()

        worker.finished.connect(_on_thread_finished, Qt.QueuedConnection)
        worker.start()

    def _find_existing_account_by_email(self, email: str, version: str):
        """按 (认证域 cn/global + 归一化邮箱/手机号) 联合判重，避免同邮箱跨区服互相覆盖"""
        norm = (email or "").strip().lower()
        if not norm:
            return None, None
        for acc_id, acc_info in self.switcher.accounts.items():
            if not isinstance(acc_info, dict):
                continue
            existing_ver = acc_info.get("version", "cn")
            existing_norm = (acc_info.get("email") or "").strip().lower()
            if existing_ver == version and existing_norm and existing_norm == norm:
                return acc_id, acc_info
        return None, None

    # ------------------------------------------------------------------
    # 账号列表刷新与交互逻辑
    # ------------------------------------------------------------------

    def refresh_accounts(self):
        while self.cards_layout.count() > 1:
            item = self.cards_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        accounts = self.switcher.get_all_accounts()
        for acc in accounts:
            last_login = acc.get("last_login", "")
            if last_login:
                try:
                    dt = datetime.fromisoformat(last_login)
                    last_login = dt.strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    pass

            acc_raw = self.switcher.accounts.get(acc["id"], {})
            email = acc_raw.get("email", "")
            version = acc_raw.get("version", "cn")
            version_text = "国际服" if version == "global" else "国服"

            card = AccountCard(
                account_id=acc["id"],
                nickname=acc["nickname"],
                has_data=acc["has_data"],
                is_current=acc.get("is_current", False),
                email=email,
                last_login=last_login,
                version=version_text,
                hide_email=self.settings.get("hide_email", False),
            )
            card.switch_clicked.connect(self.switch_account)
            card.delete_clicked.connect(self.delete_account)
            card.rename_clicked.connect(self.rename_account)
            card.toggle_version_clicked.connect(self.toggle_version)
            card.update_clicked.connect(self.update_account_data)

            self.cards_layout.insertWidget(self.cards_layout.count() - 1, card)

        self.count_label.setText(f"共 {len(accounts)} 个账号")

    def prepare_clean_login(self, is_global: bool = False):
        server_name = "国际服" if is_global else "国服"
        save_btn_name = "【保存当前登录 (国际服)】" if is_global else "【保存当前登录】"

        has_unsaved, temp_ident = self.switcher.has_unsaved_temp_login()
        unsaved_warn = (
            f"⚠️ 注意：当前存在尚未保存的临时登录会话（{temp_ident}），继续操作将清除该未保存会话！\n\n"
            if has_unsaved
            else ""
        )

        reply = ModernDialog.show_question(
            self,
            f"登录{server_name}新账号",
            f"{unsaved_warn}"
            f"即将关闭战网，保护当前已保存活跃账号的最新状态，并重置本机为干净的{server_name}登录环境。\n\n"
            f"战网启动后请登录新账号（建议勾选“保持登录”），\n"
            f"进入主界面后点击左侧 {save_btn_name} 即可。",
        )
        if not reply:
            return

        region = self.settings.get("global_region", "KR") if is_global else "CN"

        def _task():
            ok, res = self.switcher.prepare_for_new_login()
            if not ok:
                return False, res
            started = self.switcher.start_battlenet(region=region)
            return True, started

        def _done(result):
            ok, payload = result
            self.refresh_accounts()
            if not ok:
                ModernDialog.show_error(self, "准备失败", str(payload))
                return
            started_hint = "战网已启动。" if payload else "未能自动启动战网，请手动打开战网。"
            ModernDialog.show_info(
                self,
                "准备完成",
                f"已重置为干净的{server_name}登录环境，{started_hint}\n\n"
                f"请在战网中完成登录，主界面加载后点击 {save_btn_name}。",
            )

        self._run_background_task(f"正在准备{server_name}干净登录环境...", _task, _done)

    def _save_login_common(self, is_global: bool):
        server_name = "国际服" if is_global else "国服"
        force_version = "global" if is_global else "cn"

        def _detect_task():
            running = self.switcher.is_battlenet_client_open()
            info = self.switcher.get_current_logged_account() if running else {}
            logout_flag = self.switcher.has_recent_logout_signal() if running else False
            return running, info, logout_flag

        def _after_detect(res):
            running, account_info, logout_flag = res
            if not running:
                ModernDialog.show_info(self, "提示", f"请先启动战网并登录{server_name}账号")
                return

            raw_cfg_email = (account_info.get("email") or "").strip()
            battletag = (account_info.get("battletag") or "").strip()
            if not is_global and not raw_cfg_email and not battletag:
                ModernDialog.show_info(self, "提示", "无法识别当前登录的账号\n\n请确保已在战网客户端中完成登录并进入主界面")
                return

            logout_warn = (
                "⚠️ 检测到战网近期日志包含退出登录记录，请务必确认客户端当前已重新进入主界面（而非停留在登录框）！\n\n"
                if logout_flag
                else ""
            )

            default_name = battletag or account_info.get("account_name") or ("" if is_global else "新账号")
            display_ident = (raw_cfg_email or battletag) if is_global else raw_cfg_email

            existing_id, existing_info = self._find_existing_account_by_email(display_ident, force_version)
            if existing_id:
                reply = ModernDialog.show_question(
                    self,
                    "同区服账号已存在",
                    f"{logout_warn}"
                    f"{server_name}账号 {display_ident} 已存在（昵称：【{existing_info.get('nickname')}】）。\n\n"
                    "是否用当前战网登录状态更新该账号备份？",
                )
                if reply:
                    def _update_done(upd_res):
                        ok, msg = upd_res
                        self.refresh_accounts()
                        if ok:
                            ModernDialog.show_success(self, "更新成功", f"账号【{existing_info.get('nickname')}】备份已更新")
                        else:
                            ModernDialog.show_error(self, "更新失败", f"{msg}\n\n调试日志目录: {LOGS_DIR}")

                    self._run_background_task(
                        "正在更新账号备份...",
                        self.switcher.update_account_data,
                        _update_done,
                        existing_id,
                    )
                return

            if logout_flag:
                if not ModernDialog.show_question(
                    self,
                    "检测到近期退出记录",
                    f"{logout_warn}是否仍要将当前战网状态保存为新账号？",
                ):
                    return

            dialog = SaveAccountDialog(self, display_ident, default_name, is_global=is_global)
            if dialog.exec_() != QDialog.Accepted or not dialog.result_nickname:
                return

            nickname = dialog.result_nickname

            def _save_task():
                acc_id = self.switcher.create_account_from_current(
                    nickname=nickname,
                    force_version=force_version,
                    expected_email=raw_cfg_email,
                    expected_battletag=battletag,
                )
                return acc_id, self.switcher.last_error

            def _save_done(save_res):
                acc_id, err = save_res
                self.refresh_accounts()
                if acc_id:
                    ModernDialog.show_success(self, "保存成功", f"已保存{server_name}账号【{nickname}】")
                else:
                    ModernDialog.show_error(self, "保存失败", f"{err or '保存账号数据失败'}\n\n调试日志目录: {LOGS_DIR}")

            self._run_background_task(f"正在保存{server_name}账号【{nickname}】...", _save_task, _save_done)

        self._run_background_task("正在读取当前战网登录信息...", _detect_task, _after_detect)

    def save_current_login(self):
        self._save_login_common(is_global=False)

    def save_current_login_global(self):
        self._save_login_common(is_global=True)

    def switch_account(self, account_id: str):
        acc_info = self.switcher.accounts.get(account_id, {})
        nickname = acc_info.get("nickname", "未知")
        is_current = account_id == self.switcher.current_account_id
        action_text = f"正在同步并启动【{nickname}】..." if is_current else f"正在切换到【{nickname}】..."

        def _done(res):
            success, msg = res
            self.refresh_accounts()
            if success:
                ModernDialog.show_success(
                    self,
                    "操作完成",
                    f"{msg}\n\n若本地凭证仍在有效期内将自动进入主界面；若已过期请手动登录一次并更新备份。",
                )
            else:
                ModernDialog.show_error(self, "切换失败", f"{msg}\n\n调试日志目录: {LOGS_DIR}")

        self._run_background_task(action_text, self.switcher.switch_to_account, _done, account_id)

    def rename_account(self, account_id: str):
        acc_info = self.switcher.accounts.get(account_id, {})
        old_nickname = acc_info.get("nickname", "未知")

        new_nickname, ok = ModernInputDialog.get_text(
            self,
            "重命名账号",
            f"当前昵称: {old_nickname}\n请输入新的账号备注昵称：",
            old_nickname,
        )
        if ok and new_nickname and new_nickname != old_nickname:
            saved_ok, res = self.switcher.rename_account_checked(account_id, new_nickname)
            self.refresh_accounts()
            if saved_ok:
                ModernDialog.show_success(self, "成功", f"账号已重命名为【{res}】")
            else:
                ModernDialog.show_error(self, "重命名失败", res)

    def toggle_version(self, account_id: str):
        acc_info = self.switcher.accounts.get(account_id, {})
        nickname = acc_info.get("nickname", "未知")

        saved_ok, res = self.switcher.toggle_version_checked(account_id)
        self.refresh_accounts()
        if saved_ok:
            new_version_text = "国际服" if res == "global" else "国服"
            ModernDialog.show_success(self, "区服标记已更新", f"账号【{nickname}】已标记为 {new_version_text}")
        else:
            ModernDialog.show_error(self, "修改失败", res)

    def update_account_data(self, account_id: str):
        acc_info = self.switcher.accounts.get(account_id, {})
        nickname = acc_info.get("nickname", "未知")

        if not self.switcher.is_battlenet_client_open():
            ModernDialog.show_info(self, "提示", "请先启动战网并登录该账号后再执行更新")
            return

        logout_warn = (
            "⚠️ 检测到战网近期日志包含退出登录记录，请先确认客户端当前已进入主界面（而非处于退出登录页），以免用退出态覆盖备份！\n\n"
            if self.switcher.has_recent_logout_signal()
            else ""
        )

        reply = ModernDialog.show_question(
            self,
            "更新账号备份",
            f"{logout_warn}"
            f"确定要用当前战网的登录状态覆盖账号【{nickname}】的备份吗？\n\n"
            "⚠️ 请确认当前战网正在登录的就是该账号。",
        )
        if not reply:
            return

        def _done(res):
            ok, msg = res
            self.refresh_accounts()
            if ok:
                ModernDialog.show_success(self, "更新成功", f"账号【{nickname}】数据已更新")
            else:
                ModernDialog.show_error(self, "更新失败", f"{msg}\n\n调试日志目录: {LOGS_DIR}")

        self._run_background_task(f"正在更新【{nickname}】备份...", self.switcher.update_account_data, _done, account_id)

    def delete_account(self, account_id: str):
        acc_info = self.switcher.accounts.get(account_id, {})
        nickname = acc_info.get("nickname", "未知")

        reply = ModernDialog.show_question(
            self,
            "确认删除",
            f"确定要删除账号【{nickname}】吗？\n\n这将永久删除该账号保存的本地隔离备份数据。",
        )
        if not reply:
            return

        def _done(res):
            ok, msg = res
            self.refresh_accounts()
            if not ok:
                ModernDialog.show_error(self, "删除失败", msg)
            elif "下次启动时" in str(msg):
                ModernDialog.show_info(self, "已移除", msg)

        self._run_background_task(f"正在删除【{nickname}】...", self.switcher.delete_account_checked, _done, account_id)

    def start_battlenet(self):
        if not self.switcher.start_battlenet():
            ModernDialog.show_error(self, "启动失败", "未找到战网启动程序，请在【⚙️ 设置】中检查或指定 Battle.net Launcher.exe 路径。")

    def close_battlenet(self):
        def _task():
            if not self.switcher.is_battlenet_running():
                return "not_running", ""
            ok, msg, _ = self.switcher.close_battlenet_checked()
            return ("closed" if ok else "failed"), msg

        def _done(res):
            state, msg = res
            if state == "not_running":
                ModernDialog.show_info(self, "提示", "当前未检测到正在运行的战网进程。")
            elif state == "closed":
                ModernDialog.show_info(self, "已关闭", "战网及相关启动器进程已关闭。")
            else:
                ModernDialog.show_error(self, "关闭失败", msg)

        self._run_background_task("正在关闭战网进程...", _task, _done)

    def open_settings(self):
        dialog = SettingsDialog(self, self.settings)
        if dialog.exec_() == QDialog.Accepted:
            old_settings = self.settings.copy()
            self.settings = dialog.get_settings()
            ok, err = self.save_settings()
            if ok:
                self.apply_settings()
                self.refresh_accounts()
                ModernDialog.show_success(self, "成功", "设置已保存")
            else:
                self.settings = old_settings
                ModernDialog.show_error(self, "保存设置失败", f"无法写入设置文件：\n{err}")


def relaunch_as_admin() -> bool:
    """按需安全提权重启（使用绝对脚本路径，防止父目录启动时重复拼接相对路径）"""
    try:
        if getattr(sys, "frozen", False):
            cmd_args = sys.argv[1:]
        else:
            cmd_args = [os.path.abspath(__file__)] + sys.argv[1:]
        params = subprocess.list2cmdline(cmd_args) if cmd_args else None
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, params, APP_DIR, 1
        )
        return int(ret) > 32
    except Exception:
        return False


def main():
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_TITLE)
    app.setFont(QFont("微软雅黑", 10))

    instance_lock = SingleInstanceLock()
    if not instance_lock.acquire():
        ModernDialog.show_warning(
            None,
            "程序已在运行",
            f"{APP_TITLE} 已有一个实例正在运行中。\n为防止并发读写损坏战网会话备份，请勿同时开启多个实例。",
        )
        sys.exit(1)

    app.aboutToQuit.connect(instance_lock.release)

    window = ModernGUI()
    window.show()

    exit_code = app.exec_()
    instance_lock.release()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
