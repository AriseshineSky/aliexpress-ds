from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    aliexpress_app_key: str = ""
    aliexpress_app_secret: str = ""
    aliexpress_access_token: str = ""

    # Upstash / Redis — same key as aliexpress-oauth TokenStore
    redis_url: str = ""

    # IOP sync gateway (Dropshipping)
    aliexpress_api_url: str = "https://api-sg.aliexpress.com/sync"
    aliexpress_sign_method: str = "sha256"  # sha256 | md5

    # Elasticsearch
    es_host: str = "34.16.105.219"
    es_port: int = 9200
    es_user: str = ""
    es_password: str = ""
    es_urls_index: str = "user1_aliexpress_us_product_urls"
    es_products_index: str = "user1_aliexpress_us_products"
    # DS API category tree + link-crawler calp seeds (shared coordination)
    es_categories_index: str = "user1_aliexpress_ds_categories"
    es_crawl_categories_index: str = "user1_aliexpress_crawl_categories"

    # Official Test/Formal-Test limit: 5,000 calls/app/day (Beijing time).
    # Also pace QPS to reduce ApiCallLimits / App Call Limited bans.
    aliexpress_daily_limit: int = 5000
    aliexpress_min_interval_sec: float = 1.0

    def require_credentials(self) -> None:
        missing = []
        if not self.aliexpress_app_key or self.aliexpress_app_key.startswith("your_"):
            missing.append("ALIEXPRESS_APP_KEY")
        if not self.aliexpress_app_secret or self.aliexpress_app_secret.startswith("your_"):
            missing.append("ALIEXPRESS_APP_SECRET")
        # Token can come from Redis or env — check via resolve at call time.
        has_redis = bool((self.redis_url or "").strip())
        has_token = bool(
            self.aliexpress_access_token
            and not self.aliexpress_access_token.startswith("your_")
        )
        if not has_redis and not has_token:
            missing.append("REDIS_URL or ALIEXPRESS_ACCESS_TOKEN")
        if missing:
            raise ValueError(
                "Missing credentials in .env: "
                + ", ".join(missing)
                + ". Fill App Key/Secret; token via Upstash REDIS_URL or ALIEXPRESS_ACCESS_TOKEN."
            )

    def require_es(self) -> None:
        if not self.es_user or not self.es_password:
            raise ValueError("ES_USER / ES_PASSWORD missing in .env")


def get_settings() -> Settings:
    return Settings()
