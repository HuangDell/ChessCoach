"""Local certification gate for custom Responses-compatible Agent endpoints."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


COMPATIBILITY_SCHEMA_VERSION = 1
PORTFOLIO_DATASET_ID = "agent-portfolio-v2"
PORTFOLIO_SCORER_VERSION = 3


def endpoint_fingerprint(base_url: str) -> str:
    normalized = base_url.strip().rstrip("/")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class CustomResponsesCertificate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: Literal[1] = COMPATIBILITY_SCHEMA_VERSION
    endpoint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model: str = Field(min_length=1)
    sdk_version: str = Field(min_length=1)
    policy_version: int = Field(ge=1)
    response_schema_version: int = Field(ge=1)
    dataset_id: str = Field(min_length=1)
    scorer_version: int = Field(ge=1)
    generated_at: str = Field(min_length=1)
    gate_cases: dict[str, bool] = Field(min_length=1)
    all_passed: bool

    @model_validator(mode="after")
    def _complete_gate(self) -> "CustomResponsesCertificate":
        if self.all_passed != all(self.gate_cases.values()):
            raise ValueError("all_passed must match every compatibility gate case")
        return self


class AgentCompatibilityStore:
    def __init__(self, data_dir: str | os.PathLike[str]) -> None:
        self.path = Path(data_dir) / "agent" / "compatibility" / "custom-responses.json"

    def load(self) -> CustomResponsesCertificate | None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return CustomResponsesCertificate.model_validate(raw)
        except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
            return None

    def is_compatible(
        self,
        *,
        base_url: str,
        model: str,
        sdk_version: str,
        policy_version: int,
        response_schema_version: int,
        dataset_id: str = PORTFOLIO_DATASET_ID,
        scorer_version: int = PORTFOLIO_SCORER_VERSION,
    ) -> bool:
        certificate = self.load()
        return bool(
            certificate is not None
            and certificate.all_passed
            and certificate.endpoint_sha256 == endpoint_fingerprint(base_url)
            and certificate.model == model
            and certificate.sdk_version == sdk_version
            and certificate.policy_version == policy_version
            and certificate.response_schema_version == response_schema_version
            and certificate.dataset_id == dataset_id
            and certificate.scorer_version == scorer_version
        )

    def save(self, certificate: CustomResponsesCertificate) -> None:
        if not certificate.all_passed:
            raise ValueError("A failing custom endpoint report cannot be certified")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as handle:
            handle.write(certificate.model_dump_json(indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, self.path)

    @staticmethod
    def certificate(
        *,
        base_url: str,
        model: str,
        sdk_version: str,
        policy_version: int,
        response_schema_version: int,
        gate_cases: dict[str, bool],
    ) -> CustomResponsesCertificate:
        return CustomResponsesCertificate(
            endpoint_sha256=endpoint_fingerprint(base_url),
            model=model,
            sdk_version=sdk_version,
            policy_version=policy_version,
            response_schema_version=response_schema_version,
            dataset_id=PORTFOLIO_DATASET_ID,
            scorer_version=PORTFOLIO_SCORER_VERSION,
            generated_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            gate_cases=gate_cases,
            all_passed=all(gate_cases.values()),
        )


__all__ = [
    "AgentCompatibilityStore",
    "COMPATIBILITY_SCHEMA_VERSION",
    "CustomResponsesCertificate",
    "PORTFOLIO_DATASET_ID",
    "PORTFOLIO_SCORER_VERSION",
    "endpoint_fingerprint",
]
