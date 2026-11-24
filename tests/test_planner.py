"""编排算法单元测试（区间模型，不启 Web / 数据库）。"""

import pytest

from app.planner import (
    LockInput,
    PlanningError,
    ScanInput,
    Segment,
    plan_manifest,
)


def scan(filename, offset, *pairs) -> ScanInput:
    return ScanInput(
        filename=filename,
        offset=offset,
        segments=tuple(Segment(a, b) for a, b in pairs),
    )


def test_basic_two_files_single_switch_and_infile_frames():
    # scan01: 原片 1..120；scan02: 原片 81..240，offset=-80（档内 = 原片+80）
    plan = plan_manifest(
        1, 240,
        [scan("a.dpx", 0, (1, 120)), scan("b.dpx", -80, (81, 240))],
    )
    assert plan.switch_count == 1
    assert [r.filename for r in plan.runs] == ["a.dpx", "b.dpx"]
    first, second = plan.runs
    # 换文件点尽量靠后 => 第一段吃到 120，第二段从 121 开始
    assert (first.orig_start, first.orig_end) == (1, 120)
    assert (second.orig_start, second.orig_end) == (121, 240)
    # 档内帧号 = 原片 - offset
    assert (first.file_start, first.file_end) == (1, 120)
    assert (second.file_start, second.file_end) == (201, 320)

    sw = plan.switches[0]
    assert sw.at_frame == 121
    assert sw.overlap_frames == [119, 120]
    assert sw.from_file_frames == [119, 120]
    assert sw.to_file_frames == [199, 200]

    # 清单覆盖整卷且无缝
    covered = []
    for run in plan.runs:
        covered.extend(range(run.orig_start, run.orig_end + 1))
    assert covered == list(range(1, 241))


def test_switch_requires_two_overlap_frames():
    # 旧文件 1..100，新文件 101..200：接入点 101 前两帧 99,100 不在新文件中 => 接不上
    with pytest.raises(PlanningError) as exc:
        plan_manifest(
            1, 200,
            [scan("a.dpx", 0, (1, 100)), scan("b.dpx", 0, (101, 200))],
        )
    assert exc.value.first_gap_frame == 101


def test_gap_reports_first_unconnectable_frame():
    with pytest.raises(PlanningError) as exc:
        plan_manifest(1, 100, [scan("a.dpx", 0, (1, 50))])
    assert exc.value.first_gap_frame == 51


def test_minimize_switch_count_with_three_files():
    # a:1..80, b:41..200, c:161..240，两两重叠均 >=2 帧
    plan = plan_manifest(
        1, 240,
        [
            scan("a.dpx", 0, (1, 80)),
            scan("b.dpx", 0, (41, 200)),
            scan("c.dpx", 0, (161, 240)),
        ],
    )
    # a、c 无法直接衔接（无重叠），最优为 a->b->c，两次换文件
    assert plan.switch_count == 2
    assert [r.filename for r in plan.runs] == ["a.dpx", "b.dpx", "c.dpx"]
    # 每段尽量吃满：a 吃到 80，b 吃到 200，c 从 201 开始
    assert plan.runs[0].orig_end == 80
    assert plan.runs[-1].orig_start == 201


def test_locked_segment_preserves_file_and_infile_frames():
    # 无锁时 b 覆盖 41..240 会让最优变成 a->b 一次切换且 a 只吃到 40；
    # 锁定 20..40 必须留在 a，重编后该帧段归属与档内帧号不变。
    plan = plan_manifest(
        1, 240,
        [scan("a.dpx", 0, (1, 120)), scan("b.dpx", -80, (81, 240))],
        locks=[LockInput(20, 40, "a.dpx")],
    )
    locked_run = next(r for r in plan.runs if r.orig_start <= 20 and r.orig_end >= 40)
    assert locked_run.filename == "a.dpx"
    assert locked_run.file_start <= 20 and locked_run.file_end >= 40
    assert locked_run.locked is True
    # 锁定段没有被切点拆开
    assert not any(r.orig_start in range(21, 41) for r in plan.runs)


def test_lock_against_unavailable_file_fails():
    with pytest.raises(PlanningError):
        plan_manifest(
            1, 100,
            [scan("a.dpx", 0, (1, 50)), scan("b.dpx", 0, (49, 100))],
            locks=[LockInput(60, 70, "a.dpx")],  # a 在该区间不可用
        )


def test_merged_segments_and_non_one_range():
    # 原片从 1001 开始；同一文件两段首尾相接应合并
    plan = plan_manifest(
        1001, 1100,
        [
            scan("a.dpx", 1000, (1001, 1050), (1051, 1100)),
        ],
    )
    assert plan.switch_count == 0
    assert len(plan.runs) == 1
    run = plan.runs[0]
    assert (run.orig_start, run.orig_end) == (1001, 1100)
    assert (run.file_start, run.file_end) == (1, 100)


def test_overlapping_segments_merge_within_file():
    plan = plan_manifest(
        1, 100,
        [scan("a.dpx", 0, (1, 60), (55, 100))],
    )
    assert len(plan.runs) == 1
    assert (plan.runs[0].orig_start, plan.runs[0].orig_end) == (1, 100)


def test_out_of_range_segment_rejected():
    with pytest.raises(ValueError):
        plan_manifest(1, 100, [scan("a.dpx", 0, (1, 101))])


def test_switch_at_roll_start_has_no_overlap_constraint():
    # b 从首帧即可用，直接用 b 全卷（零切换），不受"前两帧"限制
    plan = plan_manifest(
        1, 100,
        [scan("a.dpx", 0, (1, 50)), scan("b.dpx", 0, (1, 100))],
    )
    assert plan.switch_count == 0
    assert plan.runs[0].filename == "b.dpx"


def test_deterministic_tie_break():
    # 两个文件都覆盖全卷 => 零切换，按文件名字典序确定
    plan = plan_manifest(
        1, 100,
        [scan("z.dpx", 0, (1, 100)), scan("a.dpx", 0, (1, 100))],
    )
    assert plan.runs[0].filename == "a.dpx"
