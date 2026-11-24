"""取帧清单编排器（纯函数，不依赖数据库 / Web 框架）。

编排模型
========
一卷胶片的原片帧号是连续整数区间 ``[first_frame, last_frame]``。每份扫描文件
提供若干"可用段"（原片帧号区间），档内帧号 = 原片帧号 - ``offset``。

算法完全在 *区间 / 切点* 上运算，不逐帧展开：

1. 每份文件先把重叠或首尾相接的可用段合并成若干不相交区间；
2. 汇总所有区间端点（以及锁定段边界）作为候选切点，把整卷切成若干基本小段；
3. 以"切点 + 当前文件"为状态做 Dijkstra 最短路：
   - 同文件内向前推进不产生换文件次数；
   - 换到另一份文件要求 **接入前两帧**（新段起点的前两个原片帧）在新旧文件中
     都可用，因此换文件权重为 1，目标是换文件次数最少；
4. 锁定段必须由指定文件完整覆盖，锁定段内部不允许出现切点（不得被拆开）。

排不出时抛出 :class:`PlanningError`，其中 ``first_gap_frame`` 即第一个接不上的
原片帧。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import bisect


@dataclass(frozen=True)
class Segment:
    """原片帧号上的闭区间 [start, end]。"""

    start: int
    end: int

    def __post_init__(self) -> None:
        if not isinstance(self.start, int) or not isinstance(self.end, int):
            raise ValueError("段起止帧号必须为整数")
        if self.start > self.end:
            raise ValueError(f"非法段: start({self.start}) > end({self.end})")

    def intersects(self, other: "Segment") -> bool:
        return not (self.end < other.start or other.end < self.start)

    def contains(self, point: int) -> bool:
        return self.start <= point <= self.end


@dataclass(frozen=True)
class ScanInput:
    """一份扫描文件的输入：文件名、档内帧偏移、原片帧号可用段。"""

    filename: str
    offset: int
    segments: tuple[Segment, ...]


@dataclass(frozen=True)
class LockInput:
    """已修复并锁定的同源帧段：区间内必须使用 ``filename`` 且保留档内帧号。"""

    start: int
    end: int
    filename: str


@dataclass
class Run:
    """清单中的一段：原片帧 [orig_start, orig_end] 取自某文件。"""

    filename: str
    orig_start: int
    orig_end: int
    file_start: int
    file_end: int
    locked: bool = False

    def as_dict(self) -> dict:
        return {
            "filename": self.filename,
            "orig_start": self.orig_start,
            "orig_end": self.orig_end,
            "file_start": self.file_start,
            "file_end": self.file_end,
            "locked": self.locked,
        }


@dataclass
class Switch:
    """两段之间的换文件点，含调色对照所需的两帧在两份文件中的档内帧号。"""

    at_frame: int
    from_filename: str
    to_filename: str
    overlap_frames: list[int]
    from_file_frames: list[int]
    to_file_frames: list[int]

    def as_dict(self) -> dict:
        return {
            "at_frame": self.at_frame,
            "from_filename": self.from_filename,
            "to_filename": self.to_filename,
            "overlap_frames": self.overlap_frames,
            "from_file_frames": self.from_file_frames,
            "to_file_frames": self.to_file_frames,
        }


@dataclass
class Plan:
    runs: list[Run] = field(default_factory=list)
    switches: list[Switch] = field(default_factory=list)

    @property
    def switch_count(self) -> int:
        return len(self.switches)


class PlanningError(Exception):
    """编排失败。``first_gap_frame`` 为第一个接不上的原片帧（无可用起始时即首帧）。"""

    def __init__(self, message: str, first_gap_frame: int):
        super().__init__(message)
        self.first_gap_frame = first_gap_frame


# --------------------------------------------------------------------------- #
# 输入校验
# --------------------------------------------------------------------------- #
def _validate_segments(segments, first: int, last: int, owner: str) -> list[Segment]:
    merged: list[Segment] = []
    for seg in segments:
        if seg.start < first or seg.end > last:
            raise ValueError(
                f"{owner} 的可用段 [{seg.start}, {seg.end}] 超出原片范围 [{first}, {last}]"
            )
        # 插入并保持有序
        pos = bisect.bisect_left(merged, seg.start, key=lambda s: s.start)
        merged.insert(pos, seg)

    out: list[Segment] = []
    for seg in merged:
        if out and seg.start <= out[-1].end + 1:  # 重叠或首尾相接 => 合并（区间运算，不展帧）
            out[-1] = Segment(out[-1].start, max(out[-1].end, seg.end))
        else:
            out.append(Segment(seg.start, seg.end))
    return out


def _validate_inputs(
    first: int,
    last: int,
    scans: list[ScanInput],
    locks: list[LockInput],
) -> None:
    if not isinstance(first, int) or not isinstance(last, int) or first > last:
        raise ValueError("原片帧号范围非法")
    if not scans:
        raise PlanningError("没有任何扫描文件", first)

    names = [s.filename for s in scans]
    if len(set(names)) != len(names):
        raise ValueError("扫描文件名必须唯一")

    for lock in locks:
        if lock.start < first or lock.end > last:
            raise ValueError(
                f"锁定段 [{lock.start}, {lock.end}] 超出原片范围 [{first}, {last}]"
            )
        if lock.filename not in names:
            raise ValueError(f"锁定段引用了不存在的文件: {lock.filename}")

    # 锁定段之间不得相交（每帧至多有一个锁定归属）
    ordered = sorted(locks, key=lambda l: l.start)
    for prev, cur in zip(ordered, ordered[1:]):
        if cur.start <= prev.end:
            raise ValueError(
                f"锁定段重叠: [{prev.start},{prev.end}] 与 [{cur.start},{cur.end}]"
            )


# --------------------------------------------------------------------------- #
# 区间工具（均为 O(log n) 的区间查询，不逐帧扫描）
# --------------------------------------------------------------------------- #
class Coverage:
    """一份文件合并后的可用区间，支持覆盖 / 包含查询。"""

    def __init__(self, intervals: list[Segment]):
        self.intervals = intervals
        self.starts = [iv.start for iv in intervals]

    def covers(self, start: int, end: int) -> bool:
        """闭区间 [start, end] 是否被完整覆盖。"""
        if start > end:
            return False
        idx = bisect.bisect_right(self.starts, start) - 1
        return idx >= 0 and self.intervals[idx].end >= end

    def point_covered(self, point: int) -> bool:
        idx = bisect.bisect_right(self.starts, point) - 1
        return idx >= 0 and self.intervals[idx].contains(point)


def _blocks_with_other_lock(
    start: int, end: int, filename: str, locks: list[LockInput]
) -> bool:
    """(start, end) 内是否存在归属其他文件的锁定段（锁必须完整落在同一段内）。"""
    for lock in locks:
        if lock.end <= start or lock.start >= end:
            continue
        if lock.filename != filename:
            return True
    return False


# --------------------------------------------------------------------------- #
# 主编排
# --------------------------------------------------------------------------- #
def plan_manifest(
    first_frame: int,
    last_frame: int,
    scans: list[ScanInput],
    locks: list[LockInput] | None = None,
) -> Plan:
    """按帧段编排取帧清单。

    :raises PlanningError: 无法覆盖整卷，异常上带第一个接不上的原片帧。
    :raises ValueError: 输入本身非法（越界、重名、锁冲突等）。
    """
    locks = list(locks or [])
    _validate_inputs(first_frame, last_frame, scans, locks)

    cover: dict[str, Coverage] = {}
    offset_of: dict[str, int] = {}
    for scan in scans:
        cover[scan.filename] = Coverage(
            _validate_segments(scan.segments, first_frame, last_frame, scan.filename)
        )
        offset_of[scan.filename] = scan.offset

    filenames = sorted(cover)

    # 1) 候选切点：所有可用区间端点 + 1、锁边界、卷首尾。
    #    锁内部不产生切点 —— 锁定帧段必须完整地待在同一个 Run 里。
    cuts = {first_frame, last_frame + 1}
    for lock in locks:
        cuts.add(lock.start)
        cuts.add(lock.end + 1)
    for cov in cover.values():
        for iv in cov.intervals:
            cuts.add(iv.start)
            cuts.add(iv.end + 1)
    cuts = sorted(cuts)

    def valid_run(filename: str, start: int, end_exclusive: int) -> bool:
        """Run = 原片帧 [start, end_exclusive-1] 是否合法。"""
        end = end_exclusive - 1
        if start > end:
            return False
        if not cover[filename].covers(start, end):
            return False
        if _blocks_with_other_lock(start, end_exclusive, filename, locks):
            return False
        return True

    def overlaps_two_frames(prev_file: str, next_file: str, start: int) -> bool:
        """接入下一份文件前的两帧 (start-2, start-1) 须在新旧文件中都可用。"""
        a, b = start - 2, start - 1
        if a < first_frame:  # 卷首没有"接入前两帧"，起始段不受此约束
            return False
        return (
            cover[prev_file].point_covered(a)
            and cover[prev_file].point_covered(b)
            and cover[next_file].point_covered(a)
            and cover[next_file].point_covered(b)
        )

    # 2) Dijkstra：状态 = (切点下标, 当前文件)。同文件推进代价 0，换文件代价 1。
    #    代价相同按文件名路径字典序裁决，保证结果确定性。
    import heapq

    dist: dict[tuple[int, str], tuple[int, tuple[str, ...]]] = {}
    prev: dict[tuple[int, str], tuple[int, str]] = {}
    heap: list = []

    def push(state, cost, path, predecessor):
        old = dist.get(state)
        key = (cost, path)
        if old is None or key < old:
            dist[state] = key
            prev[state] = predecessor
            heapq.heappush(heap, (cost, path, state))

    target_index = len(cuts) - 1  # 切点 last_frame + 1 的下标
    for name in filenames:
        if valid_run(name, cuts[0], cuts[target_index]):
            push((target_index, name), 0, (name,), (-1, ""))
        for j in range(1, target_index):
            if valid_run(name, cuts[0], cuts[j]):
                push((j, name), 0, (name,), (-1, ""))

    while heap:
        cost, path, (i, fname) = heapq.heappop(heap)
        if dist.get((i, fname)) != (cost, path):
            continue  # 过期堆项
        if i == target_index:
            # 堆序保证第一个弹出的终点即最优解
            return _build_plan(i, fname, prev, cuts, cover, offset_of, locks, first_frame)

        # 同文件零代价推进
        for j in range(i + 1, target_index + 1):
            if valid_run(fname, cuts[i], cuts[j]):
                push((j, fname), cost, path, (i, fname))
            else:
                break  # 区间端点有序：更长的段只会覆盖更差，截断即可

        # 换文件：代价 +1，且接入前两帧在新旧文件中都可用
        for other in filenames:
            if other == fname:
                continue
            if not overlaps_two_frames(fname, other, cuts[i]):
                continue
            for j in range(i + 1, target_index + 1):
                if valid_run(other, cuts[i], cuts[j]):
                    push((j, other), cost + 1, path + (other,), (i, fname))
                else:
                    break

    # 3) 排不出：找到第一个无法继续覆盖的原片帧
    reachable_cuts = sorted({idx for (idx, _name) in dist})
    gap_cut = first_frame
    for idx in reachable_cuts:
        gap_cut = cuts[idx]  # 可达切点之后的第一帧
    # 若该帧其实仍有文件可用（仅因两帧对照规则接不上），它依然是"第一个接不上的帧"
    raise PlanningError(
        f"无法从原片帧 {gap_cut} 起继续编排（可用段不足或换文件前两帧无法对照）",
        gap_cut,
    )


def _build_plan(i, fname, prev, cuts, cover, offset_of, locks, first_frame) -> Plan:
    # 回溯状态链：每个状态 (切点下标, 文件) 表示截止该切点、当前段使用该文件
    chain: list[tuple[int, str]] = []
    state = (i, fname)
    while state[0] != -1:
        chain.append(state)
        state = prev[state]
    chain.reverse()

    # 每个状态对应一个 Run：从上个切点到本切点（前闭后闭）
    raw_runs: list[Run] = []
    prev_cut_idx = 0
    for idx, name in chain:
        raw_runs.append(
            _make_run(name, cuts[prev_cut_idx], cuts[idx] - 1, offset_of[name], locks)
        )
        prev_cut_idx = idx

    # 合并相邻且同文件的段（同一基本段切分产生的相邻 Run）
    runs: list[Run] = []
    for run in raw_runs:
        if runs and runs[-1].filename == run.filename and runs[-1].orig_end + 1 == run.orig_start:
            merged = Run(
                run.filename,
                runs[-1].orig_start,
                run.orig_end,
                runs[-1].file_start,
                run.file_end,
                locked=runs[-1].locked or run.locked,
            )
            runs[-1] = merged
        else:
            runs.append(run)

    switches = _build_switches(runs, cover, offset_of, first_frame)
    return Plan(runs=runs, switches=switches)


def _make_run(filename: str, orig_start: int, orig_end: int, offset: int, locks) -> Run:
    locked = any(
        lock.filename == filename and not (lock.end < orig_start or lock.start > orig_end)
        for lock in locks
    )
    return Run(
        filename=filename,
        orig_start=orig_start,
        orig_end=orig_end,
        file_start=orig_start - offset,
        file_end=orig_end - offset,
        locked=locked,
    )


def _build_switches(runs: list[Run], cover, offset_of, first_frame) -> list[Switch]:
    switches: list[Switch] = []
    for prev_run, next_run in zip(runs, runs[1:]):
        start = next_run.orig_start
        frames = [start - 2, start - 1]
        if frames[0] < first_frame:
            frames = [f for f in frames if f >= first_frame]
        switches.append(
            Switch(
                at_frame=start,
                from_filename=prev_run.filename,
                to_filename=next_run.filename,
                overlap_frames=frames,
                from_file_frames=[f - offset_of[prev_run.filename] for f in frames],
                to_file_frames=[f - offset_of[next_run.filename] for f in frames],
            )
        )
    return switches
