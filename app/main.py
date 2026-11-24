"""取帧清单服务：录入扫描记录、编排并查询取帧清单。"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session, object_session

from . import models, schemas
from .planner import LockInput, Plan, PlanningError, ScanInput, Segment, plan_manifest

def _seed_sample() -> None:
    if os.environ.get("SEED_SAMPLE") != "1":
        return
    db = models.SessionLocal()
    try:
        if db.query(models.Roll).filter_by(name="sample-reel").first():
            return
        roll = models.Roll(name="sample-reel", first_frame=1, last_frame=240)
        db.add(roll)
        db.flush()
        seed_scans = [
            ("sample_scan01.dpx", 0, [(1, 120)]),
            ("sample_scan02.dpx", -80, [(81, 240)]),
        ]
        for filename, offset, segs in seed_scans:
            scan = models.Scan(roll=roll, filename=filename, offset=offset)
            db.add(scan)
            db.flush()
            for start, end in segs:
                db.add(models.UsableSegment(scan=scan, start_frame=start, end_frame=end))
        plan = run_replan(db, roll)
        db.commit()
        print(f"[seed] sample-reel 已创建，换文件 {plan.switch_count} 次")
    except (PlanningError, ValueError) as exc:
        db.rollback()
        print(f"[seed] 示例数据编排失败（未写入）: {exc}")
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    _seed_sample()
    yield


app = FastAPI(
    title="胶片扫描取帧清单服务",
    version="1.0.0",
    lifespan=lifespan,
    description=(
        "登记一卷胶片的原片帧号范围与各次扫描（文件名、档内偏移、可用段），"
        "按帧段编排换文件次数最少的取帧清单。换文件时接入前两帧须在新旧文件中"
        "均可用；锁定帧段在重编时保留来源文件与档内帧号；重编失败时旧记录与清单"
        "继续生效。"
    ),
)

models.init_db()


def get_db():
    db = models.SessionLocal()
    try:
        yield db
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 编排服务（事务边界：成功才一起更新记录和清单，失败整体回滚）
# --------------------------------------------------------------------------- #
def _planner_inputs(roll: models.Roll):
    scans: list[ScanInput] = []
    for scan in roll.scans:
        scans.append(
            ScanInput(
                filename=scan.filename,
                offset=scan.offset,
                segments=tuple(
                    Segment(seg.start_frame, seg.end_frame) for seg in scan.segments
                ),
            )
        )
    locks = [
        LockInput(lock.start_frame, lock.end_frame, lock.filename)
        for lock in roll.locks
    ]
    return scans, locks


def _persist_plan(roll: models.Roll, plan: Plan) -> None:
    manifest = roll.manifest
    if manifest is None:
        manifest = models.Manifest(roll=roll)
    manifest.switch_count = plan.switch_count
    manifest.runs_json = json.dumps([run.as_dict() for run in plan.runs])
    manifest.switches_json = json.dumps([sw.as_dict() for sw in plan.switches])
    if manifest.id is None:
        object_session(roll).add(manifest)


def run_replan(db: Session, roll: models.Roll) -> Plan:
    """在当前事务内重编；失败抛 :class:`PlanningError`，由调用方回滚。"""
    scans, locks = _planner_inputs(roll)
    plan = plan_manifest(roll.first_frame, roll.last_frame, scans, locks)
    _persist_plan(roll, plan)
    return plan


def _planning_http_error(exc: PlanningError) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "error": "planning_failed",
            "message": str(exc),
            "first_gap_frame": exc.first_gap_frame,
        },
    )


def _serialize_manifest(roll: models.Roll) -> schemas.ManifestOut:
    manifest = roll.manifest
    if manifest is None:
        raise HTTPException(status_code=404, detail="该卷尚无可用清单（尚未成功编排）")
    return schemas.ManifestOut(
        roll_id=roll.id,
        roll_name=roll.name,
        first_frame=roll.first_frame,
        last_frame=roll.last_frame,
        switch_count=manifest.switch_count,
        runs=json.loads(manifest.runs_json),
        switches=json.loads(manifest.switches_json),
    )


# --------------------------------------------------------------------------- #
# 接口
# --------------------------------------------------------------------------- #
@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}


@app.post(
    "/rolls",
    status_code=201,
    tags=["rolls"],
    summary="登记胶片卷（可同时录入扫描并立即编排）",
    responses={
        201: {"description": "成功编排时返回清单；无扫描时返回建档确认"},
        409: {"model": schemas.ErrorOut, "description": "编排失败，未写入任何记录"},
    },
)
def create_roll(payload: schemas.RollCreate, db: Session = Depends(get_db)):
    if payload.first_frame > payload.last_frame:
        raise HTTPException(status_code=422, detail="原片帧号范围非法")
    if db.query(models.Roll).filter_by(name=payload.name).first():
        raise HTTPException(status_code=409, detail=f"卷名已存在: {payload.name}")

    roll = models.Roll(
        name=payload.name,
        first_frame=payload.first_frame,
        last_frame=payload.last_frame,
    )
    db.add(roll)
    db.flush()

    for scan_in in payload.scans:
        scan = models.Scan(
            roll=roll, filename=scan_in.filename, offset=scan_in.offset
        )
        db.add(scan)
        db.flush()
        for seg in scan_in.segments:
            db.add(
                models.UsableSegment(
                    scan=scan,
                    start_frame=seg.start_frame,
                    end_frame=seg.end_frame,
                )
            )

    if not payload.scans:
        db.commit()
        return {
            "status": "胶片卷已创建，尚无扫描记录，未生成清单",
            "roll_id": roll.id,
            "manifest": None,
        }

    try:
        run_replan(db, roll)
    except PlanningError as exc:
        db.rollback()
        raise _planning_http_error(exc)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc))
    db.commit()
    db.refresh(roll)
    return _serialize_manifest(roll)


@app.get("/rolls", tags=["rolls"], summary="列出所有胶片卷")
def list_rolls(db: Session = Depends(get_db)):
    rolls = db.query(models.Roll).order_by(models.Roll.id).all()
    return [
        {
            "id": r.id,
            "name": r.name,
            "first_frame": r.first_frame,
            "last_frame": r.last_frame,
            "scan_count": len(r.scans),
            "lock_count": len(r.locks),
            "has_manifest": r.manifest is not None,
        }
        for r in rolls
    ]


@app.get("/rolls/{roll_id}", tags=["rolls"], summary="卷详情：扫描、可用段、锁定段")
def get_roll(roll_id: int, db: Session = Depends(get_db)):
    roll = db.get(models.Roll, roll_id)
    if roll is None:
        raise HTTPException(status_code=404, detail="胶片卷不存在")
    return {
        "id": roll.id,
        "name": roll.name,
        "first_frame": roll.first_frame,
        "last_frame": roll.last_frame,
        "scans": [
            {
                "filename": s.filename,
                "offset": s.offset,
                "segments": [
                    {"start_frame": g.start_frame, "end_frame": g.end_frame}
                    for g in s.segments
                ],
            }
            for s in roll.scans
        ],
        "locks": [
            {
                "id": lk.id,
                "start_frame": lk.start_frame,
                "end_frame": lk.end_frame,
                "filename": lk.filename,
            }
            for lk in roll.locks
        ],
        "has_manifest": roll.manifest is not None,
    }


@app.put(
    "/rolls/{roll_id}/scans",
    response_model=schemas.ReplanResult,
    tags=["rolls"],
    summary="补交扫描 / 更正可用段：整体替换扫描记录并重新编排",
    responses={
        409: {"model": schemas.ErrorOut, "description": "编排失败，旧记录与旧清单继续生效"}
    },
)
def replace_scans(
    roll_id: int,
    scans: list[schemas.ScanIn],
    db: Session = Depends(get_db),
):
    roll = db.get(models.Roll, roll_id)
    if roll is None:
        raise HTTPException(status_code=404, detail="胶片卷不存在")
    if not scans:
        raise HTTPException(status_code=422, detail="至少需要一份扫描")

    try:
        # 同一事务内先替换记录，再重编；任何一步失败整体回滚
        for scan in list(roll.scans):
            db.delete(scan)
        db.flush()
        db.expire(roll)  # 清空身份映射中的旧扫描，避免重名校验读到已删对象
        for scan_in in scans:
            scan = models.Scan(roll=roll, filename=scan_in.filename, offset=scan_in.offset)
            db.add(scan)
            db.flush()
            for seg in scan_in.segments:
                db.add(
                    models.UsableSegment(
                        scan=scan,
                        start_frame=seg.start_frame,
                        end_frame=seg.end_frame,
                    )
                )
        db.flush()
        plan = run_replan(db, roll)
    except PlanningError as exc:
        db.rollback()
        raise _planning_http_error(exc)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc))

    db.commit()
    db.refresh(roll)
    return schemas.ReplanResult(
        status=f"重编成功，换文件 {plan.switch_count} 次",
        manifest=_serialize_manifest(roll),
    )


@app.post(
    "/rolls/{roll_id}/locks",
    response_model=schemas.ReplanResult,
    status_code=201,
    tags=["rolls"],
    summary="锁定已修复帧段并重编（保留该段文件与档内帧号）",
    responses={409: {"model": schemas.ErrorOut, "description": "编排失败，锁定未写入"}},
)
def add_lock(
    roll_id: int,
    payload: schemas.LockIn,
    db: Session = Depends(get_db),
):
    roll = db.get(models.Roll, roll_id)
    if roll is None:
        raise HTTPException(status_code=404, detail="胶片卷不存在")
    if payload.start_frame > payload.end_frame:
        raise HTTPException(status_code=422, detail="锁定段起止非法")
    if not any(s.filename == payload.filename for s in roll.scans):
        raise HTTPException(
            status_code=422,
            detail=f"当前扫描记录中没有文件 {payload.filename}，无法锁定",
        )

    try:
        db.add(
            models.Lock(
                roll=roll,
                start_frame=payload.start_frame,
                end_frame=payload.end_frame,
                filename=payload.filename,
            )
        )
        db.flush()
        plan = run_replan(db, roll)
    except PlanningError as exc:
        db.rollback()
        raise _planning_http_error(exc)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc))

    db.commit()
    db.refresh(roll)
    return schemas.ReplanResult(
        status=f"锁定成功，重编完成，换文件 {plan.switch_count} 次",
        manifest=_serialize_manifest(roll),
    )


@app.delete(
    "/rolls/{roll_id}/locks/{lock_id}",
    response_model=schemas.ReplanResult,
    tags=["rolls"],
    summary="解除锁定并重编",
    responses={409: {"model": schemas.ErrorOut, "description": "编排失败，解锁未生效"}},
)
def delete_lock(roll_id: int, lock_id: int, db: Session = Depends(get_db)):
    roll = db.get(models.Roll, roll_id)
    if roll is None:
        raise HTTPException(status_code=404, detail="胶片卷不存在")
    lock = db.get(models.Lock, lock_id)
    if lock is None or lock.roll_id != roll.id:
        raise HTTPException(status_code=404, detail="锁定段不存在")

    try:
        db.delete(lock)
        db.flush()
        plan = run_replan(db, roll)
    except PlanningError as exc:
        db.rollback()
        raise _planning_http_error(exc)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc))

    db.commit()
    db.refresh(roll)
    return schemas.ReplanResult(
        status=f"已解锁，重编完成，换文件 {plan.switch_count} 次",
        manifest=_serialize_manifest(roll),
    )


@app.post(
    "/rolls/{roll_id}/replan",
    response_model=schemas.ReplanResult,
    tags=["rolls"],
    summary="按当前记录重新编排",
)
def replan(roll_id: int, db: Session = Depends(get_db)):
    roll = db.get(models.Roll, roll_id)
    if roll is None:
        raise HTTPException(status_code=404, detail="胶片卷不存在")
    try:
        plan = run_replan(db, roll)
    except PlanningError as exc:
        db.rollback()
        raise _planning_http_error(exc)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc))
    db.commit()
    db.refresh(roll)
    return schemas.ReplanResult(
        status=f"重编成功，换文件 {plan.switch_count} 次",
        manifest=_serialize_manifest(roll),
    )


@app.get(
    "/rolls/{roll_id}/manifest",
    response_model=schemas.ManifestOut,
    tags=["rolls"],
    summary="查询当前取帧清单",
)
def get_manifest(roll_id: int, db: Session = Depends(get_db)):
    roll = db.get(models.Roll, roll_id)
    if roll is None:
        raise HTTPException(status_code=404, detail="胶片卷不存在")
    return _serialize_manifest(roll)
