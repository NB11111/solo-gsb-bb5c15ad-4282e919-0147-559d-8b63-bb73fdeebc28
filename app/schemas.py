"""请求体模型（Pydantic）。"""
from __future__ import annotations

from typing import List, Union

from pydantic import BaseModel, Field, field_validator

TopicInput = Union[str, List[str]]


class ManuscriptIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    topics: TopicInput = ""
    required_reviews: int = Field(default=3, ge=1, le=20)


class ManuscriptPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    topics: TopicInput | None = None
    required_reviews: int | None = Field(default=None, ge=1, le=20)


class ReviewerIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    topics: TopicInput = ""
    capacity: int = Field(default=3, ge=0, le=50)
    institution: str = Field(default="", max_length=120)


class ReviewerPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    topics: TopicInput | None = None
    capacity: int | None = Field(default=None, ge=0, le=50)
    institution: str | None = Field(default=None, max_length=120)


class ConflictIn(BaseModel):
    manuscript_id: int
    reviewer_id: int


class SettingsIn(BaseModel):
    default_required_reviews: int | None = Field(default=None, ge=1, le=20)
    default_capacity: int | None = Field(default=None, ge=0, le=50)
    mismatch_weight: int | None = Field(default=None, ge=0, le=100000)
    load_weight: int | None = Field(default=None, ge=0, le=100000)
    # 每篇稿件同机构最多人数；0 表示关闭机构多样性约束
    institution_cap: int | None = Field(default=None, ge=0, le=20)


class LockIn(BaseModel):
    manuscript_id: int
    reviewer_id: int
    locked: bool


class TopicsIn(BaseModel):
    """仅用于前端校验的占位模型（当前未使用，保留扩展）。"""

    @field_validator("*")
    @classmethod
    def noop(cls, v):
        return v
