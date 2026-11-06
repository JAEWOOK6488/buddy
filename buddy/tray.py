"""Tray icon (AppIndicator area on Ubuntu) with buddy's settings menu."""

from pathlib import Path

from PyQt6.QtCore import QObject, QPointF, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QActionGroup, QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import QMenu, QSystemTrayIcon

from .config import AVAILABLE_MODELS


APP_ICON_PATH = Path(__file__).resolve().parent / "assets" / "buddy.svg"


def _create_buddy_icon() -> QIcon:
    """The app icon; falls back to drawing the blue arrowhead if the asset is missing."""
    if APP_ICON_PATH.is_file():
        return QIcon(str(APP_ICON_PATH))
    icon_size = 64
    icon_pixmap = QPixmap(icon_size, icon_size)
    icon_pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(icon_pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    arrow_path = QPainterPath()
    arrow_path.moveTo(QPointF(12, 10))
    arrow_path.lineTo(QPointF(54, 30))
    arrow_path.lineTo(QPointF(36, 36))
    arrow_path.lineTo(QPointF(28, 54))
    arrow_path.closeSubpath()
    painter.setPen(QPen(QColor(255, 255, 255), 3))
    painter.setBrush(QColor(59, 130, 246))
    painter.drawPath(arrow_path)
    painter.end()

    return QIcon(icon_pixmap)


class BuddyTray(QObject):
    open_notch_requested = pyqtSignal()
    stop_answer_requested = pyqtSignal()
    buddy_cursor_toggled = pyqtSignal(bool)
    speech_toggled = pyqtSignal(bool)
    model_selected = pyqtSignal(str)
    clear_conversation_requested = pyqtSignal()
    quit_requested = pyqtSignal()

    def __init__(
        self, hotkey_display_text: str, stop_hotkey_display_text: str, selected_model: str, speech_enabled: bool
    ):
        super().__init__()
        self._tray_icon = QSystemTrayIcon(_create_buddy_icon())
        self._tray_icon.setToolTip(f"buddy — {hotkey_display_text} to ask")

        tray_menu = QMenu()

        open_notch_action = QAction(f"Ask buddy  ({hotkey_display_text})", tray_menu)
        open_notch_action.triggered.connect(self.open_notch_requested.emit)
        tray_menu.addAction(open_notch_action)

        stop_answer_action = QAction(f"Stop answer  ({stop_hotkey_display_text})", tray_menu)
        stop_answer_action.triggered.connect(self.stop_answer_requested.emit)
        tray_menu.addAction(stop_answer_action)
        tray_menu.addSeparator()

        buddy_cursor_action = QAction("Show buddy cursor", tray_menu)
        buddy_cursor_action.setCheckable(True)
        buddy_cursor_action.setChecked(True)
        buddy_cursor_action.toggled.connect(self.buddy_cursor_toggled.emit)
        tray_menu.addAction(buddy_cursor_action)

        speech_action = QAction("Speak answers", tray_menu)
        speech_action.setCheckable(True)
        speech_action.setChecked(speech_enabled)
        speech_action.toggled.connect(self.speech_toggled.emit)
        tray_menu.addAction(speech_action)

        model_menu = tray_menu.addMenu("Model")
        model_action_group = QActionGroup(model_menu)
        model_action_group.setExclusive(True)
        for model_id, model_label in AVAILABLE_MODELS.items():
            model_action = QAction(model_label, model_menu)
            model_action.setCheckable(True)
            model_action.setChecked(model_id == selected_model)
            model_action.triggered.connect(lambda _checked, chosen_model=model_id: self.model_selected.emit(chosen_model))
            model_action_group.addAction(model_action)
            model_menu.addAction(model_action)

        clear_action = QAction("Clear conversation", tray_menu)
        clear_action.triggered.connect(self.clear_conversation_requested.emit)
        tray_menu.addAction(clear_action)
        tray_menu.addSeparator()

        quit_action = QAction("Quit buddy", tray_menu)
        quit_action.triggered.connect(self.quit_requested.emit)
        tray_menu.addAction(quit_action)

        # Keep references: Qt does not own the menu through the tray icon.
        self._tray_menu = tray_menu
        self._model_action_group = model_action_group
        self._tray_icon.setContextMenu(tray_menu)

    @staticmethod
    def is_available() -> bool:
        return QSystemTrayIcon.isSystemTrayAvailable()

    def show(self) -> None:
        self._tray_icon.show()

    def hide(self) -> None:
        self._tray_icon.hide()

    def show_message(self, title: str, message: str) -> None:
        self._tray_icon.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, 6000)
