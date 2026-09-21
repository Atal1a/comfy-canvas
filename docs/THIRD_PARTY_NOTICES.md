# Third-party source dependencies

Comfy Canvas project-authored code is licensed under GPL-3.0-only, copyright 2026 Atal1a; see [LICENSE](../LICENSE). This grant does not relicense third-party materials or grant rights to model weights or externally sourced workflow graphs.

`patches/comfyui-progress.patch` modifies GPL-3.0-only ComfyUI source and is distributed under GPL-3.0-only. Upstream copyright notices remain applicable. Other external projects listed below retain their own licenses.

This repository does not vendor the following projects. Bootstrap scripts clone the exact commits recorded in `manifests/dependencies.lock.json`. The links and SPDX identifiers below are provided for review; each upstream license remains authoritative.

| Component | Pinned source | License |
|---|---|---|
| RES4LYF | [419de2d](https://github.com/ClownsharkBatwing/RES4LYF/commit/419de2d7c78f415dde9aa352a7231820ebfc17a4) | LicenseRef-RES4LYF — custom terms include commercial-service restrictions; not vendored |
| comfyui_memory_cleanup | [58de13a](https://github.com/LAOGOU-666/Comfyui-Memory_Cleanup/commit/58de13a6090e04408e343501ff8902c034d9f518) | NOASSERTION — no license file found; not vendored |
| ComfyUI | [c2bcbec](https://github.com/comfyanonymous/ComfyUI/commit/c2bcbecd82ec5ae66594340b395c24ef0217b238) | GPL-3.0-only |
| comfyui-krea2edit | [86f886d](https://github.com/lbouaraba/comfyui-krea2edit/commit/86f886dac23013d88996e3a2e99093ba44d322fb) | Apache-2.0 |
| ComfyUI-KJNodes | [6ab7e81](https://github.com/kijai/ComfyUI-KJNodes/commit/6ab7e8130e449ed2c0037589bcf84146ceb7fc9c) | GPL-3.0-only |
| ComfyUI-MiniMax-H3-Turbo | [4274783](https://github.com/Larryvrh/ComfyUI-MiniMax-H3-Turbo/commit/4274783a23afcfdbea3b4876cb79effd6c510785) | Apache-2.0 |
| ComfyUI-Custom-Scripts | [609f3af](https://github.com/pythongosssss/ComfyUI-Custom-Scripts/commit/609f3afaa74b2f88ef9ce8d939626065e3247469) | MIT |
| ComfyUI-EditUtils | [aedff84](https://github.com/lrzjason/ComfyUI-EditUtils/commit/aedff84e01cd96592474b83fc7be26255c0fb618) | GPL-3.0-only |
| ComfyUI-Easy-Use | [005c578](https://github.com/yolain/ComfyUI-Easy-Use/commit/005c57839c5bee88f1a0a41970ca965ab470a4c8) | GPL-3.0-only |
| ComfyUI-SeedVR2_VideoUpscaler | [4490bd1](https://github.com/numz/ComfyUI-SeedVR2_VideoUpscaler/commit/4490bd1f482e026674543386bb2a4d176da245b9) | Apache-2.0 |
| ComfyUI_LayerStyle | [d94bef1](https://github.com/chflame163/ComfyUI_LayerStyle/commit/d94bef1ee5ed3656f5ff1bb2830a4ffd94f40935) | MIT |
| Comfyui_TTP_Toolset | [c8889e4](https://github.com/TTPlanetPig/Comfyui_TTP_Toolset/commit/c8889e40e90e293226cc6810c7d27b9c17300da6) | MIT |

Model weights are deliberately excluded. Their licenses and redistribution terms must be reviewed separately before publishing download links or packaged artifacts.

The Flux enhanced-upscale workflow was collected from a third party; its original source is still being traced. Other workflow graphs were assembled by Atal1a. See [workflow provenance](workflow-provenance.md); graph authorship does not change the licenses of the nodes and models used by those graphs.
