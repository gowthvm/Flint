import logging
import logging.handlers
import sys

from core import paths

_CLI_HANDLER_ATTR = "_flint_cli_handler"


def setup_cli_logging(name: str = "flint", level: str = "WARNING") -> logging.Logger:
    """Attach the CLI's stderr handler (L21).  Safe to call repeatedly.

    Records at *level* and above go to a ``StreamHandler(sys.stderr)`` so
    machine-readable stdout (``RESULT`` lines, progress JSON) is never
    interleaved with log noise.  Python's ``lastResort`` handler — the bare
    stderr fallback used when a record reaches no handler at all — is
    disabled, so nothing can bypass this path and print on its own.
    """
    logger = logging.getLogger(name)
    # Disable the bare stderr fallback first so even a concurrent record
    # cannot bypass the handler below.
    logging.lastResort = None
    for existing in logger.handlers:
        if getattr(existing, _CLI_HANDLER_ATTR, False):
            return logger
    numeric = getattr(logging, str(level).upper(), None)
    if not isinstance(numeric, int):
        numeric = logging.WARNING
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(numeric)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    setattr(handler, _CLI_HANDLER_ATTR, True)
    logger.addHandler(handler)
    # C02: the logger's own level must match the handler — a NOTSET logger
    # inherits the root WARNING and would drop INFO records before the
    # handler ever sees them.
    logger.setLevel(numeric)
    return logger


# C02: level names accepted by apply_log_level (values come from logging).
_LEVEL_NAMES = (
    "NOTSET",
    "DEBUG",
    "INFO",
    "WARNING",
    "ERROR",
    "CRITICAL",
    "WARN",
    "FATAL",
)


def apply_log_level(level: str, name: str = "flint") -> str:
    """Apply a configured log level (C02) to the *name* logger.

    ``level`` is a case-insensitive level name (``"INFO"``, ``"debug"`` …).
    Raises ``ValueError`` for anything that is not a real logging level so a
    bad setting fails loudly instead of silently changing verbosity.
    Returns the normalized level name that was applied.

    Callers should pass ``core.settings.get("log_level")`` (e.g. from the UI
    at startup and whenever the setting changes).
    """
    normalized = str(level).strip().upper()
    if normalized not in _LEVEL_NAMES:
        raise ValueError(f"unknown log level: {level!r}")
    logging.getLogger(name).setLevel(getattr(logging, normalized))
    return normalized


def setup_logging(name: str = "flint", level: str = "INFO") -> logging.Logger:
    # C02: the startup log belongs with the rest of the app data, next to
    # settings.json/history.json, not in %TEMP%. `paths.APP_DIR` is read
    # dynamically so the conftest redirection covers it.
    log_path = paths.APP_DIR / f"{name}-startup.log"

    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    log_level = getattr(logging, level.upper(), logging.INFO)
    logger.setLevel(log_level)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # A read-only APP_DIR must degrade to console logging, never stop the
    # app from starting.
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            str(log_path), maxBytes=1024 * 1024, backupCount=3, encoding="utf-8"
        )
    except OSError:
        logger.warning("could not open %s; logging to console only", log_path)
    else:
        handler.setFormatter(fmt)
        logger.addHandler(handler)

    # also add a console handler for debug convenience
    console = logging.StreamHandler()
    console.setLevel(log_level)
    console.setFormatter(fmt)
    logger.addHandler(console)

    return logger
