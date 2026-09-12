#!/usr/bin/env python3
"""Turn an env3 inventory into the gap table against the measured target state.

Step 2 of the third environment's preparation is "list the gaps against the
target state", where the target state is what the second environment was
measured to be. Both halves of that comparison now exist in writing -- the
inventory script emits machine-readable facts, and the target state is recorded
in docs/status/stage2-env3-reference-state-20260912.md -- so the comparison
itself does not need to be done by hand.

    bash tools/env3/inventory.sh > inventory-node60.txt      # on each node
    python tools/env3/gap_report.py inventory-node*.txt

Every expectation below cites where its value came from. A fact the inventory
could not collect is reported as unknown, never assumed to pass.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


# Namespace-derived keys carry the namespace name, so hyphens are part of a
# valid key (ns_otel-demo, ns_chaos-mesh, ...). Leaving them out of this
# pattern silently dropped those facts and reported them as uncollected.
FACT_LINE = re.compile(r"^FACT ([A-Za-z0-9_.-]+)=(.*)$")

OK = "ok"
GAP = "gap"
UNKNOWN = "unknown"
INFO = "info"


@dataclass(frozen=True)
class Expectation:
    key: str
    label: str
    source: str
    check: Callable[[str], bool]
    want: str
    severity: str = "blocker"

    def evaluate(self, facts: Mapping[str, str]) -> tuple[str, str]:
        value = facts.get(self.key)
        if value is None or value == "unknown" or value == "":
            return UNKNOWN, "未采集到"
        return (OK if self.check(value) else GAP), value


def _int_at_least(minimum: int) -> Callable[[str], bool]:
    def check(value: str) -> bool:
        try:
            return int(value) >= minimum
        except ValueError:
            return False

    return check


def _kernel_at_least(major: int, minor: int) -> Callable[[str], bool]:
    def check(value: str) -> bool:
        parts = re.findall(r"\d+", value)
        if len(parts) < 2:
            return False
        return (int(parts[0]), int(parts[1])) >= (major, minor)

    return check


def _http_reachable(value: str) -> bool:
    # A registry answers /v2/ with 200 or 401; 000 means the request never
    # completed, which is what an unreachable network looks like here.
    return value in {"200", "401", "403"}


# The target state,每一条都注明出处。
EXPECTATIONS: tuple[Expectation, ...] = (
    Expectation(
        "cpu_cores", "CPU 核数", "平台 limits 6 核 + OTel Demo + 可观测栈",
        _int_at_least(16), "≥ 16（第二套环境是 32）",
    ),
    Expectation(
        "mem_total_mb", "内存", "平台 limits 11 GiB + OTel Demo 7.2 GiB + 可观测栈",
        _int_at_least(32 * 1024), "≥ 32 GiB（第二套环境是 123 GiB）",
    ),
    Expectation(
        "cgroup_version", "cgroup 版本", "agent-exec 依赖 cgroup v2 委派",
        lambda v: v == "v2", "v2",
    ),
    Expectation(
        "kernel", "内核版本", "AppArmor 方案用 mount_setattr(AT_RECURSIVE)",
        _kernel_at_least(5, 12), "≥ 5.12",
    ),
    Expectation(
        "swap_on", "swap", "kubelet 默认要求关闭", lambda v: v == "no", "关闭",
        severity="warning",
    ),
    Expectation(
        "time_synced", "时间同步", "证据窗口与指标对齐依赖它",
        lambda v: v.lower() in {"yes", "true"}, "已同步",
    ),
    Expectation(
        "has_docker", "Docker", "deploy/stage2/README.md：agent-exec 依赖 Docker 的私有 cgroup 命名空间",
        lambda v: v == "yes", "已安装",
    ),
    Expectation(
        "k8s_runtime", "集群运行时", "同上；第二套环境实测 docker://29.6.x",
        lambda v: v.startswith("docker://"), "docker://…",
    ),
    Expectation(
        # The platform's own floor, not the second environment's version:
        # deploy/stage2/README.md has a "Kubernetes 1.28 compatibility" section,
        # and the manifests use the pre-1.30 AppArmor beta annotation, which is
        # the only form 1.28/1.29 accept.
        "k8s_server", "Kubernetes 版本", "deploy/stage2/README.md 明确兼容 1.28；清单用 AppArmor beta 注解",
        lambda v: re.match(r"v1\.(2[89]|[3-9][0-9])\.", v) is not None, "≥ v1.28",
    ),
    Expectation(
        "k8s_nodes", "节点数", "1 控制面 + 工作节点", _int_at_least(1), "≥ 1",
    ),
    Expectation(
        "storageclass", "存储类", "PVC resbench-stage2-data 要 20Gi RWO",
        lambda v: bool(v.strip()), "至少一个",
    ),
    Expectation(
        "cni", "CNI 配置", "Pod 网络", lambda v: bool(v.strip()), "至少一份 conflist",
    ),
    Expectation(
        "has_helm", "helm", "OTel Demo / Coroot / Chaos Mesh 都走 chart",
        lambda v: v == "yes", "已安装",
    ),
    Expectation(
        "apparmor_profile", "AppArmor profile resbench-agent-runtime",
        "deploy/stage2/apparmor/：agent-runtime 容器要它",
        _int_at_least(1), "已加载（enforce）",
    ),
    Expectation(
        "egress_registry_k8s_io", "出网 registry.k8s.io", "拉 k8s 组件镜像",
        _http_reachable, "可达", severity="warning",
    ),
    Expectation(
        "egress_ghcr_io", "出网 ghcr.io", "Chaos Mesh 四个镜像", _http_reachable,
        "可达", severity="warning",
    ),
    Expectation(
        "egress_quay_io", "出网 quay.io", "prometheus / node-exporter",
        _http_reachable, "可达", severity="warning",
    ),
    Expectation(
        "egress_old_harbor", "能否直连旧集群 Harbor 1.94.151.57:85",
        "可观测栈 / Coroot / ChaosBlade operator 的镜像都在那里",
        _http_reachable, "可达则省去镜像搬运；不可达则必须全部镜像化",
        severity="warning",
    ),
)

# 这些是"目标状态里应该有、空环境里必然没有"的组件；列出来是为了生成待装清单，
# 不是当作缺陷。
COMPONENTS: tuple[tuple[str, str, str], ...] = (
    ("ns_otel-demo", "被测系统 OTel Demo", "scripts/deploy_application.py + environment/kubernetes/otel-demo/"),
    ("ns_observability", "可观测栈（7 个对象）", "deploy/observability/reference-stack.yaml"),
    ("ns_coroot", "Coroot", "官方 chart coroot-operator 0.8.2 + authAnonymousRole: Viewer"),
    ("ns_chaos-mesh", "Chaos Mesh", "官方 chart（按 k8s 版本选 2.7.3 / 2.8.0）+ deploy/chaos-mesh/"),
    ("chaosblade_operator", "ChaosBlade operator + tool", "deploy/chaosblade/reference-install.yaml"),
    ("chaosblade_wrapper", "ChaosBlade cgroup 包装", "deploy/chaosblade/cgroupns-wrapper.yaml"),
    ("ns_resiliencebenchmark-system", "平台本体（三容器 + PVC + RBAC + Secret）", "deploy/stage2/ + env3 专属 overlay"),
)


def parse_facts(text: str) -> dict[str, str]:
    facts: dict[str, str] = {}
    for line in text.splitlines():
        match = FACT_LINE.match(line.strip())
        if match:
            facts[match.group(1)] = match.group(2).strip()
    return facts


def render(reports: Sequence[tuple[str, Mapping[str, str]]]) -> tuple[str, int]:
    lines: list[str] = []
    gaps = 0
    unknowns = 0

    for name, facts in reports:
        lines.append(f"\n## {name}")
        host = facts.get("host", "unknown")
        lines.append(
            f"   {host} | {facts.get('os', 'unknown')} | 内核 {facts.get('kernel', 'unknown')} | "
            f"{facts.get('cpu_cores', '?')} 核 / {facts.get('mem_total_mb', '?')} MiB"
        )
        lines.append("")
        lines.append(f"   {'检查项':<38} {'状态':<6} {'实测':<22} 期望")
        lines.append("   " + "-" * 100)
        for item in EXPECTATIONS:
            status, value = item.evaluate(facts)
            if status == GAP and item.severity == "blocker":
                gaps += 1
            elif status == GAP:
                pass
            if status == UNKNOWN:
                unknowns += 1
            mark = {OK: "ok", GAP: "差距" if item.severity == "blocker" else "注意", UNKNOWN: "未知"}[status]
            lines.append(f"   {item.label:<38} {mark:<6} {value[:22]:<22} {item.want}")
        lines.append("")
        lines.append("   待装组件（空环境里没有是正常的，这里生成待办）：")
        for key, label, asset in COMPONENTS:
            value = facts.get(key, "unknown")
            state = {"present": "已有", "absent": "待装", "unknown": "未知"}.get(value, value)
            lines.append(f"     {state:<5} {label:<34} ← {asset}")

    lines.append("")
    lines.append(f"合计：阻塞级差距 {gaps} 项，未采集到 {unknowns} 项。")
    if unknowns:
        lines.append("未采集到的不算通过——补齐之后再判。")
    return "\n".join(lines), (1 if gaps else 0)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inventory", nargs="+", type=Path, help="inventory.sh 的输出，一台一个文件")
    args = parser.parse_args(argv)

    reports: list[tuple[str, Mapping[str, str]]] = []
    for path in args.inventory:
        facts = parse_facts(path.read_text(encoding="utf-8", errors="replace"))
        if not facts:
            print(f"{path}: 没有找到 FACT 行；这份盘点是不是旧版脚本产出的？", file=sys.stderr)
            return 2
        reports.append((path.name, facts))

    text, code = render(reports)
    print(text)
    return code


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
