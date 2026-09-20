"""请求体模型（Pydantic）。"""
from __future__ import annotations

from typing import List, Union

from pydantic import BaseModel, Field, model_validator

TopicInput = Union[str, List[str]]


class ManuscriptIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    topics: TopicInput = ""
    required_reviews: int = Field(default=3, ge=1, le=20)
    # 所需主审人数：0~2，且不得超过所需评审总数（0=不要求主审）
    required_chairs: int = Field(default=0, ge=0, le=2)

    @model_validator(mode="after")
    def _check_chairs_within_reviews(self):
        if self.required_chairs > self.required_reviews:
            raise ValueError("所需主审人数不得超过所需评审总数")
        return self


class ManuscriptPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    topics: TopicInput | None = None
    required_reviews: int | None = Field(default=None, ge=1, le=20)
    required_chairs: int | None = Field(default=None, ge=0, le=2)


class ReviewerIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    topics: TopicInput = ""
    capacity: int = Field(default=3, ge=0, le=50)
    institution: str = Field(default="", max_length=120)
    can_chair: bool = False  # 是否具备“可任主审”资格


class ReviewerPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    topics: TopicInput | None = None
    capacity: int | None = Field(default=None, ge=0, le=50)
    institution: str | None = Field(default=None, max_length=120)
    can_chair: bool | None = None


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
