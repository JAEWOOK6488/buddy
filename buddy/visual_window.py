"""Frameless, transparent window that shows a model-written visual (HTML page) floating
over the desktop: 3D scenes, grid-paper explanation cards, diagrams.

The page runs model-written JavaScript, so it is sandboxed:
- served from the private buddy:// scheme (the page, the bundled libraries and fonts);
- every request to any other scheme (http, https, file, ws, ...) is blocked;
- navigation away from the page and new windows are refused;
- an off-the-record profile: no cookies, cache or storage on disk.
"""

import json
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QRect, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QCursor, QGuiApplication, QKeySequence, QShortcut
from PyQt6.QtWidgets import QVBoxLayout, QWidget

try:
    from PyQt6.QtWebEngineCore import (
        QWebEnginePage,
        QWebEngineProfile,
        QWebEngineSettings,
        QWebEngineUrlRequestInterceptor,
        QWebEngineUrlRequestJob,
        QWebEngineUrlScheme,
        QWebEngineUrlSchemeHandler,
    )
    from PyQt6.QtWebEngineWidgets import QWebEngineView

    WEB_ENGINE_IMPORT_ERROR: Optional[str] = None
except ImportError as import_error:  # PyQt6-WebEngine not installed or Qt version mismatch
    WEB_ENGINE_IMPORT_ERROR = str(import_error)

VISUAL_SCHEME = b"buddy"
VISUAL_HOST = "visual"
VISUAL_PAGE_URL = QUrl("buddy://visual/index.html")
CLOSE_REQUEST_PATH = "/close"
WEB_DIRECTORY = Path(__file__).resolve().parent / "web"
# Request paths -> directories they may read from.
SERVED_DIRECTORIES = {
    "/vendor/": WEB_DIRECTORY / "vendor",
    "/kit/": WEB_DIRECTORY / "kit",
}
CONTENT_TYPES = {
    ".js": b"text/javascript",
    ".css": b"text/css",
    ".ttf": b"font/ttf",
    ".html": b"text/html",
}
ALLOWED_REQUEST_SCHEMES = {"buddy", "data", "blob", "about"}
BLANK_PAGE_URL = QUrl("about:blank")

# Fraction of the cursor screen the visual covers.
WINDOW_WIDTH_FRACTION = 0.62
WINDOW_HEIGHT_FRACTION = 0.74

# 2D visuals (no canvas): measured after load so the window can shrink to the content and
# stop covering the desktop below it. Measured twice because Mermaid renders asynchronously.
CONTENT_MEASURE_DELAYS_MILLISECONDS = (500, 1600)
MEASURE_CONTENT_SCRIPT = """(() => {
  if (document.querySelector("canvas")) return null;
  let bottom = 0;
  for (const element of document.body.children) {
    if (element.id === "buddy-clear" || element.id === "buddy-error") continue;
    bottom = Math.max(bottom, element.getBoundingClientRect().bottom);
  }
  return bottom;
})()"""
CONTENT_BOTTOM_MARGIN_PIXELS = 24
MINIMUM_WINDOW_HEIGHT_PIXELS = 120

IMPORT_MAP = {
    "imports": {
        "three": "buddy://visual/vendor/three/three.module.min.js",
        "three/addons/": "buddy://visual/vendor/three/addons/",
        "buddy-kit": "buddy://visual/kit/buddy-kit.js",
    }
}

# Added before the model's own markup: charset, import map, base style, error reporting.
PAGE_HEAD_PREFIX = (
    '<meta charset="utf-8">'
    f'<script type="importmap">{json.dumps(IMPORT_MAP)}</script>'
    '<link rel="stylesheet" href="buddy://visual/kit/buddy-kit.css">'
    "<script>"
    "function buddyShowError(message){var note=document.getElementById('buddy-error');"
    "if(!note){note=document.createElement('div');note.id='buddy-error';(document.body||document.documentElement).appendChild(note);}"
    "note.textContent='visual error: '+message;console.error(message);}"
    "window.addEventListener('error',function(event){buddyShowError(event.message||String(event.error));});"
    "window.addEventListener('unhandledrejection',function(event){buddyShowError(String(event.reason));});"
    "</script>"
)
CLEAR_BUTTON_HTML = (
    "<div id=\"buddy-clear\" title=\"Esc\" onclick=\"location.href='buddy://visual/close'\">× Clear</div>"
)


def is_web_engine_available() -> bool:
    return WEB_ENGINE_IMPORT_ERROR is None


def register_visual_url_scheme() -> None:
    """Must run before the QApplication is created."""
    if not is_web_engine_available():
        return
    url_scheme = QWebEngineUrlScheme(VISUAL_SCHEME)
    url_scheme.setSyntax(QWebEngineUrlScheme.Syntax.Host)
    url_scheme.setFlags(
        # Not LocalScheme: that would make buddy:// count as file URLs, which the
        # page settings below forbid, so the page could not load its own libraries.
        QWebEngineUrlScheme.Flag.SecureScheme | QWebEngineUrlScheme.Flag.CorsEnabled
    )
    QWebEngineUrlScheme.registerScheme(url_scheme)


def build_visual_page(model_html: str) -> str:
    """Wraps the model's markup with buddy's head prefix and the close pill."""
    lowered_html = model_html.lower()
    head_index = lowered_html.find("<head>")
    if head_index != -1:
        insert_at = head_index + len("<head>")
        page = model_html[:insert_at] + PAGE_HEAD_PREFIX + model_html[insert_at:]
    elif "<html" in lowered_html:
        html_tag_end = lowered_html.find(">", lowered_html.find("<html")) + 1
        page = model_html[:html_tag_end] + "<head>" + PAGE_HEAD_PREFIX + "</head>" + model_html[html_tag_end:]
    else:
        page = "<!doctype html><html><head>" + PAGE_HEAD_PREFIX + "</head><body>" + model_html + "</body></html>"

    body_close_index = page.lower().rfind("</body>")
    if body_close_index != -1:
        return page[:body_close_index] + CLEAR_BUTTON_HTML + page[body_close_index:]
    return page + CLEAR_BUTTON_HTML


if is_web_engine_available():

    class _VisualSchemeHandler(QWebEngineUrlSchemeHandler):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.page_html = b""

        def requestStarted(self, job: "QWebEngineUrlRequestJob") -> None:  # noqa: N802 - Qt override
            request_path = job.requestUrl().path()
            if job.requestUrl().host() != VISUAL_HOST:
                job.fail(QWebEngineUrlRequestJob.Error.UrlNotFound)
                return
            if request_path == "/index.html":
                self._reply(job, b"text/html", self.page_html)
                return
            for path_prefix, directory in SERVED_DIRECTORIES.items():
                if not request_path.startswith(path_prefix):
                    continue
                file_path = (directory / request_path[len(path_prefix):]).resolve()
                # Only files inside the served directory (no "..").
                if directory.resolve() in file_path.parents and file_path.is_file():
                    content_type = CONTENT_TYPES.get(file_path.suffix, b"application/octet-stream")
                    self._reply(job, content_type, file_path.read_bytes())
                    return
            job.fail(QWebEngineUrlRequestJob.Error.UrlNotFound)

        @staticmethod
        def _reply(job, content_type: bytes, body: bytes) -> None:
            reply_buffer = QBuffer(job)
            reply_buffer.setData(QByteArray(body))
            reply_buffer.open(QIODevice.OpenModeFlag.ReadOnly)
            job.reply(content_type, reply_buffer)

    class _BlockOutsideRequests(QWebEngineUrlRequestInterceptor):
        def interceptRequest(self, info) -> None:  # noqa: N802 - Qt override
            if info.requestUrl().scheme() not in ALLOWED_REQUEST_SCHEMES:
                info.block(True)

    class _VisualPage(QWebEnginePage):
        close_requested = pyqtSignal()

        def acceptNavigationRequest(self, url, navigation_type, is_main_frame) -> bool:  # noqa: N802
            if url.scheme() == "buddy" and url.path() == CLOSE_REQUEST_PATH:
                self.close_requested.emit()
                return False
            # Only the visual itself may load; links and redirects elsewhere are refused.
            return url == VISUAL_PAGE_URL or url == BLANK_PAGE_URL

        def createWindow(self, window_type):  # noqa: N802 - no popups
            return None

        def javaScriptConsoleMessage(self, level, message, line_number, source_id) -> None:  # noqa: N802
            if level == QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel:
                print(f"buddy visual: {message} ({source_id}:{line_number})")


class VisualWindow(QWidget):
    """One reusable window; showing a new visual replaces the previous one."""

    closed = pyqtSignal()

    def __init__(self):
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle("buddy visual")

        self._profile = QWebEngineProfile(self)  # no storage name -> off the record
        self._scheme_handler = _VisualSchemeHandler(self)
        self._profile.installUrlSchemeHandler(VISUAL_SCHEME, self._scheme_handler)
        self._request_interceptor = _BlockOutsideRequests(self)
        self._profile.setUrlRequestInterceptor(self._request_interceptor)

        settings = self._profile.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanAccessClipboard, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.ShowScrollBars, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.WebGLEnabled, True)

        self._page = _VisualPage(self._profile, self)
        self._page.setBackgroundColor(QColor(Qt.GlobalColor.transparent))
        self._page.close_requested.connect(self.close_visual)
        self._page.loadFinished.connect(self._on_load_finished)
        self._full_height = 0

        self._web_view = QWebEngineView(self)
        self._web_view.setPage(self._page)
        self._web_view.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._web_view.setStyleSheet("background: transparent;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._web_view)

        close_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        close_shortcut.activated.connect(self.close_visual)

    def show_visual(self, model_html: str, title: str) -> None:
        self.setWindowTitle(f"buddy — {title}")
        self._scheme_handler.page_html = build_visual_page(model_html).encode("utf-8")
        self._place_on_cursor_screen()
        # Reload even if the URL is the same, so the new page replaces the old one.
        self._page.load(VISUAL_PAGE_URL)
        self.show()
        self.raise_()
        self.activateWindow()

    def _place_on_cursor_screen(self) -> None:
        cursor_screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        available = cursor_screen.availableGeometry()
        width = int(available.width() * WINDOW_WIDTH_FRACTION)
        height = int(available.height() * WINDOW_HEIGHT_FRACTION)
        # Centered, slightly below the notch.
        left = available.x() + (available.width() - width) // 2
        top = available.y() + max(48, (available.height() - height) // 2)
        self._full_height = min(height, available.bottom() - top)
        self.setGeometry(QRect(left, top, width, self._full_height))

    def _on_load_finished(self, is_ok: bool) -> None:
        if not is_ok or self._page.url() != VISUAL_PAGE_URL:
            return
        for delay in CONTENT_MEASURE_DELAYS_MILLISECONDS:
            QTimer.singleShot(delay, self._measure_content)

    def _measure_content(self) -> None:
        if self.isVisible():
            self._page.runJavaScript(MEASURE_CONTENT_SCRIPT, 0, self._fit_height_to_content)

    def _fit_height_to_content(self, content_bottom) -> None:
        if not isinstance(content_bottom, (int, float)) or content_bottom <= 0:
            return
        fitted_height = int(content_bottom) + CONTENT_BOTTOM_MARGIN_PIXELS
        self.resize(self.width(), max(MINIMUM_WINDOW_HEIGHT_PIXELS, min(self._full_height, fitted_height)))

    def close_visual(self) -> None:
        if not self.isVisible():
            return
        self.hide()
        # Unload the page so its animation loop stops using the GPU.
        self._scheme_handler.page_html = b""
        self._page.load(BLANK_PAGE_URL)
        self.closed.emit()

    @property
    def is_shown(self) -> bool:
        return self.isVisible()
