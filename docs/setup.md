# Windows 安装与配置

[返回首页](../README.md)

## 安装

需要 Python、Git、Node.js/npm 和 FFmpeg。支持 Python 3.12 / 3.13，建议使用 Node.js 22。安装脚本优先选择 Python 3.13，其次是 3.12；也可用 `-PythonExecutable` 指定解释器。显卡和显存要求取决于使用的模型。

在项目目录运行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\bootstrap.ps1
```

安装脚本会创建应用环境和 `config.toml`。先启动你已经配置好的 ComfyUI，再将 `paths.comfyui` 改为它的本地目录、`comfyui.url` 改为它的服务地址；按照 [模型说明](models.md) 填写已有模型的文件名。运行 `.\.venv\Scripts\python.exe -m mobile_server.doctor` 检查配置后，双击 `启动ComfyCanvas.bat`。第一次启动需要在终端创建管理员账号。

模型、GPU 版 PyTorch 和自定义节点使用 ComfyUI 自身的环境；Comfy Canvas 的 `.venv` 用于运行网页服务。

还需将 `paths.output` 指向 ComfyUI 实际使用的输出目录（通常是其目录下的 `output`）。应用和 ComfyUI 在同一台 Windows 电脑上运行。

可选：需要另建 ComfyUI 环境时，`bootstrap.ps1 -InstallComfyUI` 会下载固定版本源码并安装其普通依赖，但显卡版 PyTorch 仍需自行配置。`install_verified_nodes.ps1` 仅面向这套 `runtime/ComfyUI` 环境；接入已有 ComfyUI 时，按 [依赖说明](dependencies.md) 在已有环境中安装所需节点。默认保持 `comfyui.auto_start = false`。

## 配置

路径、端口、模型文件名、工作流开关、用户限额在 `config.toml` 中设置，无需修改应用代码。

```powershell
.\.venv\Scripts\python.exe -m mobile_server.config show
.\.venv\Scripts\python.exe -m mobile_server.doctor
.\.venv\Scripts\python.exe -m mobile_server.doctor --strict
```

`doctor` 检查配置和依赖，缺少模型时给出警告。加上 `--strict` 后，警告也会让检查失败。`--source-only` 用于只检查源码包，不检查本机模型是否安装。

### 仅本机访问

在 `config.toml` 的 `[server]` 中设置 `host = "127.0.0.1"`。启动后打开 `http://127.0.0.1:8090`。

### 局域网访问与多人使用

在 `[server]` 中设置 `host = "0.0.0.0"`（示例配置的默认值），其他设备连接同一局域网，打开启动窗口显示的地址。ComfyUI 本身仍监听 `127.0.0.1`。

需要放行 Windows 防火墙时，以管理员身份运行 `配置局域网防火墙.bat`；脚本只允许专用网络的本地子网访问。

自己换用手机时登录同一个账号。邀请其他人时，在“设置 → 用户与邀请”生成邀请码，让对方注册账号。生成任务共用服务器的队列；`[limits]` 可设置用户存储额度、排队数量和单次生成数量。服务器电脑和两个服务需要保持运行。

生成结果和账号数据保存在服务器电脑上。备份时应同时保存配置、数据库和作品文件。
