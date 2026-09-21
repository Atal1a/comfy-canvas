# Comfy Canvas

[中文](README.md) · [Version 1.0.1](docs/releases/1.0.1.md)

Generate images, edit photos and create videos from a desktop or phone browser, then browse and organize your work in a masonry gallery. Built on ComfyUI, Comfy Canvas runs on a Windows PC for personal use or shared access over your LAN.

[![Watch Comfy Canvas](docs/showcase/film-v10-preview.gif)](docs/showcase/comfy-canvas-film-v10.mp4)

[Watch the film · 2:28](docs/showcase/comfy-canvas-film-v10.mp4) · [Desktop](docs/showcase/desktop/README.md) · [Mobile](docs/showcase/mobile/README.md)

## Room for your work

A dark background, rounded cards and large previews keep the images in focus. Portrait and landscape images fit together in a masonry gallery. Open a work to inspect details, compare it with the source, or bring its settings into your next edit.

![Desktop gallery](docs/showcase/desktop/gallery.png)

On desktop, reference images, instructions and settings are arranged in steps. On a phone, full-screen viewing, bottom controls and floating navigation keep actions within reach. Pinch the gallery to switch between one, two and three columns.

## Create, inspect, organize

- **Image generation**: turn a text prompt into an image with Krea Turbo, without a reference image. Set dimensions, batch size and seeds, and adjust up to ten optional LoRAs.
- **Image editing**: modify existing images, crop references, compare before and after, then continue editing or upscale the result.
- **Video generation**: turn a text prompt or first-frame image into a video, set its duration and aspect ratio, and play the result.
- Follow the shared queue, check progress and cancel tasks.
- Zoom, pan, compare results and reuse settings for another edit.
- Filter, sort, collect and share works; recover deleted items within the recycle-bin retention period.

Workflows include Krea2 Turbo text-to-image, Krea2 Identity Edit, Qwen Image Edit 2511 and MiniMax H3. [Full feature list](docs/features.md)

## Two ways to use it

### On your own PC

Run ComfyUI and Comfy Canvas on the same Windows PC. Set `server.host` to `127.0.0.1` for access from that computer only, then open `http://127.0.0.1:8090`.

To use your own phone as well, enable LAN access as described below and sign in with the same account.

### Your PC as a shared LAN server

One Windows PC runs ComfyUI, the models and Comfy Canvas. Other people connect from a computer or phone on the same LAN; they only need a browser.

- Set `server.host` to `0.0.0.0` and use the LAN address shown in the launch window.
- Create invitation codes in the administrator settings so others can register their own accounts.
- Each account manages its own works and can share results through messages. Generation uses the server GPU and shared queue.
- Administrators manage users, storage and tasks, with user limits in the configuration.

Keep the server PC, ComfyUI and Comfy Canvas running.

## Install

Prepare a ComfyUI environment that can run your chosen workflows, plus Python 3.12 / 3.13, Git, Node.js 22 and FFmpeg.

Run in the project directory:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\bootstrap.ps1
```

Set the ComfyUI directory, service URL, output directory and model filenames in `config.toml`. Start ComfyUI, then run `启动ComfyCanvas.bat`. The first launch prompts you to create an administrator account.

[Setup guide (Chinese)](docs/setup.md) · [Models](docs/models.md) · [Custom nodes](docs/dependencies.md)

## Project

Designed and developed by Atal1a. Built with FastAPI and SQLite, communicating with ComfyUI through HTTP / WebSocket.

[Contributing](CONTRIBUTING.md) · [Third-party components](THIRD_PARTY_NOTICES.md) · [Workflow sources](docs/workflow-provenance.md) · [Demo assets](docs/showcase/NOTES.md) · [Security](SECURITY.md)

## License

Copyright © 2026 Atal1a. Project-authored code is licensed under [GNU GPL v3.0](LICENSE) (`GPL-3.0-only`). Third-party code, models and workflows retain their own licenses; see [third-party notices](THIRD_PARTY_NOTICES.md).
