# Multi-level run artifact example

这是三关离线合同示例，不是一次真实集群实验。它展示 L1 一次通过、L2 首次诊断失败后第二次通过、L3 复合扰动一次通过时应保留的 Episode、controller record、level result、progression state、run summary 和 score。

`controller-record.jsonl` 使用 Controller 证据格式，分别记录每次目标漂移和 L3 telemetry rule 的触发、完成及清理。`level-results/` 中每个文件对应一个 trial；`episode-score.json` 由四次尝试计算。固定时间、Pod 名称、UID、规则 ID 和数值均为示例数据，不可作为部署状态或实测结果引用。

完整配置复用 `tasks/examples/multi-level/episode.3-levels.yaml`，其快照保存在本目录 `episode.yaml`。正式运行应另外保存公开 Agent task、私有 Oracle observation、harness trace、后端原始回执和所有文件的 provenance/hash manifest。
