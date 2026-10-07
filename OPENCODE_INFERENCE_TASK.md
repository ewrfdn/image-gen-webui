# OpenCode 任务：独立推理 API

请先阅读 `方案-三层架构与推理服务.md`，以该文件的第 1–10 节，尤其第 5、8、9、10 节为实现契约。仅实现 `inference-service/` 与 `contracts/inference.openapi.yaml`，不要开始业务 API 或 UI。

## 交付范围

- FastAPI 独立服务，默认空载启动；从 `MODEL_ROOT` 下的一层 checkpoint 目录发现完整的本地 Diffusers 模型，从各 checkpoint 的 `loras/` 目录发现 LoRA。模型与 LoRA ID 由目录名和文件名稳定确定，禁止请求指定任意路径、URL 或执行模型目录中的自定义代码。
- 实现规划中的健康、资源目录、模型状态、资源、能力、同步 load/unload 和 `/v1/images/generations` 接口。推理与管理使用不同服务间 Token。统一错误结构、request ID、状态码与 `Retry-After`。
- 首期最多一个驻留模型、一个执行槽。加载、卸载、LoRA 应用、生成与清理原子互斥；忙时快速返回。GET 健康与状态查询在生成期间仍可响应。
- 首先支持 SDXL Base 的完整本地 Diffusers pipeline 和每请求零或一个 LoRA。基础生成与 LoRA 生成后都清理 adapter；清理失败将实例标记不可生成。生成返回单张 PNG Base64 和实际 seed、模型修订/资源指纹、LoRA 摘要、耗时。Qwen-Image-2.1 只保留清晰的 backend 扩展点；没有真实兼容样本和 GPU 验证前不要宣称支持其 LoRA。
- 给出锁定版本的依赖、配置示例、启动说明、CPU 层协议/并发测试，以及独立的 DGX GPU 冒烟脚本。测试需覆盖空载、显式加载/幂等、卸载/使用中冲突、非法 ID、目录扫描、参数校验、忙时拒绝和 LoRA 隔离/失败恢复。不要下载大模型到仓库，不要提交 Token。

## 验证与回报

先执行可在本机运行的协议测试并修复失败。若取得 DGX 连接信息，在 DGX 的 `~/models` 中只读检查模型目录，将 `MODEL_ROOT` 指向实际 checkpoint 根目录并运行真实加载、无 LoRA 出图与可用的匹配 LoRA 冒烟；记录模型目录、GPU 型号、依赖版本、显存峰值和结果。若目录格式与规划不符，报告差异并提供安全的配置或迁移方案，不移动权重文件。最后汇报已实现接口、测试结果和未验证能力。
