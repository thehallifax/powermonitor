#!/usr/bin/env python3
"""Household-scoped import framework for local upload workflows."""

from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Protocol
from uuid import uuid4

from .models import HouseholdContext


@dataclass(frozen=True)
class ImportSource:
    source_id: str
    display_name: str
    supported_extensions: tuple[str, ...]
    description: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ImportValidationIssue:
    severity: str
    message: str
    row: Optional[int] = None
    field: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ImportPreview:
    source: ImportSource
    total_records: int
    valid_records: int
    date_start: Optional[str] = None
    date_end: Optional[str] = None
    total_energy_kwh: float = 0.0
    issues: list[ImportValidationIssue] = field(default_factory=list)
    sample_records: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ImportResult:
    job_id: str
    source: ImportSource
    status: str
    processed_records: int = 0
    inserted_records: int = 0
    updated_records: int = 0
    issues: list[ImportValidationIssue] = field(default_factory=list)
    message: str = ""

    @property
    def success(self) -> bool:
        return self.status == "completed"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ImportJob:
    job_id: str
    household_id: str
    source: ImportSource
    filename: str
    status: str
    created_at: str
    updated_at: str
    preview: Optional[ImportPreview] = None
    result: Optional[ImportResult] = None

    @classmethod
    def create(
        cls,
        household_id: str,
        source: ImportSource,
        filename: str,
        status: str = "previewed",
    ) -> "ImportJob":
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        return cls(
            job_id=uuid4().hex,
            household_id=household_id,
            source=source,
            filename=Path(filename).name,
            status=status,
            created_at=now,
            updated_at=now,
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class Importer(Protocol):
    source: ImportSource

    def detect(self, file_path: Path) -> bool:
        ...

    def parse(self, file_path: Path) -> Any:
        ...

    def validate(self, parsed_data: Any) -> list[ImportValidationIssue]:
        ...

    def preview(self, parsed_data: Any) -> ImportPreview:
        ...

    def commit(
        self,
        parsed_data: Any,
        household_context: HouseholdContext,
        job_id: Optional[str] = None,
    ) -> ImportResult:
        ...


def detect_importer(file_path: Path, importers: list[Importer]) -> Optional[Importer]:
    for importer in importers:
        if importer.detect(file_path):
            return importer
    return None
