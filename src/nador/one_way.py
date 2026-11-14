"""Opt-in, controller-managed one-way measurements."""

from ipaddress import IPv4Address
from typing import Self

from metamodel.models.base import StrictBaseModel
from pydantic import Field, model_validator


class N3Settings(StrictBaseModel):
    enabled: bool = False
    log_samples: bool = False


class UpfTransitSettings(StrictBaseModel):
    enabled: bool = False
    peer: IPv4Address | None = None


class OneWaySettings(StrictBaseModel):
    image: str | None = None
    sample: int = Field(default=16, ge=1, le=0xffffffff)
    test_port: int = Field(default=0, ge=0, le=65535)
    n3: N3Settings = Field(default_factory=N3Settings)
    upf_transit: UpfTransitSettings = Field(default_factory=UpfTransitSettings)

    @model_validator(mode="after")
    def check_requirements(self) -> Self:
        if (self.n3.enabled or self.upf_transit.enabled) and not self.image:
            raise ValueError("Enabled one-way probes require one_way.image")
        if self.image and (":" not in self.image.rsplit("/", 1)[-1] or any(c.isspace() for c in self.image)):
            raise ValueError("one_way.image must be a tagged image or digest")
        if self.upf_transit.enabled and (not self.upf_transit.peer or not self.test_port):
            raise ValueError("UPF transit requires upf_transit.peer and test_port")
        return self
