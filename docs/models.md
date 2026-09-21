# 模型清单

模型需放入 ComfyUI 对应目录。下表列出默认文件名；使用其他兼容文件时，在 `config.toml` 中填写实际名称。

## 复用已有模型

已有模型可以直接复用。文本编码器支持 `models/text_encoders` 和旧目录 `models/clip`，基础模型支持 `models/diffusion_models` 和旧目录 `models/unet`。同名文件按当前 ComfyUI 的顺序查找：编码器先查 `text_encoders`，基础模型先查 `unet`。

例如，已有 Qwen FP8 mixed、Klein True v2 和对应编码器时，可以在 `config.toml` 中填写：

```toml
[models.qwen2511]
base_model = 'qwen_image_edit_2511_fp8mixed.safetensors'
lightning_lora = '(默认启用加速)Qwen-Image-Edit-2511-Lightning-8steps-V1.0-bf16.safetensors'

[models.enhanced_upscale]
base_model = 'flux\Flux2-Klein-9B-True-v2-fp8mixed.safetensors'
text_encoder = 'qwen_3_8b.safetensors'
```

这些字段填写对应模型目录下的相对文件名，包含子目录。在已有 TOML 表中更新相应字段。模型、编码器和 VAE 需要相互兼容。

`manifests/models.lock.json` 记录参考模型的来源和校验值。自定义文件使用配置中的名称，不要求与参考模型的哈希一致。

## 默认参考文件

四条主工作流的 12 个唯一文件已经记录在 `manifests/models.lock.json`，包括 Hugging Face 仓库、固定 revision、仓库内路径、许可证标识、字节数和 SHA-256。Krea Turbo 文生图与 Identity Edit 共用基础模型、编码器和 VAE，文生图不需要 Identity Edit LoRA。来源 URL 指向上游模型仓库。

文生图文件名在 `[models.krea_turbo]` 中设置；未填写时沿用 `[models.krea_identity]` 的对应配置。

| 工作流 | ComfyUI 目录 | 默认文件 | 必需 |
|---|---|---|---|
| Krea2 Identity | `models/diffusion_models` | `krea2_turbo_fp8_scaled.safetensors` | 是 |
| Krea2 Identity | `models/text_encoders` | `qwen3vl_4b_fp8_scaled.safetensors` | 是 |
| Krea2 Identity | `models/vae` | `qwen_image_vae.safetensors` | 是 |
| Krea2 Identity | `models/loras` | `krea2_identity_edit_v1_2.safetensors` | 是 |
| MiniMax H3 | `models/diffusion_models` | `minimax_h3_fl2va_pruned_int8_convrot.safetensors` | 是 |
| MiniMax H3 | `models/text_encoders` | `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` | 是 |
| MiniMax H3 | `models/vae` | `minimax_h3_video_vae_fp16.safetensors` | 是 |
| MiniMax H3 | `models/vae` | `minimax_h3_audio_vae_fp32.safetensors` | 是 |
| MiniMax H3 | `models/loras` | `minimax_h3_turbo_v4_step600_ema.safetensors` | 是 |
| Qwen 2511 | `models/diffusion_models` | `qwen_image_edit_2511_fp8_e4m3fn_scaled.safetensors` | 是 |
| Qwen 2511 | `models/text_encoders` | `qwen_2.5_vl_7b_fp8_scaled.safetensors` | 是 |
| Qwen 2511 | `models/vae` | `qwen_image_vae.safetensors` | 是 |
| Qwen 2511 | `models/loras` | `Qwen-Image-Edit-2511-Lightning-8steps-V1.0-bf16.safetensors` | 是 |
| Flux 增强放大 | `models/diffusion_models` | `flux-2-klein-9b-fp8.safetensors` | 使用该功能时是 |
| Flux 增强放大 | `models/text_encoders` | `qwen_3_8b_fp8mixed.safetensors` | 使用该功能时是 |
| Flux 增强放大 | `models/vae` | `flux2-vae.safetensors` | 使用该功能时是 |
| Flux 增强放大 | `models/SEEDVR2` | `seedvr2_ema_3b_fp8_e4m3fn.safetensors` | 使用该功能时是 |
| Flux 增强放大 | `models/SEEDVR2` | `ema_vae_fp16.safetensors` | 使用该功能时是 |
| Flux 增强放大 | `models/loras/flux` | `f2k_9B_小志一致性（0.5-0.7）lcs_consist_0412预览版preview.safetensors` | 可选，影响一致性与风格 |

Krea、H3 和 Qwen 每条工作流最多支持 10 个可选 LoRA 槽位，按模型架构选择兼容文件。

Flux 放大的默认一致性 LoRA 在 `config.toml` 的 `[models.enhanced_upscale]` 中设置。文件存在时使用 `基础模型 → LoRA → 采样器`，强度可在界面调整；文件不存在时，采样器直接使用基础模型，生成的一致性和风格可能不同。

## 来源与授权

- Krea Turbo 与 Identity Edit 来自锁文件指向的 Krea/Identity 模型仓库，使用非 SPDX 的 Krea 社区许可，下载前应阅读仓库中的 `LICENSE.pdf` 和 `NOTICE`；
- MiniMax H3 基础文件来自 Comfy-Org 的 H3 仓库，使用模型专用许可；Turbo LoRA 为 Apache-2.0；
- Qwen 2511 基础文件、Lightning、文本编码器和 VAE 的锁定来源均标记为 Apache-2.0；
- 模型从各自来源下载，按上游许可使用。

Flux 增强放大的模型按各自上游条款获取。一致性 LoRA 使用配置中的本地文件。SeedVR2、编码器、VAE 和一致性 LoRA 的来源与许可记录仍待补全。

## 验证

安装后运行：

```bash
python -m mobile_server.doctor --strict
```

普通诊断检查配置路径和已存在锁定文件的字节数，不读取整个模型。需要完整校验时显式运行：

```bash
python -m mobile_server.doctor --strict --verify-model-hashes
```

完整哈希会读取数十 GB 数据，耗时取决于磁盘速度。诊断只读，不修改模型文件。
