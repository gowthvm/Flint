import logging
import logging.handlers
import os
from pathlib import Path


def setup_logging(name: str = "flint", level: str = "INFO") -> logging.Logger:
    temp = os.environ.get("TEMP", ".")
    log_path = Path(temp) / f"{name}-startup.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    log_level = getattr(logging, level.upper(), logging.INFO)
    logger.setLevel(log_level)

    handler = logging.handlers.RotatingFileHandler(
        str(log_path), maxBytes=1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(fmt)
    logger.addHandler(handler)

    # also add a console handler for debug convenience
    console = logging.StreamHandler()
    console.setLevel(log_level)
    console.setFormatter(fmt)
    logger.addHandler(console)

    return logger
