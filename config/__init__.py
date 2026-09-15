# config/__init__.py
import json, platform
from pathlib import Path

_CONFIG_PATH = Path(__file__).parent / "api_keys.json"

# The Gemini models the tools call for text and images, named once so changing
# one is a single edit. The Live voice model and web search's grounding model are
# pinned where they are used, for the reasons given there.
GEMINI_MODEL      = "gemini-flash-latest"
GEMINI_LITE_MODEL = "gemini-flash-lite-latest"

def _platform_os() -> str:
    """Auto-detect OS when config file is absent."""
    return {"Windows": "windows", "Darwin": "mac", "Linux": "linux"}.get(
        platform.system(), "linux"
    )

def get_config() -> dict:
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def get_os() -> str:
    """Returns: 'windows' | 'mac' | 'linux'"""
    return get_config().get("os_system", _platform_os()).lower()

def is_windows() -> bool: return get_os() == "windows"
def is_mac()     -> bool: return get_os() == "mac"
def is_linux()   -> bool: return get_os() == "linux"
