import logging
import logging.handlers
import os
from pathlib import Path


def setup_logging(name: str = "flint") -> logging.Logger:
    temp = os.environ.get("TEMP", ".")
    log_path = Path(temp) / f"{name}-startup.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)

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
    console.setFormatter(fmt)
    logger.addHandler(console)

    return logger
