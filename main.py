import ctypes
import faulthandler
import os
import subprocess
import sys
import time
import traceback
from contextlib import ExitStack
from types import TracebackType

from PyQt6.QtCore import QTimer
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import QApplication

from core import cli, settings
from core.log import setup_logging
from ui import dialogs
from ui.style import build_style
from ui.window import MainWindow

logger = setup_logging()
_crash_file_stack = ExitStack()
_SINGLE_INSTANCE_NAME = "FlintFlashingApp_v1"
_CRASH_PATH = os.path.join(
    os.environ.get("LOCALAPPDATA", os.environ.get("TEMP", ".")),
    "Flint",
    "crash.log",
)


def _log(message: str) -> None:
    logger.info(message)


def _install_crash_logging() -> None:
    try:
        os.makedirs(os.path.dirname(_CRASH_PATH), exist_ok=True)
        # The file must stay open for the process lifetime (faulthandler and
        # the excepthook write to it), so it is kept in a module-level
        # ExitStack rather than closed at the end of this function.
        crash = _crash_file_stack.enter_context(
            open(_CRASH_PATH, "a", encoding="utf-8")  # noqa: SIM115
        )
        faulthandler.enable(crash)

        def _hook(
            exc_type: type[BaseException],
            exc_value: BaseException,
            exc_tb: TracebackType | None,
        ) -> None:
            try:
                crash.write(
                    "".join(
                        traceback.format_exception(
                            exc_type, exc_value, exc_tb
                        )
                    )
                )
                crash.flush()
            except OSError:
                pass
            logger.exception("Unhandled exception", exc_info=(exc_type, exc_value, exc_tb))
            sys.__excepthook__(exc_type, exc_value, exc_tb)

        sys.excepthook = _hook
    except OSError:
        logger.exception("Failed to install crash logging")


def _windowless_python() -> str:
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        alt = exe[: -len("python.exe")] + "pythonw.exe"
        if os.path.isfile(alt):
            return alt
    return exe


def _ensure_admin() -> None:
    if ctypes.windll.shell32.IsUserAnAdmin():
        _log("already elevated")
        return
    # Respect user preference: ask before auto-elevating.
    try:
        ask = bool(settings.get("ask_before_elevation"))
    except Exception:
        ask = True
    if ask:
        # Use a native MessageBox to ask before elevation since QApplication
        # may not exist yet.
        try:
            MB_YESNO = 0x04
            IDYES = 6
            res = ctypes.windll.user32.MessageBoxW(
                None,
                "Flint requires administrator privileges to access raw disks.\n\nElevate now?",
                "Flint — elevation required",
                MB_YESNO,
            )
            if res != IDYES:
                _log("user declined elevation; continuing without admin")
                return
        except Exception:
            # If the native dialog fails, fall back to auto-elevate.
            pass

    python = _windowless_python()
    _log(f"not elevated; spawning runas: {python} {sys.argv}")
    params = subprocess.list2cmdline(sys.argv)
    result = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", python, params, None, 10  # SW_SHOWDEFAULT
    )
    _log(f"ShellExecuteW returned {result}")
    sys.exit(0)


_server_global: QLocalServer | None = None


def release_single_instance() -> None:
    """Drop the single-instance lock before spawning an elevated relaunch.

    The relaunched process must be able to acquire the pipe; without this,
    the old instance (still alive while it shuts down) holds it and the new
    instance exits thinking another copy is running.
    """
    global _server_global
    if _server_global is None:
        return
    server = _server_global
    _server_global = None
    try:
        server.close()
        QLocalServer.removeServer(_SINGLE_INSTANCE_NAME)
    except Exception:
        logger.exception("failed to release single-instance lock")


def _acquire_single_instance(
    name: str = _SINGLE_INSTANCE_NAME,
) -> QLocalServer | None:
    # If another instance is alive, poke it to come to the foreground and
    # exit. A listen failure right after that means the other instance is
    # mid-exit (e.g. an elevation relaunch): retry briefly instead of
    # exiting immediately.
    for _ in range(6):
        probe = QLocalSocket()
        probe.connectToServer(name)
        if probe.waitForConnected(500):
            probe.write(b"show")
            probe.flush()
            probe.waitForBytesWritten(500)
            probe.disconnectFromServer()
            return None
        probe.abort()
        QLocalServer.removeServer(name)
        server = QLocalServer()
        if server.listen(name):
            return server
        time.sleep(0.3)
    return None


def _maybe_show_crash_report(window: MainWindow) -> None:
    """If the crash log grew since the last run, offer to copy the report."""
    try:
        with open(_CRASH_PATH, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return
    seen = int(settings.get("crash_report_seen_bytes") or 0)
    if len(content) <= seen or not content[seen:].strip():
        return
    settings.set_many(crash_report_seen_bytes=len(content))
    dlg = dialogs.FlintDialog(
        window,
        kind="warning",
        title="Flint \u2014 crashed last time",
        message=(
            "Flint crashed on a previous run.\n\n"
            "The crash report has been saved to disk."
        ),
        buttons=[
            ("Copy report", "ghost", "copy"),
            ("Ignore", "primary", "no"),
        ],
    )
    if dlg.run() == "copy":
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(
                content[seen:].strip() or content.strip()
            )


def main() -> int:
    _log(f"start: pid={os.getpid()} argv={sys.argv}")
    _install_crash_logging()
    args = sys.argv[1:]
    if "--cli" in args or (
        args
        and (
            args[0] in cli.TOP_LEVEL_COMMANDS
            or args[0] in ("--version", "--help", "-h")
        )
    ):
        # Headless mode: no elevation prompt loop, no single-instance lock,
        # no window. Exit codes and machine-readable output come from the
        # cli module (modern top-level commands, the --cli compat alias and
        # the global --version/--help flags all detour here).
        return cli.main()
    _ensure_admin()
    app = QApplication(sys.argv)
    app.setStyleSheet(build_style(settings.get("theme")))
    app.setQuitOnLastWindowClosed(False)

    server = _acquire_single_instance()
    if server is None:
        _log("another instance is running; exiting")
        return 0
    global _server_global
    _server_global = server

    window = MainWindow()

    def _on_new_instance() -> None:
        while server.hasPendingConnections():
            conn = server.nextPendingConnection()
            if conn is None:
                continue

            def _on_data(c: QLocalSocket = conn) -> None:
                c.readAll()
                window._force_show()

            conn.readyRead.connect(_on_data)

    server.newConnection.connect(_on_new_instance)

    window.show()
    _log(
        f"shown: visible={window.isVisible()} "
        f"minimized={window.isMinimized()} "
        f"native_visible={window._native_is_visible()}"
    )

    def _ensure_visible() -> None:
        window._force_show()
        if window.isMinimized():
            window.showNormal()
        _log(
            f"retry show: visible={window.isVisible()} "
            f"minimized={window.isMinimized()} "
            f"native_visible={window._native_is_visible()}"
        )

    def _on_quit() -> None:
        window._shutdown()
        _log(
            f"aboutToQuit: visible={window.isVisible()} "
            f"writer={window._writer is not None} "
            f"verifier={window._verifier is not None} "
            f"tray={window._tray is not None}"
        )

    app.aboutToQuit.connect(_on_quit)
    QTimer.singleShot(400, lambda: _maybe_show_crash_report(window))
    QTimer.singleShot(300, _ensure_visible)
    QTimer.singleShot(1200, _ensure_visible)
    QTimer.singleShot(3000, _ensure_visible)
    # Quiet 7-day update check; never blocks and only surfaces a new release.
    try:
        QTimer.singleShot(6000, window._maybe_auto_check_updates)
    except Exception:
        logger.exception("failed to schedule update check")

    rc = app.exec()
    _log(f"app exited rc={rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())