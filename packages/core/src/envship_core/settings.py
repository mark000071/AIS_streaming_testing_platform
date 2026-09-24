from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)


class Settings(BaseSettings):
    """Platform configuration: defaults < config/platform.yaml < ENVSHIP_* environment variables."""

    model_config = SettingsConfigDict(env_prefix="ENVSHIP_", extra="ignore")

    redis_url: str = Field(
        default="redis://localhost:6379/0", validation_alias=AliasChoices("ENVSHIP_REDIS_URL", "REDIS_URL")
    )
    data_dir: Path = Path("data")
    metrics_port: int = 0

    # ingest-fi (Digitraffic, CC BY 4.0)
    fi_mode: str = "mqtt"  # mqtt | rest
    fi_mqtt_host: str = "meri.digitraffic.fi"
    fi_rest_url: str = "https://meri.digitraffic.fi/api/ais/v1/locations"
    fi_poll_s: float = 5.0
    digitraffic_user: str = "envship-demo"
    mmsi_blacklist: list[int] = Field(default_factory=list)

    # replay
    replay_source: Path = Path("fixtures/raw_ais")
    replay_speed: float = 20.0
    replay_loop: bool = True

    # tracker
    window_stride_steps: int = 15
    max_report_age_s: float = 60.0
    min_mean_sog_kn: float = 2.0
    benchmark_max_age_s: float = 30.0
    turn_threshold_deg: float = 20.0
    truth_grace_s: float = 60.0
    truth_max_gap_s: float = 120.0
    state_keep_s: float = 2700.0
    state_flush_s: float = 10.0

    # reconcile
    min_truth_coverage: float = 0.5
    comparable_coverage: float = 0.9
    predictor_alive_s: float = 30.0

    # archive
    parquet_flush_s: float = 15.0

    # api
    frame_s: float = 2.0
    web_dist: Path | None = None
    query_timeout_s: float = 10.0
    query_max_rows: int = 10_000
    snapshot_refresh_s: float = 10.0

    # jobs
    bucket_s: int = 300

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ):
        yaml_path = os.environ.get("ENVSHIP_CONFIG", "config/platform.yaml")
        return (init_settings, env_settings, YamlConfigSettingsSource(settings_cls, yaml_file=yaml_path))


@lru_cache
def get_settings() -> Settings:
    return Settings()
