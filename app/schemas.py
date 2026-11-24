"""Pydantic 请求 / 响应模式（含 Swagger 示例）。"""

from __future__ import annotations

from pydantic import BaseModel, Field, ConfigDict


class SegmentIn(BaseModel):
    start_frame: int = Field(..., description="可用段起始原片帧号（含）")
    end_frame: int = Field(..., description="可用段结束原片帧号（含）")


class ScanIn(BaseModel):
    filename: str = Field(..., description="扫描文件名，卷内唯一")
    offset: int = Field(
        ...,
        description="档内帧号相对原片帧号的偏移：档内帧号 = 原片帧号 - offset",
    )
    segments: list[SegmentIn] = Field(..., description="该文件的原片帧号可用段")


class RollCreate(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "reel-A",
                "first_frame": 1,
                "last_frame": 240,
                "scans": [
                    {
                        "filename": "reelA_scan01.dpx",
                        "offset": 0,
                        "segments": [{"start_frame": 1, "end_frame": 120}],
                    },
                    {
                        "filename": "reelA_scan02.dpx",
                        "offset": -80,
                        "segments": [{"start_frame": 81, "end_frame": 240}],
                    },
                ],
            }
        }
    )

    name: str
    first_frame: int = Field(..., description="整卷起始原片帧号（含）")
    last_frame: int = Field(..., description="整卷结束原片帧号（含）")
    scans: list[ScanIn] = Field(default_factory=list)


class LockIn(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "start_frame": 20,
                "end_frame": 40,
                "filename": "reelA_scan01.dpx",
            }
        }
    )

    start_frame: int
    end_frame: int
    filename: str = Field(..., description="锁定帧段必须保留的来源文件")


class RunOut(BaseModel):
    filename: str
    orig_start: int = Field(..., description="本段起始原片帧号")
    orig_end: int = Field(..., description="本段结束原片帧号")
    file_start: int = Field(..., description="本段起始档内帧号")
    file_end: int = Field(..., description="本段结束档内帧号")
    locked: bool


class SwitchOut(BaseModel):
    at_frame: int = Field(..., description="新文件接入的第一个原片帧号")
    from_filename: str
    to_filename: str
    overlap_frames: list[int] = Field(
        ..., description="接入前两帧的原片帧号（调色对照帧）"
    )
    from_file_frames: list[int] = Field(..., description="两帧在旧文件中的档内帧号")
    to_file_frames: list[int] = Field(..., description="两帧在新文件中的档内帧号")


class ManifestOut(BaseModel):
    roll_id: int
    roll_name: str
    first_frame: int
    last_frame: int
    switch_count: int
    runs: list[RunOut]
    switches: list[SwitchOut]


class ReplanResult(BaseModel):
    status: str
    manifest: ManifestOut


class ErrorOut(BaseModel):
    detail: str
