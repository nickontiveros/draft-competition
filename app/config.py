import os
from dataclasses import dataclass, field


@dataclass
class Settings:
    db_path: str = field(default_factory=lambda: os.environ.get("DB_PATH", "tracker.db"))
    admin_token: str = field(default_factory=lambda: os.environ.get("ADMIN_TOKEN", ""))
    # Fernet key for encrypting Kalshi private keys at rest. Generate with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    cred_secret: str = field(default_factory=lambda: os.environ.get("CRED_SECRET", ""))
    polygon_rpc_url: str = field(
        default_factory=lambda: os.environ.get("POLYGON_RPC_URL", "https://polygon-rpc.com")
    )
    kalshi_base_url: str = field(
        default_factory=lambda: os.environ.get(
            "KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2"
        )
    )
    polymarket_data_url: str = field(
        default_factory=lambda: os.environ.get(
            "POLYMARKET_DATA_URL", "https://data-api.polymarket.com"
        )
    )
    sync_interval_minutes: int = field(
        default_factory=lambda: int(os.environ.get("SYNC_INTERVAL_MINUTES", "5"))
    )
    # When set, connectors are replaced with fixture-backed mocks (for local demo/dev).
    mock_connectors: bool = field(
        default_factory=lambda: os.environ.get("MOCK_CONNECTORS", "") == "1"
    )
    starting_bankroll: float = field(
        default_factory=lambda: float(os.environ.get("STARTING_BANKROLL", "100"))
    )


settings = Settings()
