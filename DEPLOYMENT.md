# local-dgx 整套服务部署记录

日期：2026-10-12。当前状态：实现和本地验证完成，DGX部署被主机重启后的网络连接阻断，尚未完成整套服务运行验收。

## 已完成

- `codex/platform-ui-server` 分支：实现提交 `74d7a7d`，离线目录挂载/镜像版本修正提交 `678b3e7`，后续包含UI退出登录隔离修正。
- 本地server 11项测试通过：并发maxCount、1秒多节点超时、模型/LoRA版本、图片保存、参考图编辑、视频协议、Key权限与撤销、空闲释放边界、持久化恢复、重启后的未知状态隔离、任务公平扫描。
- 本地infra 25项测试通过；DGX实际新镜像中的同25项CPU协议测试通过。未调用GPU生成。
- 实际浏览器连接测试server，验证后端接入、LoRA、参考上传、任务出图与下载、视频排队、Key管理、队列上限、桌面/手机布局、退出登录和API Key会话隔离。测试使用受控后端，图片是测试fixture，不是DGX模型产物。
- 同一Git提交通过bundle传到DGX，仓库为`/home/sakana/work/image-gen-platform`；已准备隔离模型/LoRA目录和本机.env，未输出或提交Token。
- 新infra镜像`image-gen-inference:0.2.0`已构建。server基础镜像拉取过程中SSH连接中断；server镜像与数据库尚未确认启动。

## 尚未完成

- DGX仓库当时停留在`74d7a7d`；`678b3e7`及后续修正需要联网后同步。
- 第一次manifest生成因Compose继承只读卷而失败；已修复为独立`compose.catalog.yaml`，需在DGX执行验证。
- 整套Compose启动、真实目录聚合、生产PostgreSQL集成测试、live API Key smoke检查和访问转发。
- 本次真实GPU推理按照用户要求跳过：重启前GLM占用约94GiB，未执行任何停止GLM命令。重启后资源和容器状态需要重新确认。

## 恢复后的顺序

先重新确认主机、Git HEAD、容器、镜像和是否有仍在运行的构建/manifest进程，再同步当前分支。保留.env、models、platform-data和数据库卷，不清除业务数据。

```bash
cd ~/work/image-gen-platform
export VCS_REF=$(git rev-parse HEAD)
docker compose -f compose.yaml -f compose.resources.json build
docker compose -f compose.yaml -f compose.resources.json -f compose.catalog.yaml run --rm --no-deps inference
docker compose -f compose.yaml -f compose.resources.json up -d --wait
python3 scripts/verify_platform.py
```

生产地址为DGX回环`http://127.0.0.1:18080`，使用SSH转发后在本机打开。管理员Token只保存在DGX部署.env中。
