from __future__ import annotations

from pydantic import BaseModel, Field


class TaskItem(BaseModel):
    task_id: str = Field(
        description="Unique identifier for the task, e.g., 'weather_001'"
    )
    description: str = Field(description="Detailed purpose of the task")