"""Critical path method on the working-day axis.

All four relation types and min/max lags are difference constraints on start
times: ``s_dst - s_src >= w`` (see ``model.difference_constraints``). The
forward pass is a longest path from the per-task lower bounds; the backward
pass is a longest path in the reversed graph from the per-task upper bounds.
Both are label-correcting (SPFA), so max lags (back edges) are handled and a
positive cycle is reported instead of looping.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass


@dataclass(frozen=True)
class CpmResult:
    early_start: dict[str, int]
    late_start: dict[str, int]
    total_float: dict[str, int]
    finish: int
    critical: frozenset[str]
    positive_cycle: bool


def _longest(
    nodes: list[str],
    arcs: list[tuple[str, str, int]],
    init: dict[str, int],
) -> tuple[dict[str, int], bool]:
    succ: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for src, dst, weight in arcs:
        succ[src].append((dst, weight))
    dist = dict(init)
    queue = deque(nodes)
    queued = set(nodes)
    relax_count: dict[str, int] = defaultdict(int)
    limit = len(nodes) + 1
    while queue:
        node = queue.popleft()
        queued.discard(node)
        for nxt, weight in succ.get(node, []):
            candidate = dist[node] + weight
            if candidate > dist[nxt]:
                dist[nxt] = candidate
                relax_count[nxt] += 1
                if relax_count[nxt] > limit:
                    return dist, True
                if nxt not in queued:
                    queue.append(nxt)
                    queued.add(nxt)
    return dist, False


def cpm(
    durations: dict[str, int],
    constraints: list[tuple[str, str, int]],
    lower: dict[str, int],
    upper_end: dict[str, int] | None = None,
    project_end: int | None = None,
) -> CpmResult:
    """Early/late starts. ``upper_end`` are hard end bounds; ``project_end`` caps all ends.

    Without ``project_end`` the late pass is anchored at the early finish, so
    float is measured against the unconstrained program finish; deadlines in
    ``upper_end`` can make float negative (a guaranteed miss without resources).
    """
    nodes = sorted(durations)
    early, cyclic = _longest(nodes, constraints, {n: lower.get(n, 0) for n in nodes})
    finish = max((early[n] + durations[n] for n in nodes), default=0)
    anchor = finish if project_end is None else project_end
    neg_late = {}
    for node in nodes:
        bound = anchor - durations[node]
        if upper_end and node in upper_end:
            bound = min(bound, upper_end[node] - durations[node])
        neg_late[node] = -bound
    reversed_arcs = [(dst, src, weight) for src, dst, weight in constraints]
    late_neg, cyclic_back = _longest(nodes, reversed_arcs, neg_late)
    late = {node: -value for node, value in late_neg.items()}
    total_float = {node: late[node] - early[node] for node in nodes}
    critical = frozenset(node for node in nodes if total_float[node] <= 0)
    return CpmResult(
        early_start=early,
        late_start=late,
        total_float=total_float,
        finish=finish,
        critical=critical,
        positive_cycle=cyclic or cyclic_back,
    )
