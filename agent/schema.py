from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class Artifact(BaseModel):

    artifact_type: str

    name: str

    table: str | None = None

    when: str | None = None

    insert: bool | None = None

    update: bool | None = None

    type: str | None = None

    order: int | None = None

    description: str | None = None

    published: bool | None = None

    workflow_definition: dict[str, Any] | list[Any] | str | None = None

    workflow_steps: list[Artifact] | None = None

    script: str | None = None


Artifact.model_rebuild()
