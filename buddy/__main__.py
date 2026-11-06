"""Entry point: python -m buddy"""

import os
import signal
import sys


# NVIDIA PRIME render offload variables (usually exported globally on hybrid laptops).
PRIME_OFFLOAD_VARIABLES = ("__NV_PRIME_RENDER_OFFLOAD", "__GLX_VENDOR_LIBRARY_NAME", "__VK_LAYER_NV_optimus")


def configure_visual_gpu() -> None:
    """Picks the GPU path for the visual window (Qt WebEngine) before Qt starts.

    With PRIME offload exported, Qt creates its GL on NVIDIA's GLX while Chromium's ANGLE
    initializes on another EGL display, fails with "Invalid visual ID requested" and aborts
    the whole process. BUDDY_VISUAL_GPU:
      auto (default) - drop the PRIME variables for buddy only, so it renders on the
                       integrated GPU (also keeps the discrete GPU asleep)
      nvidia         - keep PRIME offload and run ANGLE on EGL instead of GLX
    """
    gpu_mode = os.environ.get("BUDDY_VISUAL_GPU", "auto").strip().lower()
    if os.environ.get("__NV_PRIME_RENDER_OFFLOAD") != "1":
        return
    if gpu_mode == "nvidia":
        existing_flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = f"{existing_flags} --use-gl=angle --use-angle=gl-egl".strip()
        return
    for variable_name in PRIME_OFFLOAD_VARIABLES:
        os.environ.pop(variable_name, None)


def main() -> int:
    from .platform_check import find_display_session_problem

    session_problem = find_display_session_problem()
    if session_problem is not None:
        print(session_problem, file=sys.stderr)
        return 1

    # Always use the X11 backend, even if a Wayland socket happens to exist.
    os.environ["QT_QPA_PLATFORM"] = "xcb"

    from .config import load_config

    # Reads .env too, so BUDDY_VISUAL_GPU from there applies before Qt starts.
    config = load_config()
    configure_visual_gpu()

    from PyQt6.QtCore import QCoreApplication, Qt
    from PyQt6.QtWidgets import QApplication

    # Qt WebEngine (visual window) needs its module loaded, a shared GL context and its
    # URL scheme registered before the QApplication exists.
    from .visual_window import register_visual_url_scheme

    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    register_visual_url_scheme()

    from .app import BuddyController

    application = QApplication(sys.argv)
    application.setApplicationName("buddy")
    # Matches buddy.desktop so GNOME shows the right name and icon for buddy's windows.
    application.setDesktopFileName("buddy")
    # The app lives in the tray; closing a window must not quit it.
    application.setQuitOnLastWindowClosed(False)

    # One buddy per user: a second launch (e.g. from the app grid) opens the running one's notch.
    from PyQt6.QtNetwork import QLocalServer, QLocalSocket

    instance_server_name = f"buddy-{os.getuid()}"
    existing_instance = QLocalSocket()
    existing_instance.connectToServer(instance_server_name)
    if existing_instance.waitForConnected(500):
        existing_instance.write(b"show")
        existing_instance.waitForBytesWritten(500)
        existing_instance.disconnectFromServer()
        print("buddy is already running; opened its notch.")
        return 0
    QLocalServer.removeServer(instance_server_name)  # stale socket from a crash
    instance_server = QLocalServer()
    instance_server.listen(instance_server_name)

    controller = BuddyController(application, config)

    def on_second_launch() -> None:
        connection = instance_server.nextPendingConnection()
        if connection is not None:
            connection.disconnected.connect(connection.deleteLater)
        controller.show_notch()

    instance_server.newConnection.connect(on_second_launch)
    controller.start()

    # Let Ctrl+C in the terminal stop the app.
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    return application.exec()


if __name__ == "__main__":
    sys.exit(main())
