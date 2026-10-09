<h1 align="center">Comfy Canvas</h1>

<p align="center">
  <strong>让 ComfyUI 更顺手。</strong><br>
  面向电脑与手机的开源创作界面。
</p>

<p align="center">
  <a href="#安装">安装</a> ·
  <a href="docs/features.md">功能与工作流</a> ·
  <a href="docs/releases/1.0.1.md">版本说明</a> ·
  <a href="docs/README.en.md">English</a>
</p>

[![观看演示：Comfy Canvas 的电脑与手机界面](docs/showcase/intro/cover.jpg)](https://github.com/user-attachments/assets/f71ac999-59b7-4a68-96ee-3665c4d4aff7)

<p align="center"><a href="https://github.com/user-attachments/assets/f71ac999-59b7-4a68-96ee-3665c4d4aff7">观看演示 · 55 秒</a> · <a href="docs/showcase/intro/comfy-canvas-intro.mp4?raw=true">下载视频</a></p>

Comfy Canvas 为 ComfyUI 提供参数表单、任务队列、结果预览和作品管理。上传参考图、填写提示词、调整参数，再查看结果、对比原图，或带着已有参数继续修改。

服务运行在自己的 Windows 电脑上。电脑和手机都通过浏览器访问，也可以与同一局域网里的其他人一起使用。

## 从创作到继续修改

- **图片生成与编辑**：输入提示词生成图片，或上传参考图描述修改要求；设置画幅、Seed 和 LoRA。
- **视频生成**：从文字或首帧图片开始，设置时长与画幅，在结果页播放视频。
- **继续修改**：复用原图、提示词和参数，调整后再次生成；也可以对结果进行高清放大。

![电脑与手机上的参数修改页面](docs/showcase/intro/continue-editing.jpg)

Comfy Canvas 为本项目作者制作的工作流提供配套的创作界面。当前版本仅支持下列工作流及其对应模型。

Krea2 Turbo、Krea2 Identity Edit、Qwen Image Edit 2511、MiniMax H3，以及 Flux 高清放大。[查看工作流与完整功能](docs/features.md)

## 查看细节与生成信息

| 拖动对比原图与结果 | 电脑端悬停查看提示词与参数 |
| :---: | :---: |
| ![拖动分隔线，对比庭院修改前后的光线](docs/showcase/intro/compare.jpg) | ![鼠标悬停在作品卡片上，展示提示词和生成参数](docs/showcase/intro/hover.jpg) |

打开作品后可以放大、拖动查看局部。手机支持双指缩放图片，也可以用双指手势调整瀑布流的列数。

## 浏览与整理作品

横图和竖图按原有比例排列。筛选、排序、收藏作品，查看生成进度，或把结果分享给其他账号。误删的作品可在回收站保留期内恢复。

![按图片比例排列的作品瀑布流](docs/showcase/intro/gallery.jpg)

[更多桌面操作](docs/showcase/desktop/README.md) · [更多手机操作](docs/showcase/mobile/README.md)

## 选择使用方式

**自己使用**：在自己的 Windows 电脑上运行 ComfyUI 和 Comfy Canvas，用电脑或手机浏览器完成创作与作品管理。

**局域网共享**：一台 Windows 电脑运行模型，其他人通过浏览器访问。各账号管理自己的作品，生成任务共用服务器的显卡和队列。

<details>
<summary>本机与局域网配置</summary>

仅在本机访问时，将 `config.toml` 中的 `server.host` 设为 `127.0.0.1`，打开 `http://127.0.0.1:8090`。

使用手机或其他局域网设备访问时，将 `server.host` 设为 `0.0.0.0`，打开启动窗口显示的局域网地址。自己的设备可以登录同一个账号。

管理员在“设置 → 用户与邀请”中生成邀请码，其他人注册自己的账号。管理员可以查看用户、存储和队列，并在配置中设置用户限额。

服务器电脑需要保持开机，ComfyUI 和 Comfy Canvas 都要运行。详细步骤见[安装与配置](docs/setup.md)。

</details>

## 安装

先准备能够运行所需工作流的 ComfyUI 环境，以及 Python 3.12 / 3.13、Git、Node.js 22 和 FFmpeg。

在项目目录运行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\bootstrap.ps1
```

编辑 `config.toml`，填写 ComfyUI 目录、服务地址、输出目录和模型文件名。先启动 ComfyUI，再双击 `启动ComfyCanvas.bat`；首次启动会提示创建管理员账号。

[安装与配置](docs/setup.md) · [模型清单](docs/models.md) · [节点依赖](docs/dependencies.md)

## 项目

由 Atal1a 设计与开发。后端使用 FastAPI 和 SQLite，通过 HTTP / WebSocket 连接 ComfyUI。

[开发与测试](.github/CONTRIBUTING.md) · [第三方组件](docs/THIRD_PARTY_NOTICES.md) · [工作流来源](docs/workflow-provenance.md) · [素材说明](docs/showcase/NOTES.md) · [安全说明](.github/SECURITY.md)

## 许可证

Copyright © 2026 Atal1a。项目自有代码采用 [GNU GPL v3.0](LICENSE)（`GPL-3.0-only`）。第三方代码、模型和工作流遵循各自许可证，见[第三方说明](docs/THIRD_PARTY_NOTICES.md)。
