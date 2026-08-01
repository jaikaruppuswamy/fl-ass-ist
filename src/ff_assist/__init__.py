"""fl-ass-ist — fantasy football analyst data layer."""

from .config import ConfigError, LeagueConfig, Settings, get_settings, load_settings, mask
from .scoring import ScoringSettings
from .slots import LineupSlots, optimize_lineup

__version__ = "0.2.0"

__all__ = [
    "ConfigError",
    "LeagueConfig",
    "LineupSlots",
    "ScoringSettings",
    "Settings",
    "get_settings",
    "load_settings",
    "mask",
    "optimize_lineup",
    "__version__",
]
