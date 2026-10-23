import os
import re
from pathlib import Path
from dotenv import load_dotenv

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent
load_dotenv(PROJECT_ROOT / ".env")


def _float_env(name: str, default: str) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


def _int_env(name: str, default: str) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return int(default)


def _str_env(name: str, default: str) -> str:
    value = os.getenv(name, "").strip()
    return value or default


def _str_env_unless_legacy(name: str, default: str, legacy_values: set[str]) -> str:
    value = os.getenv(name, "").strip()
    if not value or value.lower() in {legacy.lower() for legacy in legacy_values}:
        return default
    return value


def _bool_env(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).strip().lower() not in {"0", "false", "no", "off", "disabled"}


def normalize_handle(handle: str) -> str:
    """Normalize a sender handle to a consistent format for comparison.

    Phone numbers are coerced to E.164 (+1XXXXXXXXXX for US/CA numbers).
    Emails and Apple IDs are returned as-is (lowercased).
    Falls back to the stripped original if the digit count is unrecognized.
    """
    h = handle.strip()
    if "@" in h:
        return h.lower()
    digits = re.sub(r"\D", "", h)
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return h


GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = _str_env("GEMINI_MODEL", "gemini-3.1-flash-lite")
GEMINI_REWRITE_MODEL = _str_env("GEMINI_REWRITE_MODEL", GEMINI_MODEL)
_LEGACY_GEMINI_IMAGE_MODELS = {
    "gemini-2.5-flash-image",
    "gemini-2.5-flash-image-preview",
}
GEMINI_IMAGE_MODEL = _str_env_unless_legacy(
    "GEMINI_IMAGE_MODEL",
    "gemini-3.1-flash-image",
    _LEGACY_GEMINI_IMAGE_MODELS,
)
GEMINI_IMAGE_API_VERSION = _str_env("GEMINI_IMAGE_API_VERSION", "v1")
GEMINI_ENABLED = _bool_env("GEMINI_ENABLED", "true")
GEMINI_DAILY_BUDGET_USD = _float_env("GEMINI_DAILY_BUDGET_USD", "1.00")
GEMINI_DAILY_ALERT_USD = _float_env("GEMINI_DAILY_ALERT_USD", "0.25")
GEMINI_BUDGET_ALERT_COOLDOWN_MINUTES = _float_env("GEMINI_BUDGET_ALERT_COOLDOWN_MINUTES", "30")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_VISION_MODEL = _str_env("OPENAI_VISION_MODEL", "")
OPENAI_IMAGE_MODEL = _str_env("OPENAI_IMAGE_MODEL", "")
OPENAI_IMAGE_SIZE = _str_env("OPENAI_IMAGE_SIZE", "1024x1024")
OPENAI_IMAGE_QUALITY = _str_env("OPENAI_IMAGE_QUALITY", "low")
IMAGE_PROVIDER = os.getenv("IMAGE_PROVIDER", os.getenv("IMAGE_GENERATION_PROVIDER", "auto")).strip().lower()
# Backward-compatible alias for older docs/scripts. New config should use IMAGE_PROVIDER.
IMAGE_GENERATION_PROVIDER = IMAGE_PROVIDER
IMAGE_SCAN_PROVIDER = os.getenv("IMAGE_SCAN_PROVIDER", "auto").strip().lower()
LOCAL_IMAGE_ENDPOINT = os.getenv("LOCAL_IMAGE_ENDPOINT", "").strip()
LOCAL_IMAGE_MODEL = _str_env("LOCAL_IMAGE_MODEL", "flux")
LOCAL_IMAGE_TIMEOUT = _float_env("LOCAL_IMAGE_TIMEOUT", "180")
NANO_BANANA_IMAGE_MODEL = _str_env("NANO_BANANA_IMAGE_MODEL", "gemini-3.1-flash-image")
NANO_BANANA_IMAGE_SIZE = _str_env("NANO_BANANA_IMAGE_SIZE", "2K")
NANO_BANANA_IMAGE_ASPECT_RATIO = _str_env("NANO_BANANA_IMAGE_ASPECT_RATIO", "1:1")
OWNER_ALERT_WEBHOOK_URL = os.getenv("OWNER_ALERT_WEBHOOK_URL", "").strip()
OWNER_ALERT_WEBHOOK_TIMEOUT = _float_env("OWNER_ALERT_WEBHOOK_TIMEOUT", "5")
DEFAULT_FANTASY_DASHBOARD_URL = "https://davos-fourth-down.echase1919.chatgpt.site"
FANTASY_DASHBOARD_URL = _str_env("FANTASY_DASHBOARD_URL", DEFAULT_FANTASY_DASHBOARD_URL)
FANTASY_ACCESS_PRIVATE_KEY_PATH = Path(
    _str_env(
        "FANTASY_ACCESS_PRIVATE_KEY_PATH",
        str(Path.home() / ".config" / "davosbot" / "fantasy_access_private.pem"),
    )
)
OWNER_ID = normalize_handle(os.getenv("OWNER_ID", ""))
MAC_MINI_APPLE_ID = os.getenv("MAC_MINI_APPLE_ID", "").strip().lower()
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma4")
_OLLAMA_MODEL_BASE = OLLAMA_MODEL.strip().split(":", 1)[0].lower()
_DEFAULT_OLLAMA_SIMPLE_CHAT_MODEL = "gemma3" if _OLLAMA_MODEL_BASE == "gemma4" else OLLAMA_MODEL
OLLAMA_SIMPLE_CHAT_MODEL = _str_env("OLLAMA_SIMPLE_CHAT_MODEL", _DEFAULT_OLLAMA_SIMPLE_CHAT_MODEL)
OLLAMA_NUM_CTX = _int_env("OLLAMA_NUM_CTX", "8192")
OLLAMA_SIMPLE_CHAT_NUM_PREDICT = _int_env("OLLAMA_SIMPLE_CHAT_NUM_PREDICT", "64")
OLLAMA_SIMPLE_CHAT_TEMPERATURE = _float_env("OLLAMA_SIMPLE_CHAT_TEMPERATURE", "0.7")
OLLAMA_SIMPLE_CHAT_TIMEOUT = _float_env("OLLAMA_SIMPLE_CHAT_TIMEOUT", "3.5")
OLLAMA_KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "1h").strip()
OLLAMA_KEEP_WARM_ENABLED = _bool_env("OLLAMA_KEEP_WARM_ENABLED", "true")
OLLAMA_KEEP_WARM_INTERVAL_SECONDS = _float_env("OLLAMA_KEEP_WARM_INTERVAL_SECONDS", "1800")
OLLAMA_KEEP_WARM_TIMEOUT = _float_env("OLLAMA_KEEP_WARM_TIMEOUT", "30")
ADVANCED_TEXT_MODEL = _str_env("ADVANCED_TEXT_MODEL", "gemini-3.5-flash")
ADVANCED_CODE_MODEL = _str_env("ADVANCED_CODE_MODEL", f"gemini:{ADVANCED_TEXT_MODEL}")
ADVANCED_VISION_MODEL = _str_env_unless_legacy("ADVANCED_VISION_MODEL", GEMINI_IMAGE_MODEL, _LEGACY_GEMINI_IMAGE_MODELS)
MODEL_ROUTE_SIMPLE_CHAT = _str_env("MODEL_ROUTE_SIMPLE_CHAT", f"ollama:{OLLAMA_SIMPLE_CHAT_MODEL}")
MODEL_ROUTE_TOOL_USE = _str_env("MODEL_ROUTE_TOOL_USE", f"gemini:{GEMINI_MODEL}")
MODEL_ROUTE_HELPER_REWRITE = _str_env("MODEL_ROUTE_HELPER_REWRITE", f"gemini:{GEMINI_REWRITE_MODEL}")
MODEL_ROUTE_COMPLEX_REASONING = _str_env("MODEL_ROUTE_COMPLEX_REASONING", f"gemini:{ADVANCED_TEXT_MODEL}")
MODEL_ROUTE_IMAGE_SCAN = _str_env("MODEL_ROUTE_IMAGE_SCAN", f"{IMAGE_SCAN_PROVIDER}:{ADVANCED_VISION_MODEL}")
MODEL_ROUTE_IMAGE_GENERATION = _str_env("MODEL_ROUTE_IMAGE_GENERATION", f"{IMAGE_PROVIDER}:{LOCAL_IMAGE_MODEL}")
MODEL_ROUTE_NANO_BANANA_IMAGE = _str_env("MODEL_ROUTE_NANO_BANANA_IMAGE", f"gemini:{NANO_BANANA_IMAGE_MODEL}")
MODEL_ROUTE_CODE_REVIEW = _str_env("MODEL_ROUTE_CODE_REVIEW", ADVANCED_CODE_MODEL)
SLOW_MESSAGE_LOG_SECONDS = _float_env("SLOW_MESSAGE_LOG_SECONDS", "8")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "5"))
MARKET_TRACKER_ENABLED = _bool_env("MARKET_TRACKER_ENABLED", "true")
MARKET_ALERTS_ENABLED = _bool_env("MARKET_ALERTS_ENABLED", "true")
MARKET_POLL_MINUTES = max(2, _int_env("MARKET_POLL_MINUTES", "5"))
MARKET_DATA_TIMEOUT = max(3.0, _float_env("MARKET_DATA_TIMEOUT", "10"))
MARKET_ALERT_COOLDOWN_MINUTES = max(60, _int_env("MARKET_ALERT_COOLDOWN_MINUTES", "90"))
DB_PATH = os.getenv("DB_PATH", os.path.expanduser("~/Library/Messages/chat.db"))
BOT_DB_PATH = os.getenv("BOT_DB_PATH", str(PROJECT_ROOT / "davosbot.db"))
SOUL_PATH = os.getenv("SOUL_PATH", str(PROJECT_ROOT / "SOUL.md"))
MEMORY_PATH = os.getenv("MEMORY_PATH", str(PROJECT_ROOT / "MEMORY.md"))
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
GENERATED_DIR = os.getenv("GENERATED_DIR", str(PROJECT_ROOT / "generated"))
IMAGE_OUTPUT_DIR = os.getenv(
    "IMAGE_OUTPUT_DIR",
    os.getenv("OPENAI_IMAGE_OUTPUT_DIR", os.path.join(GENERATED_DIR, "images")),
)
# Backward-compatible alias for older code/tests. New config should use IMAGE_OUTPUT_DIR.
OPENAI_IMAGE_OUTPUT_DIR = IMAGE_OUTPUT_DIR

# Password required to perform owner-only actions from a non-owner handle.
# Set this in .env. Never hard-code it here, never echo it to the LLM.
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()

# SMTP for contact-card sharing (Stage 5)
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = os.getenv("SMTP_PORT", "465")
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM_ADDRESS = os.getenv("SMTP_FROM_ADDRESS", "")
