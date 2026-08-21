"""設定. 環境変数と .env から読む."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """アプリケーション設定."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str
    """PostgreSQL への接続 URL (環境変数 DATABASE_URL)."""

    kabu_data_dir: Path
    """生データの置き場 (環境変数 KABU_DATA_DIR). Mac は ./.data、ラズパイは /mnt/usb/data."""

    edinet_api_key: str | None = None
    """EDINET API のサブスクリプションキー (環境変数 EDINET_API_KEY)."""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """設定を取得する. プロセス内で 1 度だけ読み込む."""
    return Settings()  # type: ignore[call-arg]
