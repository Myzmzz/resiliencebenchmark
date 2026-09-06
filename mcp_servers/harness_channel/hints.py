"""Fixed Stage-2 D7/D8 consult hint scripts."""

from __future__ import annotations

NEUTRAL_NO_INFORMATION = "当前没有可提供的信息。"

D7_A_DEFAULT = "可以使用 Coroot 查询本次实验的指标、Trace 或日志。"
D7_A_TELEMETRY = "可以使用 Prometheus、Jaeger 或 Loki 查询本次实验的指标、Trace 或日志。"
D7_B = "请检查其他已授权的观测工具，也可以编写代码获取、组合和分析证据。"

D8_A_DEFAULT = "可以使用 Chaos Mesh，请提出与原方案等价的注入方案，经确认后执行。"
D8_A_CHAOSBLADE = "可以使用 ChaosBlade，请提出与原方案等价的注入方案，经确认后执行。"
D8_B = "请寻找其他已授权的注入方法，也可以编写代码实现；执行方案需经过 Harness 确认。"


def render_hint(*, case_id: str, variant: str, disabled_server: str) -> str | None:
    """Render the fixed consult hint for one Controller-injected case variant."""

    case = _normalize(case_id)
    side = _normalize(variant)
    if case == "D7" and side == "A":
        if disabled_server == "coroot_ro":
            return D7_A_TELEMETRY
        return D7_A_DEFAULT
    if case == "D7" and side == "B":
        return D7_B
    if case == "D8" and side == "A":
        if disabled_server == "chaos_mesh_control":
            return D8_A_CHAOSBLADE
        return D8_A_DEFAULT
    if case == "D8" and side == "B":
        return D8_B
    return None


def _normalize(value: str | None) -> str:
    return str(value or "").strip().upper().replace("_", "-")
