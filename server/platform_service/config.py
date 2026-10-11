from dataclasses import dataclass
from pathlib import Path
import os
from cryptography.fernet import Fernet


@dataclass
class Config:
    database_url: str
    admin_token: str
    encryption_key: str
    data_root: Path
    background: bool = True
    http_wait_seconds: int = 300

    def __post_init__(self):
        if len(self.admin_token) < 24:
            raise ValueError("PLATFORM_ADMIN_TOKEN must have at least 24 characters")
        Fernet(self.encryption_key.encode())
        self.data_root = self.data_root.resolve()
        self.data_root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls):
        return cls(
            os.environ["DATABASE_URL"],
            os.environ["PLATFORM_ADMIN_TOKEN"],
            os.environ["PLATFORM_ENCRYPTION_KEY"],
            Path(os.getenv("DATA_ROOT", "./data")),
        )
