"""Reusable Kaggle bootstrap for ComfyUI notebooks.

Public entry point::

    from kgcomfy import KaggleComfy, load_config

    app = KaggleComfy(load_config("tts-img.yaml"))
    app.run()
"""

from .app import KaggleComfy
from .config import Config, ConfigError, load_config
from .logging_utils import setup_logging
from .shell import CommandError, Runner, SafetyError

__version__ = "0.1.0"

__all__ = [
    "KaggleComfy",
    "Config",
    "ConfigError",
    "CommandError",
    "Runner",
    "SafetyError",
    "load_config",
    "setup_logging",
    "__version__",
]
