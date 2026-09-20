# 依赖安装

## 仓库包含什么

- `mobile_server/`：应用、静态资源、测试和权威 `*.mobile.json` 工作流；
- `workflow_sources/`：Krea 与 Qwen 桌面参考工作流；
- `scripts/`、`tools/`、配置样例、CI 和 ComfyUI 最小补丁；
- `dependencies.lock.json`：公共源码依赖的仓库、固定提交、SPDX 许可证、用途和安装层级。

## 仓库不包含什么

- Python 虚拟环境、Node 依赖、便携 Git/FFmpeg 和 ComfyUI checkout；
- 自定义节点 checkout、模型、LoRA、GGUF、VAE、输入和输出；
- SQLite 数据库、账号、邀请码、日志和聊天媒体。

模型安装位置见[模型清单](models.md)。

## 可复现源码依赖

`dependencies.lock.json` 使用 schema v2，固定 ComfyUI 和十一个自定义节点。`mobile_server.doctor --source-only --strict` 会离线验证清单结构、提交格式、许可证字段、补丁路径和主工作流依赖覆盖。`NOASSERTION` 表示许可证信息未确认。

| 层级 | 功能 | 节点 |
|---|---|---|
| main | Krea Identity | `comfyui-krea2edit` |
| main | MiniMax H3 | `ComfyUI-KJNodes`、`ComfyUI-MiniMax-H3-Turbo` |
| optional | Flux 增强放大 | Custom Scripts、EditUtils、Easy Use、SeedVR2、LayerStyle、TTP Toolset、Memory Cleanup、RES4LYF |

Qwen 2511 主工作流所用专用节点来自固定版本的 ComfyUI 本体，不需要额外自定义节点。

默认使用用户已配置好的 ComfyUI。接入已有环境时，按上表在 ComfyUI 自身的 Python 环境中安装节点。

以下脚本仅适用于通过 `bootstrap.ps1 -InstallComfyUI` 创建的 `runtime/ComfyUI` 环境。安装三条主工作流的最小节点集：

```powershell
.\scripts\install_verified_nodes.ps1 -Profile main
```

安装全部功能的节点集：

```powershell
.\scripts\install_verified_nodes.ps1
```

安装器会核对已有 checkout 的 origin、切换到固定提交，并在存在 `requirements.txt` 时安装 Python 依赖。

许可证和上游地址汇总见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。项目自有代码采用 [GPL-3.0-only](../LICENSE)，第三方组件分别按上游许可使用。

## 第三方许可

Memory Cleanup 的许可记录为 `NOASSERTION`；RES4LYF 使用自定义许可，包含商业服务限制。具体来源及许可记录见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。

模型文件与安装位置见 [models.md](models.md)。
