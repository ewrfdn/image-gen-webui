# Inference service

独立的同步文生图服务。默认空载启动；管理端先调用 `load`，业务 Worker 再调用 `POST /v1/images/generations`。服务不保存用户、任务或图片。支持本地 Diffusers 布局的 SDXL 和 Qwen-Image 2.1 checkpoint；同一进程一次只加载一个模型。

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

Qwen-Image 2.1 目录必须有 `model_index.json`，其 `_class_name` 为 `QwenImage21Pipeline`，并包含 `processor/`、`scheduler/`、`text_encoder/`、`transformer/` 和 `vae/`。单独的 ComfyUI transformer safetensors 文件不构成完整 Diffusers checkpoint。Qwen LoRA 仍放在该 checkpoint 的 `loras/` 下；真实 LoRA 的格式兼容和请求隔离需用对应 2.1 权重在 GPU 上验收。

官方 [HF 模型仓库](https://huggingface.co/Qwen/Qwen-Image-2.1) 提供上述完整目录。首次部署时在 GPU 主机下载一次，之后服务只读取本地文件，不在 `load` 请求里联网：

```bash
hf download Qwen/Qwen-Image-2.1 --local-dir ~/models/qwen-image-2.1
export MODEL_ROOT=~/models
```

也可从 Qwen 官方链接的 [ModelScope 镜像](https://modelscope.cn/models/Qwen/Qwen-Image-2.1) 下载同一 Diffusers 目录：`modelscope download Qwen/Qwen-Image-2.1 --local-dir ~/models/qwen-image-2.1`。下载完成后确认 `~/models/qwen-image-2.1/model_index.json` 存在，并以 `qwen-image-2.1` 为模型 ID 调用 API。现有三份 ComfyUI 单文件权重不会被这个 backend 加载；它们也不需要删除。

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
uvicorn inference_service.app:create_app --factory --host 127.0.0.1 --port 8000 --workers 1
```

仅在可信网络内暴露端口。管理 Token 与推理 Token 必须不同。单进程、单执行槽；不要增加 Uvicorn worker 数量，否则会重复加载模型并破坏并发保护。`GET /health/ready` 在空载时仍返回 200；具体模型是否可生成，以 `GET /internal/v1/models?loaded=true` 为准。

GPU 剩余内存不足以直接常驻完整 pipeline 时，可设置 `MODEL_OFFLOAD=model_cpu`，将组件按需从主机内存搬到 GPU；若组件级 offload 仍不足，可试 `MODEL_OFFLOAD=sequential_cpu` 逐层搬运。两种模式都需要 Accelerate，会增加主机内存占用；逐层模式显著更慢。加载与生成仍需在目标机器上实测。

SDXL 未指定采样参数时使用 25 步、`guidance_scale=7`，尺寸为 512–1024 且为 64 的倍数。Qwen-Image 2.1 使用 40 步、`guidance_scale=1`（无 CFG），尺寸为 512–2752 且为 32 的倍数，最大 4,718,592 像素。Qwen 的 `guidance_scale>1` 映射到 Diffusers 的 `true_cfg_scale`；传入 `negative_prompt` 时也需显式设置 `guidance_scale>1`。具体限制和默认值可从 `GET /internal/v1/capabilities` 查询。当前 API 只提供文生图，不包含 Qwen 的图像编辑输入。

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

### Docker dev 部署

`Dockerfile` 提供 `runtime` 和 `dev` 两个 target。镜像标签为 `image-gen-inference:0.1.0-dev`；通过 `SERVICE_VERSION`、`VCS_REF` 写入 OCI 版本标签。固定 UV 0.12.3、Python 3.14.6、PyTorch 2.9.1+cu130 和配套 Torchvision 0.24.1，其他 Python 依赖从 `uv.lock` 导出。Qwen3VL 处理器在文生图时也会导入视频处理器，因此 Torchvision 是必需的。CUDA 基础镜像为 `nvidia/cuda:13.3.1-cudnn-runtime-ubuntu24.04`，实际 Torch 使用 wheel 自带的 cu130 库，构建后仍必须在 GB10 上运行 GPU 验收。实际依赖清单保存在镜像 `/app/runtime-requirements.txt`。

在 local-dgx 的独立部署目录复制此服务源码后：

```bash
cd ~/work/image-gen-inference-dev/service
python3 scripts/prepare_dgx_dev.py --source ~/models
export VCS_REF=<source-commit-or-commit-dirty>
docker compose -f compose.dev.yaml -f compose.resources.json up -d --build --wait
docker compose -f compose.dev.yaml -f compose.resources.json exec -T inference python -m pytest -q -p no:cacheprovider
docker compose -f compose.dev.yaml -f compose.resources.json exec -T inference \
  python scripts/verify_gpu.py --model noobai-XL-1.1 \
  --lora-id bocchi_illustrious_lora --output /artifacts/sdxl
```

准备脚本生成权限为 0600 的 `.env`（两个不同的随机 Token），以及本机专用 `compose.resources.json`。原 checkpoint 只读挂载；LoRA 按 safetensors 元数据复制进隔离资源目录。SDXL 格式的 `bocchi_illustrious_lora` 映射至 NoobAI，标记为 `qwen_image_2` 的 v1–v4 映射至 Qwen；发现成功不代表 2.1 权重兼容，需要真实调用验证。

本机 AI-toolkit 的 Qwen 2.1 训练实现使用合并的 `img_mlp.gate_up`，其输出顺序为 `[gate_layer; proj]`。Diffusers 使用独立投影；直接加载原 LoRA 会忽略这部分权重。准备脚本对副本做无损转换：两个投影共享原 A，B 按输出行拆为前后两半，保留原始文件；转换文件的 safetensors 元数据记录来源 SHA256 和转换版本。重复执行保留 Token，仅在需要转换且来源更新时重建转换副本。Qwen backend 在应用适配器前检查每个模块、A/B 配对与矩阵维度，未知模块和部分不兼容权重返回 `400 lora_incompatible`，不接受部分加载。

默认监听 DGX 回环地址 `127.0.0.1:18001`，单 worker，源码只读挂载并启用 reload。权重文件保持离线读取，容器以 UID 1000 运行。源码修改会重启模型槽，需重新 load。依赖修改必须重新 build。dev 模式用于开发验收，不用于生产。

Windows 本机访问可另开终端执行：

```powershell
ssh -N -L 18001:127.0.0.1:18001 local-dgx
```

此时 API 地址为 `http://127.0.0.1:18001`。Token 位于 DGX 部署目录的 `.env`，不写入镜像或验收报告。容器 `/artifacts` 映射至部署目录的 `verification-output/`，验收脚本保存实际 PNG 和 JSON 结果，检查鉴权、像素非空、固定 seed 的 LoRA 前后隔离、并发 `engine_busy`、卸载与重载。Qwen 验收前先通过管理 API 卸载 SDXL，再用 `--model qwen-image-2.1 --lora-id bocchi_lora_v4 --steps 4 --output /artifacts/qwen` 执行脚本。Qwen 冷加载可能需数分钟；`--keep-loaded` 可跳过重复的卸载/重载检查，报告会显式记录该项未执行。

先在本机推送，再在 DGX 拉取同一提交。DGX 的 `~/models/noobai-XL-1.1` 当前是直接 Diffusers 布局，可设 `MODEL_ROOT=~/models` 做无 LoRA 出图冒烟；其现有 LoRA 集中在 `~/models/loras`，因此不会被自动当作 NoobAI LoRA。验证指定 LoRA 时，需要在该模型自己的 `loras/` 中部署匹配权重，或在隔离测试目录准备权重映射；不要在线服务中临时跨模型搜索 LoRA。

运行 `python scripts/smoke.py --model noobai-XL-1.1`。该脚本从环境变量读取 API 地址和两个 Token，加载模型、生成并检查 PNG；`--lora-id` 可追加 LoRA 验证。模型加载、LoRA 兼容、数值隔离、OOM 恢复和显存峰值必须在 DGX 实测后才能算通过。当前仓库的 CPU 测试不覆盖这些 GPU 行为。

Qwen-Image 2.1 使用 `--model qwen-image-2.1` 执行相同脚本；建议先用 `--size 512x512 --steps 1` 验证加载和最小出图，再按显存余量验证较大尺寸及匹配的 LoRA。只在 GPU 和主机内存余量足够时运行，不影响已有任务。

2026-10-08 验证记录：本机 Python 3.12 和 DGX Python 3.12/3.14 的协议测试均通过。DGX 上 NoobAI-XL-v1.1 可由 Diffusers 加载；直接 GPU 驻留、组件级 CPU offload 和逐层 CPU offload 在其他常驻计算任务运行时分别遇到 CUDA OOM，因此尚未获得完整 PNG。不要把此环境测试视为模型本身不兼容，也不要为了验证本服务中断其他任务。

2026-10-08 Qwen-Image 2.1 记录：DGX 已下载完整官方 Diffusers 快照，注册表将 `qwen-image-2.1` 标为 available；使用独立 Python 3.14 环境可导入 `QwenImage21Pipeline`，依赖检查通过，模型发现和能力接口返回 200。真实 GPU 加载与 PNG 出图尚未执行：当时 `glm53-flash-tf` 占用约 94 GB GPU 内存，用户选择保留它并延后 GPU 测试。
