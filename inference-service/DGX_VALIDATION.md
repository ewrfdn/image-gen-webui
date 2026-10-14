# local-dgx Docker 开发验收 — 2026-10-11

结论：当前两种模型和五份 LoRA 已达到开发联调可用；尚不能认定为生产成熟。真实出图、LoRA 效果与请求隔离、并发拒绝、错误后继续生成均通过。Qwen 冷加载需数分钟，长期稳定性、真实 GPU OOM 恢复和接口允许的最大图片尺寸仍未验收。

## 部署

- 主机：`local-dgx` / `192.168.50.142`，NVIDIA GB10、ARM64、121.7 GiB 统一内存。
- 源码：`/home/sakana/work/image-gen-inference-dev/service`；本次传输工作树，没有推送 Git。
- 容器：`image-gen-inference-dev-inference-1`，dev target、单 worker、源码 reload、模型只读挂载。
- 镜像：`image-gen-inference:0.1.0-dev`，ID `sha256:49c1a45b93ac351617cea1f08a88d289d2f0ad822a8b23cf308b691d113903af`。
- OCI 版本：`0.1.0`，源码标记 `00dfbb5-dirty`。
- API：DGX `http://127.0.0.1:18001`。通过 `ssh -N -L 18001:127.0.0.1:18001 local-dgx` 转发到本机。
- 本次已建立本机后台 SSH 转发（PID 52172），本机健康接口实测返回 200；API 文档为 `http://127.0.0.1:18001/docs`。
- 两个 Token 位于部署目录 `.env`，权限 0600；报告不包含凭证。
- 当前常驻模型：`qwen-image-2.1`，状态 ready，`can_generate=true`。NoobAI 已卸载但配置保留；切换须先卸载当前模型。

运行栈：Python 3.14.6、Torch 2.9.1+cu130、Torchvision 0.24.1、Diffusers 0.41.0.dev0（固定提交 `d961a388fd02e4db38d17350c8dd9b8abe642e05`）、Transformers 5.19.0、CUDA 13.0、cuDNN 9.13。完整清单见 `verification-output/runtime-requirements.txt` 和 `environment.json`。

NGC 注册表本次连接返回 EOF，因此采用已有 CUDA 基础镜像和官方 cu130 wheel。该 Torch wheel 仍打印 GB10 SM 12.1 能力范围提示；实际 CUDA 运算及本报告中的出图全部通过。生产环境仍需验证完整的厂商适配栈；NVIDIA 的 [Spark 依赖说明](https://docs.nvidia.com/dgx/dgx-spark-porting-guide/porting/dependencies.html) 列出了官方支持范围。

## 验收结果

本机 Python 3.12 和最终 Docker 内 Python 3.14 均为 **23 项测试通过**。包含取消请求后的执行锁保护、OOM 状态恢复、加载中的源状态、LoRA 无损转换和未知模块/维度拒绝。OOM 恢复测试使用异常注入，不代表真实 GPU OOM 已验收。

| 项目 | NoobAI XL 1.1 | Qwen-Image 2.1 |
|---|---|---|
| 512×512 / 4 步 PNG 解码、非黑图 | 通过 | 通过 |
| LoRA 改变输出像素 | illustrious LoRA 通过 | v1–v4 全部通过 |
| LoRA 清理后固定 seed 逐像素复现原图 | 通过 | 通过 |
| 并发请求拒绝为 503 engine_busy / Retry-After: 1 | 通过 | 通过 |
| 无权限和错误权限 Token | 通过 | 通过 |
| 不存在的 LoRA | 404 | 404 |
| 卸载、未加载拒绝、重载后出图 | 最终镜像通过 | 转换前完整循环通过；最终转换后跳过额外重载 |
| 原始合并格式 LoRA 拒绝、拒绝后恢复原图 | 不适用 | 400 lora_incompatible，随后原图完全一致 |
| 同时加载第二个模型 | 单模型设计 | 409 capacity_conflict |
| 1024×1024 常规步数及视觉检查 | 25 步通过 | 40 步通过 |

所有 GPU 测试运行在最终相同的 Torch/CUDA 栈中。Qwen 完整生命周期在发现合并 MLP 格式问题前执行；修复后再次加载、验证四份转换适配器、错误后隔离和大图，并保持常驻。最终 `qwen/report.json` 显式标注 `lifecycle_tested=false`，不把跳过项算作通过。

## 实测耗时与内存

固定 seed 1234，单请求；以下为 API 返回的 `inference_ms`，包含 LoRA 的读取、校验、应用和清理，不包含模型冷加载。它们是本次样本，不能作为稳定吞吐或 P95 指标。

| 请求 | 时间 |
|---|---:|
| NoobAI 加载及一次预热 | 6.49 s |
| NoobAI 512×512 / 4 步，无 LoRA | 0.55–0.58 s |
| NoobAI 512×512 / 4 步，LoRA | 1.74 s |
| NoobAI 1024×1024 / 25 步，LoRA | 14.15 s |
| Qwen 加载及一次预热 | 192.53 s；先前运行 221.49 s |
| Qwen 512×512 / 4 步，无 LoRA | 1.81–1.82 s |
| Qwen 512×512 / 4 步，完整 v4 LoRA | 2.56 s |
| Qwen 1024×1024 / 40 步，完整 v4 LoRA | 78.88 s |

512×512 验收后采样：NoobAI 进程 allocated 约 6.58 GiB、reserved 约 8.03 GiB；Qwen allocated 约 30.24 GiB、reserved 约 32.54 GiB。这些是采样值，不是最大显存峰值。GB10 为统一内存，不能直接等同于独立显存卡上的预算。

## 本次修复

1. 后台 CUDA 线程现在持有执行槽直到完成。请求协程取消不会提前释放锁。
2. 加载开始即记录 checkpoint revision，加载期间源状态正确显示 present；失败时清除 revision。
3. 补齐 Qwen3VL 处理器需要的 Torchvision。
4. AI-toolkit 的 fused `img_mlp.gate_up` LoRA 副本转换为 Diffusers 的 gate_layer/proj：共享 A，B 按 `[gate; up]` 顺序拆分。原始模型和 LoRA 未修改，转换元数据记录来源 SHA256。
5. Qwen 应用 LoRA 前检查全部模块、A/B 配对和矩阵维度，防止部分权重被忽略后返回 200；服务日志保留拒绝原因。

## 证据文件

- 最终 SDXL：`verification-output/sdxl/report.json`、`quality-1024.png`。
- 最终 Qwen：`verification-output/qwen/report.json`、`additional-checks.json`、`quality-1024.png`，以及 v1–v3 和清理后的 PNG。
- DGX 上同一批文件位于部署目录 `verification-output/`，挂载为容器 `/artifacts`。
- DGX `verification-output/pre-conversion/` 仅保留发现问题之前的记录；这些记录中的图片成功不证明完整 LoRA 已应用，不用于当前兼容性结论。

最终两份 1024 样图均已解码并目视检查，主体、吉他和舞台可识别，无黑图或损坏输出。当前结论仅覆盖本报告中的模型、适配器、尺寸和步数。
