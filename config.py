import json
import os
from pathlib import Path
from typing import Any, Dict

DEFAULT_CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config(config_path: Path = DEFAULT_CONFIG_PATH) -> Dict[str, Any]:
    # A real config.json file always wins (local use, or a start command that
    # writes one). Otherwise fall back to the CONFIG_JSON environment variable
    # (Railway), so no file needs to be created at startup.
    if config_path.exists():
        with open(config_path) as f:
            return json.load(f)

    env_config = os.environ.get("CONFIG_JSON")
    if env_config:
        try:
            return json.loads(env_config)
        except json.JSONDecodeError as exc:
            raise FileNotFoundError(
                f"No config file at {config_path}, and the CONFIG_JSON environment "
                f"variable is not valid JSON: {exc}"
            ) from exc

    example = config_path.parent / "config.json.example"
    raise FileNotFoundError(
        f"Config file not found at {config_path}.\n"
        f"Copy {example} to {config_path} and fill in your account details, "
        f"or set the CONFIG_JSON environment variable."
    )


def get_accounts(config: Dict[str, Any]) -> Dict[str, Dict]:
    return config.get("accounts", {})


def get_credentials_dir(config: Dict[str, Any], base_dir: Path = DEFAULT_CONFIG_PATH.parent) -> Path:
    path = Path(config.get("credentials_dir", "./credentials"))
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def get_client_secret_path(config: Dict[str, Any]) -> Path:
    return get_credentials_dir(config) / "client_secret.json"
