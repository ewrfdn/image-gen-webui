# local-dgx 整套服务部署记录

日期：2026-10-12。状态：整套服务已部署，非GPU验收通过。真实GPU生成按照用户要求跳过。

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

当前接入local-dgx一个推理节点；第二台DGX可在后台配置地址和两个服务Token后接入。当前节点只有图片模型，没有已部署的视频或参考图backend；参考图/视频流程已用受控backend验证，不能视为真实模型质量验收。本次没有启动或停止GLM，也没有真实GPU出图、显存压力或长期稳定性测试。

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
