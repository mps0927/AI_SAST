from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..stage3_schemas import AgentName


class RouteConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str = Field(min_length=1)
    reasoning_effort: str
    max_output_tokens: int = Field(gt=0)
    num_ctx: int | None = Field(default=None, gt=0)
    temperature: float | None = Field(default=None, ge=0, le=2)
    think: bool | None = None
    fallback_model: str | None = Field(default=None, min_length=1)
    keep_alive: str | int | None = None

    @model_validator(mode="after")
    def validate_effort(self) -> "RouteConfig":
        allowed = {"none", "low", "medium", "high", "xhigh", "max"}
        if self.reasoning_effort not in allowed:
            raise ValueError(f"unsupported reasoning effort: {self.reasoning_effort}")
        return self


class ProfileConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1)
    endpoint: str | None = None
    roles: dict[AgentName, RouteConfig]

    @model_validator(mode="after")
    def all_roles_present(self) -> "ProfileConfig":
        missing = set(AgentName) - set(self.roles)
        if missing:
            raise ValueError(f"profile is missing roles: {sorted(item.value for item in missing)}")
        return self


class RoutingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str
    active_profile: str
    profiles: dict[str, ProfileConfig]

    @model_validator(mode="after")
    def active_profile_exists(self) -> "RoutingConfig":
        if self.active_profile not in self.profiles:
            raise ValueError("active_profile does not exist")
        return self


class ModelRouter:
    def __init__(self, config_path: Path, profile: str | None = None):
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        self.config = RoutingConfig.model_validate(raw)
        self.profile_name = profile or self.config.active_profile
        if self.profile_name not in self.config.profiles:
            raise ValueError(f"unknown routing profile: {self.profile_name}")
        self.profile = self.config.profiles[self.profile_name]

    @property
    def provider(self) -> str:
        return self.profile.provider

    def route(self, agent: AgentName) -> RouteConfig:
        return self.profile.roles[agent]
