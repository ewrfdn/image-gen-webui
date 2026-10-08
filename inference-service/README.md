# Inference service

独立的同步文生图服务。默认空载启动；管理端先调用 `load`，业务 Worker 再调用 `POST /v1/images/generations`。服务不保存用户、任务或图片。支持本地 Diffusers 布局的 SDXL 与 Qwen-Image 2.1，也支持下述 ComfyUI 三文件 Qwen 布局；一次只加载一个模型。

## 代码结构

```text
inference_service/
  app.py                    # 保持稳定的 ASGI 启动入口
  api/app.py                # HTTP 路由、认证、错误响应和 OpenAPI
  engine/service.py         # 单模型执行槽、加载/卸载、生成与状态
  engine/registry.py        # checkpoint 和 LoRA 的只读发现与校验
  backends/base.py          # backend 接口及共用异常
  backends/factory.py       # 根据模型架构选择 backend
  backends/sdxl/backend.py  # SDXL 的加载、出图和 LoRA 隔离
  backends/qwen_image_21/backend.py  # Qwen-Image 2.1 的独立实现
  backends/qwen_image_21/comfy_backend.py  # 管理私有 ComfyUI 进程
  config.py                 # 运行配置
  resources.py              # 各层共享的资源类型
  schemas.py                # 各层共享的请求和响应类型
```

新增模型实现时，在 `backends/` 下为该架构建立独立目录，并在 `factory.py` 注册；API 路由只调用 engine，engine 通过统一的 backend 接口执行模型操作。

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

Diffusers Qwen-Image 2.1 目录必须有 `model_index.json`，其 `_class_name` 为 `QwenImage21Pipeline`，并包含 `processor/`、`scheduler/`、`text_encoder/`、`transformer/` 和 `vae/`。Qwen LoRA 放在该 checkpoint 的 `loras/` 下；真实 LoRA 的格式兼容和请求隔离需用对应 2.1 权重在 GPU 上验收。

ComfyUI 布局使用固定 ID `qwen-image-2.1-comfy`，要求 `MODEL_ROOT` 下同时存在：

```text
diffusion_models/qwen_image_2.1_bf16.safetensors
text_encoders/qwen3vl_8b_bf16.safetensors
vae/qwen_image_2.1_vae_bf16.safetensors
```

设置 `COMFYUI_ROOT` 为 ComfyUI 源码目录，`COMFYUI_PYTHON` 为已安装其依赖的 Python 解释器。`COMFYUI_ROOT/models` 中对应路径必须指向上述 `MODEL_ROOT` 文件。管理端加载该 ID 时，服务在随机回环端口启动自己的 ComfyUI 子进程，使用独立的用户、输入、临时和输出目录，并执行一张 512×512、1 步的预热图；卸载或服务正常退出时终止该子进程。它不调用共享 ComfyUI 的加载或卸载接口。这个布局目前不支持 LoRA，capabilities 中 `lora_limit` 为 0。

## 本机协议测试

```powershell
cd inference-service
uv sync --python 3.12 --extra test
uv run --python 3.12 --extra test pytest -q
```

CPU 测试使用假 pipeline，不需要下载模型。`uv.lock` 锁定服务与测试依赖。`gpu` extra 使用含 `QwenImage21Pipeline` 的 Diffusers 官方提交和 Transformers 5.19；CUDA PyTorch 应使用设备供应方提供的构建，避免覆盖已验证的 CUDA 版本。Qwen 的真实 GPU 出图与 LoRA 兼容仍待 DGX 验证。不要把 `.env`、Token 或权重提交到 Git。

## 启动

在已具备兼容 PyTorch/CUDA、Diffusers、Transformers、Accelerate、PEFT、Safetensors 的 Python 环境中安装本项目。DGX 当前是 Python 3.14 与厂商 CUDA PyTorch 2.13；应在隔离环境中保留其 CUDA 构建并安装本项目依赖。GPU 主机的最终部署组合仍需实测。

```bash
export MODEL_ROOT=/models/checkpoints
export INFERENCE_TOKEN='replace-with-random-inference-token'
export ADMIN_TOKEN='replace-with-different-random-admin-token'
export DEVICE=cuda:0
export DTYPE=bfloat16
export MODEL_OFFLOAD=none
export COMFYUI_ROOT=/home/sakana/workspace/comfyui/ComfyUI
export COMFYUI_PYTHON=/home/sakana/workspace/comfyui/comfyui-env/bin/python
uvicorn inference_service.app:create_app --factory --host 127.0.0.1 --port 8000 --workers 1
```

仅在可信网络内暴露端口。管理 Token 与推理 Token 必须不同。单进程、单执行槽；不要增加 Uvicorn worker 数量，否则会重复加载模型并破坏并发保护。`GET /health/ready` 在空载时仍返回 200；具体模型是否可生成，以 `GET /internal/v1/models?loaded=true` 为准。

GPU 剩余内存不足以直接常驻完整 Diffusers pipeline 时，可设置 `MODEL_OFFLOAD=model_cpu`，将组件按需从主机内存搬到 GPU；若组件级 offload 仍不足，可试 `MODEL_OFFLOAD=sequential_cpu` 逐层搬运。两种模式都需要 Accelerate，会增加主机内存占用；逐层模式显著更慢。ComfyUI 布局分别映射为其 `--lowvram`、`--novram` 模式，具体节省量需在目标机器上实测。独立子进程会与共享 ComfyUI 同时占用 GPU 和主机内存。

SDXL 未指定采样参数时使用 25 步、`guidance_scale=7`，尺寸为 512–1024 且为 64 的倍数。Qwen-Image 2.1 使用 40 步、`guidance_scale=1`（无 CFG），尺寸为 512–2752 且为 32 的倍数，最大 4,718,592 像素。Diffusers 布局的 `guidance_scale>1` 映射到 `true_cfg_scale`；ComfyUI 布局映射到 KSampler 的 `cfg`。传入 `negative_prompt` 时也需显式设置 `guidance_scale>1`。具体限制和默认值可从 `GET /internal/v1/capabilities` 查询。当前 API 只提供文生图，不包含 Qwen 的图像编辑输入。

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

Qwen-Image 2.1 的 Diffusers 布局可用其模型目录名替换 `--model`；ComfyUI 三文件布局使用 `--model qwen-image-2.1-comfy`。建议先用 `--size 512x512 --steps 1` 验证加载和最小出图，再按显存余量验证较大尺寸。ComfyUI 布局不测 LoRA。只在 GPU 和主机内存余量足够时运行，不影响已有任务。

2026-10-08 验证记录：本机 Python 3.12 和 DGX Python 3.12/3.14 的协议测试均通过。DGX 上 NoobAI-XL-v1.1 可由 Diffusers 加载；直接 GPU 驻留、组件级 CPU offload 和逐层 CPU offload 在其他常驻计算任务运行时分别遇到 CUDA OOM，因此尚未获得完整 PNG。不要把此环境测试视为模型本身不兼容，也不要为了验证本服务中断其他任务。
