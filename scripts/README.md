# Scripts

本目录用于提供环境初始化、单 Episode 运行、批量评测、恢复和结果导出入口。

脚本应保持薄封装：负责参数校验和组装模块，不在脚本中隐藏 Controller 或 Evaluator 的核心逻辑。
