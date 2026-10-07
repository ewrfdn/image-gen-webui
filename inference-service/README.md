# Inference service

独立的同步文生图服务。默认空载启动；管理端先调用 `load`，业务 Worker 再调用 `POST /v1/images/generations`。服务不保存用户、任务或图片。首期只实现 SDXL 的本地 Diffusers pipeline；Qwen-Image-2.1 尚未接入。

## 资源目录

推荐布局与规划一致：

```text
MODEL_ROOT/
  sdxl-base/
    model/
      model_index.json
      unet/ vae/ text_encoder/ text_encoder_2/
      tokenizer/ tokenizer_2/ scheduler/
    loras/
      style-v1.safetensors
```

每个模型也可直接把完整 Diffusers 文件放在 `MODEL_ROOT/<model_id>/`，用于只读验证现有权重目录。LoRA **仅**从该模型自己的 `loras/` 发现；不会将 `MODEL_ROOT/loras` 的全局文件自动分配给模型。目录名和 LoRA 文件名组成稳定 ID。列表查询只检查结构和 safetensors 头，不证明权重兼容或模型可放入显存。

## 本机协议测试

```powershell
cd inference-service
uv sync --python 3.12 --extra test
uv run --python 3.12 --extra test pytest -q
```

CPU 测试使用注入的假 backend，不需要下载模型。`uv.lock` 锁定服务与测试依赖。Python 3.14 的 Diffusers/Transformers/Accelerate/PEFT 组合已在 DGX 导入并运行到模型预热，完整出图仍待空闲 GPU 验证。CUDA PyTorch 应使用设备供应方提供的构建；不要让通用包安装覆盖已验证的 PyTorch。不要把 `.env`、Token 或权重提交到 Git。

## 启动

在已具备兼容 PyTorch/CUDA、Diffusers、Transformers、Accelerate、PEFT、Safetensors 的 Python 环境中安装本项目。DGX 当前是 Python 3.14 与厂商 CUDA PyTorch 2.13；应在隔离环境中保留其 CUDA 构建并安装本项目依赖。GPU 主机的最终部署组合仍需实测。

```bash
export MODEL_ROOT=/models/checkpoints
export INFERENCE_TOKEN='replace-with-random-inference-token'
export ADMIN_TOKEN='replace-with-different-random-admin-token'
export DEVICE=cuda:0
export DTYPE=bfloat16
export MODEL_OFFLOAD=none
uvicorn inference_service.app:create_app --factory --host 127.0.0.1 --port 8000 --workers 1
```

仅在可信网络内暴露端口。管理 Token 与推理 Token 必须不同。单进程、单执行槽；不要增加 Uvicorn worker 数量，否则会重复加载模型并破坏并发保护。`GET /health/ready` 在空载时仍返回 200；具体模型是否可生成，以 `GET /internal/v1/models?loaded=true` 为准。

GPU 剩余内存不足以直接常驻完整 pipeline 时，可设置 `MODEL_OFFLOAD=model_cpu`，将组件按需从主机内存搬到 GPU；若组件级 offload 仍不足，可试 `MODEL_OFFLOAD=sequential_cpu` 逐层搬运。两种模式都需要 Accelerate，会增加主机内存占用；逐层模式显著更慢。加载与生成仍需在目标机器上实测。

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" -X POST \
  http://127.0.0.1:8000/internal/v1/models/sdxl-base/load
curl -H "Authorization: Bearer $INFERENCE_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"model":"sdxl-base","prompt":"a cabin in snow","size":"1024x1024"}' \
  http://127.0.0.1:8000/v1/images/generations
```

生成一次只返回一张 PNG 的 Base64。错误为 `{"error":{"code":"...","message":"..."}}`；`X-Request-ID` 便于关联日志。执行槽忙时返回 503 `engine_busy` 和 `Retry-After: 1`。已开始生成后的网络断开不代表 GPU 工作已停止；调用方必须按规划处理未知结果。

## DGX 验证

先在本机推送，再在 DGX 拉取同一提交。DGX 的 `~/models/noobai-XL-1.1` 当前是直接 Diffusers 布局，可设 `MODEL_ROOT=~/models` 做无 LoRA 出图冒烟；其现有 LoRA 集中在 `~/models/loras`，因此不会被自动当作 NoobAI LoRA。验证指定 LoRA 时，需要在该模型自己的 `loras/` 中部署匹配权重，或在隔离测试目录准备权重映射；不要在线服务中临时跨模型搜索 LoRA。

运行 `python scripts/smoke.py --model noobai-XL-1.1`。该脚本从环境变量读取 API 地址和两个 Token，加载模型、生成并检查 PNG；`--lora-id` 可追加 LoRA 验证。模型加载、LoRA 兼容、数值隔离、OOM 恢复和显存峰值必须在 DGX 实测后才能算通过。当前仓库的 CPU 测试不覆盖这些 GPU 行为。

2026-10-08 验证记录：本机 Python 3.12 和 DGX Python 3.12/3.14 的协议测试均通过。DGX 上 NoobAI-XL-v1.1 可由 Diffusers 加载；直接 GPU 驻留、组件级 CPU offload 和逐层 CPU offload 在其他常驻计算任务运行时分别遇到 CUDA OOM，因此尚未获得完整 PNG。不要把此环境测试视为模型本身不兼容，也不要为了验证本服务中断其他任务。
