from pydantic import BaseModel, Field, model_validator
from typing import Any, Optional


class TaskItem(BaseModel):
    task_id: str = Field(
        description="Unique identifier for the task, e.g., 'weather_001'"
    )
    description: str = Field(description="Detailed purpose of the task")


class ReplyInput(BaseModel):
    tasks: list[TaskItem] = Field(default_factory=list)
    Final: bool = Field(default=False)
    message: str = Field(default="")
    persona_snapshot: dict[str, Any] = Field(
        default_factory=dict,
        description="Persona context for the ReplyAgent to maintain personality consistency.",
    )
    already_said: list[str] = Field(
        default_factory=list,
        description="List of things already communicated to the user, to avoid repetition.",
    )
    soul_prompt: str = Field(
        default="",
        description="SOUL.md content defining the agent's core personality.",
    )


class ManagerRoute(BaseModel):
    performer_task: Optional[TaskItem] = Field(
        default=None,
        description="The single task to send to the performer node. "
        "Set to None if no more tasks are needed.",
    )
    goto_advance_reply: bool = Field(
        default=False,
        description="Whether to send an intermediate progress update before continuing.",
    )
    advance_reply_hint: Optional[str] = Field(
        default=None,
        description="Hint for what the advance reply should convey. "
        "Only used when goto_advance_reply is True.",
    )
    goto_final_reply: bool = Field(
        default=False,
        description="Whether to skip performer and go directly to final_reply.",
    )
    final_reply_hint: Optional[str] = Field(
        default=None,
        description="Hint for what the final reply should convey. "
        "Only used when goto_final_reply is True.",
    )
    thoughts: str = Field(
        description="The thoughts of your current decision.",
    )

    @model_validator(mode="after")
    def validate_exclusive_routing(self) -> ManagerRoute:
        if self.goto_advance_reply and self.goto_final_reply:
            self.goto_advance_reply = False
        return self