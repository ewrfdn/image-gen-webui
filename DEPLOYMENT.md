# local-dgx 整套服务部署记录

日期：2026-10-12。状态：整套服务已部署，非GPU验收和真实GPU端到端验收均通过。

## 用户与角色升级

管理页面拆为`/admin`：管理员可以配置节点，普通用户在服务器端被拒绝；未登录跳转`/?next=/admin`。超级管理员用户管理提供搜索、分页、编辑用户名/角色、启用禁用和掩码密码重置表单。用户后端参考FDE的controller/schema/service结构，保留本项目“仅超级管理员管理账号”权限。17项真实PostgreSQL测试覆盖后台路由、管理员节点写入、普通用户拒绝、分页、大小写用户名冲突及角色/会话/Key撤销。

新增users、login_sessions及api_keys.user_id，保留原任务、文件和Key。登录改为账号/密码；仅超级管理员创建账号，不开放自行注册。普通用户只通过平台生成和查看自己的内容，不能创建Key或访问/v1接口；管理员管理自己的Key；超级管理员管理账号、角色及所有Key。禁用账号或将管理员降为用户会撤销其Key，密码更新/重置会使原登录会话失效。

首次启动自动初始化admin超级管理员，初始密码为部署.env中的PLATFORM_ADMIN_TOKEN；后续不会重置。可通过“我的账号”修改密码。server使用独立SERVER_VERSION，默认0.3.0，infra保持0.2.0。升级需先备份PostgreSQL，然后只重建server；不用停止其他容器或加载GPU模型即可验证账号权限。

验证包括原11项平台测试、5项账号/权限/迁移测试（真实PostgreSQL共16项），以及实际浏览器账号登录、超级管理员创建普通账号、普通用户隐藏管理入口、退出时清除敏感状态和320/390/1100px布局。API层验证了用户生成、任务/文件隔离、禁止/v1访问、Key归属、角色降级立即失效、最后一个超级管理员保护、会话过期和密码更新。

DGX实际ARM64镜像与隔离PostgreSQL也通过16项测试。已备份生产库到权限0600的verification-output/pre-accounts-db.sql，然后仅替换server，infra和其他容器保持运行。真实浏览器用超级管理员创建临时普通用户/管理员，验证普通用户1024×1024 GPU出图、私有下载/历史、禁止Key及/v1访问；管理员Key调用成功，降级后立即401。临时账号最终禁用，Key撤销。证据为本机verification-output/accounts-live/report.json、user-result.png及generated.png；后续仅调整普通用户页脚并补充验收工具，运行server镜像revision对应最终源码提交。

生产库升级后逐项对照备份中的ID：原5个API Key、9个任务和9个文件记录全部保留，Key均已绑定账号，没有未完成任务；所有8个运行容器健康。数据库核对报告为DGX verification-output/accounts-final-audit.json。

## 已完成

- Git分支：`codex/platform-ui-server`。源码通过Git bundle同步到DGX，未推送GitHub。运行应用源与分支中server/ui的源码一致；随后提交包含验证工具和本报告。
- 本地server 11项测试通过：并发maxCount、1秒多节点超时、模型/LoRA版本、图片保存、参考图编辑、视频协议、Key权限与撤销、空闲释放边界、持久化恢复、重启后的未知状态隔离、任务公平扫描。
- 同一组server 11项测试也已在本机隔离的真实PostgreSQL 17容器上通过，验证实际数据库事务、外键与JSON存储；测试容器已移除，不使用生产数据库。
- 同一组11项测试在准备部署的ARM64 server镜像与ARM64 PostgreSQL上也已全部通过；已准备离线镜像包，避免DGX网络下载反复中断。
- DGX上实际部署镜像与隔离PostgreSQL中，server同11项非GPU测试全部通过；验证库使用platform_verify，生产image_gen库不受测试重建影响。
- 本地infra 25项测试通过；DGX实际新镜像中的同25项CPU协议测试通过。未调用GPU生成。
- 实际浏览器连接测试server，验证后端接入、LoRA、参考上传、任务出图与下载、视频排队、Key管理、队列上限、桌面/手机布局、退出登录和API Key会话隔离。测试使用受控后端，图片是测试fixture，不是DGX模型产物。
- DGX仓库：`/home/sakana/work/image-gen-platform`。已生成两模型的内容摘要、model-config.json和隔离LoRA目录；原始权重只读挂载，未移动或改写。凭证保存在权限0600的.env，不进入Git或报告。
- 三个容器均healthy：image-gen-platform-server-1、image-gen-platform-database-1、image-gen-platform-inference-1。server端口仅绑定DGX回环127.0.0.1:18080。
- 真实接口14项检查通过：健康与鉴权、后端接入、两模型/五LoRA聚合、API Key权限和撤销、固定时限、目录与页面访问。可用性查询实测52ms，状态need_load。
- 实际浏览器经SSH转发访问DGX，管理员登录、模型/LoRA切换、配置JSON、Key管理、参数页面和退出登录通过，无浏览器运行错误，未提交生成任务。

## 当前模型与边界

| 模型 | LoRA数 | 参考图 | 当前状态 |
|---|---:|---|---|
| qwen-image-2.1 | 4 | 当前backend不支持 | 空载，need_load |
| noobai-XL-1.1 | 1 | 当前backend不支持 | 空载，need_load |

当前接入local-dgx一个推理节点；第二台DGX可在后台配置地址和两个服务Token后接入。当前节点只有图片模型，没有已部署的视频或参考图backend；参考图/视频流程已用受控backend验证，不能视为真实模型质量验收。初次部署遵照用户选择只做非GPU检查；随后用户明确授权真实GPU测试和临时暂停其他容器，完成了下面的验收。没有做显存压力或长期稳定性测试。

## 真实GPU端到端验收与恢复

- 测试前通过SSH核实两台机器：local-dgx（spark-adeb）运行8个容器；2-local-dgx（spark-d705）没有运行容器。两台glm53-flash-tf均已停止。没有启动原本停止的GLM。
- 暂停第一台的5个其他运行容器：dsh-auth-ingress-1、dsh-auth-authelia-1、dsh-auth-gateway-1、new-api、paper-agent；保留平台的server、database、inference执行测试。第二台没有需要暂停的容器。
- Qwen真实GPU任务经过server调度自动加载并生成512×512图片；LoRA改变像素，移除LoRA后像素与基线完全一致。下载PNG通过Pillow解码与非空图像检查。
- SDXL真实同步Images API返回200，b64_json与鉴权下载的持久化PNG完全一致。数据库实查model_sha256及LoRA sha256；任务保留模型revision、node_id和storage_key。
- 实际浏览器选择noobai-XL-1.1及bocchi_illustrious_lora，按默认1024×1024、25步生成；验证真实图片渲染、下载及刷新后历史记录。最终成功UI任务为660bf60f-184a-441e-817a-b87551ec2744，无浏览器运行错误。
- 验收工具的Python路径、响应头大小写及浏览器等待/URL处理曾中断检查，修正后继续验证。中断时容器恢复逻辑正常执行，已完成的GPU任务均成功；最终验收报告passed=true、phase=finished。
- 最终9笔真实GPU任务全部成功，无等待任务；3个测试API Key均已撤销。两模型通过infra的unload接口释放，状态need_load。
- 第一台恢复到测试前同一组8个运行容器（容器ID集合完全一致），所有有健康检查的容器均healthy。第二台仍无运行容器，两台GLM保持原停止状态。

证据：verification-output/gpu-e2e/report.json、first-attempt-report.json、second-attempt-report.json、final-audit.json及真实PNG；verification-output/gpu-ui/report.json、result-page.png、real-ui-result.png。验收工具为scripts/gpu_e2e.py和scripts/live_gpu_ui.cjs；GPU验收脚本只能在明确授权暂停其他运行容器的维护窗口使用，测试期间需运行浏览器验收并写入ui.done，否则15分钟后检查失败并恢复容器。

## 访问与维护

本机已建立SSH转发，可打开http://127.0.0.1:18080。管理员凭证为DGX部署.env的PLATFORM_ADMIN_TOKEN。转发断开时重新运行：

```powershell
ssh -N -L 18080:127.0.0.1:18080 local-dgx
```

保留.env、models、platform-data和数据库卷。更新源码和镜像后：

```bash
cd ~/work/image-gen-platform
export VCS_REF=$(git rev-parse HEAD)
docker compose -f compose.yaml -f compose.resources.json build
docker compose -f compose.yaml -f compose.resources.json -f compose.catalog.yaml run --rm --no-deps inference
docker compose -f compose.yaml -f compose.resources.json up -d --wait
python3 scripts/verify_platform.py
```

DGX网络下载曾多次中断，最终在本机构建并验证ARM64 server/PostgreSQL镜像后导入；GPU依赖复用DGX已有的验收镜像。离线包传输前后的SHA256一致。服务没有模型启动预加载，因此上线本身不会加载GPU权重。

证据：DGX与本机verification-output/platform-report.json；本机verification-output/live-ui/report.json及实际页面截图；server和infra测试输出。上述文件不包含凭证。
