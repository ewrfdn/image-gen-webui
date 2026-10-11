# Frame 推理平台

两台 DGX 起步的图片/视频异步推理平台。Server 单进程维护实时模型/LoRA目录与节点占用；PostgreSQL保存任务、尝试、节点地址、API Key、系统参数和文件信息。UI只提供模型切换、提示词、LoRA、参考图、任务状态和结果下载；后台提供Key、后端和参数管理。没有workspace。

## 本地检查

```powershell
cd server
uv run --python 3.12 --extra test pytest -q
cd ../inference-service
uv run --python 3.12 --extra test pytest -q
```

`server/tests/serve_preview.py`启动真实UI/API和受控测试后端，使用临时SQLite库，不会调用GPU。`scripts/ui_smoke.cjs`运行浏览器验证，需Playwright与Edge；可通过PLAYWRIGHT_MODULE指定Playwright包位置。生产始终使用PostgreSQL。

## DGX部署

源码通过Git固定。仅监听回环地址，经SSH转发访问。保留.env和platform-data；不要提交凭证、权重或产物。

```bash
python3 scripts/prepare_platform.py --source ~/models
export VCS_REF=$(git rev-parse HEAD)
docker compose -f compose.yaml -f compose.resources.json build
```

此部署复用已经验收的`image-gen-inference:0.1.0-dev` GPU依赖镜像，通过Dockerfile.platform更新业务代码。新机器没有该镜像时，先按inference-service/README.md构建GPU基础镜像。GPU模型默认空载，不在启动时占用模型内存。

生成离线内容摘要与模型JSON，不加载GPU模型；资源挂载沿用prepare脚本生成的只读权重路径，临时仅允许写隔离资源根目录：

```bash
docker compose -f compose.yaml -f compose.resources.json run --rm --no-deps \
  --volume "$HOST_MODEL_ROOT:/models:rw" inference python scripts/build_catalog.py --root /models
docker compose -f compose.yaml -f compose.resources.json up -d --wait
python3 scripts/verify_platform.py
```

上面的HOST_MODEL_ROOT需要从.env加载，例如`set -a; . ./.env; set +a`；不要将.env内容输出到终端。首次manifest需要读取权重计算哈希，后续文件未变更时复用。原始checkpoint不移动、不改写。

```powershell
ssh -N -L 18080:127.0.0.1:18080 local-dgx
```

打开`http://127.0.0.1:18080`，使用部署.env的PLATFORM_ADMIN_TOKEN登录。后端节点在管理页配置；同一个服务地址不能重复登记为不同GPU槽。节点服务间Token加密保存，浏览器不读取已有密文或明文。Key只在创建时展示一次。

## 运行约定

- 初期一个Uvicorn worker。目录每5秒刷新，状态15秒后过期；节点查询整体1秒超时。
- `available`执行；`need_load`显式加载；`busy`/`unavailable`保持已入库任务等待，不消耗执行尝试数。不同媒体任务共享节点锁。
- maxCount限制图片+视频等待队列；事务检查后才接收入库。
- 模型释放每分钟检查，最近调用/执行结束未满1小时、仍有对应待处理任务或节点执行结果未知时保留。唯一释放接口是infra的unload；不会为了其他模型强制淘汰。
- 同步GPU调用断开后任务进入unknown并隔离节点，管理员明确重试才允许可能重复生成。重启后尚未发送的任务恢复等待，已发送的任务不盲目重试。
- 产物原子保存后关联数据库；持久化完成manifest允许DB错误/重启后恢复关联。数据库不保存大段Base64。
- 当前DGX权重为SDXL与Qwen-Image-2.1，均仅文生图。参考图/视频由声明且实现对应能力的其他infra提供；Server处理上传、编辑/视频协议和持久化，测试后端覆盖这两条流程。当前GPU节点的JSON不能宣称尚未实现的参考/视频能力。

## API Key调用

`GET /v1/models`，`POST /v1/images/generations`，`POST /v1/images/edits`兼容Images请求形状的子集：n=1、PNG、quality=auto、模型自己的尺寸限制。model使用实时目录ID。seed、采样步数和LoRA为扩展；不支持的字段明确返回400。

默认调用先入库，再等待调度与保存，成功返回200+b64_json。HTTP等待默认300秒，超时504携带X-Task-ID；业务任务继续执行。`Prefer: respond-async`扩展返回202+task ID，之后查询`GET /api/tasks/{id}`。普通SDK默认200，不把异步202冒充完全兼容。

模型JSON与部署摘要分别为`MODEL_ROOT/<model_id>/model-config.json`、`artifact-manifest.json`。格式见design下的JSON Schema和样例。修改声明后会实时同步；真实推理能力仍以backend实现为准。
