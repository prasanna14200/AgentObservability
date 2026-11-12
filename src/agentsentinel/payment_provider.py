"""Strict provider mode selection for payment execution."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

ProviderMode = Literal["MOCK", "SANDBOX", "LIVE"]
_SANDBOX_CREDENTIALS = (
    "PAYPAL_CLIENT_ID",
    "PAYPAL_CLIENT_SECRET",
    "PAYPAL_MERCHANT_ID",
)


@dataclass(frozen=True)
class ProviderModeConfig:
    """One trusted provider selection; credentials never select a mode."""

    mode: ProviderMode

    def __post_init__(self) -> None:
        if self.mode not in {"MOCK", "SANDBOX", "LIVE"}:
            raise ValueError("provider mode must be MOCK, SANDBOX, or LIVE")

    @classmethod
    def mock(cls) -> ProviderModeConfig:
        return cls("MOCK")

    @classmethod
    def sandbox(cls) -> ProviderModeConfig:
        return cls("SANDBOX")

    @classmethod
    def live(cls) -> ProviderModeConfig:
        return cls("LIVE")

    @classmethod
    def from_values(cls, values: dict[str, str]) -> ProviderModeConfig:
        if "PAYPAL_ENVIRONMENT" not in values:
            mode: ProviderMode = "MOCK"
        else:
            raw_mode = values["PAYPAL_ENVIRONMENT"]
            if not isinstance(raw_mode, str) or not raw_mode or raw_mode.strip() != raw_mode:
                raise ValueError("PAYPAL_ENVIRONMENT must be MOCK, SANDBOX, or LIVE")
            normalized = raw_mode.upper()
            if normalized == "MOCK":
                mode = "MOCK"
            elif normalized == "SANDBOX":
                mode = "SANDBOX"
            elif normalized == "LIVE":
                mode = "LIVE"
            else:
                raise ValueError("PAYPAL_ENVIRONMENT must be MOCK, SANDBOX, or LIVE")

        for name in _SANDBOX_CREDENTIALS:
            if name in values and not isinstance(values[name], str):
                raise ValueError("Sandbox credentials must be text values")
        supplied_credentials = [bool(values.get(name)) for name in _SANDBOX_CREDENTIALS]
        if any(supplied_credentials) and not all(supplied_credentials):
            raise ValueError("all Sandbox credentials must be configured together")
        if any(supplied_credentials) and mode != "SANDBOX":
            raise ValueError("Sandbox credentials require PAYPAL_ENVIRONMENT=SANDBOX")
        return cls(mode)

    @classmethod
    def from_environment(cls) -> ProviderModeConfig:
        names = ("PAYPAL_ENVIRONMENT", *_SANDBOX_CREDENTIALS)
        return cls.from_values({name: os.environ[name] for name in names if name in os.environ})
