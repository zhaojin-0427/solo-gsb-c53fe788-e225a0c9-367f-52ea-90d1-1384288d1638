"""Pydantic request models with the field-level rules from the spec."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class ItemIn(BaseModel):
    fixture: str = Field(min_length=1, max_length=64)
    channel: int = Field(ge=1, le=512)
    target: int = Field(ge=0, le=255)
    duration: int = Field(ge=0)
    start_mode: Literal["abs", "ref"]
    start_ms: Optional[int] = Field(default=None, ge=0)
    ref_id: Optional[int] = Field(default=None, ge=1)
    ref_edge: Optional[Literal["start", "end"]] = None
    ref_offset: Optional[int] = Field(default=None, ge=0)

    @field_validator("fixture")
    @classmethod
    def _fixture_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("灯具名称不能为空")
        return v

    @model_validator(mode="after")
    def _check_mode_fields(self) -> "ItemIn":
        if self.start_mode == "abs":
            if self.start_ms is None:
                raise ValueError("绝对时间模式必须提供 start_ms")
        else:
            if self.ref_id is None or self.ref_edge is None:
                raise ValueError("引用模式必须提供 ref_id 与 ref_edge")
            if self.ref_offset is None:
                object.__setattr__(self, "ref_offset", 0)
        return self


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("工程名称不能为空")
        return v
