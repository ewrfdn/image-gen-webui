# OpenCode 只读验证任务

请在 DGX 上拉取 `https://github.com/ewrfdn/image-gen-webui.git` 的最新 `main`，检查提交哈希。只验证，不编辑受 Git 跟踪的源码或 `~/models` 权重，不提交或推送，不终止已有 GPU 进程。若发现问题，报告文件、行号、复现命令和实际输出，由本机开发环境修复后再推送。

1. 阅读 `inference-service/README.md` 与 `contracts/inference.openapi.yaml`，核对推理与管理 Token、显式 load/unload、错误码、模型目录及 LoRA 隔离契约。
2. 在隔离虚拟环境运行 `inference-service/tests`；记录 Python、FastAPI、Pydantic 和测试结果。验证 Git 工作树仍干净。
3. 只读扫描 `~/models/noobai-XL-1.1` 和已部署的 Qwen-Image 2.1 三文件布局，确认模型结构和权重。ComfyUI 布局的稳定模型 ID 为 `qwen-image-2.1-comfy`；确认 `COMFYUI_ROOT/models` 中三条路径解析到 `MODEL_ROOT` 的对应文件。`~/models/loras` 是全局目录，不能自动当成任一模型的 LoRA。
4. 检查私有 ComfyUI 子进程只绑定 `127.0.0.1`，使用独立用户、输入、临时和输出目录；load 预热、generate、unload 只影响它自己，不调用共享 8188 端口。仅在 GPU/主机内存余量足够且不会影响已有进程时，使用本地回环端口分别运行 SDXL 和 `scripts/smoke.py --model qwen-image-2.1-comfy --size 512x512 --steps 1`。可试 `MODEL_OFFLOAD=none`、`model_cpu`、`sequential_cpu`。记录是否返回可解码 PNG、加载和生成耗时、峰值显存、依赖版本。不要为了测试而停止其他进程或修改权重。
5. ComfyUI 布局不支持 LoRA；检查 capabilities 的 `lora_limit=0` 与请求拒绝。若容量不足，明确记录当时的 GPU 进程占用，并将真实出图标为未验证；不要将 CPU 假 backend 测试视作 GPU 出图通过。

截至 2026-10-08，提交 `40c0ec5` 的协议测试在本机 Python 3.12 和 DGX Python 3.12/3.14 均为 9/9 通过。DGX 上同时运行的 GPU 进程占用约 29GB 与 34GB；完整驻留、组件级及逐层 CPU offload 均在实际加载/预热中遇到 CUDA OOM。OpenCode CLI 的只读会话多次停在 `init`，尚未产出独立审查报告。重新验证时以最新提交及当时实际资源状态为准。
