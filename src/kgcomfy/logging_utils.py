from __future__ import annotations

import logging
import sys


def setup_logging(level: str = "INFO") -> logging.Logger:
    """Configure the ``kgcomfy`` logger once and return it.

    Kaggle/Jupyter already configures the root logger, so the package keeps its
    own handler and disables propagation to avoid duplicated lines.
    """
    logger = logging.getLogger("kgcomfy")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter("[%(levelname)s] %(name)s: %(message)s")
        )
        logger.addHandler(handler)
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logger.propagate = False
    return logger
