import json
from pathlib import Path


CONFIG_PATH = Path("data/config.json")


DEFAULT_CONFIG = {
    "api_key": "",
    "scan_interval": 1000,
    "requests_per_second": 5,

    "auto_buy": False,

    "telegram_enabled": False,
    "telegram_token": "",
    "telegram_chat_id": "",

    "auto_order_enabled": False,
    "auto_order_quantity": 5,
    "auto_order_step": 1.0,
    "auto_order_interval": 15,
    "auto_order_allow_cancel": False,
}

def load():
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf8") as f:
                data = json.load(f)

            if not isinstance(data, dict):
                data = {}

        except (json.JSONDecodeError, OSError):
            data = {}
    else:
        data = {}

    config = DEFAULT_CONFIG.copy()
    config.update(data)

    # Проверяем типы и безопасные значения.
    config["api_key"] = str(config.get("api_key") or "")
    config["telegram_enabled"] = bool(config.get("telegram_enabled", False))
    config["telegram_token"] = str(config.get("telegram_token") or "").strip()
    config["telegram_chat_id"] = str(config.get("telegram_chat_id") or "").strip()
    config["scan_interval"] = max(1000, int(config.get("scan_interval", 1000)))
    config["requests_per_second"] = max(
        1, int(config.get("requests_per_second", 5))
    )
    config["auto_buy"] = bool(config.get("auto_buy", False))

    save(config)
    return config


def save(data):

    CONFIG_PATH.parent.mkdir(exist_ok=True)

    with open(CONFIG_PATH, "w", encoding="utf8") as f:

        json.dump(data, f, indent=4, ensure_ascii=False)