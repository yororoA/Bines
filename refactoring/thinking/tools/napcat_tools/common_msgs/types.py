from pydantic import BaseModel, Field
from typing import Literal, Optional, Union, Annotated


# -- Message data types --
class MSG_TXT(BaseModel):
    text: str = Field(description="The text content of the message.")


class MSG_AT(BaseModel):
    qq: Union[str, Literal["all"]] = Field(
        description="The qq_number(user_id) of the user to be mentioned. If all, then mention all users in the group."
    )


class MSG_IMG(BaseModel):
    file: str = Field(description="The url of the image to be sent.")


# -- Message Segment Model --
class TextSegment(BaseModel):
    type: Literal["text"] = "text"
    data: MSG_TXT


class AtSegment(BaseModel):
    type: Literal["at"] = "at"
    data: MSG_AT


class ImgSegment(BaseModel):
    type: Literal["img"] = "img"
    data: MSG_IMG


# -- Message Segment union --
MSG_SEGMENT = Annotated[
    Union[TextSegment, AtSegment, ImgSegment],
    Field(description="type"),
]


class SEND_MSG(BaseModel):
    message_type: Literal["private", "group"] = Field(
        description="The type of the message, private or group."
        + "\nFor example, private for a single user, group for a group of users."
    )
    user_id: Optional[str] = Field(
        description="The user ID of the recipient of the message. Fill in if message_type is private."
    )
    group_id: Optional[str] = Field(
        description="The group ID of the recipient of the message. Fill in if message_type is group."
    )
    message: list[MSG_SEGMENT] = Field(description="The content of the message.")
