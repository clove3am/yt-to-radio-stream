"""Domain models and request schemas."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .utils import normalize_youtube_url, slugify

Codec = Literal["aac", "aac_he", "mp3", "mp3_low"]
SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


@dataclass(frozen=True)
class Station:
    id: int
    name: str
    url: str
    slug: str
    codec: str
    on_demand: bool
    desired_state: bool
    created_at: str

    @property
    def stream_extension(self) -> str:
        return "mp3" if self.codec in {"mp3", "mp3_low"} else "aac"

    @property
    def stream_bitrate(self) -> int | None:
        return {"mp3": 128, "mp3_low": 64}.get(self.codec)

    @property
    def stream_path(self) -> str:
        return f"/stream/{self.slug}.{self.stream_extension}"


def _clean_name(value: str) -> str:
    value = " ".join(value.split())
    if not value:
        raise ValueError("Give the station a name.")
    return value


def _clean_codec_mode(codec: str, on_demand: bool) -> None:
    if codec == "aac_he" and on_demand:
        raise ValueError(
            "HE-AAC copy is always-on only. Choose AAC copy for on-demand."
        )


class StationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    url: str = Field(min_length=1, max_length=2048)
    codec: Codec = "aac"
    on_demand: bool = False
    autostart: bool = True

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return _clean_name(value)

    @field_validator("url")
    @classmethod
    def clean_url(cls, value: str) -> str:
        return normalize_youtube_url(value)

    @model_validator(mode="after")
    def validate_mode(self) -> "StationCreate":
        _clean_codec_mode(self.codec, self.on_demand)
        return self


class StationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=80)
    url: str | None = Field(default=None, min_length=1, max_length=2048)
    codec: Codec | None = None
    on_demand: bool | None = None

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str | None) -> str | None:
        return None if value is None else _clean_name(value)

    @field_validator("url")
    @classmethod
    def clean_url(cls, value: str | None) -> str | None:
        return None if value is None else normalize_youtube_url(value)

    @model_validator(mode="after")
    def validate_update(self) -> "StationUpdate":
        if (
            self.name is None
            and self.url is None
            and self.codec is None
            and self.on_demand is None
        ):
            raise ValueError("Nothing to update.")
        if self.codec is not None and self.on_demand is not None:
            _clean_codec_mode(self.codec, self.on_demand)
        return self


class StationImportItem(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    url: str = Field(min_length=1, max_length=2048)
    codec: Codec = "aac"
    on_demand: bool = False
    enabled: bool = True
    slug: str | None = Field(default=None, max_length=64)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return _clean_name(value)

    @field_validator("url")
    @classmethod
    def clean_url(cls, value: str) -> str:
        return normalize_youtube_url(value)

    @field_validator("slug")
    @classmethod
    def clean_slug(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip().lower()
        if not SLUG_PATTERN.fullmatch(value):
            raise ValueError(
                "Slugs may contain lowercase letters, numbers and dashes."
            )
        return value

    @model_validator(mode="after")
    def validate_mode(self) -> "StationImportItem":
        _clean_codec_mode(self.codec, self.on_demand)
        return self


class StationImport(BaseModel):
    stations: list[StationImportItem] = Field(default_factory=list, max_length=500)
    replace: bool = False


def station_slug(name: str) -> str:
    return slugify(name)
