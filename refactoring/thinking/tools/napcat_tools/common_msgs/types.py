from pydantic import BaseModel, Field
from typing import Literal, Optional, Union, Annotated


class MSG_TXT(BaseModel):
    text: str = Field(description="The text content of the message.")


class MSG_AT(BaseModel):
    qq: Union[str, Literal["all"]] = Field(
        description="The qq_number(user_id) of the user to be mentioned. If all, then mention all users in the group."
    )


class MSG_IMG(BaseModel):
    file: str = Field(description="The url of the image to be sent.")


class TextSegment(BaseModel):
    type: Literal["text"] = "text"
    data: MSG_TXT


class AtSegment(BaseModel):
    type: Literal["at"] = "at"
    data: MSG_AT


class ImgSegment(BaseModel):
    type: Literal["img"] = "img"
    data: MSG_IMG


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


class DELETE_MSG(BaseModel):
    message_id: Union[int, str] = Field(description="要撤回的消息ID")


class GET_MSG(BaseModel):
    message_id: Union[int, str] = Field(description="要获取的消息ID")


class FORWARD_NEWS(BaseModel):
    text: str = Field(description="新闻条目文本")


class SEND_FORWARD_MSG(BaseModel):
    message_type: Optional[Literal["private", "group"]] = Field(
        default=None,
        description="消息类型 (private/group)，与 user_id 或 group_id 配合使用"
    )
    user_id: Optional[str] = Field(default=None, description="用户QQ号，私聊时填写")
    group_id: Optional[str] = Field(default=None, description="群号，群聊时填写")
    message: list[MSG_SEGMENT] = Field(description="合并转发的消息内容")
    auto_escape: Optional[Union[bool, str]] = Field(default=None, description="是否作为纯文本发送")
    source: Optional[str] = Field(default=None, description="合并转发来源")
    news: Optional[list[FORWARD_NEWS]] = Field(default=None, description="合并转发新闻")
    summary: Optional[str] = Field(default=None, description="合并转发摘要")
    prompt: Optional[str] = Field(default=None, description="合并转发提示")


class SEND_GROUP_FORWARD_MSG(BaseModel):
    group_id: str = Field(description="群号")
    message: list[MSG_SEGMENT] = Field(description="合并转发的消息内容")
    source: Optional[str] = Field(default=None, description="合并转发来源")
    news: Optional[list[FORWARD_NEWS]] = Field(default=None, description="合并转发新闻")
    summary: Optional[str] = Field(default=None, description="合并转发摘要")
    prompt: Optional[str] = Field(default=None, description="合并转发提示")


class SEND_PRIVATE_FORWARD_MSG(BaseModel):
    user_id: str = Field(description="用户QQ号")
    message: list[MSG_SEGMENT] = Field(description="合并转发的消息内容")
    source: Optional[str] = Field(default=None, description="合并转发来源")
    news: Optional[list[FORWARD_NEWS]] = Field(default=None, description="合并转发新闻")
    summary: Optional[str] = Field(default=None, description="合并转发摘要")
    prompt: Optional[str] = Field(default=None, description="合并转发提示")


class GET_GROUP_MSG_HISTORY(BaseModel):
    group_id: str = Field(description="群号")
    message_seq: Optional[int] = Field(default=None, description="起始消息序号，不填则从最新开始")
    count: Optional[int] = Field(default=20, description="获取的消息数量，默认20")


class GET_FRIEND_MSG_HISTORY(BaseModel):
    user_id: str = Field(description="好友QQ号")
    message_seq: Optional[int] = Field(default=None, description="起始消息序号，不填则从最新开始")
    count: Optional[int] = Field(default=20, description="获取的消息数量，默认20")


class SEND_POKE(BaseModel):
    user_id: str = Field(description="要戳的用户QQ号")
    group_id: Optional[str] = Field(default=None, description="群号，群聊戳一戳时填写，不填则为私聊戳一戳")