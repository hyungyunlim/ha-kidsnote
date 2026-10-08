DOMAIN = "kidsnote"

CONF_IMMICH_URL = "immich_url"
CONF_IMMICH_API_KEY = "immich_api_key"
CONF_ALBUM = "album_name"
CONF_SCRIPT = "delivery_script"
CONF_INTERVAL = "interval_minutes"

DEFAULT_ALBUM = "Kidsnote - {child}"
DEFAULT_INTERVAL = 60


def signal_updated(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_updated"
