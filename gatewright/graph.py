# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Dependency graph helpers for jobs (`needs:`).

`toposort` orders jobs so dependencies come first and detects cycles;
`levels` groups jobs into "waves" that could run in parallel (dry-run output);
`with_dependencies` expands a user selection (`-j unit-tests`) to everything it needs.
"""

from __future__ import annotations

from .models import Job


class CycleError(Exception):
    """Raised by toposort; `.cycle` is the list of job ids forming the loop."""
    def __init__(self, cycle: list[str]):
        self.cycle = cycle
        super().__init__(" -> ".join(cycle))


def toposort(jobs: list[Job]) -> list[Job]:
    """Stable topological order. Dependencies outside `jobs` are ignored."""
    by_id = {j.id: j for j in jobs}
    order: list[Job] = []
    state: dict[str, int] = {}  # 1 = visiting, 2 = done
    stack: list[str] = []

    def visit(jid: str) -> None:
        if state.get(jid) == 2:
            return
        if state.get(jid) == 1:
            i = stack.index(jid)
            raise CycleError(stack[i:] + [jid])
        state[jid] = 1
        stack.append(jid)
        for dep in by_id[jid].needs:
            if dep in by_id:
                visit(dep)
        stack.pop()
        state[jid] = 2
        order.append(by_id[jid])

    for j in jobs:
        visit(j.id)
    return order


def with_dependencies(selected: set[str], jobs: list[Job]) -> set[str]:
    """Expand a selection to include everything it transitively needs."""
    by_id = {j.id: j for j in jobs}
    out = set()
    todo = list(selected)
    while todo:
        jid = todo.pop()
        if jid in out or jid not in by_id:
            continue
        out.add(jid)
        todo.extend(by_id[jid].needs)
    return out


def levels(jobs: list[Job]) -> list[list[Job]]:
    """Group jobs into waves that could run in parallel (used by dry-run)."""
    ids = {j.id for j in jobs}
    depth: dict[str, int] = {}
    for j in toposort(jobs):
        deps = [depth[d] for d in j.needs if d in ids]
        depth[j.id] = (max(deps) + 1) if deps else 0
    out: list[list[Job]] = []
    for j in toposort(jobs):
        d = depth[j.id]
        while len(out) <= d:
            out.append([])
        out[d].append(j)
    return out
