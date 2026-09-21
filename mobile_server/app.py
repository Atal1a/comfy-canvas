from __future__ import annotations

from . import __version__
from .model_paths import model_path
from .services.preflight import validate_dependencies

import asyncio
from contextlib import asynccontextmanager, closing
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from datetime import timedelta
import hashlib
from io import BytesIO
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time
import unicodedata
from typing import Annotated, Any
from urllib.parse import quote, urlparse

import httpx
import websockets
from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageFilter, ImageOps, UnidentifiedImageError

from .workflow_compiler import WORKFLOWS, WorkflowCompileError, compile_workflow, enabled_workflows as available_workflows, get_spec, is_two_stage_key, validate_mobile_workflows
from .runtime import (
    PrecompressedStaticFiles,
    apply_security_headers,
    configure_runtime_logging,
    precompressed_file_response,
)
from .database import connect_database, database_session
from .repositories.accounts import AccountRepository
from .repositories.gallery import GalleryRepository
from .services.auth import normalize_username, password_hash, token_digest, verify_password
from .services.avatar import (
    normalized_avatar_crop,
    write_avatar_file as write_avatar_file_service,
)
from .services.gallery import (
    display_job_title as display_job_title_service,
    normalize_job_title,
)
from .services.lora_catalog import LoraClassification, LoraClassifier
from .services.media import (
    enrich_output_record,
    image_key,
    output_media_type,
    output_sets,
    private_image_response as private_image_response_service,
    private_media_cache_headers,
    prune_thumbnail_cache as prune_thumbnail_cache_service,
    remove_images,
    remove_output_files as remove_output_files_service,
    thumbnail_for as thumbnail_for_service,
    video_thumbnail_for as video_thumbnail_for_service,
    private_video_poster_response,
)
from .services.queue import (
    H3_RUNTIME_PROFILE_VERSION,
    h3_profile_fields,
    h3_settings_fingerprint,
    h3_similarity,
    queue_default_estimate,
    queue_percentile,
    queue_profile,
    queue_profile_candidates,
    queue_remaining_estimate,
    weighted_percentile,
)
from .services.workflows import (
    EnhancedUpscaleConfig,
    build_enhanced_upscale_graph as build_enhanced_upscale_graph_service,
    normalize_enhanced_upscale_settings as normalize_enhanced_upscale_settings_service,
)
from .migrations import ensure_migration_table, record_schema_baseline
from .schemas import AccountStorageResponse
from .routes.diagnostics import create_diagnostics_router
from .routes.accounts import AccountRouteDependencies, create_account_router
from .routes.auth import AuthRouteDependencies, create_auth_router
from .routes.gallery import GalleryRouteDependencies, create_gallery_router
from .routes.queue import QueueRouteDependencies, create_queue_router
from .routes.workflows import WorkflowRouteDependencies, create_workflow_router
from .config import load_settings
from .launcher import comfyui_command, prepare_environment


SETTINGS = load_settings()
KNOWN_WORKFLOW_KEYS = frozenset(spec.key for spec in WORKFLOWS)
UNKNOWN_WORKFLOW_KEYS = frozenset(SETTINGS.enabled_workflows) - KNOWN_WORKFLOW_KEYS
if UNKNOWN_WORKFLOW_KEYS:
    raise RuntimeError("Unknown configured workflows: " + ", ".join(sorted(UNKNOWN_WORKFLOW_KEYS)))
ENABLED_WORKFLOW_KEYS = frozenset(SETTINGS.enabled_workflows)


def enabled_workflows():
    return available_workflows(ENABLED_WORKFLOW_KEYS)


ROOT = SETTINGS.project_root
COMFY_ROOT = SETTINGS.comfyui_root
DESKTOP_WORKFLOW_DIR = COMFY_ROOT / "user" / "default" / "workflows"
MOBILE_WORKFLOW_DIR = ROOT / "mobile_server" / "workflows"
ENHANCED_UPSCALE_WORKFLOW = MOBILE_WORKFLOW_DIR / "flux2-klein-enhanced-upscale.mobile.json"
LLAMA_ENHANCE_WORKFLOW = MOBILE_WORKFLOW_DIR / "llama-prompt-enhance.mobile.json"
LLAMA_IMAGE_PROMPT_WORKFLOW = MOBILE_WORKFLOW_DIR / "llama-image-prompt.mobile.json"
LLAMA_MODEL = SETTINGS.prompt_assistant_model
LLAMA_MMPROJ = SETTINGS.prompt_assistant_mmproj
SEEDVR2_MAX_SEED = 4_294_967_295
MAX_GENERATION_SEED = 18_446_744_073_709_551_615
SEED_MODES = {"random", "fixed", "increment", "decrement"}
DEFAULT_LORA_CATEGORIES = ("内容增强", "风格", "调整")
KREA_MODELS = {
    "official_turbo": SETTINGS.krea_identity_model,
}
KREA_MODEL_LABELS = {
    "official_turbo": "Krea Turbo",
}
KREA_IDENTITY_EDIT_LORA = SETTINGS.krea_identity_lora
H3_MODELS = {
    "diffusion": SETTINGS.h3_diffusion_model,
    "text_encoder": SETTINGS.h3_text_encoder,
    "video_vae": SETTINGS.h3_video_vae,
    "audio_vae": SETTINGS.h3_audio_vae,
}
H3_DIFFUSION_MODELS = {
    "original_int8": H3_MODELS["diffusion"],
}
H3_MODEL_LABELS = {
    "original_int8": "H3 Original INT8",
}
H3_TURBO_LORAS = (
    SETTINGS.h3_turbo_lora,
)
H3_DIMENSIONS = {
    "0.4": {"16:9": (864, 480), "9:16": (480, 864), "1:1": (640, 640)},
    "0.5": {"16:9": (960, 544), "9:16": (544, 960), "1:1": (704, 704)},
    "0.6": {"16:9": (1056, 608), "9:16": (608, 1056), "1:1": (768, 768)},
    "0.7": {"16:9": (1152, 640), "9:16": (640, 1152), "1:1": (832, 832)},
    "0.8": {"16:9": (1216, 672), "9:16": (672, 1216), "1:1": (896, 896)},
    "0.9": {"16:9": (1280, 736), "9:16": (736, 1280), "1:1": (960, 960)},
    "1.0": {"16:9": (1376, 768), "9:16": (768, 1376), "1:1": (1024, 1024)},
}
H3_TARGET_SECONDS = 300
H3_QUEUE_BUDGET_SECONDS = 420
H3_HARD_TIMEOUT_SECONDS = 480
H3_ADMIN_QUEUE_BUDGET_SECONDS = 900
# Administrators can intentionally run high-resolution, long H3 generations.
# Keep a recovery limit for genuinely stuck work, but allow enough time for
# the heaviest supported 15-second jobs to finish sampling and encoding.
H3_ADMIN_HARD_TIMEOUT_SECONDS = 3600
KREA_IDENTITY_MAX_PIXELS = 2 * 1024 * 1024
KREA_IDENTITY_REFERENCE_SYSTEM_PROMPT = (
    "Describe the person's identity by detailing facial features, hair, skin tone, "
    "body proportions, and distinctive visual traits. Treat pose, clothing, background, "
    "lighting, camera angle, and composition as changeable unless the instruction preserves them:"
)
ENHANCED_UPSCALE_MODEL = SETTINGS.enhanced_upscale_model
ENHANCED_UPSCALE_TEXT_ENCODER = SETTINGS.enhanced_upscale_text_encoder
ENHANCED_UPSCALE_VAE = SETTINGS.enhanced_upscale_vae
ENHANCED_UPSCALE_LORA = SETTINGS.enhanced_upscale_consistency_lora
ENHANCED_UPSCALE_SEEDVR_DIT = SETTINGS.enhanced_upscale_seedvr_dit
ENHANCED_UPSCALE_SEEDVR_VAE = SETTINGS.enhanced_upscale_seedvr_vae
ENHANCED_UPSCALE_PROMPT = "High definition, 4K, add realistic details, restore high frequency details while preserving identity and composition"
ENHANCED_UPSCALE_PROFILES = {
    "realistic_detail": {
        "strength": 0.7,
        "grain": 0.2,
        "latent_noise_scale": 0.0,
        "prompt": ENHANCED_UPSCALE_PROMPT,
    },
    "realistic_soft": {
        "strength": 0.7,
        "grain": 0.0,
        "latent_noise_scale": 0.002,
        "prompt": (
            "High definition natural photograph, preserve identity, composition, skin tone, and lighting. "
            "Restore only details supported by the source image, keep natural skin and smooth tonal transitions. "
            "Avoid exaggerated pores, gritty or waxy skin, oversharpening, and invented high-frequency texture."
        ),
    },
    "anime": {
        "strength": 0.6,
        "grain": 0.0,
        "latent_noise_scale": 0.0,
        "prompt": (
            "Faithfully upscale the input image to high resolution, using the source image as the sole visual reference. "
            "Preserve exactly the same character identity, face shape, eyes, nose, mouth, expression, hairstyle, proportions, "
            "pose, composition, color palette, existing edge treatment, brushwork, shading, texture density, and level of stylization. "
            "Recover only details supported by the source and improve clarity without redesigning or restyling any element. "
            "Do not add outlines, change line weight, alter facial features, replace the shading, or introduce photographic textures."
        ),
    },
}
UPLOAD_DIR = COMFY_ROOT / "input" / "mobile_uploads"
OUTPUT_DIR = SETTINGS.output_dir
MOBILE_OUTPUT_DIR = OUTPUT_DIR / "mobile"
DATA_DIR = SETTINGS.data_dir
THUMBNAIL_DIR = DATA_DIR / "thumbnails"
CHAT_MEDIA_DIR = DATA_DIR / "chat_media"
AVATAR_DIR = DATA_DIR / "avatars"
DATABASE = DATA_DIR / "jobs.sqlite3"
FFMPEG_PATH = SETTINGS.ffmpeg_path
FFPROBE_PATH = SETTINGS.ffprobe_path
STATIC_SOURCE_DIR = ROOT / "mobile_server" / "static"
STATIC_DIR = ROOT / "mobile_server" / "static_dist"
STATIC_IMMUTABLE_MAX_AGE = 365 * 24 * 60 * 60
PRIVATE_PREVIEW_MAX_AGE = 7 * 24 * 60 * 60
PRIVATE_PREVIEW_STALE_WHILE_REVALIDATE = 24 * 60 * 60
PRIVATE_MEDIA_MAX_AGE = 24 * 60 * 60
VERSIONED_STATIC_FILE_RE = re.compile(r"(?:^|[-_.])v\d+(?=[-_.]|$)", re.IGNORECASE)
HASHED_STATIC_FILE_RE = re.compile(r"-[A-Za-z0-9_-]{8,}\.(?:css|js)$", re.IGNORECASE)
RUNTIME_LOGGER = logging.getLogger("comfy_canvas")
COMFY_URL = SETTINGS.comfy_url
COMFY_WS_URL = "ws" + COMFY_URL.removeprefix("http") + "/ws"
COMFY_CLIENT_ID = "mobile-server-dispatcher"
H3_HEARTBEAT_DIR = COMFY_ROOT / "user" / "h3_heartbeats"
MAX_UPLOAD_BYTES = SETTINGS.max_upload_bytes
MAX_AVATAR_BYTES = 10 * 1024 * 1024
MAX_AVATAR_PIXELS = 40_000_000
AVATAR_SIZE = 256
ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp"}
CHAT_IMAGE_TYPES = ALLOWED_TYPES | {"image/gif"}
MAX_CHAT_VIDEO_BYTES = SETTINGS.max_chat_video_bytes
MAX_CHAT_IMAGES = 9
CHAT_VIDEO_TYPES = {"video/mp4", "video/webm", "video/quicktime"}
CHAT_MEDIA_SUFFIXES = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
    "video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov",
}
DEFAULT_COLLECTION_NAME = "Favorites"
PROMPT_TEMPLATE_SCOPE = "image_edit"
PROMPT_TEMPLATE_SEED_VERSION = "2"
USAGE_TIP_SEED_VERSION = "4"
H3_PAGE_HELP_SEED_VERSION = "1"
TIP_PLACEMENTS = {"create_input", "create_prompt", "create_parameters", "pre_upscale", "postprocess", "flux_upscale"}
PAGE_HELP_TOPICS = {"create_step_1", "create_step_2", "flux_upscale"}
PREPROCESS_RETENTION = timedelta(hours=1)
PREPROCESS_CLEANUP_INTERVAL_SECONDS = 10 * 60
RECYCLE_RETENTION = timedelta(minutes=15)
RECYCLE_CLEANUP_INTERVAL_SECONDS = 5
THUMBNAIL_SIZES = {128, 192, 256, 512, 960}
THUMBNAIL_CACHE_LIMIT = 1024 * 1024 * 1024
THUMBNAIL_CACHE_TARGET = 800 * 1024 * 1024
QUEUE_POLL_SECONDS = 1.0
QUEUE_USER_LIMIT = SETTINGS.queue_user_limit
USER_GENERATION_COUNT_LIMIT = SETTINGS.user_generation_limit
LORA_WEIGHT_LIMIT = 15
ADMIN_GENERATION_COUNT_LIMIT = SETTINGS.admin_generation_limit
H3_RERUN_LOTTERY_MAX = 3
MODEL_INITIALIZATION_TIMEOUT_SECONDS = 10 * 60
H3_FIRST_SAMPLE_TIMEOUT_SECONDS = 3 * 60
H3_CANCEL_RECOVERY_TIMEOUT_SECONDS = 90
H3_RECOVERY_WINDOW_SECONDS = 30 * 60
H3_RECOVERY_HEALTH_TIMEOUT_SECONDS = 120
H3_HEARTBEAT_STALL_SECONDS = 3 * 60
TASK_HARD_TIMEOUT_SECONDS = 60 * 60
KREA_PROMPT_TIMEOUT_SECONDS = 30 * 60
PROMPT_TOOL_TIMEOUT_SECONDS = 10 * 60
QUEUE_TERMINAL_STATES = {"completed", "failed", "cancelled"}
QUEUE_ACTIVE_STATES = {"waiting", "dispatching", "running", "paused", "cancelling", "recovering"}
STORAGE_USAGE_CACHE_SECONDS = 60.0
DIAGNOSTICS_CACHE_SECONDS = 30.0
COMFY_DIAGNOSTICS_CACHE_SECONDS = 5.0
PROMPT_TOOL_TARGETS = {
    "qwen2511-modular-flux2": "Qwen-Image-Edit 2511 [ZH]",
    "krea-identity-edit": "Krea2 图生图 [ZH]",
    "minimax-h3": "MiniMax H3 [ZH]",
}
PROMPT_TOOL_STYLES = {
    "simple": "Prompt Style - 简洁 [ZH]",
    "detailed": "Prompt Style - 详细 [ZH]",
    "extreme": "Prompt Style - 极度详细 [ZH]",
    "cinematic": "Prompt Style - 电影级 [ZH]",
}
PROMPT_TOOL_STYLE_INSTRUCTIONS = {
    "simple": "使用一句简洁但信息完整的描述，保留画面的关键主体、动作和场景。",
    "detailed": "使用两到三句连贯的详细描述，覆盖主体、姿态、外观、环境、光线和关键材质。",
    "extreme": "进行高度详细的视觉描述，准确覆盖主体特征、姿态、服饰、材质、背景、光影与构图。",
    "cinematic": "使用电影级自然语言描述，准确覆盖主体、姿态、镜头、场景、光线、色彩和写实质感。",
}
H3_STEP1_HELP = """MiniMax H3 提示词指南

先在 Step 1 选择“文生视频”或“图生视频”。文生视频从完整的视听描述开始；图生视频会把上传图片作为 0.00 秒的首帧，人物身份、服装、颜色、关键物体和空间关系应保持一致，再描述后续动作。

官方推荐三段式结构：
integrated_multimodal_description: 写镜头、主体动作、对白和同步声音。
overall_soundscape: 写环境声与画面中真实发生的动作声。
non_diegetic_music: 写只有观众能听到的配乐；不需要配乐时写 N/A。

镜头时间线从 [Shot 1] 开始且不写起始时间。切镜可写成 [Shot 2] At 00:03.500, the camera cuts to...；后续时间必须递增并落在视频时长内。运镜要写清类型，必要时补充幅度和速度。

图生视频可在三段式内容前加：
For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.

动作建议按“首帧锚点 → 动作开始 → 连续发展 → 结果或反应”组织。5 秒视频优先单镜头，或最多安排一次明确切镜。"""
H3_STEP2_HELP = """MiniMax H3 参数建议

推荐使用 0.6 MP、5 秒、6 步与 SageAttention，兼顾画质和生成时间。界面只会提供适合当前设备的参数；组合预计超时会在进入队列前被拒绝。

提高分辨率、时长和步数会叠加增加耗时，建议一次只提高一项。5 秒视频优先保持单镜头和清晰的动作主线。"""
DEFAULT_USAGE_TIPS = (
    ("create_input", "qwen2511-modular-flux2", "选择图片", "优先使用主体清晰、遮挡较少的图片；人物、手部和文字区域越清楚，编辑结果通常越稳定。"),
    ("create_prompt", "qwen2511-modular-flux2", "编辑提示词", "先写需要改变的内容，再明确必须保留的人物、姿势、构图和区域；避免互相矛盾的要求。"),
    ("create_input", "krea-identity-edit", "源图也可以只作身份参考", "选择“身份重塑”后，源图用于提取人物脸部、发型、肤色与身体特征；姿势、服装、背景和构图可按指令重新生成。"),
    ("create_input", "krea-identity-edit", "双参考图的固定顺序", "双参考图模式中，图1必须是要保留构图的场景，图2必须是要放入场景的人物或主体；顺序互换会明显降低效果。建议约 1MP 输出，人物保真强度从 4、场景强度从 1 开始。"),
    ("create_prompt", "krea-identity-edit", "区分编辑与身份重塑", "原图编辑需说明必须保留的区域；身份重塑则直接描述希望这个人物出现的新姿势、服装、场景、镜头和光线。"),
    ("create_prompt", "krea-identity-edit", "双参考图提示词", "直接描述图2人物在图1场景中的位置、动作、服装与镜头，例如“将图2中的人物放在图1车辆旁边”。仅使用你有权使用的人物参考图。"),
    ("postprocess", "qwen2511-modular-flux2", "后处理", "Flux 增强放大适合在扩大尺寸的同时补充纹理；处理后重点检查小人脸、手部和文字。"),
    ("flux_upscale", None, "Flux 放大", "2K 适合预览与常规输出，3K、4K 耗时和显存占用依次增加。纹理强度越高改动越明显；远景人物、文字和规则图案建议先使用较低强度。"),
)
DEFAULT_PROMPT_TEMPLATES = (
    ("局部替换", "仅将图中的[]替换为[]。保持主体身份、姿势、构图、光线、背景和其他区域不变；编辑边缘应自然连续。"),
    ("表情调整", "仅将人物的表情调整为[]。保持人物身份、面部结构、头部姿态、身体姿势、镜头、背景和其他内容不变。"),
)
SAMPLERS = ["euler", "euler_ancestral", "dpmpp_2m", "dpmpp_2m_sde", "ddim"]
SCHEDULERS = ["simple", "normal", "karras", "beta", "beta57"]
COMFY_PROGRESS: dict[str, dict[str, Any]] = {}
COMFY_AVAILABLE: bool | None = None
QUEUE_RECOVERING = True
SCHEDULER_LOCK: asyncio.Lock | None = None
CURRENT_USER: ContextVar[dict[str, Any] | None] = ContextVar("mobile_current_user", default=None)
COMFY_HTTP_CLIENT: ContextVar[httpx.AsyncClient | None] = ContextVar("mobile_comfy_http_client", default=None)
LORA_REGISTRY_LOCK = threading.RLock()
LORA_REGISTRY_LAST_SCAN = 0.0
RECYCLE_LOCK = threading.RLock()
SESSION_COOKIE = "mobile_session"
SESSION_DAYS = SETTINGS.session_days
USER_QUOTA_BYTES = SETTINGS.user_quota_bytes
LOGIN_FAILURES: dict[tuple[str, str], list[float]] = {}
CHAT_CONNECTIONS: dict[int, set[WebSocket]] = {}
TASK_CONNECTIONS: dict[int, set[WebSocket]] = {}
TASK_CONNECTION_ROLES: dict[int, bool] = {}
CHAT_MESSAGE_LIMIT = 4000
CHAT_PREPARED_RETENTION = timedelta(hours=1)


def current_user() -> dict[str, Any]:
    user = CURRENT_USER.get()
    if not user:
        raise HTTPException(401, "请先登录")
    return user


def current_owner_id() -> int | None:
    user = CURRENT_USER.get()
    return int(user["user_id"]) if user else None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def db_connect() -> sqlite3.Connection:
    return connect_database(DATABASE)


async def comfy_progress_listener() -> None:
    """Translate ComfyUI's per-node events into monotonic workflow progress."""
    while True:
        try:
            async with websockets.connect(f"{COMFY_WS_URL}?clientId={COMFY_CLIENT_ID}", open_timeout=5) as socket:
                # ComfyUI replaces a duplicate clientId without closing the old
                # socket. Reconnect periodically so a stale listener cannot stay
                # silently orphaned after a WebUI restart or diagnostic session.
                while True:
                    try:
                        raw_event = await asyncio.wait_for(socket.recv(), timeout=15)
                    except TimeoutError:
                        break
                    try:
                        event = json.loads(raw_event)
                    except (TypeError, json.JSONDecodeError):
                        continue
                    process_comfy_progress_event(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            # ComfyUI may still be starting; retry without taking down the mobile UI.
            await asyncio.sleep(2)


def process_comfy_progress_event(event: dict[str, Any]) -> None:
    """Accept both the legacy and current ComfyUI progress websocket formats."""
    data = event.get("data", {})
    if not isinstance(data, dict):
        return
    prompt_id = data.get("prompt_id")
    if not prompt_id:
        return
    event_type = event.get("type")
    if event_type == "progress":
        maximum, value = data.get("max"), data.get("value")
        if isinstance(maximum, (int, float)) and maximum > 0 and isinstance(value, (int, float)):
            update_comfy_progress(
                prompt_id, data.get("node"), value, maximum,
                list_index=data.get("list_index"), list_total=data.get("list_total"),
            )
    elif event_type == "progress_state":
        # ComfyUI 0.32 publishes sampler counters in this aggregate event.
        # This is the only per-step event for several custom samplers, including H3.
        nodes = data.get("nodes")
        if not isinstance(nodes, dict):
            return
        for node_id, state in nodes.items():
            if not isinstance(state, dict):
                continue
            maximum, value = state.get("max"), state.get("value")
            if isinstance(maximum, (int, float)) and maximum > 0 and isinstance(value, (int, float)):
                update_comfy_progress(prompt_id, state.get("node_id", node_id), value, maximum)
    elif event_type == "executing":
        if data.get("node") is None:
            mark_comfy_progress_complete(prompt_id)
        else:
            update_comfy_progress(
                prompt_id, data["node"],
                list_index=data.get("list_index"), list_total=data.get("list_total"),
            )
    elif event_type == "execution_cached":
        for node_id in data.get("nodes") or []:
            update_comfy_progress(prompt_id, node_id, cached=True)
    elif event_type in {"execution_success", "executed"} and data.get("node") is None:
        mark_comfy_progress_complete(prompt_id)


async def task_event_loop() -> None:
    last_snapshots: dict[int, str] = {}
    while True:
        try:
            for user_id, sockets in list(TASK_CONNECTIONS.items()):
                if not sockets:
                    last_snapshots.pop(user_id, None)
                    continue
                snapshot = queue_snapshot(user_id, TASK_CONNECTION_ROLES.get(user_id, False))
                encoded = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
                if encoded == last_snapshots.get(user_id):
                    continue
                last_snapshots[user_id] = encoded
                payload = json.dumps({"type": "queue", "snapshot": snapshot}, ensure_ascii=False)
                dead: list[WebSocket] = []
                for socket in list(sockets):
                    try:
                        await socket.send_text(payload)
                    except Exception:
                        dead.append(socket)
                for socket in dead:
                    sockets.discard(socket)
            await asyncio.sleep(0.75)
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(1.5)


ENHANCED_UPSCALE_STAGES = (
    ("preparing", "准备图片"),
    ("seedvr2", "SeedVR2 纹理重绘"),
    ("flux_loading", "释放显存并加载 Flux"),
    ("sampling", "Flux 分块增强"),
    ("postprocess", "解码与图像处理"),
    ("saving", "保存结果"),
)


def enhanced_upscale_stage(node_type: str) -> str:
    lowered = node_type.lower()
    if node_type == "SaveImage":
        return "saving"
    if node_type == "SeedVR2VideoUpscaler" or node_type.startswith("SeedVR2Load"):
        return "seedvr2"
    if node_type == "VRAMCleanup" or node_type in {
        "UNETLoader", "LoraLoaderModelOnly", "CLIPLoader", "VAELoader",
        "Flux2KleinEditTextEncode_EditUtils",
    }:
        return "flux_loading"
    if "sampler" in lowered or node_type == "BinyuanUltimateSampler":
        return "sampling"
    if any(token in lowered for token in (
        "decode", "color", "grain", "brightness", "scalerestore",
        "imagelisttoimagebatch", "image_assy",
    )):
        return "postprocess"
    return "preparing"


def enhanced_upscale_operation(node_type: str) -> str | None:
    """Map the fixed Flux upscale graph to user-visible units of completed work."""
    lowered = node_type.lower()
    if node_type == "SeedVR2VideoUpscaler":
        return "seedvr2"
    if node_type == "Flux2KleinEditTextEncode_EditUtils":
        return "conditioning"
    if "sampler" in lowered or node_type == "BinyuanUltimateSampler":
        return "sampling"
    if "decode" in lowered:
        return "decode"
    if "scalerestore" in lowered:
        return "restore"
    if "colormatch" in lowered:
        return "color"
    if "imagelisttoimagebatch" in lowered or "image_assy" in lowered:
        return "assembly"
    if "grain" in lowered:
        return "grain"
    if "brightness" in lowered:
        return "brightness"
    if node_type == "SaveImage":
        return "saving"
    if node_type == "VRAMCleanup" or node_type in {
        "UNETLoader", "LoraLoaderModelOnly", "CLIPLoader", "VAELoader",
    }:
        return "flux_loading"
    return None


def enhanced_upscale_work_progress(
    record: dict[str, Any], node_id: Any, fraction: float | None, cached: bool,
) -> tuple[float, float, float, bool] | None:
    """Return current units, total units, percent and whether the active unit is measurable."""
    tile_total = record.get("tile_total")
    if not isinstance(tile_total, int) or tile_total <= 0:
        return None
    node_type = str(record.get("node_types", {}).get(str(node_id), ""))
    operation = enhanced_upscale_operation(node_type)
    repeated = ("seedvr2", "conditioning", "sampling", "decode", "restore", "color")
    total_units = float(6 * tile_total + 6)
    starts = {
        "seedvr2": 1,
        "flux_loading": 1 + tile_total,
        "conditioning": 2 + tile_total,
        "sampling": 2 + 2 * tile_total,
        "decode": 2 + 3 * tile_total,
        "restore": 2 + 4 * tile_total,
        "color": 2 + 5 * tile_total,
        "assembly": 2 + 6 * tile_total,
        "grain": 3 + 6 * tile_total,
        "brightness": 4 + 6 * tile_total,
        "saving": 5 + 6 * tile_total,
    }
    if operation is None:
        current_units = 0.0
        measurable = False
    elif operation in repeated:
        raw_index = record.get("list_index")
        list_index = int(raw_index) if isinstance(raw_index, int) else 0
        list_index = max(0, min(list_index, tile_total - 1))
        inner = 1.0 if cached else fraction if fraction is not None else 0.0
        current_units = float(starts[operation] + list_index) + max(0.0, min(float(inner), 1.0))
        measurable = fraction is not None or isinstance(raw_index, int) or cached
    else:
        current_units = float(starts[operation] + (1 if cached else 0))
        measurable = cached
    return current_units, total_units, min(99.0, 100.0 * current_units / total_units), measurable


def prompt_progress_plan(graph: dict[str, Any]) -> dict[str, Any]:
    """Build phase boundaries from the actual graph sent to ComfyUI."""
    node_types = {str(node_id): str(node.get("class_type") or "") for node_id, node in graph.items()}
    types = set(node_types.values())
    has_seedvr = "SeedVR2VideoUpscaler" in types
    sampler_nodes = {
        node_id for node_id, node_type in node_types.items()
        if "sampler" in node_type.lower() or node_type == "BinyuanUltimateSampler"
    }
    if has_seedvr and sampler_nodes:
        bounds = {"loading": (1, 8), "seedvr2": (8, 42), "cleanup": (42, 44), "sampling": (44, 90), "postprocess": (90, 98), "saving": (98, 99)}
    elif has_seedvr:
        bounds = {"loading": (1, 10), "seedvr2": (10, 95), "postprocess": (95, 98), "saving": (98, 99)}
    else:
        bounds = {"loading": (1, 8), "encoding": (8, 15), "sampling": (15, 90), "postprocess": (90, 98), "saving": (98, 99)}
    enhanced_upscale = has_seedvr and bool(sampler_nodes) and "VRAMCleanup" in types
    stage_keys = [key for key, _ in ENHANCED_UPSCALE_STAGES] if enhanced_upscale else []
    node_stages = {
        node_id: enhanced_upscale_stage(node_type)
        for node_id, node_type in node_types.items()
    } if enhanced_upscale else {}
    return {
        "node_types": node_types, "bounds": bounds, "has_seedvr": has_seedvr,
        "has_sampler": bool(sampler_nodes), "enhanced_upscale": enhanced_upscale,
        "stage_keys": stage_keys, "node_stages": node_stages,
    }


def progress_phase(record: dict[str, Any], node_id: Any) -> tuple[str, str]:
    node_type = str(record.get("node_types", {}).get(str(node_id), ""))
    lowered = node_type.lower()
    if node_type == "SaveImage":
        return "saving", "正在写入结果图"
    if node_type == "SeedVR2VideoUpscaler":
        return "seedvr2", "SeedVR2 正在放大当前图像"
    if node_type == "VRAMCleanup":
        return "cleanup", "正在释放 SeedVR2 显存"
    if node_type == "Flux2KleinEditTextEncode_EditUtils":
        return "flux_loading", "Flux 正在编码当前分块"
    if "sampler" in lowered or node_type == "BinyuanUltimateSampler":
        return "sampling", "Flux 正在采样" if record.get("has_seedvr") else "正在采样"
    if "decode" in lowered:
        return "postprocess", "正在进行 VAE 解码"
    if "scalerestore" in lowered:
        return "postprocess", "正在恢复分块尺寸"
    if "colormatch" in lowered or "color" in lowered:
        return "postprocess", "正在匹配原图色彩"
    if "imagelisttoimagebatch" in lowered or "image_assy" in lowered:
        return "postprocess", "正在拼合增强分块"
    if "grain" in lowered:
        return "postprocess", "正在添加纹理颗粒"
    if "brightness" in lowered:
        return "postprocess", "正在调整图像亮度"
    if "encode" in lowered and "loader" not in lowered:
        return "encoding", "正在编码图像与提示词"
    if "loader" in lowered or "load" in lowered or "lora" in lowered:
        return "loading", "正在加载模型"
    return "loading", "正在准备工作流"


def register_comfy_prompt(prompt_id: str, graph: dict[str, Any]) -> None:
    previous = COMFY_PROGRESS.get(prompt_id, {})
    plan = prompt_progress_plan(graph)
    previous.update(plan)
    previous.setdefault("percent", 1.0)
    previous.setdefault("phase", "loading")
    previous.setdefault("label", "正在准备工作流")
    previous.setdefault("approximate", True)
    if plan.get("enhanced_upscale"):
        previous.setdefault("stage_index", 0)
    COMFY_PROGRESS[prompt_id] = previous


async def restore_running_comfy_progress_plans() -> None:
    """Rebuild graph metadata after the WebUI restarts while ComfyUI keeps running."""
    try:
        queue = await comfy_json("GET", "/queue")
    except Exception:
        return
    for queue_name in ("queue_running", "queue_pending", "queue_queued"):
        for entry in queue.get(queue_name) or []:
            if not isinstance(entry, (list, tuple)) or len(entry) < 3:
                continue
            prompt_id, graph = entry[1], entry[2]
            if isinstance(prompt_id, str) and isinstance(graph, dict):
                register_comfy_prompt(prompt_id, graph)


def update_comfy_progress(
    prompt_id: str, node_id: Any, value: Any = None, maximum: Any = None,
    cached: bool = False, list_index: Any = None, list_total: Any = None,
) -> None:
    record = COMFY_PROGRESS.setdefault(prompt_id, {})
    node_key = str(node_id)
    same_node = record.get("node") == node_key
    if isinstance(list_total, int) and list_total > 0 and isinstance(list_index, int) and list_index >= 0:
        record.update({"list_index": list_index, "list_total": list_total})
        if record.get("enhanced_upscale"):
            record["tile_total"] = list_total
            record["tile_current"] = min(list_index + 1, list_total)
    elif not same_node:
        record.pop("list_index", None)
        record.pop("list_total", None)
    phase, label = progress_phase(record, node_id)
    bounds = record.get("bounds") or {"loading": (1, 8), "sampling": (15, 90), "postprocess": (90, 98), "saving": (98, 99)}
    low, high = bounds.get(phase, bounds.get(record.get("phase"), (1, 8)))
    fraction = None
    if isinstance(value, (int, float)) and isinstance(maximum, (int, float)) and maximum > 0:
        fraction = max(0.0, min(float(value) / float(maximum), 1.0))
    candidate = high if cached else low + (high - low) * fraction if fraction is not None else low
    record.update({"node": node_key, "phase": phase, "label": label, "value": value, "max": maximum, "approximate": fraction is None})
    if record.get("enhanced_upscale"):
        work = enhanced_upscale_work_progress(record, node_id, fraction, cached)
        if work is not None:
            current_units, total_units, work_percent, measurable = work
            record["overall_current"] = max(float(record.get("overall_current") or 0), current_units)
            record["overall_total"] = total_units
            record["percent"] = max(float(record.get("percent") or 0), work_percent)
            record["approximate"] = not measurable
        else:
            record["percent"] = max(float(record.get("percent") or 0), min(candidate, 99.0))
    else:
        record["percent"] = max(float(record.get("percent") or 0), min(candidate, 99.0))
    stage_keys = record.get("stage_keys") or []
    stage_key = (record.get("node_stages") or {}).get(str(node_id))
    if stage_key in stage_keys:
        record["stage_index"] = max(int(record.get("stage_index") or 0), stage_keys.index(stage_key))
        record["stage_key"] = stage_keys[int(record["stage_index"])]
    record["updated_at"] = time.monotonic()


def mark_comfy_progress_complete(prompt_id: str) -> None:
    record = COMFY_PROGRESS.setdefault(prompt_id, {})
    record.update({"percent": 100.0, "phase": "completed", "label": "处理完成", "approximate": False})
    if isinstance(record.get("overall_total"), (int, float)):
        record["overall_current"] = record["overall_total"]
    stage_keys = record.get("stage_keys") or []
    if stage_keys:
        record.update({"stage_index": len(stage_keys) - 1, "stage_key": stage_keys[-1]})


def init_database() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    with closing(db_connect()) as db, db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        ensure_migration_table(db)
        db.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL, username_key TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user',
            disabled INTEGER NOT NULL DEFAULT 0,
            avatar_version INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS sessions (
            session_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS invites (
            invite_id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_hash TEXT NOT NULL UNIQUE, created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
            used_at TEXT, used_by INTEGER, revoked_at TEXT
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS usage_tips (
            tip_id INTEGER PRIMARY KEY AUTOINCREMENT,
            placement TEXT NOT NULL, workflow_key TEXT,
            title TEXT NOT NULL, body TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS help_entries (
            help_id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT NOT NULL, workflow_key TEXT, topic_key TEXT NOT NULL,
            title TEXT NOT NULL, body TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(scope, workflow_key, topic_key)
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS lora_categories (
            category_id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL COLLATE NOCASE UNIQUE,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS lora_metadata (
            lora_name TEXT NOT NULL, family TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            recommended_min REAL, recommended_max REAL,
            category_id INTEGER,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY(lora_name, family),
            FOREIGN KEY(category_id) REFERENCES lora_categories(category_id) ON DELETE SET NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS lora_file_registry (
            family TEXT NOT NULL, lora_name TEXT NOT NULL,
            file_identity TEXT NOT NULL, content_fingerprint TEXT NOT NULL,
            size_bytes INTEGER NOT NULL, modified_ns INTEGER NOT NULL,
            last_seen_at TEXT NOT NULL,
            PRIMARY KEY(family, lora_name)
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_lora_file_identity ON lora_file_registry(file_identity,size_bytes)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_lora_file_fingerprint ON lora_file_registry(content_fingerprint,size_bytes)")
        db.execute("""CREATE TABLE IF NOT EXISTS lora_name_aliases (
            family TEXT NOT NULL, old_name TEXT NOT NULL, new_name TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY(family, old_name)
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS lora_favorites (
            owner_id INTEGER NOT NULL,
            family TEXT NOT NULL, lora_name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(owner_id, family, lora_name),
            FOREIGN KEY(owner_id) REFERENCES users(user_id) ON DELETE CASCADE
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_lora_favorites_owner_family ON lora_favorites(owner_id,family,created_at)")
        db.execute("""CREATE TABLE IF NOT EXISTS recycle_bin (
            entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('group','job','item')),
            record_id TEXT NOT NULL, item_id TEXT NOT NULL DEFAULT '',
            deleted_at TEXT NOT NULL, purge_at TEXT NOT NULL,
            UNIQUE(owner_id,kind,record_id,item_id),
            FOREIGN KEY(owner_id) REFERENCES users(user_id) ON DELETE CASCADE
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_recycle_owner_time ON recycle_bin(owner_id,purge_at,entry_id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_recycle_record ON recycle_bin(owner_id,record_id,kind,item_id)")
        db.execute("""CREATE TABLE IF NOT EXISTS jobs (
            prompt_id TEXT PRIMARY KEY, workflow_key TEXT NOT NULL,
            prompt_text TEXT NOT NULL, source_prompt_text TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
            submitted_at TEXT NOT NULL, completed_at TEXT,
            error_message TEXT, outputs_json TEXT NOT NULL DEFAULT '[]',
            parameters_json TEXT NOT NULL DEFAULT '{}',
            storage_scope TEXT NOT NULL DEFAULT 'mobile',
            is_favorite INTEGER NOT NULL DEFAULT 0
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS generation_groups (
            group_id TEXT PRIMARY KEY, workflow_key TEXT NOT NULL,
            prompt_text TEXT NOT NULL, source_prompt_text TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
            submitted_at TEXT NOT NULL, completed_at TEXT,
            error_message TEXT, outputs_json TEXT NOT NULL DEFAULT '[]',
            parameters_json TEXT NOT NULL DEFAULT '{}',
            items_json TEXT NOT NULL DEFAULT '[]',
            source_image_json TEXT NOT NULL DEFAULT '{}',
            reference_image_json TEXT NOT NULL DEFAULT '{}',
            edit_mask_json TEXT NOT NULL DEFAULT '{}',
            storage_scope TEXT NOT NULL DEFAULT 'mobile',
            is_favorite INTEGER NOT NULL DEFAULT 0
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS favorite_collections (
            collection_id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL COLLATE NOCASE UNIQUE,
            created_at TEXT NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS collection_memberships (
            collection_id INTEGER NOT NULL,
            record_id TEXT NOT NULL,
            PRIMARY KEY(collection_id, record_id),
            FOREIGN KEY(collection_id) REFERENCES favorite_collections(collection_id) ON DELETE CASCADE
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS prompt_templates (
            template_id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL COLLATE NOCASE UNIQUE,
            body TEXT NOT NULL,
            scope TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS app_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS upscale_jobs (
            upscale_id TEXT PRIMARY KEY, prompt_id TEXT,
            parent_group_id TEXT NOT NULL, parent_item_id TEXT NOT NULL,
            status TEXT NOT NULL, submitted_at TEXT NOT NULL, completed_at TEXT,
            error_message TEXT, output_json TEXT NOT NULL DEFAULT '{}',
            parameters_json TEXT NOT NULL DEFAULT '{}', source_image_json TEXT NOT NULL DEFAULT '{}'
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS detail_jobs (
            detail_id TEXT PRIMARY KEY, prompt_id TEXT,
            parent_group_id TEXT NOT NULL, parent_item_id TEXT NOT NULL,
            status TEXT NOT NULL, submitted_at TEXT NOT NULL, completed_at TEXT,
            error_message TEXT, output_json TEXT NOT NULL DEFAULT '{}',
            parameters_json TEXT NOT NULL DEFAULT '{}', source_image_json TEXT NOT NULL DEFAULT '{}'
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS preprocess_jobs (
            preprocess_id TEXT PRIMARY KEY, prompt_id TEXT,
            status TEXT NOT NULL, submitted_at TEXT NOT NULL, completed_at TEXT,
            error_message TEXT, output_json TEXT NOT NULL DEFAULT '{}',
            parameters_json TEXT NOT NULL DEFAULT '{}', source_image_json TEXT NOT NULL DEFAULT '{}'
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS prompt_tool_jobs (
            tool_id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL,
            operation TEXT NOT NULL, target_workflow TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT 'pure', style TEXT NOT NULL DEFAULT 'detailed',
            input_text TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
            submitted_at TEXT NOT NULL, completed_at TEXT, prompt_id TEXT,
            output_text TEXT NOT NULL DEFAULT '', error_message TEXT,
            source_image_json TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY(owner_id) REFERENCES users(user_id) ON DELETE CASCADE
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_prompt_tool_owner_time ON prompt_tool_jobs(owner_id,submitted_at DESC)")
        db.execute("""CREATE TABLE IF NOT EXISTS task_queue (
            queue_id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_kind TEXT NOT NULL, record_id TEXT NOT NULL UNIQUE,
            owner_id INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'waiting',
            queued_at TEXT NOT NULL, started_at TEXT, item_started_at TEXT, finished_at TEXT,
            prompt_id TEXT, profile_key TEXT NOT NULL DEFAULT '',
            attempt INTEGER NOT NULL DEFAULT 0,
            interrupted_count INTEGER NOT NULL DEFAULT 0,
            priority INTEGER NOT NULL DEFAULT 0,
            error_message TEXT, cancel_requested_at TEXT, last_interrupt_at TEXT,
            recovery_retry_count INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(owner_id) REFERENCES users(user_id) ON DELETE CASCADE
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_task_queue_active ON task_queue(state,priority,queue_id)")
        db.execute("""CREATE TABLE IF NOT EXISTS h3_recovery_events (
            recovery_id INTEGER PRIMARY KEY AUTOINCREMENT,
            queue_id INTEGER NOT NULL, record_id TEXT NOT NULL,
            settings_fingerprint TEXT NOT NULL, reason TEXT NOT NULL,
            created_at TEXT NOT NULL, finished_at TEXT, outcome TEXT NOT NULL DEFAULT 'recovering',
            FOREIGN KEY(queue_id) REFERENCES task_queue(queue_id) ON DELETE CASCADE
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_h3_recovery_recent ON h3_recovery_events(created_at DESC)")
        db.execute("""CREATE TABLE IF NOT EXISTS h3_setting_cooldowns (
            settings_fingerprint TEXT PRIMARY KEY, blocked_until TEXT NOT NULL,
            reason TEXT NOT NULL, created_at TEXT NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS server_control (
            control_id INTEGER PRIMARY KEY CHECK(control_id=1),
            performance_mode INTEGER NOT NULL DEFAULT 0,
            recovery_state TEXT NOT NULL DEFAULT 'idle',
            recovery_message TEXT NOT NULL DEFAULT '',
            recovery_started_at TEXT,
            changed_at TEXT NOT NULL, changed_by INTEGER
        )""")
        db.execute(
            "INSERT OR IGNORE INTO server_control(control_id,performance_mode,changed_at) VALUES(1,0,?)",
            (utc_now(),),
        )
        db.execute("""CREATE TABLE IF NOT EXISTS friend_requests (
            request_id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id INTEGER NOT NULL, receiver_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL, acted_at TEXT,
            FOREIGN KEY(sender_id) REFERENCES users(user_id) ON DELETE CASCADE,
            FOREIGN KEY(receiver_id) REFERENCES users(user_id) ON DELETE CASCADE
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_friend_request_receiver ON friend_requests(receiver_id,status,created_at DESC)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_friend_request_sender ON friend_requests(sender_id,status,created_at DESC)")
        db.execute("""CREATE TABLE IF NOT EXISTS friendships (
            user_a_id INTEGER NOT NULL, user_b_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(user_a_id,user_b_id),
            CHECK(user_a_id < user_b_id),
            FOREIGN KEY(user_a_id) REFERENCES users(user_id) ON DELETE CASCADE,
            FOREIGN KEY(user_b_id) REFERENCES users(user_id) ON DELETE CASCADE
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS chat_conversations (
            conversation_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_a_id INTEGER NOT NULL, user_b_id INTEGER NOT NULL,
            created_at TEXT NOT NULL, last_message_at TEXT,
            UNIQUE(user_a_id,user_b_id), CHECK(user_a_id < user_b_id),
            FOREIGN KEY(user_a_id) REFERENCES users(user_id) ON DELETE CASCADE,
            FOREIGN KEY(user_b_id) REFERENCES users(user_id) ON DELETE CASCADE
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS chat_messages (
            message_id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL, sender_id INTEGER NOT NULL,
            sender_name TEXT NOT NULL, body TEXT NOT NULL DEFAULT '',
            client_nonce TEXT, created_at TEXT NOT NULL,
            FOREIGN KEY(conversation_id) REFERENCES chat_conversations(conversation_id) ON DELETE CASCADE,
            FOREIGN KEY(sender_id) REFERENCES users(user_id) ON DELETE CASCADE,
            UNIQUE(sender_id,client_nonce)
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_chat_message_conversation ON chat_messages(conversation_id,message_id DESC)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_chat_message_unread ON chat_messages(conversation_id,sender_id,message_id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_chat_conversation_activity ON chat_conversations(last_message_at DESC,conversation_id DESC)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_chat_conversation_user_a_activity ON chat_conversations(user_a_id,last_message_at DESC,conversation_id DESC)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_chat_conversation_user_b_activity ON chat_conversations(user_b_id,last_message_at DESC,conversation_id DESC)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_friend_request_user_status ON friend_requests(receiver_id,status,created_at DESC)")
        db.execute("""CREATE TABLE IF NOT EXISTS chat_reads (
            conversation_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            last_opened_message_id INTEGER NOT NULL DEFAULT 0, last_opened_at TEXT,
            PRIMARY KEY(conversation_id,user_id),
            FOREIGN KEY(conversation_id) REFERENCES chat_conversations(conversation_id) ON DELETE CASCADE,
            FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS chat_attachments (
            attachment_id TEXT PRIMARY KEY, message_id INTEGER NOT NULL UNIQUE,
            kind TEXT NOT NULL, origin_owner_id INTEGER NOT NULL,
            origin_group_id TEXT NOT NULL, manifest_json TEXT NOT NULL,
            snapshot_json TEXT NOT NULL, created_at TEXT NOT NULL,
            FOREIGN KEY(message_id) REFERENCES chat_messages(message_id) ON DELETE CASCADE
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS chat_imports (
            user_id INTEGER NOT NULL, attachment_id TEXT NOT NULL,
            imported_group_id TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY(user_id,attachment_id),
            FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE,
            FOREIGN KEY(attachment_id) REFERENCES chat_attachments(attachment_id) ON DELETE CASCADE
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS chat_prepared_assets (
            token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
            attachment_id TEXT NOT NULL, file_id TEXT NOT NULL,
            source_image_json TEXT NOT NULL, created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL, consumed_at TEXT,
            FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE,
            FOREIGN KEY(attachment_id) REFERENCES chat_attachments(attachment_id) ON DELETE CASCADE
        )""")
        lora_metadata_columns = {row[1] for row in db.execute("PRAGMA table_info(lora_metadata)")}
        if "category_id" not in lora_metadata_columns:
            db.execute("ALTER TABLE lora_metadata ADD COLUMN category_id INTEGER")
        category_seed = db.execute("SELECT value FROM app_metadata WHERE key='lora_category_seed_version'").fetchone()
        if not category_seed:
            now = utc_now()
            for sort_order, name in enumerate(DEFAULT_LORA_CATEGORIES):
                db.execute(
                    "INSERT OR IGNORE INTO lora_categories(name,sort_order,created_at,updated_at) VALUES(?,?,?,?)",
                    (name, sort_order, now, now),
                )
            db.execute("INSERT INTO app_metadata(key,value) VALUES('lora_category_seed_version','1')")
        user_columns = {row[1] for row in db.execute("PRAGMA table_info(users)")}
        if "avatar_version" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN avatar_version INTEGER NOT NULL DEFAULT 0")
        columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
        if "parameters_json" not in columns:
            db.execute("ALTER TABLE jobs ADD COLUMN parameters_json TEXT NOT NULL DEFAULT '{}'")
        if "storage_scope" not in columns:
            db.execute("ALTER TABLE jobs ADD COLUMN storage_scope TEXT NOT NULL DEFAULT 'legacy'")
        if "is_favorite" not in columns:
            db.execute("ALTER TABLE jobs ADD COLUMN is_favorite INTEGER NOT NULL DEFAULT 0")
        if "group_id" not in columns:
            db.execute("ALTER TABLE jobs ADD COLUMN group_id TEXT")
        if "owner_id" not in columns:
            db.execute("ALTER TABLE jobs ADD COLUMN owner_id INTEGER")
        if "logical_index" not in columns:
            db.execute("ALTER TABLE jobs ADD COLUMN logical_index INTEGER NOT NULL DEFAULT 0")
        if "attempt_no" not in columns:
            db.execute("ALTER TABLE jobs ADD COLUMN attempt_no INTEGER NOT NULL DEFAULT 1")
        if "superseded" not in columns:
            db.execute("ALTER TABLE jobs ADD COLUMN superseded INTEGER NOT NULL DEFAULT 0")
        if "title" not in columns:
            db.execute("ALTER TABLE jobs ADD COLUMN title TEXT NOT NULL DEFAULT ''")
        if "source_prompt_text" not in columns:
            db.execute("ALTER TABLE jobs ADD COLUMN source_prompt_text TEXT NOT NULL DEFAULT ''")
        queue_columns = {row[1] for row in db.execute("PRAGMA table_info(task_queue)")}
        if "cancel_requested_at" not in queue_columns:
            db.execute("ALTER TABLE task_queue ADD COLUMN cancel_requested_at TEXT")
        if "last_interrupt_at" not in queue_columns:
            db.execute("ALTER TABLE task_queue ADD COLUMN last_interrupt_at TEXT")
        if "item_started_at" not in queue_columns:
            db.execute("ALTER TABLE task_queue ADD COLUMN item_started_at TEXT")
        if "recovery_retry_count" not in queue_columns:
            db.execute("ALTER TABLE task_queue ADD COLUMN recovery_retry_count INTEGER NOT NULL DEFAULT 0")
        control_columns = {row[1] for row in db.execute("PRAGMA table_info(server_control)")}
        if "recovery_state" not in control_columns:
            db.execute("ALTER TABLE server_control ADD COLUMN recovery_state TEXT NOT NULL DEFAULT 'idle'")
        if "recovery_message" not in control_columns:
            db.execute("ALTER TABLE server_control ADD COLUMN recovery_message TEXT NOT NULL DEFAULT ''")
        if "recovery_started_at" not in control_columns:
            db.execute("ALTER TABLE server_control ADD COLUMN recovery_started_at TEXT")
        group_columns = {row[1] for row in db.execute("PRAGMA table_info(generation_groups)")}
        if "items_json" not in group_columns:
            db.execute("ALTER TABLE generation_groups ADD COLUMN items_json TEXT NOT NULL DEFAULT '[]'")
        if "source_image_json" not in group_columns:
            db.execute("ALTER TABLE generation_groups ADD COLUMN source_image_json TEXT NOT NULL DEFAULT '{}'")
        if "edit_source_image_json" not in group_columns:
            db.execute("ALTER TABLE generation_groups ADD COLUMN edit_source_image_json TEXT NOT NULL DEFAULT '{}'")
        if "reference_image_json" not in group_columns:
            db.execute("ALTER TABLE generation_groups ADD COLUMN reference_image_json TEXT NOT NULL DEFAULT '{}'")
        if "edit_mask_json" not in group_columns:
            db.execute("ALTER TABLE generation_groups ADD COLUMN edit_mask_json TEXT NOT NULL DEFAULT '{}'")
        if "preprocess_json" not in group_columns:
            db.execute("ALTER TABLE generation_groups ADD COLUMN preprocess_json TEXT NOT NULL DEFAULT '{}'")
        if "owner_id" not in group_columns:
            db.execute("ALTER TABLE generation_groups ADD COLUMN owner_id INTEGER")
        if "title" not in group_columns:
            db.execute("ALTER TABLE generation_groups ADD COLUMN title TEXT NOT NULL DEFAULT ''")
        if "source_prompt_text" not in group_columns:
            db.execute("ALTER TABLE generation_groups ADD COLUMN source_prompt_text TEXT NOT NULL DEFAULT ''")
        if "origin_json" not in group_columns:
            db.execute("ALTER TABLE generation_groups ADD COLUMN origin_json TEXT NOT NULL DEFAULT '{}'")
        upscale_columns = {row[1] for row in db.execute("PRAGMA table_info(upscale_jobs)")}
        if "source_kind" not in upscale_columns:
            db.execute("ALTER TABLE upscale_jobs ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'edit'")
        if "engine" not in upscale_columns:
            db.execute("ALTER TABLE upscale_jobs ADD COLUMN engine TEXT NOT NULL DEFAULT 'seedvr2'")
        if "owner_id" not in upscale_columns:
            db.execute("ALTER TABLE upscale_jobs ADD COLUMN owner_id INTEGER")
        detail_columns = {row[1] for row in db.execute("PRAGMA table_info(detail_jobs)")}
        if "owner_id" not in detail_columns:
            db.execute("ALTER TABLE detail_jobs ADD COLUMN owner_id INTEGER")
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_group_active_order "
            "ON jobs(group_id,superseded,logical_index,submitted_at)"
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_groups_owner_status_time ON generation_groups(owner_id,status,submitted_at DESC)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_jobs_owner_status_time ON jobs(owner_id,status,submitted_at DESC)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_queue_owner_state ON task_queue(owner_id,state,queue_id DESC)")
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_upscale_jobs_group_item_time "
            "ON upscale_jobs(parent_group_id,parent_item_id,submitted_at DESC)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_detail_jobs_group_item_time "
            "ON detail_jobs(parent_group_id,parent_item_id,submitted_at DESC)"
        )
        preprocess_columns = {row[1] for row in db.execute("PRAGMA table_info(preprocess_jobs)")}
        if "owner_id" not in preprocess_columns:
            db.execute("ALTER TABLE preprocess_jobs ADD COLUMN owner_id INTEGER")
        queue_columns = {row[1] for row in db.execute("PRAGMA table_info(task_queue)")}
        if "progress_value" not in queue_columns:
            db.execute("ALTER TABLE task_queue ADD COLUMN progress_value REAL NOT NULL DEFAULT 0")
        if "progress_phase" not in queue_columns:
            db.execute("ALTER TABLE task_queue ADD COLUMN progress_phase TEXT NOT NULL DEFAULT ''")
        if "progress_approximate" not in queue_columns:
            db.execute("ALTER TABLE task_queue ADD COLUMN progress_approximate INTEGER NOT NULL DEFAULT 1")
        collection_columns = {row[1] for row in db.execute("PRAGMA table_info(favorite_collections)")}
        if "owner_id" not in collection_columns:
            db.execute("ALTER TABLE favorite_collections ADD COLUMN owner_id INTEGER")
        template_columns = {row[1] for row in db.execute("PRAGMA table_info(prompt_templates)")}
        if "owner_id" not in template_columns:
            db.execute("ALTER TABLE prompt_templates ADD COLUMN owner_id INTEGER")
        if "is_system" not in template_columns:
            db.execute("ALTER TABLE prompt_templates ADD COLUMN is_system INTEGER NOT NULL DEFAULT 0")
            db.execute("UPDATE prompt_templates SET is_system=1, owner_id=NULL")
        auth_schema = db.execute("SELECT value FROM app_metadata WHERE key='multi_user_schema'").fetchone()
        if not auth_schema:
            # Replace the old globally-unique names with per-owner uniqueness.
            # System templates have a NULL owner and remain globally read-only.
            db.execute("ALTER TABLE prompt_templates RENAME TO prompt_templates_legacy")
            db.execute("""CREATE TABLE prompt_templates (
                template_id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL COLLATE NOCASE, body TEXT NOT NULL,
                scope TEXT NOT NULL, sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                owner_id INTEGER, is_system INTEGER NOT NULL DEFAULT 0,
                UNIQUE(owner_id, name)
            )""")
            db.execute("INSERT INTO prompt_templates SELECT template_id,name,body,scope,sort_order,created_at,updated_at,owner_id,is_system FROM prompt_templates_legacy")
            db.execute("DROP TABLE prompt_templates_legacy")
            db.execute("ALTER TABLE favorite_collections RENAME TO favorite_collections_legacy")
            db.execute("""CREATE TABLE favorite_collections (
                collection_id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL COLLATE NOCASE, created_at TEXT NOT NULL,
                owner_id INTEGER, UNIQUE(owner_id, name)
            )""")
            db.execute("INSERT INTO favorite_collections SELECT collection_id,name,created_at,owner_id FROM favorite_collections_legacy")
            db.execute("ALTER TABLE collection_memberships RENAME TO collection_memberships_legacy")
            db.execute("""CREATE TABLE collection_memberships (
                collection_id INTEGER NOT NULL, record_id TEXT NOT NULL,
                PRIMARY KEY(collection_id, record_id),
                FOREIGN KEY(collection_id) REFERENCES favorite_collections(collection_id) ON DELETE CASCADE
            )""")
            db.execute("INSERT INTO collection_memberships SELECT collection_id,record_id FROM collection_memberships_legacy")
            db.execute("DROP TABLE collection_memberships_legacy")
            db.execute("DROP TABLE favorite_collections_legacy")
            db.execute("INSERT INTO app_metadata(key,value) VALUES('multi_user_schema','1')")
        seeded = db.execute("SELECT value FROM app_metadata WHERE key='prompt_template_seed_version'").fetchone()
        if not seeded:
            now = utc_now()
            db.executemany(
                "INSERT INTO prompt_templates(name, body, scope, sort_order, created_at, updated_at, owner_id, is_system) VALUES(?,?,?,?,?,?,NULL,1)",
                [(name, body, PROMPT_TEMPLATE_SCOPE, index, now, now) for index, (name, body) in enumerate(DEFAULT_PROMPT_TEMPLATES)],
            )
            db.execute("INSERT INTO app_metadata(key, value) VALUES('prompt_template_seed_version', ?)", (PROMPT_TEMPLATE_SEED_VERSION,))
        elif seeded[0] != PROMPT_TEMPLATE_SEED_VERSION:
            # Replace only the shared library. Private user templates are user
            # data and must survive system-template revisions unchanged.
            now = utc_now()
            db.execute("DELETE FROM prompt_templates WHERE scope=? AND is_system=1", (PROMPT_TEMPLATE_SCOPE,))
            db.executemany(
                "INSERT INTO prompt_templates(name, body, scope, sort_order, created_at, updated_at, owner_id, is_system) VALUES(?,?,?,?,?,?,NULL,1)",
                [(name, body, PROMPT_TEMPLATE_SCOPE, index, now, now) for index, (name, body) in enumerate(DEFAULT_PROMPT_TEMPLATES)],
            )
            db.execute("UPDATE app_metadata SET value=? WHERE key='prompt_template_seed_version'", (PROMPT_TEMPLATE_SEED_VERSION,))
        tips_seeded = db.execute("SELECT value FROM app_metadata WHERE key='usage_tip_seed_version'").fetchone()
        if not tips_seeded:
            now = utc_now()
            db.executemany(
                "INSERT INTO usage_tips(placement,workflow_key,title,body,enabled,sort_order,created_at,updated_at) VALUES(?,?,?,?,1,?,?,?)",
                [(placement, workflow_key, title, body, index, now, now) for index, (placement, workflow_key, title, body) in enumerate(DEFAULT_USAGE_TIPS)],
            )
            db.execute("INSERT INTO app_metadata(key,value) VALUES('usage_tip_seed_version',?)", (USAGE_TIP_SEED_VERSION,))
        elif tips_seeded[0] != USAGE_TIP_SEED_VERSION:
            # Only upgrade the untouched built-in copy. Administrator-edited tips
            # remain authoritative and are never overwritten by seed migrations.
            legacy_pre_upscale_bodies = (
                "720 更保真，1080 重建更强；继续编辑前请检查人脸、手部和文字。",
                "3B 较快且稳定，7B 重建能力更强。720 更保真，1080 适合常规增强；1440 耗时和显存占用更高，也更可能改变小人脸与纹理。继续编辑前请检查人脸、手部和文字。",
            )
            db.execute(
                "DELETE FROM usage_tips WHERE placement='pre_upscale' AND workflow_key='qwen2511-modular-flux2' AND title='预放大' AND body IN (?,?)",
                legacy_pre_upscale_bodies,
            )
            db.execute(
                "UPDATE usage_tips SET body=?,updated_at=? WHERE placement='create_input' AND workflow_key='qwen2511-modular-flux2' AND title='选择图片' AND body=?",
                (DEFAULT_USAGE_TIPS[0][3], utc_now(), "优先使用主体清晰、遮挡较少的图片；低清或人物较小时可先预放大。"),
            )
            db.execute(
                "UPDATE usage_tips SET body=?,updated_at=? WHERE placement='postprocess' AND workflow_key='qwen2511-modular-flux2' AND title='后处理' AND body=?",
                (DEFAULT_USAGE_TIPS[5][3], utc_now(), "SeedVR2 适合保真放大，Flux 增强适合补充纹理；处理后重点检查小人脸和文字。"),
            )
            now = utc_now()
            db.executemany(
                """
                INSERT INTO usage_tips(
                    placement,workflow_key,title,body,enabled,sort_order,created_at,updated_at
                )
                SELECT ?,?,?,?,1,?,?,?
                WHERE NOT EXISTS (
                    SELECT 1 FROM usage_tips
                    WHERE placement=? AND workflow_key IS ? AND title=?
                )
                """,
                [
                    (
                        placement,
                        workflow_key,
                        title,
                        body,
                        index,
                        now,
                        now,
                        placement,
                        workflow_key,
                        title,
                    )
                    for index, (placement, workflow_key, title, body) in enumerate(DEFAULT_USAGE_TIPS)
                ],
            )
            db.execute("UPDATE app_metadata SET value=? WHERE key='usage_tip_seed_version'", (USAGE_TIP_SEED_VERSION,))
        help_migrated = db.execute("SELECT value FROM app_metadata WHERE key='help_entry_migration_version'").fetchone()
        if not help_migrated:
            now = utc_now()
            db.execute(
                """INSERT OR IGNORE INTO help_entries(
                    scope,workflow_key,topic_key,title,body,enabled,sort_order,created_at,updated_at
                ) SELECT placement,workflow_key,'legacy:' || tip_id,title,body,enabled,sort_order,created_at,updated_at
                  FROM usage_tips"""
            )
            db.execute("INSERT INTO app_metadata(key,value) VALUES('help_entry_migration_version','1')")
        page_help_migrated = db.execute("SELECT value FROM app_metadata WHERE key='page_help_migration_version'").fetchone()
        if not page_help_migrated:
            now = utc_now()
            page_sources = {
                "create_step_1": ("快速开始", {"workflow_intro", "create_input", "create_prompt", "pre_upscale"}),
                "create_step_2": ("参数与 LoRA", {"create_parameters", "parameter", "feature", "postprocess"}),
            }
            for workflow_key in ENABLED_WORKFLOW_KEYS:
                for topic_key, (title, scopes) in page_sources.items():
                    exists = db.execute(
                        "SELECT 1 FROM help_entries WHERE scope='page' AND workflow_key=? AND topic_key=?",
                        (workflow_key, topic_key),
                    ).fetchone()
                    if exists:
                        continue
                    placeholders = ",".join("?" for _ in scopes)
                    rows = db.execute(
                        f"SELECT title,body FROM help_entries WHERE enabled=1 AND scope IN ({placeholders}) AND (workflow_key IS NULL OR workflow_key=?) ORDER BY sort_order,help_id",
                        (*sorted(scopes), workflow_key),
                    ).fetchall()
                    body = "\n\n".join(f"{row[0]}\n{row[1]}" for row in rows if row[1].strip())
                    if body:
                        db.execute(
                            "INSERT INTO help_entries(scope,workflow_key,topic_key,title,body,enabled,sort_order,created_at,updated_at) VALUES('page',?,?,?,?,1,0,?,?)",
                            (workflow_key, topic_key, title, body, now, now),
                        )
            exists = db.execute(
                "SELECT 1 FROM help_entries WHERE scope='page' AND workflow_key IS NULL AND topic_key='flux_upscale'"
            ).fetchone()
            if not exists:
                rows = db.execute(
                    "SELECT title,body FROM help_entries WHERE enabled=1 AND scope='flux_upscale' ORDER BY sort_order,help_id"
                ).fetchall()
                body = "\n\n".join(f"{row[0]}\n{row[1]}" for row in rows if row[1].strip())
                if body:
                    db.execute(
                        "INSERT INTO help_entries(scope,workflow_key,topic_key,title,body,enabled,sort_order,created_at,updated_at) VALUES('page',NULL,'flux_upscale','Flux 放大',?,1,0,?,?)",
                        (body, now, now),
                    )
            db.execute("INSERT INTO app_metadata(key,value) VALUES('page_help_migration_version','1')")
        page_help_slots_seeded = db.execute("SELECT value FROM app_metadata WHERE key='page_help_slot_seed_version'").fetchone()
        if not page_help_slots_seeded:
            now = utc_now()
            for spec in enabled_workflows():
                defaults = {
                    "create_step_1": (
                        "开始创作",
                        "选择参考图并填写编辑要求，然后进入下一步。" if spec.image_node else "填写画面内容与要求，然后进入下一步。",
                    ),
                    "create_step_2": (
                        "参数与 LoRA",
                        "确认生成参数和 LoRA。通过“LoRA 说明”添加 LoRA 后，请自行填写权重；推荐区间仅作参考。",
                    ),
                }
                for topic_key, (title, body) in defaults.items():
                    db.execute(
                        """INSERT INTO help_entries(scope,workflow_key,topic_key,title,body,enabled,sort_order,created_at,updated_at)
                           SELECT 'page',?,?,?,?,1,0,?,? WHERE NOT EXISTS(
                               SELECT 1 FROM help_entries WHERE scope='page' AND workflow_key=? AND topic_key=?
                           )""",
                        (spec.key, topic_key, title, body, now, now, spec.key, topic_key),
                    )
            db.execute(
                """INSERT INTO help_entries(scope,workflow_key,topic_key,title,body,enabled,sort_order,created_at,updated_at)
                   SELECT 'page',NULL,'flux_upscale','Flux 放大','选择图片和目标长边，按需要调整纹理强度与颗粒后开始放大。',1,0,?,?
                   WHERE NOT EXISTS(SELECT 1 FROM help_entries WHERE scope='page' AND workflow_key IS NULL AND topic_key='flux_upscale')""",
                (now, now),
            )
            db.execute("INSERT INTO app_metadata(key,value) VALUES('page_help_slot_seed_version','1')")
        h3_help_seeded = db.execute("SELECT value FROM app_metadata WHERE key='h3_page_help_seed_version'").fetchone()
        if not h3_help_seeded or h3_help_seeded[0] != H3_PAGE_HELP_SEED_VERSION:
            now = utc_now()
            h3_pages = (
                ("create_step_1", "MiniMax H3 提示词指南", H3_STEP1_HELP, "填写画面内容与要求，然后进入下一步。"),
                ("create_step_2", "MiniMax H3 参数建议", H3_STEP2_HELP, "确认生成参数和 LoRA。通过“LoRA 说明”添加 LoRA 后，请自行填写权重；推荐区间仅作参考。"),
            )
            for topic_key, title, body, untouched_body in h3_pages:
                existing = db.execute(
                    "SELECT help_id,body FROM help_entries WHERE scope='page' AND workflow_key='minimax-h3' AND topic_key=? ORDER BY help_id LIMIT 1",
                    (topic_key,),
                ).fetchone()
                if not existing:
                    db.execute(
                        "INSERT INTO help_entries(scope,workflow_key,topic_key,title,body,enabled,sort_order,created_at,updated_at) VALUES('page','minimax-h3',?,?,?,1,0,?,?)",
                        (topic_key, title, body, now, now),
                    )
                elif existing[1] == untouched_body:
                    db.execute(
                        "UPDATE help_entries SET title=?,body=?,updated_at=? WHERE help_id=?",
                        (title, body, now, int(existing[0])),
                    )
            db.execute(
                "INSERT INTO app_metadata(key,value) VALUES('h3_page_help_seed_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (H3_PAGE_HELP_SEED_VERSION,),
            )
        record_schema_baseline(db, utc_now())


def db_execute(query: str, values: tuple[Any, ...] = ()) -> None:
    with database_session(DATABASE, write=True) as db:
        db.execute(query, values)


ACCOUNT_REPOSITORY = AccountRepository(lambda: DATABASE)


def db_user(user_id: int) -> dict[str, Any] | None:
    return ACCOUNT_REPOSITORY.user(user_id)


def db_user_by_name(username_key: str) -> dict[str, Any] | None:
    return ACCOUNT_REPOSITORY.user_by_name(username_key)


def create_user_account(username: str, password: str, role: str = "user") -> dict[str, Any]:
    display, key = normalize_username(username)
    if role not in {"user", "admin"}:
        raise HTTPException(400, "无效账号角色")
    now = utc_now()
    try:
        user_id = ACCOUNT_REPOSITORY.insert_user(
            display, key, password_hash(password), role, now, DEFAULT_COLLECTION_NAME,
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "用户名已存在") from exc
    return db_user(user_id) or {}


def claim_legacy_data(user_id: int) -> None:
    """Assign every pre-auth private record to the first administrator."""
    ACCOUNT_REPOSITORY.claim_legacy_data(user_id)


def create_admin_account(username: str, password: str) -> dict[str, Any]:
    init_database()
    if ACCOUNT_REPOSITORY.has_administrator():
        raise HTTPException(409, "管理员账号已经存在")
    user = create_user_account(username, password, "admin")
    claim_legacy_data(int(user["user_id"]))
    return user


def issue_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    ACCOUNT_REPOSITORY.insert_session(
        token_digest(token), user_id, now.isoformat(),
        (now + timedelta(days=SESSION_DAYS)).isoformat(),
    )
    return token


def session_user(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    return ACCOUNT_REPOSITORY.session_user(token_digest(token), utc_now())


def generation_count_limit(user: dict[str, Any] | None = None) -> int:
    account = user if user is not None else CURRENT_USER.get()
    return ADMIN_GENERATION_COUNT_LIMIT if account and account.get("role") == "admin" else USER_GENERATION_COUNT_LIMIT


def avatar_path(user_id: int) -> Path:
    return AVATAR_DIR / f"{int(user_id)}.webp"


def avatar_url(user: dict[str, Any]) -> str | None:
    version = int(user.get("avatar_version") or 0)
    user_id = int(user["user_id"])
    return f"/api/users/{user_id}/avatar?v={version}" if version > 0 and avatar_path(user_id).is_file() else None


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": user["user_id"], "username": user["username"], "role": user["role"],
        "disabled": bool(user["disabled"]), "max_generation_count": generation_count_limit(user),
        "avatar_url": avatar_url(user), "avatar_version": int(user.get("avatar_version") or 0),
    }


def public_chat_user(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(user["user_id"]), "username": user["username"],
        "avatar_url": avatar_url(user), "avatar_version": int(user.get("avatar_version") or 0),
    }


def related_chat_user_ids(user_id: int) -> set[int]:
    related = {int(user_id)}
    with sqlite3.connect(DATABASE) as db:
        for row in db.execute(
            "SELECT user_a_id,user_b_id FROM friendships WHERE user_a_id=? OR user_b_id=?",
            (user_id, user_id),
        ):
            related.update((int(row[0]), int(row[1])))
        for row in db.execute(
            "SELECT sender_id,receiver_id FROM friend_requests WHERE status='pending' AND (sender_id=? OR receiver_id=?)",
            (user_id, user_id),
        ):
            related.update((int(row[0]), int(row[1])))
        for row in db.execute(
            "SELECT user_a_id,user_b_id FROM chat_conversations WHERE user_a_id=? OR user_b_id=?",
            (user_id, user_id),
        ):
            related.update((int(row[0]), int(row[1])))
    return related


async def broadcast_profile_update(user: dict[str, Any]) -> None:
    await emit_chat_event(related_chat_user_ids(int(user["user_id"])), {
        "type": "profile_updated", "user": public_chat_user(user),
    })


def db_job(prompt_id: str) -> dict[str, Any] | None:
    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        owner = current_owner_id()
        row = db.execute("SELECT * FROM jobs WHERE prompt_id = ?" + (" AND owner_id=?" if owner else ""), (prompt_id, owner) if owner else (prompt_id,)).fetchone()
    return dict(row) if row else None


def db_group(group_id: str) -> dict[str, Any] | None:
    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        owner = current_owner_id()
        row = db.execute("SELECT * FROM generation_groups WHERE group_id = ?" + (" AND owner_id=?" if owner else ""), (group_id, owner) if owner else (group_id,)).fetchone()
    return dict(row) if row else None


def unrestricted_group(group_id: str) -> dict[str, Any] | None:
    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM generation_groups WHERE group_id=?", (group_id,)).fetchone()
    return dict(row) if row else None


def db_group_jobs(group_id: str) -> list[dict[str, Any]]:
    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT * FROM jobs WHERE group_id = ? AND COALESCE(superseded,0)=0 ORDER BY logical_index,submitted_at", (group_id,)).fetchall()
    return [dict(row) for row in rows]


def db_group_relations(
    group_id: str,
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    """Load all records needed to render one group through a single connection."""
    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        children = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM jobs WHERE group_id=? AND COALESCE(superseded,0)=0 "
                "ORDER BY logical_index,submitted_at",
                (group_id,),
            ).fetchall()
        ]
        upscale_rows = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM upscale_jobs WHERE parent_group_id=? "
                "ORDER BY parent_item_id,submitted_at DESC",
                (group_id,),
            ).fetchall()
        ]
        detail_rows = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM detail_jobs WHERE parent_group_id=? "
                "ORDER BY parent_item_id,submitted_at DESC",
                (group_id,),
            ).fetchall()
        ]
    upscales: dict[str, list[dict[str, Any]]] = {}
    details: dict[str, list[dict[str, Any]]] = {}
    for entry in upscale_rows:
        upscales.setdefault(str(entry["parent_item_id"]), []).append(entry)
    for entry in detail_rows:
        details.setdefault(str(entry["parent_item_id"]), []).append(entry)
    return children, upscales, details


def db_upscale(upscale_id: str) -> dict[str, Any] | None:
    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        owner = current_owner_id()
        row = db.execute("SELECT * FROM upscale_jobs WHERE upscale_id=?" + (" AND owner_id=?" if owner else ""), (upscale_id, owner) if owner else (upscale_id,)).fetchone()
    return dict(row) if row else None


def unrestricted_upscale(upscale_id: str) -> dict[str, Any] | None:
    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM upscale_jobs WHERE upscale_id=?", (upscale_id,)).fetchone()
    return dict(row) if row else None


def unrestricted_preprocess(preprocess_id: str) -> dict[str, Any] | None:
    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM preprocess_jobs WHERE preprocess_id=?", (preprocess_id,)).fetchone()
    return dict(row) if row else None


def db_item_upscales(group_id: str, item_id_value: str) -> list[dict[str, Any]]:
    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT * FROM upscale_jobs WHERE parent_group_id=? AND parent_item_id=? ORDER BY submitted_at DESC",
            (group_id, item_id_value),
        ).fetchall()
    return [dict(row) for row in rows]


def db_detail(detail_id: str) -> dict[str, Any] | None:
    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        owner = current_owner_id()
        row = db.execute("SELECT * FROM detail_jobs WHERE detail_id=?" + (" AND owner_id=?" if owner else ""), (detail_id, owner) if owner else (detail_id,)).fetchone()
    return dict(row) if row else None


def db_item_details(group_id: str, item_id_value: str) -> list[dict[str, Any]]:
    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT * FROM detail_jobs WHERE parent_group_id=? AND parent_item_id=? ORDER BY submitted_at DESC",
            (group_id, item_id_value),
        ).fetchall()
    return [dict(row) for row in rows]


def db_preprocess(preprocess_id: str) -> dict[str, Any] | None:
    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        owner = current_owner_id()
        row = db.execute("SELECT * FROM preprocess_jobs WHERE preprocess_id=?" + (" AND owner_id=?" if owner else ""), (preprocess_id, owner) if owner else (preprocess_id,)).fetchone()
    return dict(row) if row else None


def db_prompt_tool(tool_id: str) -> dict[str, Any] | None:
    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        owner = current_owner_id()
        row = db.execute(
            "SELECT * FROM prompt_tool_jobs WHERE tool_id=?" + (" AND owner_id=?" if owner else ""),
            (tool_id, owner) if owner else (tool_id,),
        ).fetchone()
    return dict(row) if row else None


def unrestricted_prompt_tool(tool_id: str) -> dict[str, Any] | None:
    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM prompt_tool_jobs WHERE tool_id=?", (tool_id,)).fetchone()
    return dict(row) if row else None


def public_prompt_tool(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["tool_id"], "operation": row["operation"],
        "target_workflow": row["target_workflow"], "mode": row.get("mode") or "pure",
        "style": row.get("style") or "detailed", "input": row.get("input_text") or "",
        "status": row["status"], "submitted_at": row["submitted_at"],
        "completed_at": row.get("completed_at"), "output": clean_prompt_tool_output(row.get("output_text")),
        "error": row.get("error_message"), "has_image": bool(source_image_from(row)),
    }


def db_queue_record(task_kind: str, record_id: str) -> dict[str, Any] | None:
    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT q.*,u.username FROM task_queue q JOIN users u ON u.user_id=q.owner_id WHERE q.task_kind=? AND q.record_id=?",
            (task_kind, record_id),
        ).fetchone()
    return dict(row) if row else None


def performance_mode_enabled() -> bool:
    with sqlite3.connect(DATABASE) as db:
        row = db.execute("SELECT performance_mode FROM server_control WHERE control_id=1").fetchone()
    return bool(row and row[0])


def comfy_recovery_status() -> dict[str, Any]:
    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT recovery_state,recovery_message,recovery_started_at FROM server_control WHERE control_id=1"
        ).fetchone()
    return dict(row) if row else {"recovery_state": "idle", "recovery_message": "", "recovery_started_at": None}


def set_comfy_recovery_status(state: str, message: str = "") -> None:
    started_at = utc_now() if state == "recovering" else None
    db_execute(
        "UPDATE server_control SET recovery_state=?,recovery_message=?,recovery_started_at=? WHERE control_id=1",
        (state, message, started_at),
    )


def h3_fingerprint_summary(parameters: dict[str, Any]) -> str:
    model = str(parameters.get("h3_model") or "original_int8")
    mode = "图生视频" if parameters.get("mode") == "i2v" else "文生视频"
    return (
        f"{H3_MODEL_LABELS.get(model, model)} · {mode} · "
        f"{parameters.get('width', '?')}×{parameters.get('height', '?')} · "
        f"{parameters.get('frames', '?')} frames · {parameters.get('steps', '?')} steps"
    )


def h3_cooldown_remaining(parameters: dict[str, Any]) -> int:
    fingerprint = h3_settings_fingerprint(parameters)
    with sqlite3.connect(DATABASE) as db:
        row = db.execute("SELECT blocked_until FROM h3_setting_cooldowns WHERE settings_fingerprint=?", (fingerprint,)).fetchone()
    if not row:
        return 0
    until = timestamp_value(row[0])
    if not until:
        return 0
    return max(0, math.ceil((until - datetime.now(timezone.utc)).total_seconds()))


def enforce_h3_cooldown(parameters: dict[str, Any]) -> None:
    remaining = h3_cooldown_remaining(parameters)
    if remaining:
        minutes = max(1, math.ceil(remaining / 60))
        raise HTTPException(429, f"This MiniMax H3 runtime setting ({h3_fingerprint_summary(parameters)}) is temporarily paused after repeated stalls. Try again in about {minutes} minutes or change model/sampling settings.")


def enqueue_task(task_kind: str, record_id: str, owner_id: int, profile_key: str) -> int:
    state = "paused" if performance_mode_enabled() else "waiting"
    with sqlite3.connect(DATABASE) as db:
        cursor = db.execute(
            "INSERT INTO task_queue(task_kind,record_id,owner_id,state,queued_at,profile_key) VALUES(?,?,?,?,?,?)",
            (task_kind, record_id, owner_id, state, utc_now(), profile_key),
        )
        return int(cursor.lastrowid)


def queue_duration_samples(profile_key: str, limit: int = 60) -> tuple[list[float], bool]:
    """Return recent comparable durations and whether they match the refined profile."""
    candidates = queue_profile_candidates(profile_key)
    with sqlite3.connect(DATABASE) as db:
        for index, candidate in enumerate(candidates):
            rows = db.execute(
                "SELECT (julianday(finished_at)-julianday(started_at))*86400 FROM task_queue "
                "WHERE profile_key=? AND state='completed' AND started_at IS NOT NULL AND finished_at IS NOT NULL "
                "ORDER BY finished_at DESC LIMIT ?",
                (candidate, limit),
            ).fetchall()
            samples = sorted(float(row[0]) for row in rows if row[0] and 1 <= float(row[0]) <= 7200)
            if len(samples) >= 3 or index == len(candidates) - 1:
                return samples, index == 0
    return [], True


def h3_history_estimate(profile_key: str, limit: int = 16) -> dict[str, Any]:
    """Estimate an H3 run from nearby, completed local runs only.

    Old and current runtime profiles intentionally share the same pool.  Their
    durations stay untouched; similarity only chooses and weights observations.
    """
    target = h3_profile_fields(profile_key)
    with closing(db_connect()) as db:
        rows = db.execute(
            "SELECT profile_key,(julianday(finished_at)-julianday(started_at))*86400 AS seconds,finished_at "
            "FROM task_queue WHERE state='completed' AND profile_key LIKE 'generation:minimax-h3:%' "
            "AND started_at IS NOT NULL AND finished_at IS NOT NULL ORDER BY finished_at DESC LIMIT 240"
        ).fetchall()
    candidates: list[tuple[float, float, dict[str, Any], str]] = []
    for profile, seconds, finished_at in rows:
        try:
            duration = float(seconds)
        except (TypeError, ValueError):
            continue
        if not 1 <= duration <= 7200:
            continue
        sample = h3_profile_fields(str(profile or ""))
        candidates.append((h3_similarity(target, sample), duration, sample, str(finished_at or "")))
    if not candidates:
        return {"available": False, "low": None, "high": None, "seconds": None, "samples": 0, "durations": [], "weighted_samples": []}
    same_mode = [entry for entry in candidates if entry[2]["mode"] == target["mode"]]
    selected = sorted(same_mode or candidates, key=lambda entry: (entry[0], entry[3]), reverse=True)[:limit]
    weighted = [(duration, max(0.1, score)) for score, duration, _, _ in selected]
    low = max(5, round(weighted_percentile(weighted, 0.25)))
    high = max(low + 5, round(weighted_percentile(weighted, 0.75)))
    return {
        "available": True, "low": low, "high": high,
        "seconds": round(weighted_percentile(weighted, 0.5)),
        "samples": len(selected), "durations": [duration for _, duration, _, _ in selected],
        "weighted_samples": weighted,
    }


def apply_h3_history_estimate(parameters: dict[str, Any]) -> dict[str, Any]:
    estimate = h3_history_estimate(queue_profile("generation", parameters, "minimax-h3"))
    parameters["estimated_seconds"] = estimate["seconds"]
    parameters["estimate_range"] = [estimate["low"], estimate["high"]] if estimate["available"] else []
    parameters["estimate_samples"] = estimate["samples"]
    parameters["estimate_source"] = "completed_h3_history" if estimate["available"] else "no_completed_h3_history"
    return estimate


def queue_estimate(profile_key: str) -> dict[str, Any]:
    if profile_key.startswith("generation:minimax-h3:"):
        estimate = h3_history_estimate(profile_key)
        if estimate["available"]:
            return {
                "low": estimate["low"], "high": estimate["high"], "confidence": "history",
                "samples": estimate["samples"], "durations": estimate["durations"],
            }
        return {"low": 0, "high": 0, "confidence": "none", "samples": 0, "durations": []}
    samples, exact_profile = queue_duration_samples(profile_key)
    if len(samples) >= 3:
        low = max(5, round(queue_percentile(samples, 0.25)))
        high = max(low + 5, round(queue_percentile(samples, 0.75)))
        confidence = "high" if exact_profile and len(samples) >= 8 else "medium"
        return {"low": low, "high": high, "confidence": confidence, "samples": len(samples), "durations": samples}
    low, high = queue_default_estimate(profile_key)
    return {"low": low, "high": high, "confidence": "low", "samples": len(samples), "durations": samples}


def queue_estimate_seconds(profile_key: str) -> tuple[int, int]:
    estimate = queue_estimate(profile_key)
    return int(estimate["low"]), int(estimate["high"])


def active_queue_rows() -> list[dict[str, Any]]:
    with closing(db_connect()) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT q.*,u.username,g.workflow_key AS group_workflow,g.parameters_json AS group_parameters "
            "FROM task_queue q JOIN users u ON u.user_id=q.owner_id "
            "LEFT JOIN generation_groups g ON q.task_kind='generation' AND g.group_id=q.record_id "
            "WHERE q.state IN ('waiting','dispatching','running','paused','cancelling','recovering') "
            "ORDER BY CASE WHEN q.state IN ('running','dispatching','cancelling','recovering') THEN 0 ELSE 1 END,q.priority DESC,q.queue_id"
        ).fetchall()
    return [dict(row) for row in rows]


def queue_kind_label(row: dict[str, Any]) -> str:
    if row["task_kind"] == "generation":
        workflow = str(row.get("group_workflow") or "")
        group = None
        if not workflow:
            group = unrestricted_group(row["record_id"])
            workflow = str(group.get("workflow_key") or "") if group else ""
        if workflow == "qwen2511-modular-flux2":
            return "Qwen 图片编辑"
        if workflow == "minimax-h3":
            return "MiniMax H3 视频生成"
        if workflow == "krea-identity-edit":
            try:
                parameters = json.loads(row.get("group_parameters") or (group.get("parameters_json") if group else "{}") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                parameters = {}
            return (
                "Krea2 双参考图"
                if parameters.get("reference_mode") in {"dual_reference", "face_swap"}
                else "Krea2 图生图"
            )
        return "图片生成"
    if row["task_kind"] == "preprocess":
        return "编辑前预放大"
    if row["task_kind"] == "prompt_tool":
        tool = unrestricted_prompt_tool(row["record_id"])
        return "图片反推" if tool and tool.get("operation") == "interrogate" else "提示词增强"
    upscale = unrestricted_upscale(row["record_id"])
    return "Flux 放大" if upscale and upscale.get("engine") == "flux2" else "SeedVR2 放大"


def queue_phase_label(
    row: dict[str, Any],
    event: dict[str, Any],
    item_current: int | None,
    item_total: int | None,
) -> str:
    """Describe the actual active graph stage without implying false precision."""
    if row["state"] == "cancelling":
        return "正在停止当前 ComfyUI 任务"
    if row["state"] == "dispatching":
        return "正在向 ComfyUI 提交工作流"

    phase = str(event.get("phase") or "loading")
    node_type = str(event.get("node_types", {}).get(str(event.get("node") or ""), ""))
    lowered = node_type.lower()
    item_suffix = f"第 {item_current}/{item_total} 张图" if item_current and item_total and item_total > 1 else "当前图像"

    group = unrestricted_group(row["record_id"]) if row["task_kind"] == "generation" else None
    workflow = str(group.get("workflow_key") or "") if group else ""
    parameters: dict[str, Any] = {}
    if group:
        try:
            parameters = json.loads(group.get("parameters_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            parameters = {}

    if phase == "loading":
        if workflow == "krea-identity-edit":
            model = str(parameters.get("krea_model") or "official_turbo")
            model_name = KREA_MODEL_LABELS.get(model, "Krea2")
            return f"正在加载 {model_name} 与 Identity LoRA"
        if workflow == "qwen2511-modular-flux2":
            return "正在加载 Qwen 图像编辑模型"
        if row["task_kind"] == "upscale":
            upscale = unrestricted_upscale(row["record_id"])
            return "正在加载 Flux 放大模型" if upscale and upscale.get("engine") == "flux2" else "正在加载 SeedVR2 放大模型"
        if row["task_kind"] == "preprocess":
            return "正在加载编辑前预放大模型"
        return str(event.get("label") or "正在初始化模型与工作流")

    if phase == "encoding":
        if workflow == "krea-identity-edit":
            mode = str(parameters.get("reference_mode") or "edit")
            if mode in {"dual_reference", "face_swap"}:
                return "正在编码图1场景、图2人物参考与提示词"
            if mode == "identity":
                return "正在编码身份参考图与提示词"
            return "正在编码原图与编辑提示词"
        if workflow == "qwen2511-modular-flux2":
            return "正在编码参考图与编辑提示词"
        return "正在编码提示词"

    if phase == "cleanup":
        return "正在释放 SeedVR2 显存"
    if phase == "sampling":
        prefix = "Flux 正在采样" if event.get("has_seedvr") else "正在采样"
        return f"{prefix}{item_suffix}"
    if phase == "seedvr2":
        return f"SeedVR2 正在放大{item_suffix}"
    if phase == "saving":
        return f"正在写入{item_suffix}的结果"
    if "decode" in lowered:
        return "正在进行 VAE 解码"
    if "colormatch" in lowered or "color" in lowered:
        return "正在匹配原图色彩"
    if "grain" in lowered:
        return "正在添加纹理颗粒"
    if "brightness" in lowered:
        return "正在调整图像亮度"
    if phase == "postprocess":
        return "正在进行图像后处理"
    return str(event.get("label") or "正在处理当前任务")


def queue_progress_info(row: dict[str, Any]) -> dict[str, Any]:
    if row["state"] == "cancelling":
        return {
            "value": max(0, min(round(float(row.get("progress_value") or 0)), 99)),
            "phase": "正在停止当前 ComfyUI 任务", "phase_key": "cancelling",
            "approximate": True,
            "item_current": None, "item_total": None,
            "item_progress": None, "item_approximate": True,
            "step_current": None, "step_total": None, "exact": False,
            "overall_current": None, "overall_total": None,
            "tile_current": None, "tile_total": None, "overall_exact": False,
        }
    if row["state"] not in {"running", "dispatching"}:
        return {
            "value": 0, "phase": "等待调度", "phase_key": "waiting", "approximate": True,
            "item_current": None, "item_total": None,
            "item_progress": None, "item_approximate": True,
            "step_current": None, "step_total": None, "exact": False,
            "overall_current": None, "overall_total": None,
            "tile_current": None, "tile_total": None, "overall_exact": False,
        }
    event = COMFY_PROGRESS.get(row.get("prompt_id") or "", {})
    item_progress = max(0.0, min(float(event.get("percent") or (0 if row["state"] == "dispatching" else 1)), 99.0))
    current = item_progress
    item_current: int | None = 1
    item_total: int | None = 1
    if row["task_kind"] == "generation":
        group = unrestricted_group(row["record_id"])
        if group:
            expected = generation_expected(group)
            completed = len([child for child in db_group_jobs(group["group_id"]) if child["status"] == "completed"])
            current = 100 * min(0.99, (completed + item_progress / 100) / expected)
            item_current = min(completed + 1, expected)
            item_total = expected
    raw_value = event.get("value")
    raw_maximum = event.get("max")
    exact = (
        isinstance(raw_value, (int, float)) and isinstance(raw_maximum, (int, float))
        and raw_maximum > 0
    )
    overall_current = event.get("overall_current")
    overall_total = event.get("overall_total")
    overall_exact = (
        bool(event.get("enhanced_upscale"))
        and isinstance(overall_current, (int, float))
        and isinstance(overall_total, (int, float)) and overall_total > 0
    )
    stored = max(0.0, min(float(row.get("progress_value") or 0), 99.0))
    current = max(current, stored)
    phase_key = str(event.get("phase") or ("dispatching" if row["state"] == "dispatching" else "loading"))
    phase = queue_phase_label(row, event, item_current, item_total)
    # Workflow-level percentages use weighted graph phases, even when sampler
    # step counters are exact. Keep the two accuracy concepts separate.
    approximate = True
    if current > stored or phase != row.get("progress_phase") or approximate != bool(row.get("progress_approximate", 1)):
        db_execute(
            "UPDATE task_queue SET progress_value=?,progress_phase=?,progress_approximate=? WHERE queue_id=?",
            (current, phase, int(approximate), row["queue_id"]),
        )
    return {
        "value": round(current), "phase": phase, "phase_key": phase_key, "approximate": approximate,
        "item_current": item_current, "item_total": item_total,
        "item_progress": round(item_progress), "item_approximate": True,
        "step_current": round(float(raw_value), 2) if exact else None,
        "step_total": round(float(raw_maximum), 2) if exact else None,
        "exact": exact,
        "overall_current": round(float(overall_current), 2) if overall_exact else None,
        "overall_total": round(float(overall_total), 2) if overall_exact else None,
        "tile_current": event.get("tile_current") if overall_exact else None,
        "tile_total": event.get("tile_total") if overall_exact else None,
        "overall_exact": overall_exact,
    }


def queue_progress(row: dict[str, Any]) -> int:
    return int(queue_progress_info(row)["value"])


def running_task_timeout_reason(row: dict[str, Any], now: datetime | None = None) -> str | None:
    """Return a user-facing timeout reason for a managed ComfyUI task."""
    if row.get("state") != "running" or not row.get("prompt_id"):
        return None
    started = timestamp_value(row.get("item_started_at")) or timestamp_value(row.get("started_at"))
    if not started:
        return None
    elapsed = max(0.0, ((now or datetime.now(timezone.utc)).astimezone(timezone.utc) - started).total_seconds())
    event = COMFY_PROGRESS.get(str(row["prompt_id"]), {})
    phase = str(event.get("phase") or "loading")
    percent = float(event.get("percent") or 1)
    if phase == "loading" and percent <= 8:
        last_event = event.get("updated_at")
        idle = time.monotonic() - float(last_event) if isinstance(last_event, (int, float)) else elapsed
        if elapsed >= MODEL_INITIALIZATION_TIMEOUT_SECONDS and idle >= MODEL_INITIALIZATION_TIMEOUT_SECONDS:
            return "模型初始化超过 10 分钟且没有进展，任务已自动停止"
    profile = str(row.get("profile_key") or "")
    if profile.startswith("prompt-tool:"):
        hard_limit = PROMPT_TOOL_TIMEOUT_SECONDS
    elif profile.startswith("generation:minimax-h3:"):
        hard_limit = (
            H3_ADMIN_HARD_TIMEOUT_SECONDS
            if ":tier=admin:" in profile
            else H3_HARD_TIMEOUT_SECONDS
        )
    elif profile.startswith("generation:krea-identity-edit:"):
        hard_limit = KREA_PROMPT_TIMEOUT_SECONDS
    else:
        hard_limit = TASK_HARD_TIMEOUT_SECONDS
    if elapsed >= hard_limit:
        return f"当前处理已运行超过 {hard_limit // 60} 分钟，达到服务器安全上限并自动停止"
    return None


def h3_first_sample_stalled(row: dict[str, Any], now: datetime | None = None) -> bool:
    """H3 never reached its local pre-sampler watchdog marker.

    Do not infer this from the browser-facing progress websocket: that stream
    can be replaced while ComfyUI is still denoising.  The H3 node writes the
    marker from inside ComfyUI, so its absence is specifically the unattended
    "never got to H3 preparation/sampling" case.
    """
    if row.get("state") != "running" or not str(row.get("profile_key") or "").startswith("generation:minimax-h3:"):
        return False
    started = timestamp_value(row.get("item_started_at")) or timestamp_value(row.get("started_at"))
    if not started:
        return False
    heartbeat = h3_sampler_heartbeat(str(row.get("prompt_id") or ""))
    return not heartbeat and (now or datetime.now(timezone.utc)).astimezone(timezone.utc) - started >= timedelta(seconds=H3_FIRST_SAMPLE_TIMEOUT_SECONDS)


def h3_sampler_heartbeat(prompt_id: str) -> dict[str, Any] | None:
    path = H3_HEARTBEAT_DIR / f"{prompt_id}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if str(data.get("prompt_id") or "") == prompt_id else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def h3_sampler_heartbeat_stalled(row: dict[str, Any]) -> bool:
    """A started sampler with no heartbeat is a real unattended-recovery signal."""
    if row.get("state") != "running" or not str(row.get("profile_key") or "").startswith("generation:minimax-h3:"):
        return False
    prompt_id = str(row.get("prompt_id") or "")
    heartbeat = h3_sampler_heartbeat(prompt_id)
    if not heartbeat or heartbeat.get("stage") not in {"model_patch_started", "sampler_ready", "sampling_started", "forward_started", "step_completed"}:
        return False
    try:
        updated = float(heartbeat["updated_at"])
    except (TypeError, ValueError, KeyError):
        return False
    # The final denoise step is followed by VAE encode/save work, which does not
    # refresh the sampler file. Let the ordinary task limit cover that phase so
    # a slow-but-healthy finalization cannot be mistaken for a sampler hang.
    if heartbeat.get("stage") == "step_completed":
        try:
            if int(heartbeat.get("step") or 0) >= int(heartbeat.get("total") or 0):
                return False
        except (TypeError, ValueError):
            return False
    return time.time() - updated >= H3_HEARTBEAT_STALL_SECONDS


def clear_h3_sampler_heartbeat(prompt_id: str) -> None:
    if prompt_id:
        (H3_HEARTBEAT_DIR / f"{prompt_id}.json").unlink(missing_ok=True)


def prune_h3_sampler_heartbeats() -> None:
    """Keep abandoned prompt markers from accumulating after process crashes."""
    try:
        H3_HEARTBEAT_DIR.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - 48 * 60 * 60
        for path in H3_HEARTBEAT_DIR.glob("*.json"):
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
    except OSError:
        pass


def queue_snapshot(viewer_id: int, admin: bool = False) -> dict[str, Any]:
    rows = active_queue_rows()
    cumulative_low = cumulative_high = 0
    cumulative_confidence = "high"
    tasks: list[dict[str, Any]] = []
    paused = performance_mode_enabled()
    for position, row in enumerate(rows, 1):
        estimate = queue_estimate(row.get("profile_key") or "")
        low, high = int(estimate["low"]), int(estimate["high"])
        is_running = row["state"] in {"running", "dispatching", "cancelling", "recovering"}
        progress = queue_progress_info(row)
        now = datetime.now(timezone.utc)
        started = timestamp_value(row.get("started_at"))
        item_started = timestamp_value(row.get("item_started_at")) or started
        queued = timestamp_value(row.get("queued_at"))
        elapsed = max(0, int((now - started).total_seconds())) if is_running and started else 0
        item_elapsed = max(0, int((now - item_started).total_seconds())) if is_running and item_started else 0
        wait_until = started or now
        queue_elapsed = max(0, int((wait_until - queued).total_seconds())) if queued else 0
        own = int(row["owner_id"]) == viewer_id
        eta_confidence = estimate["confidence"] if is_running else cumulative_confidence
        task = {
            "id": row["queue_id"], "record_id": row["record_id"] if own or admin else None, "kind": row["task_kind"],
            "label": queue_kind_label(row), "state": row["state"], "position": position,
            "queued_at": row["queued_at"], "own": own, "can_cancel": row["state"] != "cancelling" and (own or admin),
            "owner": row["username"] if admin else ("我的任务" if own else "其他用户"),
            "eta_low": max(0, low - elapsed) if is_running else cumulative_low,
            "eta_high": max(0, high - elapsed) if is_running else cumulative_high,
            "eta_confidence": eta_confidence,
            "duration_low": low, "duration_high": high,
            "elapsed_seconds": elapsed,
            "queue_elapsed_seconds": queue_elapsed,
            "item_elapsed_seconds": item_elapsed,
            "progress": progress["value"],
            "progress_phase": progress["phase"],
            "progress_phase_key": progress["phase_key"],
            "progress_approximate": progress["approximate"],
            "progress_exact": progress["exact"],
            "item_current": progress["item_current"], "item_total": progress["item_total"],
            "item_progress": progress["item_progress"],
            "item_progress_approximate": progress["item_approximate"],
            "step_current": progress["step_current"], "step_total": progress["step_total"],
            "overall_current": progress["overall_current"], "overall_total": progress["overall_total"],
            "tile_current": progress["tile_current"], "tile_total": progress["tile_total"],
            "overall_exact": progress["overall_exact"],
        }
        if row.get("group_workflow") == "minimax-h3":
            try:
                parameters = json.loads(row.get("group_parameters") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                parameters = {}
            vram = parameters.get("vram") if isinstance(parameters.get("vram"), dict) else {}
            task.update({
                "vram_risk": vram.get("risk", "unknown"),
                "vram_reason": vram.get("reason", ""),
                "runtime_profile_version": parameters.get("runtime_profile_version", H3_RUNTIME_PROFILE_VERSION),
            })
        tasks.append(task)
        contribution_low, contribution_high = queue_remaining_estimate(estimate, elapsed) if is_running else (low, high)
        cumulative_low += contribution_low
        cumulative_high += contribution_high
        confidence_order = {"none": -1, "low": 0, "medium": 1, "history": 2, "high": 2}
        if confidence_order.get(estimate["confidence"], -1) < confidence_order.get(cumulative_confidence, -1):
            cumulative_confidence = estimate["confidence"]
    recovery = comfy_recovery_status()
    return {
        "performance_mode": paused,
        "recovering": QUEUE_RECOVERING or recovery["recovery_state"] == "recovering",
        "recovery_state": recovery["recovery_state"],
        "recovery_message": recovery["recovery_message"],
        "comfy_available": COMFY_AVAILABLE,
        "tasks": tasks,
        "active_count": len(tasks),
    }


def collections_for_record(record_id: str) -> list[dict[str, Any]]:
    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT c.collection_id AS id, c.name FROM favorite_collections c "
            "JOIN collection_memberships m ON m.collection_id=c.collection_id "
            "WHERE m.record_id=?" + (" AND c.owner_id=?" if current_owner_id() else "") + " ORDER BY c.name COLLATE NOCASE",
            (record_id, current_owner_id()) if current_owner_id() else (record_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def all_collections() -> list[dict[str, Any]]:
    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT c.collection_id AS id, c.name, COUNT(m.record_id) AS count "
            "FROM favorite_collections c LEFT JOIN collection_memberships m ON m.collection_id=c.collection_id "
            + ("WHERE c.owner_id=? " if current_owner_id() else "")
            + "GROUP BY c.collection_id ORDER BY c.name COLLATE NOCASE",
            (current_owner_id(),) if current_owner_id() else (),
        ).fetchall()
    return [dict(row) for row in rows]


def collection_name(value: str) -> str:
    value = value.strip()
    if not 1 <= len(value) <= 40:
        raise HTTPException(400, "Collection name must contain 1 to 40 characters")
    return value


def template_name(value: Any) -> str:
    value = str(value or "").strip()
    if not 1 <= len(value) <= 40:
        raise HTTPException(400, "Template name must contain 1 to 40 characters")
    return value


def template_body(value: Any) -> str:
    value = str(value or "").strip()
    if not 1 <= len(value) <= 6000:
        raise HTTPException(400, "Template text must contain 1 to 6000 characters")
    return value


def template_scope(value: Any) -> str:
    value = str(value or "").strip()
    if value != PROMPT_TEMPLATE_SCOPE:
        raise HTTPException(400, "Invalid template scope")
    return value


def public_prompt_template(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    return {
        "id": item["template_id"], "name": item["name"], "text": item["body"],
        "scope": item["scope"], "created_at": item["created_at"], "updated_at": item["updated_at"],
        "system": bool(item.get("is_system", 0)),
    }


def all_prompt_templates(scope: str) -> list[dict[str, Any]]:
    scope = template_scope(scope)
    with closing(sqlite3.connect(DATABASE)) as db, db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT * FROM prompt_templates WHERE scope=?" + (" AND (is_system=1 OR owner_id=?)" if current_owner_id() else "") + " ORDER BY is_system DESC, sort_order, name COLLATE NOCASE, template_id",
            (scope, current_owner_id()) if current_owner_id() else (scope,),
        ).fetchall()
    return [public_prompt_template(row) for row in rows]


def prompt_template(template_id: int) -> dict[str, Any]:
    with closing(sqlite3.connect(DATABASE)) as db, db:
        db.row_factory = sqlite3.Row
        owner = current_owner_id()
        row = db.execute("SELECT * FROM prompt_templates WHERE template_id=?" + (" AND (is_system=1 OR owner_id=?)" if owner else ""), (template_id, owner) if owner else (template_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Template not found")
    return public_prompt_template(row)


def update_record_collections(record_id: str, collection_ids: list[int]) -> list[dict[str, Any]]:
    ids = sorted(set(collection_ids))
    if any(not isinstance(item, int) or item < 1 for item in ids):
        raise HTTPException(400, "Invalid collection")
    with sqlite3.connect(DATABASE) as db:
        owner = current_owner_id()
        existing = {row[0] for row in db.execute("SELECT collection_id FROM favorite_collections" + (" WHERE owner_id=?" if owner else ""), (owner,) if owner else ())}
        if not set(ids).issubset(existing):
            raise HTTPException(404, "Collection not found")
        db.execute("DELETE FROM collection_memberships WHERE record_id=?", (record_id,))
        db.executemany("INSERT INTO collection_memberships(collection_id, record_id) VALUES(?, ?)", [(item, record_id) for item in ids])
        db.execute("UPDATE generation_groups SET is_favorite=? WHERE group_id=?", (int(bool(ids)), record_id))
        db.execute("UPDATE jobs SET is_favorite=? WHERE prompt_id=?", (int(bool(ids)), record_id))
    return collections_for_record(record_id)


def remove_record_collections(record_id: str) -> None:
    db_execute("DELETE FROM collection_memberships WHERE record_id=?", (record_id,))


def remove_output_files(images: list[dict[str, str]]) -> None:
    remove_output_files_service(images, safe_output_path)


def prune_thumbnail_cache() -> None:
    prune_thumbnail_cache_service(
        THUMBNAIL_DIR,
        THUMBNAIL_CACHE_LIMIT,
        THUMBNAIL_CACHE_TARGET,
    )


def thumbnail_for(path: Path, size: int) -> Path:
    return thumbnail_for_service(
        path,
        size,
        thumbnail_dir=THUMBNAIL_DIR,
        allowed_sizes=THUMBNAIL_SIZES,
        cache_limit=THUMBNAIL_CACHE_LIMIT,
        cache_target=THUMBNAIL_CACHE_TARGET,
    )


def video_thumbnail_for(path: Path, size: int) -> Path:
    return video_thumbnail_for_service(
        path,
        size,
        thumbnail_dir=THUMBNAIL_DIR,
        allowed_sizes=THUMBNAIL_SIZES,
        cache_limit=THUMBNAIL_CACHE_LIMIT,
        cache_target=THUMBNAIL_CACHE_TARGET,
        ffmpeg_path=FFMPEG_PATH,
        media_process_flags=_media_process_flags,
    )


async def private_image_response(path: Path, preview: int | None = None) -> FileResponse:
    return await private_image_response_service(
        path,
        preview,
        create_thumbnail=thumbnail_for,
        media_max_age=PRIVATE_MEDIA_MAX_AGE,
        preview_max_age=PRIVATE_PREVIEW_MAX_AGE,
        preview_stale_while_revalidate=PRIVATE_PREVIEW_STALE_WHILE_REVALIDATE,
    )


def remove_upscale_records(group_id: str, item_id_value: str | None = None, source_kind: str | None = None, engine: str | None = None) -> None:
    query = "SELECT * FROM upscale_jobs WHERE parent_group_id=?"
    values: tuple[Any, ...] = (group_id,)
    if item_id_value is not None:
        query += " AND parent_item_id=?"
        values = (group_id, item_id_value)
    if source_kind is not None:
        query += " AND source_kind=?"
        values = (*values, source_kind)
    if engine is not None:
        query += " AND engine=?"
        values = (*values, engine)
    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(row) for row in db.execute(query, values).fetchall()]
    for row in rows:
        try:
            output = json.loads(row.get("output_json") or "{}")
            source = json.loads(row.get("source_image_json") or "{}")
        except json.JSONDecodeError:
            output, source = {}, {}
        if isinstance(output, dict) and output.get("filename"):
            remove_output_files([output])
        source_path = safe_source_path(source) if isinstance(source, dict) else None
        if source_path and source_path.is_file():
            source_path.unlink()
    delete_query = "DELETE FROM upscale_jobs WHERE parent_group_id=?"
    delete_values: tuple[Any, ...] = (group_id,)
    if item_id_value is not None:
        delete_query += " AND parent_item_id=?"
        delete_values = (group_id, item_id_value)
    if source_kind is not None:
        delete_query += " AND source_kind=?"
        delete_values = (*delete_values, source_kind)
    if engine is not None:
        delete_query += " AND engine=?"
        delete_values = (*delete_values, engine)
    db_execute(delete_query, delete_values)


def remove_detail_records(group_id: str, item_id_value: str | None = None) -> None:
    query = "SELECT * FROM detail_jobs WHERE parent_group_id=?"
    values: tuple[Any, ...] = (group_id,)
    if item_id_value is not None:
        query += " AND parent_item_id=?"
        values = (group_id, item_id_value)
    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(row) for row in db.execute(query, values).fetchall()]
    for row in rows:
        try:
            output = json.loads(row.get("output_json") or "{}")
            source = json.loads(row.get("source_image_json") or "{}")
        except json.JSONDecodeError:
            output, source = {}, {}
        if isinstance(output, dict) and output.get("filename"):
            remove_output_files([output])
        source_path = safe_source_path(source) if isinstance(source, dict) else None
        if source_path and source_path.is_file():
            source_path.unlink()
    if item_id_value is None:
        db_execute("DELETE FROM detail_jobs WHERE parent_group_id=?", (group_id,))
    else:
        db_execute("DELETE FROM detail_jobs WHERE parent_group_id=? AND parent_item_id=?", (group_id, item_id_value))


def item_id(prompt_id: str, final: dict[str, str], stage1: dict[str, str] | None = None) -> str:
    payload = "|".join((prompt_id, *image_key(final), *(image_key(stage1) if stage1 else ("", "", ""))))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def items_from_children(workflow_key: str, children: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for child in children:
        outputs = child["images"] if "images" in child else output_sets(child["outputs_json"])
        finals = outputs.get("final", [])
        stages = outputs.get("stage1", [])
        for index, final in enumerate(finals):
            stage1 = stages[index] if index < len(stages) else None
            items.append({"id": item_id(child.get("id") or child.get("prompt_id", ""), final, stage1), "prompt_id": child.get("id") or child.get("prompt_id", ""), "final": final, "stage1": stage1})
    return items


def stored_items(
    row: dict[str, Any],
    children: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    try:
        items = json.loads(row.get("items_json") or "[]")
    except json.JSONDecodeError:
        items = []
    if isinstance(items, list) and items:
        return items
    if row.get("group_id") and not row.get("prompt_id"):
        return items_from_children(
            row["workflow_key"],
            children if children is not None else db_group_jobs(row["group_id"]),
        )
    return items_from_children(row["workflow_key"], [row])


def recycle_record_id(row: dict[str, Any]) -> str:
    return str(row.get("group_id") or row.get("prompt_id") or "")


def recycle_record_kind(row: dict[str, Any]) -> str:
    return "group" if row.get("group_id") and not row.get("prompt_id") else "job"


def recycle_item_ids(row: dict[str, Any]) -> set[str]:
    owner_id = row.get("owner_id")
    record_id = recycle_record_id(row)
    if owner_id is None or not record_id:
        return set()
    try:
        with sqlite3.connect(DATABASE) as db:
            return {
                str(item_id) for (item_id,) in db.execute(
                    "SELECT item_id FROM recycle_bin WHERE owner_id=? AND kind='item' AND record_id=?",
                    (int(owner_id), record_id),
                ).fetchall()
            }
    except sqlite3.OperationalError:
        return set()


def visible_stored_items(
    row: dict[str, Any],
    hidden: set[str] | None = None,
    children: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    hidden = recycle_item_ids(row) if hidden is None else hidden
    return [
        item for item in stored_items(row, children)
        if str(item.get("id") or "") not in hidden
    ]


def recycle_task_entry(row: dict[str, Any]) -> dict[str, Any] | None:
    owner_id = row.get("owner_id")
    record_id = recycle_record_id(row)
    if owner_id is None or not record_id:
        return None
    try:
        with sqlite3.connect(DATABASE) as db:
            db.row_factory = sqlite3.Row
            entry = db.execute(
                "SELECT * FROM recycle_bin WHERE owner_id=? AND kind=? AND record_id=? AND item_id=''",
                (int(owner_id), recycle_record_kind(row), record_id),
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    return dict(entry) if entry else None


def outputs_from_items(items: list[dict[str, Any]]) -> dict[str, list[dict[str, str]]]:
    return {
        "final": [item["final"] for item in items if item.get("final")],
        "stage1": [item["stage1"] for item in items if item.get("stage1")],
    }


def source_image_from(row: dict[str, Any]) -> dict[str, str] | None:
    try:
        value = json.loads(row.get("source_image_json") or "{}")
    except json.JSONDecodeError:
        value = {}
    return value if isinstance(value, dict) and value.get("filename") else None


def json_image_from(row: dict[str, Any], column: str) -> dict[str, str] | None:
    try:
        value = json.loads(row.get(column) or "{}")
    except json.JSONDecodeError:
        value = {}
    return value if isinstance(value, dict) and value.get("filename") else None


def group_preprocess(row: dict[str, Any]) -> dict[str, Any] | None:
    try:
        value = json.loads(row.get("preprocess_json") or "{}")
    except json.JSONDecodeError:
        value = {}
    return value if isinstance(value, dict) and value.get("output") else None


DELETED_SEED_INDICES_KEY = "_deleted_seed_indices"
SEED_LIST_FIELDS = ("stage1_seeds", "generation_seeds")


def compact_deleted_seed_indices(parameters: dict[str, Any]) -> bool:
    raw_indices = parameters.pop(DELETED_SEED_INDICES_KEY, [])
    if not isinstance(raw_indices, list):
        return False
    deleted_indices = {
        int(index) for index in raw_indices
        if isinstance(index, int) or (isinstance(index, str) and index.isdigit())
    }
    if not deleted_indices:
        return bool(raw_indices)
    for field in SEED_LIST_FIELDS:
        seeds = parameters.get(field)
        if isinstance(seeds, list):
            parameters[field] = [seed for index, seed in enumerate(seeds) if index not in deleted_indices]
    return True


def compact_group_deleted_seeds(group_id: str) -> None:
    group = unrestricted_group(group_id)
    if not group:
        return
    parameters = json.loads(group.get("parameters_json") or "{}")
    if compact_deleted_seed_indices(parameters):
        db_execute(
            "UPDATE generation_groups SET parameters_json=? WHERE group_id=?",
            (json.dumps(parameters), group_id),
        )


def remove_group_item_seed(
    group: dict[str, Any],
    child: dict[str, Any],
    item_index: int,
    item_count: int,
    defer_compaction: bool,
) -> None:
    parameters = json.loads(group.get("parameters_json") or "{}")
    field = next((name for name in SEED_LIST_FIELDS if isinstance(parameters.get(name), list)), None)
    if not field:
        return
    logical_index = int(child.get("logical_index") or 0)
    if defer_compaction:
        raw_indices = parameters.get(DELETED_SEED_INDICES_KEY, [])
        deleted_indices = {
            int(index) for index in raw_indices
            if isinstance(index, int) or (isinstance(index, str) and index.isdigit())
        } if isinstance(raw_indices, list) else set()
        deleted_indices.add(logical_index)
        parameters[DELETED_SEED_INDICES_KEY] = sorted(deleted_indices)
    else:
        compact_deleted_seed_indices(parameters)
        seeds = parameters[field]
        child_parameters = json.loads(child.get("parameters_json") or "{}")
        actual_field = "stage1_seed" if field == "stage1_seeds" else "seed"
        actual_seed = child_parameters.get(actual_field)
        seed_index: int | None = None
        if len(seeds) == item_count and 0 <= item_index < len(seeds):
            seed_index = item_index
        elif actual_seed is not None:
            matching = [index for index, seed in enumerate(seeds) if str(seed) == str(actual_seed)]
            if logical_index in matching:
                seed_index = logical_index
            elif matching:
                seed_index = matching[0]
        if seed_index is None and 0 <= logical_index < len(seeds):
            seed_index = logical_index
        if seed_index is None and 0 <= item_index < len(seeds):
            seed_index = item_index
        if seed_index is None:
            return
        seeds.pop(seed_index)
    db_execute(
        "UPDATE generation_groups SET parameters_json=? WHERE group_id=?",
        (json.dumps(parameters), group["group_id"]),
    )


def align_group_seeds_to_items(
    parameters: dict[str, Any],
    items: list[dict[str, Any]],
    children: list[dict[str, Any]],
) -> bool:
    """Keep per-result seed lists aligned with the results that still exist."""
    field = next((name for name in SEED_LIST_FIELDS if isinstance(parameters.get(name), list)), None)
    if not field:
        return False
    seeds = list(parameters[field])
    actual_field = "stage1_seed" if field == "stage1_seeds" else "seed"
    children_by_id = {str(child.get("prompt_id") or ""): child for child in children}
    aligned: list[Any] = []
    for item_index, item in enumerate(items):
        child = children_by_id.get(str(item.get("prompt_id") or ""))
        if not child:
            return False
        try:
            child_parameters = json.loads(child.get("parameters_json") or "{}")
        except json.JSONDecodeError:
            child_parameters = {}
        value = child_parameters.get(actual_field)
        if value is None:
            try:
                logical_index = int(child.get("logical_index") or 0)
            except (TypeError, ValueError):
                logical_index = -1
            if 0 <= logical_index < len(seeds):
                value = seeds[logical_index]
            elif len(seeds) == len(items) and item_index < len(seeds):
                value = seeds[item_index]
            else:
                return False
        aligned.append(value)
    if aligned == seeds:
        return False
    parameters[field] = aligned
    parameters.pop(DELETED_SEED_INDICES_KEY, None)
    return True


def synchronize_terminal_group_seeds(group_id: str) -> bool:
    group = unrestricted_group(group_id)
    if not group or group.get("status") not in {"completed", "failed", "cancelled"}:
        return False
    parameters = json.loads(group.get("parameters_json") or "{}")
    compact_deleted_seed_indices(parameters)
    if not align_group_seeds_to_items(parameters, stored_items(group), db_group_jobs(group_id)):
        return False
    db_execute(
        "UPDATE generation_groups SET parameters_json=? WHERE group_id=?",
        (json.dumps(parameters), group_id),
    )
    return True


def repair_terminal_group_seed_lists() -> int:
    with sqlite3.connect(DATABASE) as db:
        group_ids = [
            str(row[0]) for row in db.execute(
                "SELECT group_id FROM generation_groups "
                "WHERE storage_scope='mobile' AND status IN ('completed','failed','cancelled')"
            ).fetchall()
        ]
    return sum(synchronize_terminal_group_seeds(group_id) for group_id in group_ids)


def progress_for_children(
    children: list[dict[str, Any]],
    expected_total: int | None = None,
) -> dict[str, Any] | None:
    total = max(len(children), int(expected_total or 0))
    if not children:
        if total:
            return {
                "value": 0,
                "indeterminate": True,
                "label": f"正在队列中等待 ComfyUI · 共 {total} 张",
            }
        return None
    if all(child["status"] in {"completed", "failed"} for child in children):
        if len(children) >= total:
            return None
        completed = sum(child["status"] == "completed" for child in children)
        return {
            "value": round(100 * completed / total),
            "indeterminate": True,
            "label": f"已完成 {completed}/{total} 张 · 准备下一张",
        }
    completed = sum(child["status"] == "completed" for child in children)
    active = next((child for child in children if child["status"] == "running"), None)
    active_number = min(
        total,
        max(completed + 1, int(active.get("logical_index") or 0) + 1),
    ) if active else min(completed + 1, total)
    event = COMFY_PROGRESS.get(active["prompt_id"], {}) if active else {}
    if active and isinstance(event.get("percent"), (int, float)):
        current = max(0, min(float(event["percent"]) / 100, .99))
        percent = round(100 * (completed + current) / total)
        return {
            "value": percent,
            "indeterminate": bool(event.get("approximate")),
            "label": f"正在处理第 {active_number}/{total} 张 · {event.get('label') or '正在运行'}",
        }
    if active:
        return {"value": round(100 * completed / total), "indeterminate": True, "label": f"正在生成第 {active_number}/{total} 张"}
    return {"value": round(100 * completed / total), "indeterminate": True, "label": "正在队列中等待 ComfyUI"}


def enhanced_upscale_progress(row: dict[str, Any]) -> dict[str, Any] | None:
    status = str(row.get("status") or "")
    if status not in {"queued", "running"}:
        return None
    progress = progress_for_children([{
        "prompt_id": row.get("prompt_id") or row["upscale_id"], "status": status,
    }]) or {"value": 0, "indeterminate": True, "label": "正在队列中等待 ComfyUI"}
    event = COMFY_PROGRESS.get(str(row.get("prompt_id") or ""), {})
    stage_keys = [key for key, _ in ENHANCED_UPSCALE_STAGES]
    if status == "running":
        current_index = max(0, min(int(event.get("stage_index") or 0), len(stage_keys) - 1))
        current_key = stage_keys[current_index]
        raw_value, raw_maximum = event.get("value"), event.get("max")
        event_stage = (event.get("node_stages") or {}).get(str(event.get("node") or ""))
        exact = (
            isinstance(raw_value, (int, float)) and isinstance(raw_maximum, (int, float))
            and raw_maximum > 0 and event_stage == current_key
        )
        tile_current = event.get("tile_current")
        tile_total = event.get("tile_total")
        tile_exact = (
            isinstance(tile_current, int) and isinstance(tile_total, int)
            and 0 < tile_current <= tile_total
        )
        label = str(
            (event.get("label") if event_stage == current_key else None)
            or ENHANCED_UPSCALE_STAGES[current_index][1]
        )
        if tile_exact and event_stage in {"seedvr2", "flux_loading", "sampling", "postprocess"}:
            label = f"{label} · 第 {tile_current}/{tile_total} 块"
        progress.update({
            "value": min(99, round(float(event.get("percent") or progress.get("value") or 0))),
            "indeterminate": bool(event.get("approximate", True)),
            "label": label,
            "stage_key": current_key,
            "stage_index": current_index + 1,
            "stage_total": len(stage_keys),
            "overall_current": round(float(event["overall_current"]), 2) if isinstance(event.get("overall_current"), (int, float)) else None,
            "overall_total": round(float(event["overall_total"]), 2) if isinstance(event.get("overall_total"), (int, float)) else None,
            "tile_current": tile_current if tile_exact else None,
            "tile_total": tile_total if tile_exact else None,
            "step_current": round(float(raw_value), 2) if exact else None,
            "step_total": round(float(raw_maximum), 2) if exact else None,
            "step_exact": exact,
        })
    else:
        progress.update({
            "value": 0, "indeterminate": True, "stage_key": "waiting",
            "stage_index": 0, "stage_total": len(stage_keys),
            "overall_current": None, "overall_total": None,
            "tile_current": None, "tile_total": None,
            "step_current": None, "step_total": None, "step_exact": False,
        })
    current = int(progress["stage_index"])
    progress["stages"] = [
        {
            "key": key, "label": label,
            "state": "completed" if current > index + 1 else "active" if current == index + 1 else "pending",
        }
        for index, (key, label) in enumerate(ENHANCED_UPSCALE_STAGES)
    ]
    return progress


PUBLIC_SEED_FIELDS = ("seed", "stage1_seed", "stage2_seed")


def stringify_public_seed_values(parameters: dict[str, Any]) -> dict[str, Any]:
    """Keep 64-bit seed digits intact when parameters cross into JavaScript."""
    for field in PUBLIC_SEED_FIELDS:
        value = parameters.get(field)
        if value is not None and not isinstance(value, bool):
            parameters[field] = str(value)
    for field in SEED_LIST_FIELDS:
        values = parameters.get(field)
        if isinstance(values, list):
            parameters[field] = [str(value) for value in values]
    return parameters


def public_parameters(row: dict[str, Any]) -> dict[str, Any]:
    parameters = json.loads(row.get("parameters_json") or "{}")
    compact_deleted_seed_indices(parameters)
    if row.get("workflow_key") == "krea-identity-edit":
        parameters.setdefault("krea_model", "official_turbo")
        parameters.setdefault("identity_lora_strength", 1.0)
        parameters.setdefault("fit_mode", "fit")
        parameters.setdefault("output_megapixels", 1.0)
    elif row.get("workflow_key") == "minimax-h3":
        parameters.setdefault("h3_model", "original_int8")
    return stringify_public_seed_values(parameters)


@dataclass
class GalleryHydration:
    collections: dict[str, list[dict[str, Any]]]
    hidden_items: dict[str, set[str]]
    children: dict[str, list[dict[str, Any]]]
    upscales: dict[str, dict[str, list[dict[str, Any]]]]
    details: dict[str, dict[str, list[dict[str, Any]]]]


def load_gallery_hydration(
    rows: list[tuple[str, dict[str, Any]]],
    hidden_items: dict[str, set[str]] | None = None,
) -> GalleryHydration:
    record_ids = [recycle_record_id(row) for _, row in rows]
    group_ids = [row["group_id"] for kind, row in rows if kind == "group"]
    collections: dict[str, list[dict[str, Any]]] = {record_id: [] for record_id in record_ids}
    children: dict[str, list[dict[str, Any]]] = {group_id: [] for group_id in group_ids}
    upscales: dict[str, dict[str, list[dict[str, Any]]]] = {group_id: {} for group_id in group_ids}
    details: dict[str, dict[str, list[dict[str, Any]]]] = {group_id: {} for group_id in group_ids}
    if not record_ids:
        return GalleryHydration(collections, hidden_items or {}, children, upscales, details)

    with closing(db_connect()) as db:
        db.row_factory = sqlite3.Row
        placeholders = ",".join("?" for _ in record_ids)
        collection_query = (
            "SELECT m.record_id,c.collection_id AS id,c.name "
            "FROM collection_memberships m JOIN favorite_collections c ON c.collection_id=m.collection_id "
            f"WHERE m.record_id IN ({placeholders})"
        )
        values: list[Any] = list(record_ids)
        owner = current_owner_id()
        if owner:
            collection_query += " AND c.owner_id=?"
            values.append(owner)
        collection_query += " ORDER BY c.name COLLATE NOCASE"
        for entry in db.execute(collection_query, tuple(values)):
            collections.setdefault(str(entry["record_id"]), []).append({
                "id": int(entry["id"]), "name": entry["name"],
            })

        if group_ids:
            group_placeholders = ",".join("?" for _ in group_ids)
            for entry in db.execute(
                f"SELECT * FROM jobs WHERE group_id IN ({group_placeholders}) "
                "AND COALESCE(superseded,0)=0 ORDER BY group_id,logical_index,submitted_at",
                tuple(group_ids),
            ):
                children.setdefault(str(entry["group_id"]), []).append(dict(entry))
            for entry in db.execute(
                f"SELECT * FROM upscale_jobs WHERE parent_group_id IN ({group_placeholders}) "
                "ORDER BY parent_group_id,parent_item_id,submitted_at DESC",
                tuple(group_ids),
            ):
                group_id = str(entry["parent_group_id"])
                item_id_value = str(entry["parent_item_id"])
                upscales.setdefault(group_id, {}).setdefault(item_id_value, []).append(dict(entry))
            for entry in db.execute(
                f"SELECT * FROM detail_jobs WHERE parent_group_id IN ({group_placeholders}) "
                "ORDER BY parent_group_id,parent_item_id,submitted_at DESC",
                tuple(group_ids),
            ):
                group_id = str(entry["parent_group_id"])
                item_id_value = str(entry["parent_item_id"])
                details.setdefault(group_id, {}).setdefault(item_id_value, []).append(dict(entry))
    return GalleryHydration(collections, hidden_items or {}, children, upscales, details)


def public_job(
    row: dict[str, Any],
    include_trashed_items: bool = False,
    only_item_id: str | None = None,
    hydration: GalleryHydration | None = None,
) -> dict[str, Any]:
    record_id = str(row["prompt_id"])
    collections = hydration.collections.get(record_id, []) if hydration else collections_for_record(record_id)
    hidden = hydration.hidden_items.get(record_id, set()) if hydration else None
    items = stored_items(row) if include_trashed_items else visible_stored_items(row, hidden)
    if only_item_id is not None:
        items = [item for item in items if str(item.get("id") or "") == only_item_id]
    return {
        "id": row["prompt_id"], "workflow": row["workflow_key"], "prompt": row["prompt_text"],
        "source_prompt": row.get("source_prompt_text") or "", "title": row.get("title") or "",
        "status": row["status"], "submitted_at": row["submitted_at"], "completed_at": row["completed_at"],
        "error": row["error_message"], "images": outputs_from_items(items),
        "parameters": public_parameters(row),
        "items": items,
        "progress": progress_for_children([row]),
        "favorite": bool(collections), "collections": collections,
    }


def public_group(
    row: dict[str, Any],
    include_trashed_items: bool = False,
    only_item_id: str | None = None,
    hydration: GalleryHydration | None = None,
) -> dict[str, Any]:
    group_id = str(row["group_id"])
    collections = hydration.collections.get(group_id, []) if hydration else collections_for_record(group_id)
    children = hydration.children.get(group_id, []) if hydration else None
    hidden = hydration.hidden_items.get(group_id, set()) if hydration else None
    items = stored_items(row, children) if include_trashed_items else visible_stored_items(row, hidden, children)
    if only_item_id is not None:
        items = [item for item in items if str(item.get("id") or "") == only_item_id]
    if hydration:
        upscales = hydration.upscales.get(group_id, {})
        details = hydration.details.get(group_id, {})
    else:
        children, upscales, details = db_group_relations(group_id)
    source_image = source_image_from(row)
    source_path = safe_source_path(source_image) if source_image else None
    if not source_path or not source_path.is_file():
        source_image = None
    reference_image = json_image_from(row, "reference_image_json")
    reference_path = safe_source_path(reference_image) if reference_image else None
    if not reference_path or not reference_path.is_file():
        reference_image = None
    edit_mask = json_image_from(row, "edit_mask_json")
    edit_mask_path = safe_source_path(edit_mask) if edit_mask else None
    if not edit_mask_path or not edit_mask_path.is_file():
        edit_mask = None
    for item in items:
        item_key = str(item["id"])
        item["upscales"] = [public_upscale(entry) for entry in upscales.get(item_key, [])]
        item["details"] = [public_detail(entry) for entry in details.get(item_key, [])]
    parameters = public_parameters(row)
    if row.get("status") in {"completed", "failed", "cancelled"} and align_group_seeds_to_items(parameters, items, children):
        for field in SEED_LIST_FIELDS:
            if isinstance(parameters.get(field), list):
                parameters[field] = [str(seed) for seed in parameters[field]]
    rerunnable = True
    try:
        origin = json.loads(row.get("origin_json") or "{}")
    except json.JSONDecodeError:
        origin = {}
    expected_total: int | None = None
    if row.get("status") in {"queued", "running", "cancelling"}:
        try:
            configured_total = int(parameters.get("count") or parameters.get("batch") or 0)
        except (TypeError, ValueError):
            configured_total = 0
        generation_seeds = parameters.get("generation_seeds")
        stage1_seeds = parameters.get("stage1_seeds")
        seed_total = max(
            len(generation_seeds) if isinstance(generation_seeds, list) else 0,
            len(stage1_seeds) if isinstance(stage1_seeds, list) else 0,
        )
        expected_total = max(len(children), configured_total, seed_total, 1)
    return {
        "id": row["group_id"], "workflow": row["workflow_key"], "prompt": row["prompt_text"],
        "source_prompt": row.get("source_prompt_text") or "", "title": row.get("title") or "",
        "status": row["status"], "submitted_at": row["submitted_at"], "completed_at": row["completed_at"],
        "error": row["error_message"], "images": outputs_from_items(items),
        "parameters": parameters, "rerunnable": rerunnable,
        "items": items,
        "progress": progress_for_children(children, expected_total),
        "favorite": bool(collections), "collections": collections,
        "source_image": source_image,
        "reference_image": reference_image,
        "edit_mask": edit_mask,
        "preprocess": group_preprocess(row),
        "origin": origin if isinstance(origin, dict) else {},
    }


def public_upscale(row: dict[str, Any]) -> dict[str, Any]:
    try:
        output = json.loads(row.get("output_json") or "{}")
    except json.JSONDecodeError:
        output = {}
    return {
        "id": row["upscale_id"], "prompt_id": row.get("prompt_id"),
        "status": row["status"], "submitted_at": row["submitted_at"],
        "completed_at": row.get("completed_at"), "error": row.get("error_message"),
        "output": output if isinstance(output, dict) and output.get("filename") else None,
        "engine": row.get("engine") or "seedvr2",
        "source_kind": row.get("source_kind") or "edit",
        "parameters": stringify_public_seed_values(json.loads(row.get("parameters_json") or "{}")),
        "progress": (
            enhanced_upscale_progress(row)
            if (row.get("engine") or "seedvr2") == "flux2"
            else progress_for_children([{
                "prompt_id": row.get("prompt_id") or row["upscale_id"], "status": row["status"],
            }])
        ),
    }


def public_detail(row: dict[str, Any]) -> dict[str, Any]:
    try:
        output = json.loads(row.get("output_json") or "{}")
    except json.JSONDecodeError:
        output = {}
    return {
        "id": row["detail_id"], "prompt_id": row.get("prompt_id"),
        "status": row["status"], "submitted_at": row["submitted_at"],
        "completed_at": row.get("completed_at"), "error": row.get("error_message"),
        "output": output if isinstance(output, dict) and output.get("filename") else None,
        "parameters": stringify_public_seed_values(json.loads(row.get("parameters_json") or "{}")),
        "progress": progress_for_children([{
            "prompt_id": row.get("prompt_id") or row["detail_id"], "status": row["status"],
        }]),
    }


def public_preprocess(row: dict[str, Any]) -> dict[str, Any]:
    try:
        output = json.loads(row.get("output_json") or "{}")
    except json.JSONDecodeError:
        output = {}
    return {
        "id": row["preprocess_id"], "prompt_id": row.get("prompt_id"),
        "status": row["status"], "submitted_at": row["submitted_at"],
        "completed_at": row.get("completed_at"), "error": row.get("error_message"),
        "output": output if isinstance(output, dict) and output.get("filename") else None,
        "parameters": stringify_public_seed_values(json.loads(row.get("parameters_json") or "{}")),
        "progress": progress_for_children([{
            "prompt_id": row.get("prompt_id") or row["preprocess_id"], "status": row["status"],
        }]),
    }


LORA_CLASSIFIER = LoraClassifier(KREA_IDENTITY_EDIT_LORA, H3_TURBO_LORAS)


def classify_lora(path: Path) -> LoraClassification:
    return LORA_CLASSIFIER.classify(path)


def is_qwen_image_lora(path: Path) -> bool:
    return classify_lora(path).family == "qwen2511"


def is_krea2_lora(path: Path) -> bool:
    return classify_lora(path).family == "krea2"


def lora_options(key: str) -> list[str]:
    root = COMFY_ROOT / "models" / "loras"
    paths = sorted(root.rglob("*.safetensors"))
    spec = get_spec(key)
    classified = [(path, classify_lora(path)) for path in paths]
    if spec.lora_family == "minimax_h3":
        return [
            path.relative_to(root).as_posix() for path, item in classified
            if item.family == "minimax_h3" and not item.reserved
        ]
    if spec.lora_family == "qwen2511":
        return [
            path.relative_to(root).as_posix() for path, item in classified
            if item.family == "qwen2511" and not item.reserved
        ]
    files = [path.relative_to(root).as_posix() for path in paths]
    if spec.lora_family == "qwen_any":
        return [name for name in files if not any(token in name.lower() for token in ("krea", "knpv", "mystic"))]
    return [
        path.relative_to(root).as_posix() for path, item in classified
        if item.family == "krea2" and not item.reserved
    ]


def lora_scan_report() -> dict[str, Any]:
    """Return an explainable inventory so newly unsupported exports are visible."""
    root = COMFY_ROOT / "models" / "loras"
    rows = []
    family_counts: dict[str, int] = {}
    reserved = []
    unclassified = []
    if root.is_dir():
        for path in sorted(root.rglob("*.safetensors")):
            item = classify_lora(path)
            name = path.relative_to(root).as_posix()
            rows.append((name, item))
            if item.reserved:
                reserved.append({"name": name, "family": item.family, "evidence": item.evidence})
            elif item.family:
                family_counts[item.family] = family_counts.get(item.family, 0) + 1
            else:
                unclassified.append({"name": name, "evidence": item.evidence})
    return {
        "total": len(rows),
        "selectable": sum(family_counts.values()),
        "families": family_counts,
        "reserved_count": len(reserved),
        "reserved": reserved,
        "unclassified_count": len(unclassified),
        "unclassified": unclassified,
    }


def comfy_relative_name(name: str) -> str:
    """Convert an app-canonical model name to ComfyUI's local path spelling."""
    # API/UI/history values deliberately stay portable (forward slashes), while
    # ComfyUI validates combo values against os-native names from folder_paths.
    return str(Path(*name.replace("\\", "/").split("/")))


def lora_family_name(spec: Any) -> str:
    return "krea2" if spec.lora_family == "krea" else spec.lora_family


def lora_file_fingerprint(path: Path, size: int | None = None) -> str:
    file_size = path.stat().st_size if size is None else int(size)
    sample_size = 256 * 1024
    offsets = sorted({0, max(0, file_size // 2 - sample_size // 2), max(0, file_size - sample_size)})
    digest = hashlib.sha256(f"lora-v1:{file_size}:".encode())
    with path.open("rb") as source:
        for offset in offsets:
            source.seek(offset)
            digest.update(offset.to_bytes(8, "little", signed=False))
            digest.update(source.read(sample_size))
    return digest.hexdigest()


def lora_file_identity(path: Path) -> str:
    stat = path.stat()
    return f"{int(stat.st_dev)}:{int(stat.st_ino)}"


def lora_catalog_entries() -> list[tuple[str, str, Path]]:
    root = COMFY_ROOT / "models" / "loras"
    entries: dict[tuple[str, str], Path] = {}
    if not root.is_dir():
        return []
    for spec in enabled_workflows():
        family = lora_family_name(spec)
        for name in lora_options(spec.key):
            canonical = str(name).replace("\\", "/")
            path = root.joinpath(*canonical.split("/"))
            if path.is_file():
                entries[(family, canonical)] = path
    return [(family, name, path) for (family, name), path in sorted(entries.items())]


def replace_lora_json_references(value: Any, old_name: str, new_name: str, in_lora_list: bool = False) -> bool:
    changed = False
    if isinstance(value, list):
        for item in value:
            changed = replace_lora_json_references(item, old_name, new_name, in_lora_list) or changed
        return changed
    if not isinstance(value, dict):
        return False
    for key, item in list(value.items()):
        nested_lora_list = in_lora_list or key in {"loras", "stage1_loras", "default_loras"}
        named_reference = key in {"lora_name", "turbo_lora"} or (in_lora_list and key == "name")
        if named_reference and isinstance(item, str) and item.replace("\\", "/") == old_name:
            value[key] = new_name
            changed = True
        else:
            changed = replace_lora_json_references(item, old_name, new_name, nested_lora_list) or changed
    return changed


def json_references_lora(value: Any, lora_name: str, in_lora_list: bool = False) -> bool:
    if isinstance(value, list):
        return any(json_references_lora(item, lora_name, in_lora_list) for item in value)
    if not isinstance(value, dict):
        return False
    for key, item in value.items():
        nested_lora_list = in_lora_list or key in {"loras", "stage1_loras", "default_loras"}
        named_reference = key in {"lora_name", "turbo_lora"} or (in_lora_list and key == "name")
        if named_reference and isinstance(item, str) and item.replace("\\", "/") == lora_name:
            return True
        if json_references_lora(item, lora_name, nested_lora_list):
            return True
    return False


def active_task_references_lora(lora_name: str) -> bool:
    placeholders = ",".join("?" for _ in QUEUE_ACTIVE_STATES)
    with sqlite3.connect(DATABASE) as db:
        rows = db.execute(
            f"SELECT g.parameters_json FROM task_queue q "
            f"JOIN generation_groups g ON g.group_id=q.record_id "
            f"WHERE q.task_kind='generation' AND q.state IN ({placeholders})",
            tuple(sorted(QUEUE_ACTIVE_STATES)),
        ).fetchall()
    for (raw,) in rows:
        try:
            parameters = json.loads(raw or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if json_references_lora(parameters, lora_name):
            return True
    return False


def normalized_lora_filename(value: Any) -> str:
    requested = unicodedata.normalize("NFKC", str(value or ""))
    if requested != requested.strip():
        raise HTTPException(400, "LoRA filename cannot start or end with whitespace")
    if requested.lower().endswith(".safetensors"):
        requested = requested[:-12]
    if not requested:
        raise HTTPException(400, "LoRA filename cannot be empty")
    if requested != Path(requested).name or "/" in requested or "\\" in requested:
        raise HTTPException(400, "LoRA filename cannot contain a directory")
    if requested.endswith((" ", ".")) or any(ord(char) < 32 for char in requested):
        raise HTTPException(400, "LoRA filename contains invalid characters")
    if re.search(r'[<>:"/\\|?*]', requested):
        raise HTTPException(400, "LoRA filename contains invalid characters")
    device_name = requested.split(".", 1)[0].upper()
    if device_name in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(?:COM|LPT)[1-9]", device_name):
        raise HTTPException(400, "LoRA filename is reserved by Windows")
    filename = f"{requested}.safetensors"
    if len(filename) > 160:
        raise HTTPException(400, "LoRA filename is too long")
    return filename


def rename_file_preserving_case(source: Path, target: Path) -> None:
    if source.name == target.name:
        return
    if source.name.casefold() != target.name.casefold():
        source.rename(target)
        return
    temporary = source.with_name(f".{source.name}.{secrets.token_hex(8)}.rename")
    source.rename(temporary)
    try:
        temporary.rename(target)
    except Exception:
        temporary.rename(source)
        raise


def migrate_lora_json_column(db: sqlite3.Connection, table: str, key_column: str, json_column: str, old_name: str, new_name: str) -> int:
    changed = 0
    for key, raw in db.execute(f"SELECT {key_column},{json_column} FROM {table}").fetchall():
        try:
            value = json.loads(raw or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if replace_lora_json_references(value, old_name, new_name):
            db.execute(f"UPDATE {table} SET {json_column}=? WHERE {key_column}=?", (json.dumps(value), key))
            changed += 1
    return changed


def migrate_lora_name(db: sqlite3.Connection, family: str, old_name: str, new_name: str) -> None:
    if old_name == new_name:
        return
    old_metadata = db.execute(
        "SELECT * FROM lora_metadata WHERE family=? AND lora_name=?", (family, old_name),
    ).fetchone()
    new_metadata = db.execute(
        "SELECT 1 FROM lora_metadata WHERE family=? AND lora_name=?", (family, new_name),
    ).fetchone()
    if old_metadata and not new_metadata:
        db.execute(
            "UPDATE lora_metadata SET lora_name=?,updated_at=? WHERE family=? AND lora_name=?",
            (new_name, utc_now(), family, old_name),
        )
    elif old_metadata:
        db.execute(
            """UPDATE lora_metadata SET description=?,recommended_min=?,recommended_max=?,category_id=?,enabled=?,
               created_at=?,updated_at=? WHERE family=? AND lora_name=?""",
            (
                old_metadata["description"], old_metadata["recommended_min"], old_metadata["recommended_max"],
                old_metadata["category_id"], old_metadata["enabled"], old_metadata["created_at"], utc_now(), family, new_name,
            ),
        )
        db.execute("DELETE FROM lora_metadata WHERE family=? AND lora_name=?", (family, old_name))
    now = utc_now()
    db.execute(
        "UPDATE lora_name_aliases SET new_name=?,updated_at=? WHERE family=? AND new_name=?",
        (new_name, now, family, old_name),
    )
    db.execute(
        """INSERT INTO lora_name_aliases(family,old_name,new_name,created_at,updated_at)
           VALUES(?,?,?,?,?) ON CONFLICT(family,old_name) DO UPDATE SET
           new_name=excluded.new_name,updated_at=excluded.updated_at""",
        (family, old_name, new_name, now, now),
    )
    db.execute(
        """INSERT OR IGNORE INTO lora_favorites(owner_id,family,lora_name,created_at)
           SELECT owner_id,family,?,created_at FROM lora_favorites
           WHERE family=? AND lora_name=?""",
        (new_name, family, old_name),
    )
    db.execute("DELETE FROM lora_favorites WHERE family=? AND lora_name=?", (family, old_name))
    migrate_lora_json_column(db, "generation_groups", "group_id", "parameters_json", old_name, new_name)
    migrate_lora_json_column(db, "jobs", "prompt_id", "parameters_json", old_name, new_name)
    migrate_lora_json_column(db, "chat_attachments", "attachment_id", "snapshot_json", old_name, new_name)


def synchronize_lora_registry_entries(
    db: sqlite3.Connection, entries: list[tuple[str, str, Path]],
) -> list[dict[str, str]]:
    """Synchronize a catalog snapshot inside the caller's transaction."""
    db.row_factory = sqlite3.Row
    current_keys = {(family, name) for family, name, _ in entries}
    migrations: list[dict[str, str]] = []
    registry = {
        (str(row["family"]), str(row["lora_name"])): dict(row)
        for row in db.execute("SELECT * FROM lora_file_registry").fetchall()
    }
    for family, name, path in entries:
        stat = path.stat()
        identity = f"{int(stat.st_dev)}:{int(stat.st_ino)}"
        existing = registry.get((family, name))
        if existing and int(existing["size_bytes"]) == stat.st_size and int(existing["modified_ns"]) == stat.st_mtime_ns:
            fingerprint = str(existing["content_fingerprint"])
        else:
            fingerprint = lora_file_fingerprint(path, stat.st_size)
        if not existing:
            missing = [
                row for key, row in registry.items()
                if key[0] == family and key not in current_keys
                and int(row["size_bytes"]) == stat.st_size
                and str(row["content_fingerprint"]) == fingerprint
            ]
            identity_matches = [row for row in missing if str(row["file_identity"]) == identity]
            candidates = identity_matches or missing
            if len(candidates) == 1:
                old_name = str(candidates[0]["lora_name"])
                migrate_lora_name(db, family, old_name, name)
                db.execute("DELETE FROM lora_file_registry WHERE family=? AND lora_name=?", (family, old_name))
                registry.pop((family, old_name), None)
                migrations.append({"family": family, "old_name": old_name, "new_name": name})
        now = utc_now()
        db.execute(
            """INSERT INTO lora_file_registry(family,lora_name,file_identity,content_fingerprint,size_bytes,modified_ns,last_seen_at)
               VALUES(?,?,?,?,?,?,?) ON CONFLICT(family,lora_name) DO UPDATE SET
               file_identity=excluded.file_identity,content_fingerprint=excluded.content_fingerprint,
               size_bytes=excluded.size_bytes,modified_ns=excluded.modified_ns,last_seen_at=excluded.last_seen_at""",
            (family, name, identity, fingerprint, stat.st_size, stat.st_mtime_ns, now),
        )
        registry[(family, name)] = {
            "family": family, "lora_name": name, "file_identity": identity,
            "content_fingerprint": fingerprint, "size_bytes": stat.st_size,
            "modified_ns": stat.st_mtime_ns, "last_seen_at": now,
        }
    return migrations


def synchronize_lora_registry(force: bool = False) -> list[dict[str, str]]:
    global LORA_REGISTRY_LAST_SCAN
    if not force and time.monotonic() - LORA_REGISTRY_LAST_SCAN < 5:
        return []
    with LORA_REGISTRY_LOCK:
        if not force and time.monotonic() - LORA_REGISTRY_LAST_SCAN < 5:
            return []
        entries = lora_catalog_entries()
        with sqlite3.connect(DATABASE) as db:
            migrations = synchronize_lora_registry_entries(db, entries)
        LORA_REGISTRY_LAST_SCAN = time.monotonic()
        return migrations


def lora_alias_map(family: str) -> dict[str, str]:
    try:
        with sqlite3.connect(DATABASE) as db:
            return {
                str(old_name): str(new_name)
                for old_name, new_name in db.execute(
                    "SELECT old_name,new_name FROM lora_name_aliases WHERE family=? ORDER BY old_name", (family,),
                ).fetchall()
            }
    except sqlite3.OperationalError:
        # Some lightweight callers build descriptors before init_database().
        return {}


async def lora_registry_loop() -> None:
    while True:
        await asyncio.sleep(30)
        try:
            await asyncio.to_thread(synchronize_lora_registry, True)
        except Exception:
            pass


def h3_frame_count(duration: float) -> int:
    frames = max(5, round(duration * 24))
    return frames + (5 - frames % 17) % 17


def h3_vram_assessment(megapixels: float, frames: int, effect_loras: int, low_vram: bool) -> dict[str, Any]:
    pressure = float(megapixels) * max(1.0, int(frames) / 124) * (1 + min(10, max(0, effect_loras)) * 0.08)
    if low_vram:
        pressure *= 0.72
    if pressure >= 2.2:
        risk = "high"
        reason = "This setting has exceeded 16 GB VRAM in comparable local H3 runs."
    elif pressure >= 1.75:
        risk = "medium"
        reason = "This setting is close to the 16 GB VRAM limit, especially with additional LoRAs."
    else:
        risk = "low"
        reason = "This setting is inside the locally observed 16 GB VRAM envelope."
    return {
        "risk": risk,
        "reason": reason,
        "pressure": round(pressure, 2),
        "recommend_low_vram": risk != "low" and not low_vram,
    }


def h3_source_dimensions(source_width: int, source_height: int, megapixels: float) -> tuple[int, int]:
    """Fit an I2V canvas to the source ratio on H3's required 32px grid."""
    if source_width <= 0 or source_height <= 0:
        raise HTTPException(415, "Invalid MiniMax H3 source image dimensions")
    source_ratio = source_width / source_height
    target_pixels = float(megapixels) * 1_000_000
    scale = (target_pixels / (source_width * source_height)) ** 0.5
    # H3's native canvas caps the short edge at 768 and the long edge at 1344.
    scale = min(scale, 1344 / max(source_width, source_height), 768 / min(source_width, source_height))
    ideal_width, ideal_height = source_width * scale, source_height * scale
    candidates: list[tuple[float, float, int, int]] = []
    for width in range(64, 1345, 32):
        ideal_for_width = width / source_ratio
        for height in {
            max(64, min(1344, int(round(ideal_for_width / 32)) * 32)),
            max(64, min(1344, int(ideal_for_width // 32) * 32)),
            max(64, min(1344, (int(ideal_for_width // 32) + 1) * 32)),
        }:
            if min(width, height) > 768:
                continue
            ratio_error = abs(width / height / source_ratio - 1)
            size_error = abs(width - ideal_width) / max(ideal_width, 1) + abs(height - ideal_height) / max(ideal_height, 1)
            candidates.append((ratio_error * 8 + size_error, size_error, width, height))
    if not candidates:
        raise HTTPException(422, "MiniMax H3 could not derive a safe source-ratio canvas")
    _, _, width, height = min(candidates)
    return width, height


def h3_permissions(user: dict[str, Any] | None = None) -> dict[str, Any]:
    account = user if user is not None else CURRENT_USER.get()
    is_admin = bool(account and account.get("role") == "admin")
    return {
        "tier": "admin" if is_admin else "standard",
        "maximum_megapixels": 1.0 if is_admin else 0.7,
        "maximum_duration": 15 if is_admin else 8,
        "maximum_steps": 12 if is_admin else 8,
        # Administrators may deliberately run jobs beyond the standard safe
        # queue estimate. The execution timeout remains in force as a final
        # recovery guard, but no estimated-duration budget is imposed here.
        "queue_budget_seconds": None if is_admin else H3_QUEUE_BUDGET_SECONDS,
        "hard_timeout_seconds": H3_ADMIN_HARD_TIMEOUT_SECONDS if is_admin else H3_HARD_TIMEOUT_SECONDS,
    }


def workflow_descriptor(spec) -> dict[str, Any]:
    descriptions = {
        "qwen2511-modular-flux2": "上传图片，按提示词修改画面。",
        "krea-identity-edit": "使用 Krea2 Identity Edit v1.2 进行单图编辑、身份重塑，或图1场景 + 图2人物的双参考图编辑。",
    }
    common = {
        "samplers": SAMPLERS, "schedulers": SCHEDULERS, "loras": lora_options(spec.key),
        "lora_family": lora_family_name(spec),
        "lora_aliases": lora_alias_map(lora_family_name(spec)),
        "output_folder": f"output/mobile/{spec.key}", "max_lora_slots": spec.max_lora_slots,
        "lora_weight_min": -LORA_WEIGHT_LIMIT, "lora_weight_max": LORA_WEIGHT_LIMIT,
        "default_loras": [], "kind": spec.kind, "two_stage": False,
        "description": descriptions.get(spec.key, spec.description),
        "lora_title": "LoRA",
    }
    if spec.variant == "minimax_h3":
        permissions = h3_permissions()
        common["groups"] = [
            {"id": "model", "title": "Model", "fields": ["h3_model"]},
            {"id": "video", "title": "Resolution and duration", "fields": ["aspect_ratio", "megapixels", "duration"]},
            {"id": "sampling", "title": "Sampling", "fields": ["seed", "random_seed", "steps"]},
            {"id": "advanced", "title": "Turbo and VRAM", "advanced": True, "fields": ["turbo_lora", "lora_strength", "low_vram"]},
        ]
        common["defaults"] = {
            "negative_prompt": "", "count": 1, "mode": "t2v",
            "h3_model": "original_int8",
            "aspect_ratio": "16:9", "megapixels": 0.6, "duration": 5,
            "seed": 0, "random_seed": False, "steps": 6,
            "turbo_lora": H3_TURBO_LORAS[0], "lora_strength": 1.0,
            "low_vram": False,
        }
        common["turbo_lora_options"] = [
            {"value": H3_TURBO_LORAS[0], "label": "Configured Turbo LoRA"},
        ]
        common["model_options"] = [
            {"value": key, "label": H3_MODEL_LABELS[key]}
            for key in H3_DIFFUSION_MODELS
        ]
        common["lora_title"] = "MiniMax H3 效果 LoRA（可选）"
        common["performance"] = {
            "target_seconds": H3_TARGET_SECONDS,
            "estimate_source": "completed_h3_history",
            "runtime_profile_version": H3_RUNTIME_PROFILE_VERSION,
            **permissions,
        }
    elif spec.variant == "krea_identity":
        common["model_options"] = [
            {"value": key, "label": KREA_MODEL_LABELS[key]}
            for key in KREA_MODELS
        ]
        common["lora_title"] = "额外 Krea2 LoRA（可选）"
        common["groups"] = [
            {"id": "model", "title": "参考图用途与模型", "fields": ["reference_mode", "krea_model"]},
            {"id": "generate", "title": "生成设置", "fields": ["count"]},
            {"id": "image", "title": "输出尺寸", "fields": ["size_mode", "output_megapixels", "width", "height"]},
            {"id": "edit", "title": "采样设置", "fields": ["stage1_seed", "stage1_random_seed", "stage1_steps", "stage1_cfg", "stage1_denoise", "stage1_sampler", "stage1_scheduler"]},
            {"id": "edit_control", "title": "图生图控制", "fields": ["identity_lora_strength", "ref_boost", "scene_ref_boost", "grounding_px", "fit_mode"]},
        ]
        common["defaults"] = {
            "negative_prompt": "", "count": 1,
            "reference_mode": "edit",
            "size_mode": "source_ratio",
            "output_megapixels": 1.0,
            "krea_model": "official_turbo",
            "stage1_seed": 0, "stage1_random_seed": False,
            "stage1_steps": 10, "stage1_cfg": 1, "stage1_denoise": 1,
            "stage1_sampler": "euler", "stage1_scheduler": "simple",
            "width": 1024, "height": 1024,
            "identity_lora_strength": 1.0,
            "ref_boost": 1.0,
            "scene_ref_boost": 1.0,
            "grounding_px": 768, "fit_mode": "fit",
        }
    elif spec.kind == "image_edit":
        common["lora_title"] = "LoRA"
        common["groups"] = [
            {"id": "generate", "title": "生成设置", "fields": ["count"]},
            {"id": "edit", "title": "采样设置", "fields": ["stage1_seed", "stage1_random_seed", "stage1_steps", "stage1_cfg", "stage1_denoise", "stage1_sampler", "stage1_scheduler"]},
            {"id": "edit_advanced", "title": "高级设置", "advanced": True, "fields": ["stage1_scale_megapixels"]},
        ]
        modular = spec.variant == "modular2511"
        negative_prompt = ""
        common["defaults"] = {"negative_prompt": negative_prompt, "count": 1, "stage1_seed": 0, "stage1_random_seed": False, "stage1_steps": 8 if modular else 4, "stage1_cfg": 1, "stage1_denoise": 1, "stage1_sampler": "euler_ancestral", "stage1_scheduler": "beta", "stage1_scale_megapixels": 1.25 if modular else 1.0}
    else:
        common["groups"] = [
            {"id": "generate", "title": "生成设置", "fields": ["count"]},
            {"id": "sampling", "title": "Sampling", "fields": ["seed", "random_seed", "steps", "cfg", "denoise", "sampler", "scheduler"]},
            {"id": "image", "title": "Image", "fields": ["aspect_ratio", "megapixels"]},
            {"id": "advanced", "title": "Advanced", "advanced": True, "fields": ["multiple"]},
        ]
        common["defaults"] = {"negative_prompt": "", "count": 1, "seed": 0, "random_seed": False, "steps": 8, "cfg": 1, "denoise": 1, "sampler": "euler", "scheduler": "simple", "aspect_ratio": "1:1 (Square)", "megapixels": 1.0, "batch": 1, "multiple": 16}
    return {
        "key": spec.key,
        "label": spec.label,
        "requires_image": bool(spec.image_node),
        "optional_image": spec.variant == "minimax_h3",
        **common,
    }


def as_number(value: Any, name: str, minimum: float, maximum: float, integer: bool = False) -> int | float:
    try:
        parsed = int(value) if integer else float(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, f"Invalid {name}") from exc
    if not minimum <= parsed <= maximum:
        raise HTTPException(400, f"{name} is outside the allowed range")
    return parsed


def as_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool): return value
    if isinstance(value, str) and value.lower() in {"true", "1", "on"}: return True
    if isinstance(value, str) and value.lower() in {"false", "0", "off", ""}: return False
    if value in {0, 1}: return bool(value)
    raise HTTPException(400, f"Invalid {name}")


def normalize_seed_mode(value: Any, legacy_random: bool) -> str:
    mode = str(value or ("random" if legacy_random else "fixed")).strip().lower()
    if mode not in SEED_MODES:
        raise HTTPException(400, "Seed mode must be random, fixed, increment, or decrement")
    return mode


def validate_seed_sequence(seed: int, count: int, mode: str) -> None:
    distance = max(0, int(count) - 1)
    if mode == "increment" and seed + distance > MAX_GENERATION_SEED:
        raise HTTPException(400, "递增 Seed 序列超出最大值，请降低基准 Seed 或生成数量")
    if mode == "decrement" and seed - distance < 0:
        raise HTTPException(400, "递减 Seed 序列小于 0，请提高基准 Seed 或降低生成数量")


def generation_seed_sequence(spec: Any, values: dict[str, Any]) -> list[int]:
    count = int(values["count"])
    mode = str(values.get("seed_mode") or "random")
    if mode == "random":
        return [secrets.randbits(63) for _ in range(count)]
    seed_field = "stage1_seed" if spec.kind == "image_edit" else "seed"
    base_seed = int(values.get(seed_field, 0))
    step = 1 if mode == "increment" else -1 if mode == "decrement" else 0
    return [base_seed + step * index for index in range(count)]


def normalize_settings(spec, raw: str) -> dict[str, Any]:
    try:
        values = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "Invalid settings") from exc
    if not isinstance(values, dict):
        raise HTTPException(400, "Invalid settings")
    defaults = workflow_descriptor(spec)["defaults"]
    result = dict(defaults)
    for name in defaults:
        if name in values:
            result[name] = values[name]
    if not isinstance(result["negative_prompt"], str) or len(result["negative_prompt"]) > 2000:
        raise HTTPException(400, "Negative prompt must contain at most 2000 characters")
    result["negative_prompt"] = result["negative_prompt"].strip()
    result["count"] = as_number(result["count"], "image count", 1, generation_count_limit(), True)
    if spec.variant == "minimax_h3":
        permissions = h3_permissions()
        if result["count"] != 1:
            raise HTTPException(422, "MiniMax H3 can only queue one video at a time")
        if result["mode"] not in {"t2v", "i2v"}:
            raise HTTPException(400, "MiniMax H3 mode must be t2v or i2v")
        if result["mode"] == "t2v":
            if result["aspect_ratio"] not in {"16:9", "9:16", "1:1"}:
                raise HTTPException(400, "Unsupported MiniMax H3 aspect ratio")
        else:
            # The source file is resolved later in the request. Never trust or
            # apply a client-provided ratio for image-to-video.
            result["aspect_ratio"] = "source"
        result["megapixels"] = as_number(
            result["megapixels"], "megapixels", 0.4, permissions["maximum_megapixels"],
        )
        megapixel_key = f"{result['megapixels']:.1f}"
        allowed_megapixels = [
            key for key in H3_DIMENSIONS
            if float(key) <= permissions["maximum_megapixels"]
        ]
        if megapixel_key not in allowed_megapixels:
            raise HTTPException(400, f"MiniMax H3 resolution must be one of: {', '.join(allowed_megapixels)} MP")
        result["megapixels"] = float(megapixel_key)
        result["duration"] = as_number(
            result["duration"], "duration", 5, permissions["maximum_duration"],
        )
        result["seed"] = as_number(result["seed"], "seed", 0, MAX_GENERATION_SEED, True)
        result["random_seed"] = as_bool(result["random_seed"], "random seed")
        result["seed_mode"] = normalize_seed_mode(values.get("seed_mode"), result["random_seed"])
        result["random_seed"] = result["seed_mode"] == "random"
        validate_seed_sequence(result["seed"], result["count"], result["seed_mode"])
        result["steps"] = as_number(
            result["steps"], "steps", 4, permissions["maximum_steps"], True,
        )
        if result["h3_model"] not in H3_DIFFUSION_MODELS:
            raise HTTPException(400, "Unsupported MiniMax H3 model")
        if result["turbo_lora"] not in H3_TURBO_LORAS:
            raise HTTPException(400, "Unsupported MiniMax H3 Turbo LoRA")
        result["lora_strength"] = as_number(result["lora_strength"], "LoRA strength", 0, 2)
        result["low_vram"] = as_bool(result["low_vram"], "low VRAM mode")
        result["frames"] = h3_frame_count(result["duration"])
        preset_ratio = result["aspect_ratio"] if result["mode"] == "t2v" else "16:9"
        result["width"], result["height"] = H3_DIMENSIONS[megapixel_key][preset_ratio]
        result["runtime_profile_version"] = H3_RUNTIME_PROFILE_VERSION
        result["target_seconds"] = H3_TARGET_SECONDS
        result["performance_tier"] = permissions["tier"]
        result["queue_budget_seconds"] = permissions["queue_budget_seconds"]
        result["hard_timeout_seconds"] = permissions["hard_timeout_seconds"]
        allowed_loras = set(lora_options(spec.key))
        raw_loras = values.get("loras", [])
        if not isinstance(raw_loras, list):
            raise HTTPException(400, "MiniMax H3 LoRAs must be a list")
        if len(raw_loras) > spec.max_lora_slots:
            raise HTTPException(400, f"At most {spec.max_lora_slots} MiniMax H3 LoRAs are allowed")
        result["loras"] = []
        for entry in raw_loras:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "")
            if not name:
                continue
            if name not in allowed_loras:
                raise HTTPException(400, "Selected MiniMax H3 LoRA is unavailable")
            result["loras"].append({
                "name": name,
                "weight": as_number(entry.get("weight", 1), "MiniMax H3 LoRA weight", -LORA_WEIGHT_LIMIT, LORA_WEIGHT_LIMIT),
            })
        result["vram"] = h3_vram_assessment(
            result["megapixels"], result["frames"], len(result["loras"]), result["low_vram"],
        )
        apply_h3_history_estimate(result)
        enforce_h3_cooldown(result)
        if permissions["tier"] != "admin" and result["vram"]["risk"] == "high":
            raise HTTPException(
                422,
                "This MiniMax H3 setting is likely to exceed the available 16 GB VRAM. "
                "Reduce resolution or duration, remove effect LoRAs, or enable low VRAM mode.",
            )
        return result
    if spec.kind == "image_edit":
        result["stage1_seed"] = as_number(result["stage1_seed"], "stage1 seed", 0, MAX_GENERATION_SEED, True)
        result["stage1_random_seed"] = as_bool(result["stage1_random_seed"], "stage1 random seed")
        result["seed_mode"] = normalize_seed_mode(values.get("seed_mode"), result["stage1_random_seed"])
        result["stage1_random_seed"] = result["seed_mode"] == "random"
        validate_seed_sequence(result["stage1_seed"], result["count"], result["seed_mode"])
        result["stage1_steps"] = as_number(result["stage1_steps"], "stage1 steps", 1, 100, True)
        result["stage1_cfg"] = as_number(result["stage1_cfg"], "stage1 cfg", 0, 20)
        result["stage1_denoise"] = as_number(result["stage1_denoise"], "stage1 denoise", 0, 1)
        if result["stage1_sampler"] not in SAMPLERS or result["stage1_scheduler"] not in SCHEDULERS:
            raise HTTPException(400, "Invalid Stage 1 sampler or scheduler")
        if spec.variant == "krea_identity":
            if result["reference_mode"] == "face_swap":
                # Preserve rerun compatibility with tasks created before the
                # public mode was renamed to the broader official terminology.
                result["reference_mode"] = "dual_reference"
            if result["reference_mode"] not in {"edit", "identity", "dual_reference"}:
                raise HTTPException(400, "Invalid Krea reference mode")
            if result["size_mode"] not in {"source_ratio", "custom"}:
                raise HTTPException(400, "Invalid Krea size mode")
            if result["krea_model"] not in KREA_MODELS:
                raise HTTPException(400, "Invalid Krea model")
            result["output_megapixels"] = as_number(result["output_megapixels"], "output megapixels", 0.25, 2)
            result["width"] = as_number(result["width"], "width", 64, 2048, True)
            result["height"] = as_number(result["height"], "height", 64, 2048, True)
            if result["width"] % 8 or result["height"] % 8:
                raise HTTPException(400, "Krea Identity Edit dimensions must be multiples of 8")
            if result["width"] * result["height"] > KREA_IDENTITY_MAX_PIXELS:
                raise HTTPException(400, "Krea Identity Edit output must not exceed 2 megapixels")
            result["ref_boost"] = as_number(result["ref_boost"], "reference boost", 0, 10)
            result["scene_ref_boost"] = as_number(result["scene_ref_boost"], "scene reference boost", 0, 10)
            result["grounding_px"] = as_number(result["grounding_px"], "grounding pixels", 384, 1536, True)
            if result["grounding_px"] % 64:
                raise HTTPException(400, "Grounding pixels must be a multiple of 64")
            result["identity_lora_strength"] = as_number(result["identity_lora_strength"], "Identity Edit LoRA strength", 0, 2)
            if result["fit_mode"] not in {"fit", "crop (legacy)"}:
                raise HTTPException(400, "Invalid Krea image fitting mode")
        else:
            result["stage1_scale_megapixels"] = as_number(result["stage1_scale_megapixels"], "stage1 megapixels", 0.1, 16)
        allowed_loras = set(lora_options(spec.key))
        raw_loras = values.get("loras", [])
        if len(raw_loras) > spec.max_lora_slots:
            raise HTTPException(400, f"At most {spec.max_lora_slots} LoRAs are allowed")
        result["stage1_loras"] = []
        for entry in raw_loras:
            if not isinstance(entry, dict): continue
            name = str(entry.get("name") or "")
            if not name: continue
            if name not in allowed_loras: raise HTTPException(400, "Invalid LoRA")
            result["stage1_loras"].append({
                "name": name,
                "weight": as_number(entry.get("weight", 1), "LoRA weight", -LORA_WEIGHT_LIMIT, LORA_WEIGHT_LIMIT),
            })
        return result
    result["seed"] = as_number(result["seed"], "seed", 0, MAX_GENERATION_SEED, True)
    result["random_seed"] = as_bool(result["random_seed"], "random seed")
    result["seed_mode"] = normalize_seed_mode(values.get("seed_mode"), result["random_seed"])
    result["random_seed"] = result["seed_mode"] == "random"
    validate_seed_sequence(result["seed"], result["count"], result["seed_mode"])
    result["steps"] = as_number(result["steps"], "steps", 1, 100, True)
    result["cfg"] = as_number(result["cfg"], "cfg", 0, 20)
    result["denoise"] = as_number(result["denoise"], "denoise", 0, 1)
    if result["sampler"] not in SAMPLERS:
        raise HTTPException(400, "Invalid sampler")
    if result["scheduler"] not in SCHEDULERS:
        raise HTTPException(400, "Invalid scheduler")
    if spec.kind != "image_edit" and spec.variant != "minimax_h3":
        if result["aspect_ratio"] not in ["1:1 (Square)", "2:3 (Portrait Photo)", "3:2 (Photo)", "3:4 (Portrait Standard)", "4:3 (Standard)", "9:16 (Portrait Widescreen)", "16:9 (Widescreen)", "21:9 (Ultrawide)"]:
            raise HTTPException(400, "Invalid aspect ratio")
        result["megapixels"] = as_number(result["megapixels"], "megapixels", 0.1, 16)
        result["batch"] = result["count"]
        result["multiple"] = as_number(result["multiple"], "multiple", 8, 128, True)
    allowed_loras = set(lora_options(spec.key))
    raw_loras = values.get("loras", [])
    if len(raw_loras) > spec.max_lora_slots:
        raise HTTPException(400, f"At most {spec.max_lora_slots} LoRAs are allowed")
    result["loras"] = []
    for entry in raw_loras:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "")
        if not name:
            continue
        if name not in allowed_loras:
            raise HTTPException(400, "Invalid LoRA")
        result["loras"].append({
            "name": name,
            "weight": as_number(entry.get("weight", 1), "LoRA weight", -LORA_WEIGHT_LIMIT, LORA_WEIGHT_LIMIT),
        })
    return result


def h3_rerun_lottery_count(raw: str | None) -> int:
    """Return the requested H3 rerun draw count; omission means ordinary rerun."""
    if raw is None or not str(raw).strip():
        return 1
    return int(as_number(raw, "MiniMax H3 lottery count", 1, H3_RERUN_LOTTERY_MAX, True))


def h3_rerun_lottery_values(spec, raw_settings: str, rerun_group_id: str | None, draw_count: int) -> dict[str, Any]:
    """Build a locked multi-draw H3 rerun from the caller's own history group."""
    if spec.variant != "minimax_h3":
        raise HTTPException(422, "MiniMax H3 lottery is available only for MiniMax H3 reruns")
    source_id = (rerun_group_id or "").strip()
    if not source_id:
        raise HTTPException(422, "MiniMax H3 lottery requires a historical rerun task")
    source = db_group(source_id)
    if not source or source.get("storage_scope") != "mobile":
        raise HTTPException(404, "Historical MiniMax H3 task was not found")
    if source.get("workflow_key") != "minimax-h3":
        raise HTTPException(422, "MiniMax H3 lottery requires a MiniMax H3 historical task")
    try:
        source_settings = json.loads(source.get("parameters_json") or "{}")
        submitted_settings = json.loads(raw_settings or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "Invalid settings") from exc
    if not isinstance(source_settings, dict) or not isinstance(submitted_settings, dict):
        raise HTTPException(400, "Invalid settings")

    # Re-normalize the historical settings under current model availability and
    # permissions, but never trust a client-provided generation parameter.
    locked_settings = dict(source_settings)
    locked_settings["count"] = 1
    values = normalize_settings(spec, json.dumps(locked_settings))
    negative_prompt = submitted_settings.get("negative_prompt", values["negative_prompt"])
    if not isinstance(negative_prompt, str) or len(negative_prompt) > 2000:
        raise HTTPException(400, "Negative prompt must contain at most 2000 characters")
    values["negative_prompt"] = negative_prompt.strip()
    values.update({
        "count": draw_count,
        "seed": 0,
        "random_seed": True,
        "seed_mode": "random",
        "h3_lottery": True,
        "h3_lottery_count": draw_count,
        "rerun_source_group_id": source_id,
    })
    values.pop("generation_seeds", None)
    return values


def append_extra_model_loras(
    api_graph: dict[str, Any],
    loras: list[dict[str, Any]],
    *,
    static_slots: int,
    consumer_node: str,
    consumer_input: str,
    node_prefix: str,
    relative_names: bool = False,
) -> None:
    """Continue a compiled model chain beyond its editor-backed LoRA slots."""
    if len(loras) <= static_slots:
        return
    consumer = api_graph.get(consumer_node)
    model = consumer.get("inputs", {}).get(consumer_input) if consumer else None
    if not isinstance(model, list) or len(model) != 2:
        raise WorkflowCompileError("Cannot extend the LoRA model chain")
    for slot, item in enumerate(loras[static_slots:], static_slots + 1):
        node_id = f"{node_prefix}_{slot}"
        lora_name = comfy_relative_name(item["name"]) if relative_names else item["name"]
        api_graph[node_id] = {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {
                "model": model,
                "lora_name": lora_name,
                "strength_model": item["weight"],
            },
        }
        model = [node_id, 0]
    consumer["inputs"][consumer_input] = model


def build_graph(
    spec,
    settings: dict[str, Any],
    image_name: str | None,
    reference_image_name: str | None = None,
    edit_mask_name: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    overrides: dict[str, dict[str, Any]] = {spec.prompt_node: {spec.prompt_input: settings["prompt"]}}
    bypass: set[str] = set()
    if image_name and spec.image_node:
        overrides[spec.image_node] = {spec.image_input or "image": image_name}
    if spec.variant == "minimax_h3":
        diffusion_model = H3_DIFFUSION_MODELS[settings["h3_model"]]
        required = {
            "diffusion_models": diffusion_model,
            "text_encoders": H3_MODELS["text_encoder"],
            "vae": H3_MODELS["video_vae"],
            "audio vae": H3_MODELS["audio_vae"],
            "loras": settings["turbo_lora"],
        }
        for folder, filename in required.items():
            model_folder = "vae" if folder == "audio vae" else folder
            if not model_path(COMFY_ROOT, model_folder, filename).is_file():
                raise HTTPException(409, f"MiniMax H3 model is unavailable: {filename}")
        overrides.update({
            "127": {"unet_name": diffusion_model, "weight_dtype": "default"},
            "128": {"clip_name": H3_MODELS["text_encoder"], "type": "minimax", "device": "default"},
            "119": {"vae_name": H3_MODELS["video_vae"]},
            "120": {"vae_name": H3_MODELS["audio_vae"]},
            "134": {
                "lora_name": settings["turbo_lora"],
                "strength": settings["lora_strength"],
                "low_vram": settings["low_vram"],
            },
            "129": {"noise_seed": settings["seed"]},
            "124": {"scheduler": "simple", "steps": settings["steps"], "denoise": 1.0},
            "131": {
                "prompt": settings["prompt"], "width": settings["width"],
                "height": settings["height"], "length": settings["frames"],
            },
            "130": {"fps": 24},
            "92": {"filename_prefix": "mobile/minimax-h3/MiniMax_H3", "format": "auto", "codec": "auto"},
        })
    elif spec.kind == "image_edit":
        if spec.variant == "krea_identity":
            model_name = KREA_MODELS[settings["krea_model"]]
            required = {
                "diffusion_models": model_name,
                "text_encoders": SETTINGS.krea_identity_text_encoder,
                "vae": SETTINGS.krea_identity_vae,
                "loras": KREA_IDENTITY_EDIT_LORA,
            }
            for folder, filename in required.items():
                if not model_path(COMFY_ROOT, folder, filename).is_file():
                    raise HTTPException(409, f"Krea dependency is unavailable: {filename}")
            overrides["55"] = {"unet_name": model_name, "weight_dtype": "default"}
            overrides["56"] = {
                "clip_name": SETTINGS.krea_identity_text_encoder,
                "type": "krea2",
                "device": "default",
            }
            overrides["57"] = {"vae_name": SETTINGS.krea_identity_vae}
            overrides["71"] = {
                "lora_name": KREA_IDENTITY_EDIT_LORA,
                "strength_model": settings["identity_lora_strength"],
            }
            bypass.add("108")
            for index, node_id in enumerate(("109", "110", "111", "117", "118", "119")):
                if index < len(settings["stage1_loras"]):
                    item = settings["stage1_loras"][index]
                    overrides[node_id] = {"lora_name": comfy_relative_name(item["name"]), "strength_model": item["weight"]}
                else:
                    bypass.add(node_id)
            overrides["79"] = {
                "ref_boost": settings["ref_boost"],
                "ref_boost_a": settings["scene_ref_boost"],
                "fit_mode": settings["fit_mode"],
            }
            if settings["reference_mode"] in {"dual_reference", "face_swap"}:
                if not reference_image_name:
                    raise HTTPException(400, "双参考图模式需要图2人物参考图")
                overrides["115"] = {"image": reference_image_name}
            overrides["82"] = {"width": settings["width"], "height": settings["height"], "batch_size": 1}
            grounding_system = (
                KREA_IDENTITY_REFERENCE_SYSTEM_PROMPT
                if settings["reference_mode"] == "identity"
                else ""
            )
            overrides["84"] = {
                "prompt": settings["prompt"], "grounding_px": settings["grounding_px"],
                "system_prompt": grounding_system,
            }
            overrides["85"] = {
                "prompt": settings["negative_prompt"], "grounding_px": settings["grounding_px"],
                "system_prompt": grounding_system,
            }
            overrides["53"] = {
                "seed": settings["stage1_seed"], "steps": settings["stage1_steps"],
                "cfg": settings["stage1_cfg"], "denoise": settings["stage1_denoise"],
                "sampler_name": settings["stage1_sampler"], "scheduler": settings["stage1_scheduler"],
            }
        else:
            required = {
                "diffusion_models": SETTINGS.qwen2511_model,
                "text_encoders": SETTINGS.qwen2511_text_encoder,
                "vae": SETTINGS.qwen2511_vae,
                "loras": SETTINGS.qwen2511_lightning_lora,
            }
            for folder, filename in required.items():
                if not model_path(COMFY_ROOT, folder, filename).is_file():
                    raise HTTPException(409, f"Qwen 2511 dependency is unavailable: {filename}")
            lightning_suffix = Path(SETTINGS.qwen2511_lightning_lora).name
            lora_root = COMFY_ROOT / "models" / "loras"
            lightning = next(
                (path.relative_to(lora_root).as_posix() for path in lora_root.rglob("*.safetensors") if path.name.endswith(lightning_suffix)),
                lightning_suffix,
            )
            overrides["427"] = {"clip_name": SETTINGS.qwen2511_text_encoder, "type": "qwen_image", "device": "default"}
            overrides["429"] = {"vae_name": SETTINGS.qwen2511_vae}
            overrides["430"] = {"unet_name": SETTINGS.qwen2511_model, "weight_dtype": "default"}
            overrides["440"] = {"lora_name": lightning, "strength_model": 1.0}
            overrides["434"] = {"prompt": settings["negative_prompt"]}
            overrides["437"] = {"seed": settings["stage1_seed"], "steps": settings["stage1_steps"], "cfg": settings["stage1_cfg"], "denoise": settings["stage1_denoise"], "sampler_name": settings["stage1_sampler"], "scheduler": settings["stage1_scheduler"]}
            overrides["439"] = {"megapixels": settings["stage1_scale_megapixels"]}
            for index, node_id in enumerate(("441", "442", "443", "450", "451", "452")):
                if index < len(settings["stage1_loras"]):
                    item = settings["stage1_loras"][index]
                    overrides[node_id] = {"lora_name": item["name"], "strength_model": item["weight"]}
                else:
                    bypass.add(node_id)
    else:
        overrides["11"] = {"seed": settings["seed"], "steps": settings["steps"], "cfg": settings["cfg"], "denoise": settings["denoise"], "sampler_name": settings["sampler"], "scheduler": settings["scheduler"]}
        required = {
            "diffusion_models": SETTINGS.krea_turbo_model,
            "text_encoders": SETTINGS.krea_turbo_text_encoder,
            "vae": SETTINGS.krea_turbo_vae,
        }
        for folder, filename in required.items():
            if not model_path(COMFY_ROOT, folder, filename).is_file():
                raise HTTPException(409, f"Krea Turbo dependency is unavailable: {filename}")
        overrides["1"] = {"unet_name": SETTINGS.krea_turbo_model, "weight_dtype": "default"}
        overrides["2"] = {"clip_name": SETTINGS.krea_turbo_text_encoder, "type": "krea2", "device": "default"}
        overrides["3"] = {"vae_name": SETTINGS.krea_turbo_vae}
        ratio_width, ratio_height = map(int, settings["aspect_ratio"].split()[0].split(":"))
        ratio = ratio_width / ratio_height
        multiple = settings["multiple"]
        height = (settings["megapixels"] * 1_000_000 / ratio) ** 0.5
        width = height * ratio
        overrides["7"] = {
            "width": max(multiple, round(width / multiple) * multiple),
            "height": max(multiple, round(height / multiple) * multiple),
            "batch_size": settings["batch"],
        }
        # The portable template keeps its optional slots bypassed. Build the
        # complete selected chain below so every slot follows the same path.
        bypass.update(("8", "9", "10"))
    source = MOBILE_WORKFLOW_DIR / spec.mobile_source_name
    api_graph = compile_workflow(
        source, overrides, bypass,
        output_node_types=("SaveVideo",) if spec.variant == "minimax_h3" else ("SaveImage",),
    )
    if spec.key == "krea-turbo":
        append_extra_model_loras(
            api_graph, settings["loras"], static_slots=0,
            consumer_node="11", consumer_input="model",
            node_prefix="krea_turbo_extra_lora", relative_names=True,
        )
        if settings["negative_prompt"]:
            api_graph["turbo_negative"] = {
                "class_type": "CLIPTextEncode",
                "inputs": {"clip": ["2", 0], "text": settings["negative_prompt"]},
            }
            api_graph["11"]["inputs"]["negative"] = ["turbo_negative", 0]
    if spec.variant == "krea_identity":
        append_extra_model_loras(
            api_graph, settings["stage1_loras"], static_slots=6,
            consumer_node="71", consumer_input="model",
            node_prefix="krea_identity_extra_lora", relative_names=True,
        )
    elif spec.variant == "modular2511":
        append_extra_model_loras(
            api_graph, settings["stage1_loras"], static_slots=6,
            consumer_node="428", consumer_input="model",
            node_prefix="qwen2511_extra_lora",
        )
    if spec.variant == "minimax_h3":
        effect_model: list[Any] = ["127", 0]
        for index, item in enumerate(settings.get("loras", [])):
            node_id = f"h3_effect_lora_{index}"
            api_graph[node_id] = {
                "class_type": "LoraLoaderModelOnly",
                "inputs": {
                    "model": effect_model,
                    "lora_name": comfy_relative_name(item["name"]),
                    "strength_model": item["weight"],
                },
            }
            effect_model = [node_id, 0]
        api_graph["134"]["inputs"]["model"] = effect_model
        api_graph["136"] = {
            "class_type": "MiniMaxH3MemoryEfficientSageAttentionPatch",
            "inputs": {"model": ["134", 0]},
        }
        api_graph["126"]["inputs"]["model"] = ["136", 0]
        api_graph["124"]["inputs"]["model"] = ["136", 0]
        if settings["mode"] == "i2v":
            if not image_name:
                raise HTTPException(400, "MiniMax H3 image-to-video mode requires a first-frame image")
            api_graph["140"] = {"class_type": "LoadImage", "inputs": {"image": image_name}}
            api_graph["131"]["inputs"]["first_frame"] = ["140", 0]
        for unused_node in ("115", "132", "133"):
            api_graph.pop(unused_node, None)
    if spec.variant == "krea_identity":
        # The connected pixel path inside Krea2EditModelPatch re-encodes the
        # fitted source image(s) at the exact target latent size and completely
        # replaces source_latent during sampling. A target-sized placeholder
        # therefore avoids two redundant VAEEncode passes without changing the
        # latent(s) that actually reach the diffusion model.
        api_graph["79"]["inputs"]["source_latent"] = ["82", 0]
        api_graph["79"]["inputs"]["target_latent"] = ["82", 0]
        api_graph["79"]["inputs"].pop("source_latent_b", None)
        if settings["stage1_cfg"] == 1:
            # ComfyUI's CFG=1 optimization sets uncond=None before sampling.
            # Reusing the positive input here keeps that exact sampler behavior
            # while making the second image-grounded Qwen encode unreachable.
            api_graph["53"]["inputs"]["negative"] = api_graph["53"]["inputs"]["positive"]
    if spec.variant == "krea_identity":
        dual_reference = settings["reference_mode"] in {"dual_reference", "face_swap"}
        # target_latent lets the v1.2.5 node encode both pixel references before
        # sampling starts. The standalone VAEEncode nodes would be redundant, so
        # keep the lightweight target-sized fallback latent and raw pixel paths.
        api_graph.pop("116", None)
        if dual_reference:
            api_graph["79"]["inputs"]["source_image_b"] = ["115", 0]
            api_graph["84"]["inputs"]["image_b"] = ["115", 0]
            api_graph["85"]["inputs"]["image_b"] = ["115", 0]
        else:
            api_graph.pop("115", None)
            for node_id, input_name in (
                ("79", "source_image_b"),
                ("84", "image_b"),
                ("85", "image_b"),
            ):
                api_graph.get(node_id, {}).get("inputs", {}).pop(input_name, None)
    return api_graph, json.loads(source.read_text(encoding="utf-8"))


def normalize_enhanced_upscale_settings(payload: dict[str, Any]) -> dict[str, Any]:
    return normalize_enhanced_upscale_settings_service(
        payload,
        ENHANCED_UPSCALE_PROFILES,
        SEEDVR2_MAX_SEED,
    )


def normalize_prompt_tool_values(payload: dict[str, Any]) -> dict[str, Any]:
    operation = str(payload.get("operation") or "enhance")
    target = str(payload.get("target_workflow") or "qwen2511-modular-flux2")
    mode = str(payload.get("mode") or "pure")
    style = str(payload.get("style") or "detailed")
    input_text = unicodedata.normalize("NFKC", str(payload.get("input") or "")).strip()
    if operation not in {"enhance", "interrogate"}:
        raise HTTPException(400, "Unsupported prompt assistant operation")
    if target not in PROMPT_TOOL_TARGETS:
        raise HTTPException(400, "Unsupported target workflow")
    if mode not in {"pure", "guided"}:
        raise HTTPException(400, "Unsupported image prompt mode")
    if style not in PROMPT_TOOL_STYLES:
        raise HTTPException(400, "Unsupported prompt style")
    if len(input_text) > 6000:
        raise HTTPException(400, "Prompt assistant input exceeds 6000 characters")
    if operation == "enhance" and not input_text:
        raise HTTPException(400, "Enter a prompt to enhance")
    if operation == "interrogate":
        mode = "guided" if input_text else "pure"
    return {"operation": operation, "target_workflow": target, "mode": mode, "style": style, "input": input_text}


def prompt_tool_model_files() -> tuple[Path, Path]:
    root = COMFY_ROOT / "models" / "LLM"
    return root / LLAMA_MODEL, root / LLAMA_MMPROJ


def prompt_tool_user_instruction(values: dict[str, Any]) -> str:
    target_key = values["target_workflow"]
    is_krea_identity = target_key == "krea-identity-edit"
    is_video = target_key == "minimax-h3"
    target = "Krea2 图生图" if is_krea_identity else "MiniMax H3 视频" if is_video else "Qwen Image"
    if values["operation"] == "enhance":
        return (
            f"{SETTINGS.prompt_enhance_instruction}\n目标工作流：{target}\n风格要求："
            f"{PROMPT_TOOL_STYLE_INSTRUCTIONS[values['style']]}\n\n用户输入：\n{values['input']}"
        )
    style = PROMPT_TOOL_STYLE_INSTRUCTIONS[values["style"]]
    guidance = values["input"] if values["mode"] == "guided" else ""
    instruction = (
        f"{SETTINGS.prompt_interrogate_instruction}\n目标工作流：{target}\n风格要求：{style}"
    )
    if guidance:
        instruction += "\n\n用户补充要求：\n" + guidance
    return instruction


def build_prompt_tool_graph(values: dict[str, Any], image_name: str | None = None) -> tuple[dict[str, Any], dict[str, Any], str]:
    raise HTTPException(410, "提示词助手已从发布版移除")


def clean_prompt_tool_output(value: Any) -> str:
    text_value = str(value or "").strip()
    def strip_code_fence(text: str) -> str:
        if not (text.startswith("```") and text.endswith("```")):
            return text
        lines = text.splitlines()
        return "\n".join(lines[1:-1]).strip()

    text_value = strip_code_fence(text_value)
    # Some Qwen reasoning variants omit the opening <think> token from the
    # decoded text but still emit its closing marker, leaving a draft before
    # the actual answer. Keep only the final answer after the last marker.
    lowered = text_value.lower()
    for marker in ("</think>", "<|endofthink|>", "</analysis>"):
        position = lowered.rfind(marker)
        if position >= 0:
            final_answer = text_value[position + len(marker):].strip()
            if final_answer:
                text_value = final_answer
                lowered = text_value.lower()
    text_value = strip_code_fence(text_value)
    return text_value[:6000]


def build_enhanced_upscale_graph(settings: dict[str, Any], image_name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    config = EnhancedUpscaleConfig(
        workflow_path=ENHANCED_UPSCALE_WORKFLOW,
        comfy_root=COMFY_ROOT,
        base_model=ENHANCED_UPSCALE_MODEL,
        text_encoder=ENHANCED_UPSCALE_TEXT_ENCODER,
        vae=ENHANCED_UPSCALE_VAE,
        consistency_lora=ENHANCED_UPSCALE_LORA,
        seedvr_dit=ENHANCED_UPSCALE_SEEDVR_DIT,
        seedvr_vae=ENHANCED_UPSCALE_SEEDVR_VAE,
    )
    return build_enhanced_upscale_graph_service(config, settings, image_name)


def apply_pre_edit_color_match(graph: dict[str, Any], original_image_name: str) -> None:
    """Match a pre-upscaled edit result back to the untouched upload."""
    save_nodes = [node for node in graph.values() if node.get("class_type") == "SaveImage"]
    for index, saver in enumerate(save_nodes):
        target = saver.get("inputs", {}).get("images")
        if not isinstance(target, list):
            continue
        load_id = f"pre_original_{index}"
        match_id = f"pre_color_match_{index}"
        graph[load_id] = {"class_type": "LoadImage", "inputs": {"image": original_image_name}}
        graph[match_id] = {
            "class_type": "ColorMatch",
            "inputs": {
                "image_ref": [load_id, 0], "image_target": target,
                "method": "mkl", "strength": 1.0, "multithread": True,
            },
        }
        saver["inputs"]["images"] = [match_id, 0]


def copy_image_for_processing(image: dict[str, str], prefix: str) -> tuple[str, dict[str, str]]:
    source = safe_output_path(image)
    if not source or not source.is_file():
        raise HTTPException(409, "The processing source is no longer available")
    suffix = source.suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise HTTPException(415, "Only JPEG, PNG, and WebP are supported")
    filename = f"{prefix}_{secrets.token_hex(16)}{suffix}"
    target = UPLOAD_DIR / filename
    shutil.copy2(source, target)
    record = {"filename": filename, "subfolder": "mobile_uploads", "type": "input"}
    return f"mobile_uploads/{filename}", record


def completed_detail(group_id: str, item_id_value: str) -> dict[str, Any] | None:
    for row in db_item_details(group_id, item_id_value):
        if row["status"] != "completed":
            continue
        try:
            output = json.loads(row.get("output_json") or "{}")
        except json.JSONDecodeError:
            continue
        path = safe_output_path(output) if isinstance(output, dict) else None
        if isinstance(output, dict) and output.get("filename") and path and path.is_file():
            return row
    return None


def comfy_rejection_detail(payload: Any) -> str:
    """Return a concise actionable error from a ComfyUI rejection payload."""
    if not isinstance(payload, dict):
        return "ComfyUI rejected the request"
    error = payload.get("error")
    top_level = str(error.get("message") or "").strip() if isinstance(error, dict) else ""
    node_errors = payload.get("node_errors")
    if isinstance(node_errors, dict):
        for node in node_errors.values():
            if not isinstance(node, dict):
                continue
            class_type = str(node.get("class_type") or "workflow node").strip()
            errors = node.get("errors")
            if not isinstance(errors, list):
                continue
            for item in errors:
                if not isinstance(item, dict):
                    continue
                message = str(item.get("message") or item.get("type") or "validation failed").strip()
                extra = item.get("extra_info")
                input_name = str(extra.get("input_name") or "").strip() if isinstance(extra, dict) else ""
                received = extra.get("received_value") if isinstance(extra, dict) else None
                location = f"{class_type}.{input_name}" if input_name else class_type
                value = f" ({received})" if received not in (None, "") else ""
                return f"{top_level or 'Prompt validation failed'} — {location}: {message}{value}"
    return top_level or "ComfyUI rejected the request"


async def comfy_json(method: str, path: str, **kwargs) -> dict[str, Any]:
    global COMFY_AVAILABLE
    try:
        client = COMFY_HTTP_CLIENT.get()
        if client is not None:
            response = await client.request(method, f"{COMFY_URL}{path}", **kwargs)
        else:
            async with httpx.AsyncClient(timeout=60) as temporary_client:
                response = await temporary_client.request(method, f"{COMFY_URL}{path}", **kwargs)
    except httpx.HTTPError as exc:
        COMFY_AVAILABLE = False
        raise HTTPException(503, "ComfyUI is unavailable") from exc
    COMFY_AVAILABLE = True
    if response.is_error:
        try:
            detail = comfy_rejection_detail(response.json())
        except ValueError:
            detail = "ComfyUI rejected the request"
        raise HTTPException(422 if response.status_code < 500 else 503, f"ComfyUI: {detail}")
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError as exc:
        raise HTTPException(502, "ComfyUI returned an invalid response") from exc


def safe_output_path(image: dict[str, str]) -> Path | None:
    candidate = (OUTPUT_DIR / image.get("subfolder", "") / image.get("filename", "")).resolve()
    root = MOBILE_OUTPUT_DIR.resolve()
    return candidate if candidate.is_relative_to(root) else None


def safe_source_path(image: dict[str, str]) -> Path | None:
    candidate = (COMFY_ROOT / "input" / image.get("subfolder", "") / image.get("filename", "")).resolve()
    root = UPLOAD_DIR.resolve()
    return candidate if candidate.is_relative_to(root) else None


def safe_chat_media_path(image: dict[str, str]) -> Path | None:
    candidate = (CHAT_MEDIA_DIR / image.get("subfolder", "") / image.get("filename", "")).resolve()
    root = CHAT_MEDIA_DIR.resolve()
    return candidate if candidate.is_relative_to(root) else None


_STORAGE_USAGE_CACHE_LOCK = threading.Lock()
_STORAGE_USAGE_CACHE: dict[int, int] | None = None
_STORAGE_USAGE_CACHE_EXPIRES_AT = 0.0
_STORAGE_USAGE_CACHE_KEY: tuple[Any, ...] | None = None


def invalidate_storage_usage_cache() -> None:
    global _STORAGE_USAGE_CACHE, _STORAGE_USAGE_CACHE_EXPIRES_AT, _STORAGE_USAGE_CACHE_KEY
    with _STORAGE_USAGE_CACHE_LOCK:
        _STORAGE_USAGE_CACHE = None
        _STORAGE_USAGE_CACHE_EXPIRES_AT = 0.0
        _STORAGE_USAGE_CACHE_KEY = None


def _storage_owner(row: dict[str, Any], column: str = "owner_id") -> int | None:
    value = row.get(column)
    return int(value) if value is not None else None


def _file_revision(path: Path) -> tuple[int, int]:
    try:
        info = path.stat()
        return info.st_mtime_ns, info.st_size
    except OSError:
        return 0, 0


def storage_usage_cache_key() -> tuple[Any, ...]:
    wal = Path(f"{DATABASE}-wal")
    return (
        *(str(path.resolve()) for path in (DATABASE, MOBILE_OUTPUT_DIR, UPLOAD_DIR, CHAT_MEDIA_DIR, AVATAR_DIR)),
        _file_revision(DATABASE),
        _file_revision(wal),
    )


def calculate_storage_usage() -> dict[int, int]:
    """Calculate every user's owned files in one database pass and one stat pass."""
    with database_session(DATABASE, rows=True) as db:
        user_ids = [int(row[0]) for row in db.execute("SELECT user_id FROM users").fetchall()]
        groups = [dict(row) for row in db.execute("SELECT * FROM generation_groups WHERE owner_id IS NOT NULL").fetchall()]
        jobs = [dict(row) for row in db.execute("SELECT * FROM jobs WHERE owner_id IS NOT NULL").fetchall()]
        upscales = [dict(row) for row in db.execute("SELECT * FROM upscale_jobs WHERE owner_id IS NOT NULL").fetchall()]
        details = [dict(row) for row in db.execute("SELECT * FROM detail_jobs WHERE owner_id IS NOT NULL").fetchall()]
        preprocesses = [dict(row) for row in db.execute("SELECT * FROM preprocess_jobs WHERE owner_id IS NOT NULL").fetchall()]
        prompt_tools = [dict(row) for row in db.execute("SELECT * FROM prompt_tool_jobs WHERE owner_id IS NOT NULL").fetchall()]
        prepared = [dict(row) for row in db.execute("SELECT * FROM chat_prepared_assets WHERE consumed_at IS NULL").fetchall()]
        chat_media = [dict(row) for row in db.execute(
            "SELECT origin_owner_id,manifest_json FROM chat_attachments WHERE kind='media'"
        ).fetchall()]
    paths: dict[int, set[Path]] = {user_id: {avatar_path(user_id)} for user_id in user_ids}

    def add(owner_id: int | None, path: Path | None) -> None:
        if owner_id is not None and path is not None:
            paths.setdefault(owner_id, set()).add(path)

    for row in groups:
        owner_id = _storage_owner(row)
        for column in ("source_image_json", "edit_source_image_json", "reference_image_json", "edit_mask_json"):
            image = json_image_from(row, column)
            path = safe_source_path(image) if image else None
            add(owner_id, path)
        preprocess = group_preprocess(row)
        output = preprocess.get("output") if preprocess else None
        path = safe_output_path(output) if isinstance(output, dict) else None
        add(owner_id, path)
        for item in stored_items(row):
            for key in ("final", "stage1"):
                image = item.get(key)
                path = safe_output_path(image) if isinstance(image, dict) else None
                add(owner_id, path)
    for row in jobs:
        owner_id = _storage_owner(row)
        for images in output_sets(row.get("outputs_json") or "[]").values():
            for image in images:
                add(owner_id, safe_output_path(image))
    for row in [*upscales, *details, *preprocesses]:
        owner_id = _storage_owner(row)
        for column, source in (("source_image_json", True), ("output_json", False)):
            try: image = json.loads(row.get(column) or "{}")
            except json.JSONDecodeError: image = {}
            path = safe_source_path(image) if source and isinstance(image, dict) else safe_output_path(image) if isinstance(image, dict) else None
            add(owner_id, path)
    for row in prompt_tools:
        image = source_image_from(row)
        path = safe_source_path(image) if image else None
        add(_storage_owner(row), path)
    for row in prepared:
        image = json_image_from(row, "source_image_json")
        path = safe_source_path(image) if image else None
        add(_storage_owner(row, "user_id"), path)
    for row in chat_media:
        owner_id = _storage_owner(row, "origin_owner_id")
        try:
            manifest = json.loads(row.get("manifest_json") or "{}")
        except json.JSONDecodeError:
            manifest = {}
        for entry in manifest.get("files") or []:
            if not isinstance(entry, dict) or entry.get("storage") != "chat_media":
                continue
            image = entry.get("image")
            path = safe_chat_media_path(image) if isinstance(image, dict) else None
            add(owner_id, path)
    usage: dict[int, int] = {}
    for user_id, owned_paths in paths.items():
        total = 0
        for path in owned_paths:
            try:
                if path.is_file():
                    total += path.stat().st_size
            except OSError:
                continue
        usage[user_id] = total
    return usage


def user_storage_snapshot(force: bool = False) -> dict[int, int]:
    global _STORAGE_USAGE_CACHE, _STORAGE_USAGE_CACHE_EXPIRES_AT, _STORAGE_USAGE_CACHE_KEY
    with _STORAGE_USAGE_CACHE_LOCK:
        now = time.monotonic()
        cache_key = storage_usage_cache_key()
        if not force and _STORAGE_USAGE_CACHE_KEY == cache_key and _STORAGE_USAGE_CACHE is not None and now < _STORAGE_USAGE_CACHE_EXPIRES_AT:
            return dict(_STORAGE_USAGE_CACHE)
        usage = calculate_storage_usage()
        _STORAGE_USAGE_CACHE = usage
        _STORAGE_USAGE_CACHE_KEY = cache_key
        _STORAGE_USAGE_CACHE_EXPIRES_AT = time.monotonic() + STORAGE_USAGE_CACHE_SECONDS
        return dict(usage)


def user_storage_bytes(user_id: int) -> int:
    return int(user_storage_snapshot().get(int(user_id), 0))


def active_task_count(user_id: int) -> int:
    with database_session(DATABASE) as db:
        queued = db.execute(
            "SELECT COUNT(*) FROM task_queue WHERE owner_id=? AND state IN ('waiting','dispatching','running','paused','cancelling')",
            (user_id,),
        ).fetchone()[0]
    return int(queued)


def enforce_user_capacity(upload_bytes: int = 0) -> None:
    user = CURRENT_USER.get()
    if not user:  # Internal maintenance and unit-test calls do not represent a web user.
        return
    if user["role"] == "admin":
        return
    if active_task_count(int(user["user_id"])) >= QUEUE_USER_LIMIT:
        raise HTTPException(429, "每个账号最多同时保留一个运行任务和一个排队任务")
    used = user_storage_bytes(int(user["user_id"]))
    if used + max(0, upload_bytes) >= USER_QUOTA_BYTES:
        raise HTTPException(413, "个人存储已达到 20GB，请删除部分历史后重试")


def remove_source_image(row: dict[str, Any]) -> None:
    source_images = [
        source_image_from(row),
        json_image_from(row, "edit_source_image_json"),
        json_image_from(row, "reference_image_json"),
        json_image_from(row, "edit_mask_json"),
    ]
    removed: set[Path] = set()
    for image in source_images:
        path = safe_source_path(image) if image else None
        if path and path not in removed and path.exists():
            path.unlink()
            removed.add(path)
    preprocess = group_preprocess(row)
    output = preprocess.get("output") if preprocess else None
    if isinstance(output, dict) and output.get("filename"):
        remove_output_files([output])


def copy_group_source_image(group_id: str) -> tuple[str, dict[str, str]]:
    """Copy a historical mobile upload so a rerun owns its source independently."""
    group = db_group(group_id)
    if not group or group.get("storage_scope") != "mobile":
        raise HTTPException(404, "Historical task was not found")
    image = source_image_from(group)
    path = safe_source_path(image) if image else None
    if not path or not path.is_file():
        raise HTTPException(409, "Historical source image is unavailable; please upload it again")
    suffix = path.suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise HTTPException(409, "Historical source image format is unsupported; please upload it again")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"mobile_{secrets.token_hex(16)}{suffix}"
    shutil.copy2(path, UPLOAD_DIR / filename)
    return f"mobile_uploads/{filename}", {"filename": filename, "subfolder": "mobile_uploads", "type": "input"}


def copy_group_edit_mask(group_id: str) -> tuple[str | None, dict[str, str] | None]:
    """Copy a rerun's optional image-1 mask so the new task owns its file."""
    group = db_group(group_id)
    if not group or group.get("storage_scope") != "mobile":
        return None, None
    image = json_image_from(group, "edit_mask_json")
    path = safe_source_path(image) if image else None
    if not path or not path.is_file():
        return None, None
    filename = f"krea_edit_mask_{secrets.token_hex(16)}.png"
    shutil.copy2(path, UPLOAD_DIR / filename)
    record = {"filename": filename, "subfolder": "mobile_uploads", "type": "input"}
    return f"mobile_uploads/{filename}", record


def copy_group_reference_image(group_id: str) -> tuple[str, dict[str, str]]:
    """Copy a dual-reference image 2 so a rerun owns it independently."""
    group = db_group(group_id)
    if not group or group.get("storage_scope") != "mobile":
        raise HTTPException(404, "Historical task was not found")
    image = json_image_from(group, "reference_image_json")
    path = safe_source_path(image) if image else None
    if not path or not path.is_file():
        raise HTTPException(409, "Historical second reference is unavailable; please select it again")
    suffix = path.suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise HTTPException(409, "Historical second reference format is unsupported")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"krea_dual_reference_{secrets.token_hex(16)}{suffix}"
    shutil.copy2(path, UPLOAD_DIR / filename)
    return f"mobile_uploads/{filename}", {
        "filename": filename,
        "subfolder": "mobile_uploads",
        "type": "input",
    }


def reusable_result_image(group_id: str, item_id_value: str, source_kind: str, record_id: str = "") -> dict[str, str]:
    """Resolve one owned result image without creating an abandoned temporary copy."""
    group = db_group(group_id)
    if not group or group.get("storage_scope") != "mobile":
        raise HTTPException(404, "Historical task was not found")
    item = next((entry for entry in stored_items(group) if entry.get("id") == item_id_value), None)
    if not item:
        raise HTTPException(404, "Historical result was not found")
    if source_kind == "edit":
        image = item.get("final")
    elif source_kind == "upscale":
        row = db_upscale(record_id)
        if not row or row.get("parent_group_id") != group_id or row.get("parent_item_id") != item_id_value or row.get("status") != "completed":
            raise HTTPException(404, "Upscale result was not found")
        image = json_image_from(row, "output_json")
    elif source_kind == "detail":
        row = db_detail(record_id)
        if not row or row.get("parent_group_id") != group_id or row.get("parent_item_id") != item_id_value or row.get("status") != "completed":
            raise HTTPException(404, "Detail result was not found")
        image = json_image_from(row, "output_json")
    else:
        raise HTTPException(400, "Reusable source kind must be edit, upscale, or detail")
    path = safe_output_path(image) if isinstance(image, dict) else None
    if not isinstance(image, dict) or not image.get("filename") or not path or not path.is_file():
        raise HTTPException(409, "The selected result image is no longer available")
    if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise HTTPException(415, "Only JPEG, PNG, and WebP are supported")
    return image


def copy_reusable_result(group_id: str, item_id_value: str, source_kind: str, record_id: str = "") -> tuple[str, dict[str, str]]:
    return copy_image_for_processing(reusable_result_image(group_id, item_id_value, source_kind, record_id), "continued_edit")


def image_record_from_path(path: Path) -> dict[str, str]:
    relative = path.relative_to(OUTPUT_DIR)
    return {"filename": path.name, "subfolder": str(relative.parent), "type": "output"}


def recover_stuck_groups() -> int:
    """Recover output files left behind when a mobile polling request was interrupted."""
    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        groups = [dict(row) for row in db.execute("SELECT * FROM generation_groups WHERE status IN ('queued', 'running') ORDER BY submitted_at")]
        if not groups:
            return 0
        all_group_times = [row[0] for row in db.execute("SELECT submitted_at FROM generation_groups ORDER BY submitted_at")]
        job_rows = [dict(row) for row in db.execute("SELECT outputs_json FROM jobs")]
    registered = {
        image_key(image)
        for row in job_rows
        for images in output_sets(row["outputs_json"]).values()
        for image in images
    }
    recovered = 0
    image_suffixes = {".png", ".jpg", ".jpeg", ".webp"}
    for group in groups:
        try:
            start = datetime.fromisoformat(group["submitted_at"]).timestamp()
            later_times = [value for value in all_group_times if value > group["submitted_at"]]
            end = datetime.fromisoformat(later_times[0]).timestamp() if later_times else float("inf")
            expected = int(json.loads(group["parameters_json"] or "{}").get("count", 1))
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
        if expected < 1:
            continue
        workflow_dir = MOBILE_OUTPUT_DIR / group["workflow_key"]
        if not workflow_dir.exists():
            continue
        def candidates(folder: Path) -> list[dict[str, str]]:
            if not folder.exists():
                return []
            available = [path for path in folder.iterdir() if path.is_file() and path.suffix.lower() in image_suffixes]
            tagged = [path for path in available if group["group_id"] in path.name]
            paths = tagged or [path for path in available if start <= path.stat().st_mtime < end]
            records = [image_record_from_path(path) for path in sorted(paths, key=lambda path: path.stat().st_mtime)]
            return [record for record in records if image_key(record) not in registered]
        finals = candidates(workflow_dir)
        stages = candidates(workflow_dir / "stage1") if is_two_stage_key(group["workflow_key"]) else []
        children = db_group_jobs(group["group_id"])
        if not is_two_stage_key(group["workflow_key"]) and not children:
            continue
        now = utc_now()
        if stages:
            # Qwen submits one ComfyUI prompt per image.  A connection can fail
            # after some children have been saved, so only fill empty children.
            empty_children = [child for child in children if not output_sets(child["outputs_json"])["final"] and not output_sets(child["outputs_json"])["stage1"]]
            if not empty_children or len(finals) != len(empty_children) or len(stages) != len(empty_children):
                continue
            for child, final, stage in zip(empty_children, finals, stages):
                db_execute("UPDATE jobs SET status='completed', completed_at=?, error_message=NULL, outputs_json=? WHERE prompt_id=?", (now, json.dumps({"final": [final], "stage1": [stage]}), child["prompt_id"]))
        elif is_two_stage_key(group["workflow_key"]):
            empty_children = [child for child in children if not output_sets(child["outputs_json"])["final"]]
            if not empty_children or len(finals) != len(empty_children):
                continue
            for child, final in zip(empty_children, finals):
                db_execute("UPDATE jobs SET status='completed', completed_at=?, error_message=NULL, outputs_json=? WHERE prompt_id=?", (now, json.dumps({"final": [final], "stage1": []}), child["prompt_id"]))
        else:
            current = output_sets(children[0]["outputs_json"])
            missing = expected - len(current["final"])
            if missing < 1 or len(finals) != missing:
                continue
            current["final"].extend(finals)
            db_execute("UPDATE jobs SET status='completed', completed_at=?, error_message=NULL, outputs_json=? WHERE prompt_id=?", (now, json.dumps(current), children[0]["prompt_id"]))
        refreshed_children = db_group_jobs(group["group_id"])
        if len([child for child in refreshed_children if child["status"] == "completed"]) < expected:
            continue
        items = items_from_children(group["workflow_key"], refreshed_children)
        db_execute("UPDATE generation_groups SET status='completed', completed_at=?, error_message=NULL, outputs_json=?, items_json=? WHERE group_id=?", (now, json.dumps(outputs_from_items(items)), json.dumps(items), group["group_id"]))
        registered.update(image_key(image) for image in finals + stages)
        recovered += 1
    return recovered


def update_job_from_history(prompt_id: str) -> None:
    pass


def history_output_sets(workflow_key: str, item: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    images: dict[str, list[dict[str, str]]] = {"final": [], "stage1": []}
    spec = get_spec(workflow_key)
    for node_id, output in (item.get("outputs") or {}).items():
        output_kind = "stage1" if spec.stage1_output_node and str(node_id) == spec.stage1_output_node else "final"
        for image in output.get("images", []):
            candidate = {key: image.get(key, "") for key in ("filename", "subfolder", "type")}
            candidate["media_type"] = output_media_type(candidate)
            path = safe_output_path(candidate) if image.get("type") == "output" else None
            if path and path.is_file() and image_key(candidate) not in {image_key(entry) for entry in images[output_kind]}:
                images[output_kind].append(candidate)
    return images


async def reconcile_generation_prompt_outputs(prompt_id: str) -> bool:
    row = db_job(prompt_id)
    if not row:
        return False
    try:
        history = await comfy_json("GET", f"/history/{prompt_id}")
    except Exception:
        return False
    item = history.get(prompt_id)
    if not isinstance(item, dict):
        return False
    recovered = history_output_sets(row["workflow_key"], item)
    current = output_sets(row.get("outputs_json"))
    changed = False
    for output_kind in ("final", "stage1"):
        existing = {image_key(image) for image in current[output_kind]}
        for image in recovered[output_kind]:
            if image_key(image) not in existing:
                current[output_kind].append(image)
                existing.add(image_key(image))
                changed = True
    if changed:
        db_execute("UPDATE jobs SET outputs_json=? WHERE prompt_id=?", (json.dumps(current), prompt_id))
    return changed


async def reconcile_cancelled_generation(group_id: str) -> bool:
    group = unrestricted_group(group_id)
    if not group:
        return False
    changed = False
    for child in db_group_jobs(group_id):
        changed = await reconcile_generation_prompt_outputs(child["prompt_id"]) or changed
    children = db_group_jobs(group_id)
    items = items_from_children(group["workflow_key"], children)
    stored = stored_items(group)
    if changed or items != stored:
        db_execute(
            "UPDATE generation_groups SET outputs_json=?,items_json=? WHERE group_id=?",
            (json.dumps(outputs_from_items(items)), json.dumps(items), group_id),
        )
        return True
    return False


async def recover_cancelled_generation_outputs() -> int:
    with sqlite3.connect(DATABASE) as db:
        rows = db.execute(
            "SELECT group_id FROM generation_groups WHERE storage_scope='mobile' AND status='cancelled'"
        ).fetchall()
    recovered = 0
    async with httpx.AsyncClient(timeout=60) as client:
        token = COMFY_HTTP_CLIENT.set(client)
        try:
            for (group_id,) in rows:
                try:
                    recovered += int(await reconcile_cancelled_generation(str(group_id)))
                except (HTTPException, WorkflowCompileError):
                    continue
        finally:
            COMFY_HTTP_CLIENT.reset(token)
    return recovered


async def recover_cancelled_generation_history() -> int:
    recovered = await recover_cancelled_generation_outputs()
    repair_terminal_group_seed_lists()
    return recovered


async def refresh_job(prompt_id: str) -> dict[str, Any]:
    row = db_job(prompt_id)
    if not row: raise HTTPException(404, "Job not found")
    if row["status"] in {"completed", "failed", "cancelled"}: return public_job(row)
    history = await comfy_json("GET", f"/history/{prompt_id}")
    item = history.get(prompt_id)
    if not item:
        queue = await comfy_json("GET", "/queue")
        state = "running" if any(job[1] == prompt_id for job in queue.get("queue_running", [])) else "queued"
        db_execute("UPDATE jobs SET status=? WHERE prompt_id=?", (state, prompt_id))
        return public_job(db_job(prompt_id) or row)
    status = item.get("status", {})
    if status.get("status_str") != "success":
        db_execute("UPDATE jobs SET status='failed', completed_at=?, error_message=? WHERE prompt_id=?", (utc_now(), str(status.get("messages") or "ComfyUI execution failed"), prompt_id))
    else:
        images = history_output_sets(row["workflow_key"], item)
        db_execute("UPDATE jobs SET status='completed', completed_at=?, outputs_json=? WHERE prompt_id=?", (utc_now(), json.dumps(images), prompt_id))
    return public_job(db_job(prompt_id) or row)


def remove_prompt_tool_source(row: dict[str, Any]) -> None:
    source = source_image_from(row)
    path = safe_source_path(source) if source else None
    if path and path.is_file():
        path.unlink()


def prune_prompt_tool_history(owner_id: int, keep: int = 10) -> None:
    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(row) for row in db.execute(
            "SELECT * FROM prompt_tool_jobs WHERE owner_id=? AND status IN ('completed','failed','cancelled') "
            "ORDER BY submitted_at DESC LIMIT -1 OFFSET ?",
            (owner_id, keep),
        ).fetchall()]
        for row in rows:
            remove_prompt_tool_source(row)
            db.execute("DELETE FROM task_queue WHERE task_kind='prompt_tool' AND record_id=?", (row["tool_id"],))
            db.execute("DELETE FROM prompt_tool_jobs WHERE tool_id=?", (row["tool_id"],))


async def refresh_prompt_tool(tool_id: str) -> dict[str, Any]:
    row = db_prompt_tool(tool_id)
    if not row:
        raise HTTPException(404, "Prompt assistant task not found")
    if row["status"] in QUEUE_TERMINAL_STATES:
        return public_prompt_tool(row)
    prompt_id = str(row.get("prompt_id") or "")
    if not prompt_id:
        return public_prompt_tool(row)
    history = await comfy_json("GET", f"/history/{prompt_id}")
    item = history.get(prompt_id)
    if not item:
        queue = await comfy_json("GET", "/queue")
        state = "running" if prompt_id in queue_prompt_ids(queue.get("queue_running", [])) else "queued"
        db_execute("UPDATE prompt_tool_jobs SET status=? WHERE tool_id=?", (state, tool_id))
        return public_prompt_tool(db_prompt_tool(tool_id) or row)
    status = item.get("status", {})
    now = utc_now()
    if status.get("status_str") != "success":
        message = str(status.get("messages") or "ComfyUI prompt assistant execution failed")
        db_execute("UPDATE prompt_tool_jobs SET status='failed',completed_at=?,error_message=? WHERE tool_id=?", (now, message, tool_id))
    else:
        output_node = "19" if row["operation"] == "enhance" else "21"
        output = item.get("outputs", {}).get(output_node, {})
        texts = output.get("text") or []
        text_value = clean_prompt_tool_output(texts[-1] if isinstance(texts, list) and texts else texts)
        if text_value:
            db_execute(
                "UPDATE prompt_tool_jobs SET status='completed',completed_at=?,output_text=?,error_message=NULL WHERE tool_id=?",
                (now, text_value, tool_id),
            )
        else:
            db_execute(
                "UPDATE prompt_tool_jobs SET status='failed',completed_at=?,error_message=? WHERE tool_id=?",
                (now, "Prompt assistant completed without text output", tool_id),
            )
    updated = db_prompt_tool(tool_id) or row
    remove_prompt_tool_source(updated)
    COMFY_PROGRESS.pop(prompt_id, None)
    prune_prompt_tool_history(int(updated["owner_id"]))
    return public_prompt_tool(updated)


async def refresh_upscale(upscale_id: str) -> dict[str, Any]:
    row = db_upscale(upscale_id)
    if not row:
        raise HTTPException(404, "Upscale job not found")
    if row["status"] in {"completed", "failed", "cancelled"}:
        return public_upscale(row)
    prompt_id = row.get("prompt_id")
    history = await comfy_json("GET", f"/history/{prompt_id}")
    item = history.get(prompt_id)
    if not item:
        queue = await comfy_json("GET", "/queue")
        state = "running" if any(job[1] == prompt_id for job in queue.get("queue_running", [])) else "queued"
        db_execute("UPDATE upscale_jobs SET status=? WHERE upscale_id=?", (state, upscale_id))
        return public_upscale(db_upscale(upscale_id) or row)
    status = item.get("status", {})
    if status.get("status_str") != "success":
        db_execute(
            "UPDATE upscale_jobs SET status='failed', completed_at=?, error_message=? WHERE upscale_id=?",
            (utc_now(), str(status.get("messages") or "ComfyUI execution failed"), upscale_id),
        )
    else:
        output_image = None
        for output in item.get("outputs", {}).values():
            for image in output.get("images", []):
                candidate = {key: image.get(key, "") for key in ("filename", "subfolder", "type")}
                if image.get("type") == "output" and safe_output_path(candidate):
                    output_image = candidate
        if not output_image:
            db_execute(
                "UPDATE upscale_jobs SET status='failed', completed_at=?, error_message=? WHERE upscale_id=?",
                (utc_now(), "Upscale completed without an output image", upscale_id),
            )
        else:
            db_execute(
                "UPDATE upscale_jobs SET status='completed', completed_at=?, error_message=NULL, output_json=? WHERE upscale_id=?",
                (utc_now(), json.dumps(output_image), upscale_id),
            )
            # Keep the previous successful result visible while a replacement
            # runs. Once the replacement succeeds, it becomes the only saved
            # upscale version for this edit image.
            for previous in db_item_upscales(row["parent_group_id"], row["parent_item_id"]):
                if previous["upscale_id"] == upscale_id:
                    continue
                if (previous.get("engine") or "seedvr2") != (row.get("engine") or "seedvr2"):
                    continue
                try:
                    previous_output = json.loads(previous.get("output_json") or "{}")
                    previous_source = json.loads(previous.get("source_image_json") or "{}")
                except json.JSONDecodeError:
                    previous_output, previous_source = {}, {}
                if isinstance(previous_output, dict) and previous_output.get("filename"):
                    remove_output_files([previous_output])
                previous_source_path = safe_source_path(previous_source) if isinstance(previous_source, dict) else None
                if previous_source_path and previous_source_path.is_file():
                    previous_source_path.unlink()
                db_execute("DELETE FROM upscale_jobs WHERE upscale_id=?", (previous["upscale_id"],))
        # Standalone Flux upscale keeps its upload for the before/after view.
        # Detail-page post-processing owns only a temporary working copy and
        # can release it as soon as ComfyUI has finished.
        if (row.get("source_kind") or "edit") != "standalone":
            source_record = json.loads(row.get("source_image_json") or "{}")
            source_path = safe_source_path(source_record)
            if source_path and source_path.is_file():
                source_path.unlink()
    return public_upscale(db_upscale(upscale_id) or row)


async def refresh_preprocess(preprocess_id: str) -> dict[str, Any]:
    row = db_preprocess(preprocess_id)
    if not row:
        raise HTTPException(404, "Pre-upscale job not found")
    if row["status"] in {"completed", "failed", "cancelled"}:
        return public_preprocess(row)
    prompt_id = row.get("prompt_id")
    history = await comfy_json("GET", f"/history/{prompt_id}")
    item = history.get(prompt_id)
    if not item:
        queue = await comfy_json("GET", "/queue")
        state = "running" if prompt_id in queue_prompt_ids(queue.get("queue_running", [])) else "queued"
        db_execute("UPDATE preprocess_jobs SET status=? WHERE preprocess_id=?", (state, preprocess_id))
        return public_preprocess(db_preprocess(preprocess_id) or row)
    status = item.get("status", {})
    if status.get("status_str") != "success":
        db_execute(
            "UPDATE preprocess_jobs SET status='failed', completed_at=?, error_message=? WHERE preprocess_id=?",
            (utc_now(), str(status.get("messages") or "ComfyUI execution failed"), preprocess_id),
        )
    else:
        output_image = None
        for output in item.get("outputs", {}).values():
            for image in output.get("images", []):
                candidate = {key: image.get(key, "") for key in ("filename", "subfolder", "type")}
                if image.get("type") == "output" and safe_output_path(candidate):
                    output_image = candidate
        if not output_image:
            db_execute(
                "UPDATE preprocess_jobs SET status='failed', completed_at=?, error_message=? WHERE preprocess_id=?",
                (utc_now(), "Pre-upscale completed without an output image", preprocess_id),
            )
        else:
            db_execute(
                "UPDATE preprocess_jobs SET status='completed', completed_at=?, error_message=NULL, output_json=? WHERE preprocess_id=?",
                (utc_now(), json.dumps(output_image), preprocess_id),
            )
    COMFY_PROGRESS.pop(prompt_id, None)
    return public_preprocess(db_preprocess(preprocess_id) or row)


def timestamp_value(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


async def cleanup_expired_preprocesses(now: datetime | None = None) -> int:
    """Remove abandoned pre-upscale files while preserving recent refresh recovery."""
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = current_time - PREPROCESS_RETENTION
    with closing(sqlite3.connect(DATABASE)) as db, db:
        db.row_factory = sqlite3.Row
        rows = [dict(row) for row in db.execute("SELECT * FROM preprocess_jobs ORDER BY submitted_at")]
    removed = 0
    for original in rows:
        row = original
        queued_task = db_queue_record("preprocess", row["preprocess_id"])
        if queued_task and queued_task["state"] in QUEUE_ACTIVE_STATES:
            continue
        was_active = row["status"] in {"queued", "running"}
        if was_active:
            try:
                await refresh_preprocess(row["preprocess_id"])
                row = db_preprocess(row["preprocess_id"]) or row
            except Exception:
                # ComfyUI may be restarting. Do not remove files that could still
                # be in use; the next sweep will retry reconciliation.
                row = db_preprocess(row["preprocess_id"]) or row
        reference = row.get("submitted_at") if was_active else row.get("completed_at") or row.get("submitted_at")
        recorded_at = timestamp_value(reference) or timestamp_value(row.get("submitted_at"))
        if not recorded_at or recorded_at > cutoff:
            continue
        if row["status"] in {"queued", "running"}:
            try:
                await cancel_postprocess("preprocess", row["preprocess_id"])
            except Exception:
                continue
        try:
            await delete_preprocess(row["preprocess_id"])
            removed += 1
        except HTTPException as exc:
            if exc.status_code != 404:
                continue
    return removed


async def preprocess_cleanup_loop() -> None:
    while True:
        try:
            await cleanup_expired_preprocesses()
            cleanup_expired_chat_assets()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Cleanup must never take down the mobile service.
            pass
        await asyncio.sleep(PREPROCESS_CLEANUP_INTERVAL_SECONDS)


async def refresh_detail(detail_id: str) -> dict[str, Any]:
    row = db_detail(detail_id)
    if not row:
        raise HTTPException(404, "Flux detail job not found")
    if row["status"] in {"completed", "failed", "cancelled"}:
        return public_detail(row)
    prompt_id = row.get("prompt_id")
    history = await comfy_json("GET", f"/history/{prompt_id}")
    item = history.get(prompt_id)
    if not item:
        queue = await comfy_json("GET", "/queue")
        state = "running" if any(job[1] == prompt_id for job in queue.get("queue_running", [])) else "queued"
        db_execute("UPDATE detail_jobs SET status=? WHERE detail_id=?", (state, detail_id))
        return public_detail(db_detail(detail_id) or row)
    status = item.get("status", {})
    if status.get("status_str") != "success":
        db_execute(
            "UPDATE detail_jobs SET status='failed', completed_at=?, error_message=? WHERE detail_id=?",
            (utc_now(), str(status.get("messages") or "ComfyUI execution failed"), detail_id),
        )
    else:
        output_image = None
        for output in item.get("outputs", {}).values():
            for image in output.get("images", []):
                candidate = {key: image.get(key, "") for key in ("filename", "subfolder", "type")}
                if image.get("type") == "output" and safe_output_path(candidate):
                    output_image = candidate
        if not output_image:
            db_execute(
                "UPDATE detail_jobs SET status='failed', completed_at=?, error_message=? WHERE detail_id=?",
                (utc_now(), "Flux detail completed without an output image", detail_id),
            )
        else:
            db_execute(
                "UPDATE detail_jobs SET status='completed', completed_at=?, error_message=NULL, output_json=? WHERE detail_id=?",
                (utc_now(), json.dumps(output_image), detail_id),
            )
            # A new successful detail result defines a new linear chain. Only
            # then remove the previous detail and every now-stale upscale.
            remove_upscale_records(row["parent_group_id"], row["parent_item_id"])
            for previous in db_item_details(row["parent_group_id"], row["parent_item_id"]):
                if previous["detail_id"] == detail_id:
                    continue
                try:
                    previous_output = json.loads(previous.get("output_json") or "{}")
                    previous_source = json.loads(previous.get("source_image_json") or "{}")
                except json.JSONDecodeError:
                    previous_output, previous_source = {}, {}
                if isinstance(previous_output, dict) and previous_output.get("filename"):
                    remove_output_files([previous_output])
                previous_source_path = safe_source_path(previous_source) if isinstance(previous_source, dict) else None
                if previous_source_path and previous_source_path.is_file():
                    previous_source_path.unlink()
                db_execute("DELETE FROM detail_jobs WHERE detail_id=?", (previous["detail_id"],))
    source_record = json.loads(row.get("source_image_json") or "{}")
    source_path = safe_source_path(source_record)
    if source_path and source_path.is_file():
        source_path.unlink()
    return public_detail(db_detail(detail_id) or row)


async def refresh_group_upscales(group_id: str) -> None:
    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        pending = [dict(row) for row in db.execute(
            "SELECT * FROM upscale_jobs WHERE parent_group_id=? AND status IN ('queued','running')",
            (group_id,),
        ).fetchall()]
    for row in pending:
        await refresh_upscale(row["upscale_id"])


async def refresh_group_details(group_id: str) -> None:
    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        pending = [dict(row) for row in db.execute(
            "SELECT * FROM detail_jobs WHERE parent_group_id=? AND status IN ('queued','running')",
            (group_id,),
        ).fetchall()]
    for row in pending:
        await refresh_detail(row["detail_id"])


async def refresh_group(group_id: str) -> dict[str, Any]:
    row = db_group(group_id)
    if not row:
        raise HTTPException(404, "Generation group not found")
    children = db_group_jobs(group_id)
    if not children:
        if row["status"] in {"failed", "cancelled"}:
            return public_group(row)
        raise HTTPException(404, "Generation group has no jobs")
    refreshed = [await refresh_job(child["prompt_id"]) for child in children]
    items = items_from_children(row["workflow_key"], refreshed)
    images = outputs_from_items(items)
    states = [child["status"] for child in refreshed]
    if all(state == "completed" for state in states):
        state, completed_at, error = "completed", utc_now(), None
    elif all(state in {"completed", "failed", "cancelled"} for state in states):
        state, completed_at = ("failed" if "failed" in states else "cancelled"), utc_now()
        if state == "failed":
            error = "; ".join(child["error"] or "ComfyUI execution failed" for child in refreshed if child["status"] == "failed")
        else:
            error = "用户已停止任务"
    else:
        state = "running" if "running" in states else "queued"
        completed_at, error = None, None
    db_execute(
        "UPDATE generation_groups SET status=?, completed_at=?, error_message=?, outputs_json=?, items_json=? WHERE group_id=?",
        (state, completed_at, error, json.dumps(images), json.dumps(items), group_id),
    )
    return public_group(db_group(group_id) or row)


def queue_prompt_ids(entries: Any) -> set[str]:
    """Extract prompt ids from ComfyUI's queue tuples without trusting their shape."""
    result: set[str] = set()
    for entry in entries or []:
        if isinstance(entry, (list, tuple)) and len(entry) > 1:
            result.add(str(entry[1]))
    return result


def comfy_prompt_missing_long_enough(prompt_id: str, known_ids: set[str], grace_seconds: float = 30.0) -> bool:
    """Require a continuous queue/history gap before treating a prompt as lost."""
    record = COMFY_PROGRESS.setdefault(prompt_id, {})
    if prompt_id in known_ids:
        record.pop("_missing_since", None)
        return False
    now = time.monotonic()
    missing_since = record.setdefault("_missing_since", now)
    return now - float(missing_since) >= grace_seconds


async def cancel_group(group_id: str) -> dict[str, Any]:
    group = db_group(group_id)
    if not group or group.get("storage_scope") != "mobile":
        raise HTTPException(404, "Generation group not found")
    queued = db_queue_record("generation", group_id)
    if queued and queued["state"] in QUEUE_ACTIVE_STATES:
        await cancel_queued_task(queued)
        return public_group(db_group(group_id) or group)
    children = db_group_jobs(group_id)
    active_ids = [child["prompt_id"] for child in children if child["status"] in {"queued", "running"}]
    if not active_ids:
        raise HTTPException(409, "Task is no longer running")

    queue = await comfy_json("GET", "/queue")
    running_ids = queue_prompt_ids(queue.get("queue_running", []))
    queued_ids = queue_prompt_ids(queue.get("queue_pending", queue.get("queue_queued", [])))
    queued_targets = [prompt_id for prompt_id in active_ids if prompt_id in queued_ids or prompt_id not in running_ids]
    if queued_targets:
        await comfy_json("POST", "/queue", json={"delete": queued_targets})
    if any(prompt_id in running_ids for prompt_id in active_ids):
        await comfy_json("POST", "/interrupt", json={})

    now = utc_now()
    for prompt_id in active_ids:
        await reconcile_generation_prompt_outputs(prompt_id)
        db_execute(
            "UPDATE jobs SET status='cancelled', completed_at=?, error_message=? WHERE prompt_id=? AND status IN ('queued','running')",
            (now, "用户已停止任务", prompt_id),
        )
        COMFY_PROGRESS.pop(prompt_id, None)
    refreshed = db_group_jobs(group_id)
    items = items_from_children(group["workflow_key"], refreshed)
    db_execute(
        "UPDATE generation_groups SET status='cancelled', completed_at=?, error_message=?, outputs_json=?, items_json=? WHERE group_id=?",
        (now, "用户已停止任务", json.dumps(outputs_from_items(items)), json.dumps(items), group_id),
    )
    return public_group(db_group(group_id) or group)


async def cancel_postprocess(kind: str, record_id: str) -> dict[str, Any]:
    if kind == "detail":
        row, table, id_column = db_detail(record_id), "detail_jobs", "detail_id"
    elif kind == "upscale":
        row, table, id_column = db_upscale(record_id), "upscale_jobs", "upscale_id"
    elif kind == "preprocess":
        row, table, id_column = db_preprocess(record_id), "preprocess_jobs", "preprocess_id"
    else:
        raise HTTPException(404, "Post-processing job not found")
    if not row:
        raise HTTPException(404, "Post-processing job not found")
    queue_kind = "preprocess" if kind == "preprocess" else "upscale"
    queued = db_queue_record(queue_kind, record_id)
    if queued and queued["state"] in QUEUE_ACTIVE_STATES:
        await cancel_queued_task(queued)
        if kind == "upscale":
            return public_upscale(db_upscale(record_id) or row)
        if kind == "detail":
            return public_detail(db_detail(record_id) or row)
        return public_preprocess(db_preprocess(record_id) or row)
    if row["status"] not in {"queued", "running"}:
        raise HTTPException(409, "Post-processing is no longer running")
    prompt_id = row.get("prompt_id")
    queue = await comfy_json("GET", "/queue")
    running_ids = queue_prompt_ids(queue.get("queue_running", []))
    if prompt_id in running_ids:
        await comfy_json("POST", "/interrupt", json={})
    else:
        await comfy_json("POST", "/queue", json={"delete": [prompt_id]})
    db_execute(
        f"UPDATE {table} SET status='cancelled', completed_at=?, error_message=? WHERE {id_column}=?",
        (utc_now(), "用户已停止任务", record_id),
    )
    COMFY_PROGRESS.pop(prompt_id, None)
    try:
        source = json.loads(row.get("source_image_json") or "{}")
    except json.JSONDecodeError:
        source = {}
    source_path = safe_source_path(source) if isinstance(source, dict) else None
    if source_path and source_path.is_file() and not (kind == "upscale" and row.get("source_kind") == "standalone"):
        source_path.unlink()
    if kind == "detail":
        return public_detail(db_detail(record_id) or row)
    if kind == "upscale":
        return public_upscale(db_upscale(record_id) or row)
    return public_preprocess(db_preprocess(record_id) or row)


async def finalize_cancelled_task(row: dict[str, Any]) -> None:
    now = utc_now()
    prompt_id = str(row.get("prompt_id") or "")
    if row["task_kind"] == "generation":
        if prompt_id:
            await reconcile_generation_prompt_outputs(prompt_id)
        if prompt_id:
            db_execute(
                "UPDATE jobs SET status='cancelled',completed_at=?,error_message='用户已停止任务' "
                "WHERE prompt_id=? AND status IN ('queued','running','cancelling')",
                (now, prompt_id),
            )
        group = unrestricted_group(row["record_id"])
        children = db_group_jobs(row["record_id"])
        items = items_from_children(group["workflow_key"], children) if group else []
        db_execute(
            "UPDATE generation_groups SET status='cancelled',completed_at=?,error_message='用户已停止任务',"
            "outputs_json=?,items_json=? WHERE group_id=?",
            (now, json.dumps(outputs_from_items(items)), json.dumps(items), row["record_id"]),
        )
    else:
        set_business_status(row["task_kind"], row["record_id"], "cancelled", "用户已停止任务")
        if row["task_kind"] == "prompt_tool":
            business = unrestricted_prompt_tool(row["record_id"])
        elif row["task_kind"] == "preprocess":
            business = unrestricted_preprocess(row["record_id"])
        else:
            business = unrestricted_upscale(row["record_id"])
        keep_source = bool(business and row["task_kind"] == "upscale" and business.get("source_kind") == "standalone")
        source = source_image_from(business) if business else None
        source_path = safe_source_path(source) if source else None
        if source_path and source_path.is_file() and not keep_source:
            source_path.unlink()
    if prompt_id:
        COMFY_PROGRESS.pop(prompt_id, None)
    complete_queue_task(row, "cancelled", "用户已停止任务")


async def request_comfy_cancel(row: dict[str, Any]) -> bool:
    """Send at most one global interrupt and report whether Comfy owns the prompt."""
    prompt_id = str(row.get("prompt_id") or "")
    if not prompt_id:
        return False
    queue = await comfy_json("GET", "/queue")
    running_ids = queue_prompt_ids(queue.get("queue_running", []))
    pending_ids = queue_prompt_ids(queue.get("queue_pending", queue.get("queue_queued", [])))
    if prompt_id not in running_ids and prompt_id not in pending_ids:
        return False
    # ComfyUI's /interrupt endpoint is global and model-loading nodes may not
    # observe it until they return. Repeating it only floods the UI/log and
    # cannot make an uninterruptible loader stop sooner.
    if row.get("last_interrupt_at"):
        return True
    attempt_at = utc_now()
    db_execute(
        "UPDATE task_queue SET last_interrupt_at=?,interrupted_count=interrupted_count+1 WHERE queue_id=?",
        (attempt_at, row["queue_id"]),
    )
    if prompt_id in pending_ids:
        await comfy_json("POST", "/queue", json={"delete": [prompt_id]})
    else:
        await comfy_json("POST", "/interrupt", json={})
    db_execute(
        "UPDATE task_queue SET progress_phase='中断请求已发送 · 等待 ComfyUI 退出当前节点',error_message=NULL WHERE queue_id=?",
        (row["queue_id"],),
    )
    return True


async def cancel_queued_task(row: dict[str, Any]) -> None:
    token = CURRENT_USER.set(None)
    try:
        async with queue_lock():
            current = queue_row(int(row["queue_id"]))
            if not current or current["state"] not in QUEUE_ACTIVE_STATES:
                raise HTTPException(409, "Task is no longer active")
            prompt_id = str(current.get("prompt_id") or "")
            if not prompt_id:
                await finalize_cancelled_task(current)
                return
            now = utc_now()
            db_execute(
                "UPDATE task_queue SET state='cancelling',cancel_requested_at=COALESCE(cancel_requested_at,?),"
                "progress_phase='正在停止 ComfyUI 任务',progress_approximate=1,error_message=NULL WHERE queue_id=?",
                (now, current["queue_id"]),
            )
            set_business_status(current["task_kind"], current["record_id"], "cancelling")
            if current["task_kind"] == "generation":
                db_execute(
                    "UPDATE jobs SET status='cancelling',completed_at=NULL,error_message=NULL "
                    "WHERE prompt_id=? AND status IN ('queued','running')",
                    (prompt_id,),
                )
            current = queue_row(int(current["queue_id"])) or current
            try:
                still_present = await request_comfy_cancel(current)
            except HTTPException as exc:
                db_execute(
                    "UPDATE task_queue SET error_message=?,progress_phase='停止请求失败，正在重试' WHERE queue_id=?",
                    (str(exc.detail), current["queue_id"]),
                )
                return
            if not still_present:
                await finalize_cancelled_task(current)
    finally:
        CURRENT_USER.reset(token)


def queue_lock() -> asyncio.Lock:
    global SCHEDULER_LOCK
    if SCHEDULER_LOCK is None:
        SCHEDULER_LOCK = asyncio.Lock()
    return SCHEDULER_LOCK


def record_image_name(image: dict[str, Any] | None) -> str | None:
    if not image or not image.get("filename"):
        return None
    return f"{image.get('subfolder','')}/{image['filename']}".strip("/")


def tag_output_prefixes(graph: dict[str, Any], record_id: str) -> None:
    safe_id = "".join(char for char in record_id if char.isalnum() or char in {"-", "_"})
    for node in graph.values():
        if node.get("class_type") != "SaveImage":
            continue
        inputs = node.setdefault("inputs", {})
        prefix = str(inputs.get("filename_prefix") or "mobile/output/Image")
        inputs["filename_prefix"] = f"{prefix}_{safe_id}"


def recover_postprocess_file(record_id: str) -> dict[str, str] | None:
    candidates = [
        path for path in MOBILE_OUTPUT_DIR.rglob("*")
        if path.is_file() and record_id in path.name and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
    ] if MOBILE_OUTPUT_DIR.exists() else []
    return image_record_from_path(max(candidates, key=lambda path: path.stat().st_mtime)) if candidates else None


def queue_row(queue_id: int) -> dict[str, Any] | None:
    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM task_queue WHERE queue_id=?", (queue_id,)).fetchone()
    return dict(row) if row else None


def set_business_status(task_kind: str, record_id: str, status: str, error: str | None = None) -> None:
    table, column = {
        "generation": ("generation_groups", "group_id"),
        "preprocess": ("preprocess_jobs", "preprocess_id"),
        "upscale": ("upscale_jobs", "upscale_id"),
        "prompt_tool": ("prompt_tool_jobs", "tool_id"),
    }[task_kind]
    completed_at = utc_now() if status in {"completed", "failed", "cancelled"} else None
    db_execute(
        f"UPDATE {table} SET status=?,completed_at=?,error_message=? WHERE {column}=?",
        (status, completed_at, error, record_id),
    )


def complete_queue_task(row: dict[str, Any], state: str, error: str | None = None) -> None:
    clear_h3_sampler_heartbeat(str(row.get("prompt_id") or ""))
    db_execute(
        "UPDATE task_queue SET state=?,finished_at=?,error_message=?,prompt_id=NULL,progress_value=?,progress_phase=?,progress_approximate=0 WHERE queue_id=?",
        (state, utc_now(), error, 100 if state == "completed" else row.get("progress_value", 0), "处理完成" if state == "completed" else state, row["queue_id"]),
    )


async def reconcile_orphaned_generation_task(row: dict[str, Any]) -> bool:
    """Close a queue row whose generation group or active child was deleted."""
    if row.get("task_kind") != "generation":
        return False
    prompt_id = str(row.get("prompt_id") or "")
    if unrestricted_group(row["record_id"]) and (not prompt_id or db_job(prompt_id)):
        return False
    if not prompt_id:
        complete_queue_task(row, "cancelled", "Generation task was deleted")
        return True
    history = await comfy_json("GET", f"/history/{prompt_id}")
    history_item = history.get(prompt_id)
    if history_item:
        status = history_item.get("status", {})
        if status.get("status_str") == "success":
            complete_queue_task(row, "completed")
        else:
            complete_queue_task(row, "failed", str(status.get("messages") or "ComfyUI execution failed"))
        return True
    queue = await comfy_json("GET", "/queue")
    running_ids = queue_prompt_ids(queue.get("queue_running", []))
    pending_ids = queue_prompt_ids(queue.get("queue_pending", queue.get("queue_queued", [])))
    if prompt_id in running_ids or prompt_id in pending_ids:
        now = utc_now()
        db_execute(
            "UPDATE task_queue SET state='cancelling',cancel_requested_at=COALESCE(cancel_requested_at,?),"
            "progress_phase='任务记录已删除 · 正在停止 ComfyUI',progress_approximate=1,error_message=NULL WHERE queue_id=?",
            (now, row["queue_id"]),
        )
        current = queue_row(int(row["queue_id"])) or row
        still_present = await request_comfy_cancel(current)
        if not still_present:
            await finalize_cancelled_task(current)
        return True
    complete_queue_task(row, "cancelled", "Generation task was deleted")
    return True


def generation_expected(group: dict[str, Any]) -> int:
    values = json.loads(group.get("parameters_json") or "{}")
    return max(1, int(values.get("count", 1)))


def finish_generation_group(group: dict[str, Any]) -> None:
    children = db_group_jobs(group["group_id"])
    items = items_from_children(group["workflow_key"], children)
    compact_group_deleted_seeds(group["group_id"])
    db_execute(
        "UPDATE generation_groups SET status='completed',completed_at=?,error_message=NULL,outputs_json=?,items_json=? WHERE group_id=?",
        (utc_now(), json.dumps(outputs_from_items(items)), json.dumps(items), group["group_id"]),
    )


async def release_krea_runtime(group: dict[str, Any]) -> None:
    """Fully release Krea model/node caches after a generation group."""
    if group.get("workflow_key") != "krea-identity-edit":
        return
    try:
        await comfy_json(
            "POST", "/free",
            json={"unload_models": True, "free_memory": True},
        )
    except HTTPException:
        pass


async def post_comfy_prompt(graph: dict[str, Any], editor_workflow: dict[str, Any]) -> str:
    validate_dependencies(graph, await comfy_json("GET", "/object_info"))
    response = await comfy_json(
        "POST", "/prompt",
        json={"prompt": graph, "client_id": COMFY_CLIENT_ID, "extra_data": {"extra_pnginfo": {"workflow": editor_workflow}}},
    )
    prompt_id = str(response.get("prompt_id") or "")
    if not prompt_id:
        raise HTTPException(502, "ComfyUI did not return a task id")
    node_errors = response.get("node_errors")
    if isinstance(node_errors, dict) and node_errors:
        # ComfyUI may accept a prompt when one output branch validates and a
        # different branch (including SaveImage) does not.  Do not report that
        # partial execution as a real task: remove it before it wastes compute
        # and surface the actual validation failure immediately.
        try:
            await comfy_json("POST", "/queue", json={"delete": [prompt_id]})
        except HTTPException:
            pass
        first = next(iter(node_errors.values()), {})
        messages = first.get("errors") if isinstance(first, dict) else None
        detail = ""
        if isinstance(messages, list) and messages:
            message = messages[0]
            if isinstance(message, dict):
                detail = " — ".join(str(message[key]) for key in ("message", "details") if message.get(key))
        suffix = f": {detail}" if detail else ""
        raise HTTPException(400, f"ComfyUI workflow validation failed{suffix}")
    register_comfy_prompt(prompt_id, graph)
    return prompt_id


async def dispatch_generation(row: dict[str, Any]) -> str | None:
    group = unrestricted_group(row["record_id"])
    if not group:
        raise HTTPException(404, "Generation task no longer exists")
    children = db_group_jobs(group["group_id"])
    completed = [child for child in children if child["status"] == "completed"]
    if len(completed) >= generation_expected(group):
        finish_generation_group(group)
        await release_krea_runtime(group)
        complete_queue_task(row, "completed")
        return None
    logical_index = len(completed)
    values = json.loads(group.get("parameters_json") or "{}")
    spec = get_spec(group["workflow_key"])
    if spec.kind == "image_edit":
        seeds = values.get("stage1_seeds") or []
        if logical_index < len(seeds):
            values["stage1_seed"] = int(seeds[logical_index])
            values["stage1_random_seed"] = False
    else:
        # Text-to-image batches can saturate VRAM and provide no intermediate
        # progress. Dispatch one image at a time so each result is recoverable.
        values["batch"] = 1
        seeds = values.get("generation_seeds") or []
        if logical_index < len(seeds):
            values["seed"] = int(seeds[logical_index])
            values["random_seed"] = False
    image = json_image_from(group, "edit_source_image_json") or source_image_from(group)
    image_name = record_image_name(image)
    if spec.image_node and not image_name:
        raise HTTPException(409, "Source image is no longer available")
    reference_image = json_image_from(group, "reference_image_json")
    reference_image_name = record_image_name(reference_image)
    edit_mask = json_image_from(group, "edit_mask_json")
    edit_mask_name = (
        record_image_name(edit_mask)
        if values.get("mask_mode") == "krea_ref_boost"
        else None
    )
    graph, editor_workflow = build_graph(
        spec, values, image_name, reference_image_name, edit_mask_name,
    )
    tag_output_prefixes(graph, group["group_id"])
    preprocess_data = group_preprocess(group)
    original = source_image_from(group)
    if preprocess_data and original:
        apply_pre_edit_color_match(graph, record_image_name(original) or "")
    prompt_id = await post_comfy_prompt(graph, editor_workflow)
    now = utc_now()
    db_execute(
        "INSERT INTO jobs(prompt_id,workflow_key,prompt_text,source_prompt_text,title,status,submitted_at,parameters_json,storage_scope,group_id,owner_id,logical_index,attempt_no,superseded) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
        (prompt_id, spec.key, group["prompt_text"], group.get("source_prompt_text") or "", group.get("title") or "", "queued", now, json.dumps(values), "mobile", group["group_id"], group["owner_id"], logical_index, int(row["attempt"]) + 1),
    )
    db_execute("UPDATE generation_groups SET status='running',completed_at=NULL,error_message=NULL WHERE group_id=?", (group["group_id"],))
    return prompt_id


async def dispatch_preprocess(row: dict[str, Any]) -> str:
    raise HTTPException(410, "编辑前 SeedVR2 预放大已下线")


async def dispatch_upscale(row: dict[str, Any]) -> str:
    job = unrestricted_upscale(row["record_id"])
    if not job:
        raise HTTPException(404, "Upscale task no longer exists")
    values = json.loads(job.get("parameters_json") or "{}")
    image_name = record_image_name(source_image_from(job))
    if not image_name:
        raise HTTPException(409, "Upscale source image is unavailable")
    if (job.get("engine") or "seedvr2") != "flux2":
        raise HTTPException(410, "独立 SeedVR2 后放大已下线")
    graph, editor_workflow = build_enhanced_upscale_graph(values, image_name)
    tag_output_prefixes(graph, job["upscale_id"])
    prompt_id = await post_comfy_prompt(graph, editor_workflow)
    db_execute("UPDATE upscale_jobs SET prompt_id=?,status='running',completed_at=NULL,error_message=NULL WHERE upscale_id=?", (prompt_id, job["upscale_id"]))
    return prompt_id


async def dispatch_prompt_tool(row: dict[str, Any]) -> str:
    raise HTTPException(410, "提示词助手已从发布版移除")


async def dispatch_claimed_task(row: dict[str, Any]) -> None:
    try:
        if row["task_kind"] == "generation":
            prompt_id = await dispatch_generation(row)
        elif row["task_kind"] == "preprocess":
            prompt_id = await dispatch_preprocess(row)
        elif row["task_kind"] == "prompt_tool":
            prompt_id = await dispatch_prompt_tool(row)
        else:
            prompt_id = await dispatch_upscale(row)
        if prompt_id:
            db_execute(
                "UPDATE task_queue SET state='running',prompt_id=?,started_at=COALESCE(started_at,?),item_started_at=?,attempt=attempt+1,error_message=NULL WHERE queue_id=?",
                (prompt_id, utc_now(), utc_now(), row["queue_id"]),
            )
    except Exception as exc:
        message = str(exc.detail) if isinstance(exc, HTTPException) else str(exc)
        current = queue_row(int(row["queue_id"])) or row
        if isinstance(exc, HTTPException) and exc.status_code == 503:
            db_execute(
                "UPDATE task_queue SET state='waiting',prompt_id=NULL,item_started_at=NULL,error_message=? WHERE queue_id=?",
                (message, row["queue_id"]),
            )
            return
        attempts = int(current.get("attempt") or 0) + 1
        if attempts >= 3 or isinstance(exc, HTTPException) and exc.status_code in {400, 404, 409, 410, 422}:
            complete_queue_task(current, "failed", message)
            set_business_status(row["task_kind"], row["record_id"], "failed", message)
            if row["task_kind"] == "prompt_tool":
                tool = unrestricted_prompt_tool(row["record_id"])
                if tool:
                    remove_prompt_tool_source(tool)
        else:
            db_execute(
                "UPDATE task_queue SET state='waiting',prompt_id=NULL,item_started_at=NULL,attempt=?,error_message=?,progress_value=0,progress_phase='任务已重新开始',progress_approximate=1 WHERE queue_id=?",
                (attempts, message, row["queue_id"]),
            )


async def fail_timed_out_task(row: dict[str, Any], reason: str) -> None:
    """Interrupt a timed-out Comfy prompt and close its persistent queue record."""
    prompt_id = str(row.get("prompt_id") or "")
    if prompt_id:
        try:
            queue = await comfy_json("GET", "/queue")
            running_ids = queue_prompt_ids(queue.get("queue_running", []))
            pending_ids = queue_prompt_ids(queue.get("queue_pending", queue.get("queue_queued", [])))
            if prompt_id in pending_ids:
                await comfy_json("POST", "/queue", json={"delete": [prompt_id]})
            elif prompt_id in running_ids:
                await comfy_json("POST", "/interrupt", json={})
        except HTTPException:
            pass
        COMFY_PROGRESS.pop(prompt_id, None)
    now = utc_now()
    if row["task_kind"] == "generation":
        if prompt_id:
            db_execute(
                "UPDATE jobs SET status='failed',completed_at=?,error_message=? WHERE prompt_id=? AND status IN ('queued','running')",
                (now, reason, prompt_id),
            )
        group = unrestricted_group(row["record_id"])
        children = db_group_jobs(row["record_id"])
        items = items_from_children(group["workflow_key"], children) if group else []
        db_execute(
            "UPDATE generation_groups SET status='failed',completed_at=?,error_message=?,outputs_json=?,items_json=? WHERE group_id=?",
            (now, reason, json.dumps(outputs_from_items(items)), json.dumps(items), row["record_id"]),
        )
    else:
        set_business_status(row["task_kind"], row["record_id"], "failed", reason)
        if row["task_kind"] == "prompt_tool":
            tool = unrestricted_prompt_tool(row["record_id"])
            if tool:
                remove_prompt_tool_source(tool)
    complete_queue_task(row, "failed", reason)


async def reconcile_running_task(row: dict[str, Any]) -> None:
    prompt_id = row.get("prompt_id")
    if await reconcile_orphaned_generation_task(row):
        return
    if not prompt_id:
        db_execute("UPDATE task_queue SET state='waiting',item_started_at=NULL WHERE queue_id=?", (row["queue_id"],))
        return
    if h3_first_sample_stalled(row):
        await recover_h3_stall(row, "MiniMax H3 did not reach the sampler watchdog stage within 3 minutes")
        return
    if h3_sampler_heartbeat_stalled(row):
        await recover_h3_stall(row, "MiniMax H3 sampler heartbeat stopped for 3 minutes")
        return
    timeout_reason = running_task_timeout_reason(row)
    if timeout_reason:
        # If H3 never produced a sampler heartbeat, its regular hard limit is
        # the fallback unattended watchdog. Recovery frees a wedged GPU for the
        # next queued task rather than merely closing this database record.
        if str(row.get("profile_key") or "").startswith("generation:minimax-h3:"):
            await recover_h3_stall(row, timeout_reason)
            return
        await fail_timed_out_task(row, timeout_reason)
        return
    if row["task_kind"] == "prompt_tool":
        result = await refresh_prompt_tool(row["record_id"])
        if result["status"] in QUEUE_TERMINAL_STATES:
            complete_queue_task(row, result["status"], result.get("error"))
        elif result["status"] == "queued":
            queue = await comfy_json("GET", "/queue")
            known = queue_prompt_ids(queue.get("queue_running", [])) | queue_prompt_ids(queue.get("queue_pending", queue.get("queue_queued", [])))
            started = timestamp_value(row.get("started_at"))
            if prompt_id not in known and started and datetime.now(timezone.utc) - started > timedelta(seconds=30):
                db_execute("UPDATE prompt_tool_jobs SET prompt_id=NULL,status='queued',completed_at=NULL,error_message=NULL WHERE tool_id=?", (row["record_id"],))
                db_execute(
                    "UPDATE task_queue SET state='waiting',prompt_id=NULL,item_started_at=NULL,interrupted_count=interrupted_count+1,progress_value=0,progress_phase='任务已重新开始',progress_approximate=1 WHERE queue_id=?",
                    (row["queue_id"],),
                )
        return
    if row["task_kind"] == "generation":
        result = await refresh_job(prompt_id)
        if result["status"] == "completed":
            group = unrestricted_group(row["record_id"])
            if not group:
                complete_queue_task(row, "failed", "Generation task disappeared")
            else:
                children = db_group_jobs(group["group_id"])
                adopted = next((child for child in children if child["status"] in {"queued", "running"}), None)
                if adopted:
                    db_execute(
                        "UPDATE task_queue SET state='running',prompt_id=?,item_started_at=? WHERE queue_id=?",
                        (adopted["prompt_id"], adopted.get("submitted_at") or utc_now(), row["queue_id"]),
                    )
                    return
            if group and len([child for child in db_group_jobs(group["group_id"]) if child["status"] == "completed"]) >= generation_expected(group):
                finish_generation_group(group)
                await release_krea_runtime(group)
                complete_queue_task(row, "completed")
            elif group:
                # Consecutive images in one group use the same model, source,
                # resolution and graph; only the seed changes. Keeping that
                # graph resident avoids a full Krea2 unload/reload for every
                # image—the repeated reload was itself able to stall during
                # model initialization. A full release still happens when the
                # group completes, before an independent task can start.
                db_execute("UPDATE task_queue SET state='dispatching',prompt_id=NULL,item_started_at=NULL,progress_value=0,progress_phase='',progress_approximate=1 WHERE queue_id=?", (row["queue_id"],))
                await dispatch_claimed_task(queue_row(int(row["queue_id"])) or row)
        elif result["status"] in {"failed", "cancelled"}:
            complete_queue_task(row, result["status"], result.get("error"))
            set_business_status("generation", row["record_id"], result["status"], result.get("error"))
        elif result["status"] == "queued":
            queue = await comfy_json("GET", "/queue")
            known = queue_prompt_ids(queue.get("queue_running", [])) | queue_prompt_ids(queue.get("queue_pending", queue.get("queue_queued", [])))
            if comfy_prompt_missing_long_enough(str(prompt_id), known):
                db_execute("UPDATE jobs SET superseded=1,status='cancelled',completed_at=?,error_message='服务重启后重新运行' WHERE prompt_id=?", (utc_now(), prompt_id))
                db_execute("UPDATE generation_groups SET status='queued',completed_at=NULL,error_message=NULL WHERE group_id=?", (row["record_id"],))
                db_execute("UPDATE task_queue SET state='waiting',prompt_id=NULL,item_started_at=NULL,interrupted_count=interrupted_count+1,progress_value=0,progress_phase='任务已重新开始',progress_approximate=1 WHERE queue_id=?", (row["queue_id"],))
        return
    result = await (refresh_preprocess(row["record_id"]) if row["task_kind"] == "preprocess" else refresh_upscale(row["record_id"]))
    if result["status"] in QUEUE_TERMINAL_STATES:
        complete_queue_task(row, result["status"], result.get("error"))
    elif result["status"] == "queued":
        queue = await comfy_json("GET", "/queue")
        known = queue_prompt_ids(queue.get("queue_running", [])) | queue_prompt_ids(queue.get("queue_pending", queue.get("queue_queued", [])))
        started = timestamp_value(row.get("started_at"))
        if prompt_id not in known and started and datetime.now(timezone.utc) - started > timedelta(seconds=30):
            recovered = recover_postprocess_file(row["record_id"])
            if recovered:
                table, id_column = ("preprocess_jobs", "preprocess_id") if row["task_kind"] == "preprocess" else ("upscale_jobs", "upscale_id")
                db_execute(
                    f"UPDATE {table} SET status='completed',completed_at=?,error_message=NULL,output_json=? WHERE {id_column}=?",
                    (utc_now(), json.dumps(recovered), row["record_id"]),
                )
                complete_queue_task(row, "completed")
                return
            table, id_column = ("preprocess_jobs", "preprocess_id") if row["task_kind"] == "preprocess" else ("upscale_jobs", "upscale_id")
            db_execute(f"UPDATE {table} SET prompt_id=NULL,status='queued',completed_at=NULL,error_message=NULL WHERE {id_column}=?", (row["record_id"],))
            db_execute("UPDATE task_queue SET state='waiting',prompt_id=NULL,item_started_at=NULL,interrupted_count=interrupted_count+1,progress_value=0,progress_phase='任务已重新开始',progress_approximate=1 WHERE queue_id=?", (row["queue_id"],))


async def reconcile_cancelling_task(row: dict[str, Any]) -> None:
    prompt_id = str(row.get("prompt_id") or "")
    if not prompt_id:
        await finalize_cancelled_task(row)
        return
    try:
        queue = await comfy_json("GET", "/queue")
    except HTTPException as exc:
        db_execute(
            "UPDATE task_queue SET error_message=?,progress_phase='ComfyUI 不可用，停止请求等待重试' WHERE queue_id=?",
            (str(exc.detail), row["queue_id"]),
        )
        return
    running_ids = queue_prompt_ids(queue.get("queue_running", []))
    pending_ids = queue_prompt_ids(queue.get("queue_pending", queue.get("queue_queued", [])))
    if prompt_id not in running_ids and prompt_id not in pending_ids:
        await finalize_cancelled_task(row)
        if not running_ids and not pending_ids:
            try:
                await comfy_json("POST", "/free", json={"unload_models": True, "free_memory": True})
            except HTTPException:
                pass
        return
    now = datetime.now(timezone.utc)
    requested = timestamp_value(row.get("cancel_requested_at"))
    if not row.get("last_interrupt_at"):
        try:
            await request_comfy_cancel(row)
        except HTTPException as exc:
            db_execute("UPDATE task_queue SET error_message=? WHERE queue_id=?", (str(exc.detail), row["queue_id"]))
    if requested and now - requested >= timedelta(seconds=H3_CANCEL_RECOVERY_TIMEOUT_SECONDS):
        if str(row.get("profile_key") or "").startswith("generation:minimax-h3:"):
            await recover_h3_stall(row, "取消请求后 ComfyUI 仍未退出")
        else:
            db_execute(
                "UPDATE task_queue SET error_message='ComfyUI has not acknowledged the cancel request; restart it before continuing.',"
                "progress_phase='取消未响应 · 需要重启 ComfyUI' WHERE queue_id=?", (row["queue_id"],),
            )


def h3_recovery_attempts_in_window(settings_fingerprint: str, now: datetime | None = None) -> int:
    cutoff = (now or datetime.now(timezone.utc)).astimezone(timezone.utc) - timedelta(seconds=H3_RECOVERY_WINDOW_SECONDS)
    with sqlite3.connect(DATABASE) as db:
        return int(db.execute(
            "SELECT COUNT(*) FROM h3_recovery_events WHERE settings_fingerprint=? AND created_at>=?",
            (settings_fingerprint, cutoff.isoformat()),
        ).fetchone()[0])


def h3_parameters_for_queue(row: dict[str, Any]) -> dict[str, Any]:
    group = unrestricted_group(row["record_id"])
    if not group:
        return {}
    try:
        return json.loads(group.get("parameters_json") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def terminate_comfyui_listener() -> None:
    """Terminate only the configured local ComfyUI listener."""
    if SETTINGS.comfy_host not in {"127.0.0.1", "localhost"}:
        raise RuntimeError("automatic recovery is available only for local ComfyUI instances")
    command = (
        f"$p=Get-NetTCPConnection -LocalPort {SETTINGS.comfy_port} -State Listen -ErrorAction SilentlyContinue "
        "| Select-Object -ExpandProperty OwningProcess -Unique; "
        "if($p){$p | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction Stop }}"
    )
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command], check=True, timeout=20)


def start_comfyui_process() -> None:
    subprocess.Popen(
        comfyui_command(SETTINGS),
        cwd=COMFY_ROOT,
        env=prepare_environment(SETTINGS),
        creationflags=int(getattr(subprocess, "CREATE_NEW_CONSOLE", 0)),
    )


async def wait_for_comfyui_recovery() -> None:
    deadline = time.monotonic() + H3_RECOVERY_HEALTH_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            await comfy_json("GET", "/system_stats")
            queue = await comfy_json("GET", "/queue")
            active = queue_prompt_ids(queue.get("queue_running", [])) | queue_prompt_ids(queue.get("queue_pending", queue.get("queue_queued", [])))
            if not active:
                return
        except HTTPException:
            pass
        await asyncio.sleep(2)
    raise RuntimeError("ComfyUI did not become healthy within 2 minutes")


async def restart_comfyui_and_verify() -> None:
    """Restart the configured local ComfyUI service without stopping this WebUI."""
    await asyncio.to_thread(terminate_comfyui_listener)
    await asyncio.to_thread(start_comfyui_process)
    await wait_for_comfyui_recovery()


async def recover_h3_stall(row: dict[str, Any], reason: str) -> None:
    """Recover a non-interruptible H3 loader/sampler once, without touching WebUI."""
    if not str(row.get("profile_key") or "").startswith("generation:minimax-h3:"):
        return
    if comfy_recovery_status()["recovery_state"] == "recovering":
        return
    values = h3_parameters_for_queue(row)
    fingerprint = h3_settings_fingerprint(values)
    clear_h3_sampler_heartbeat(str(row.get("prompt_id") or ""))
    now = datetime.now(timezone.utc)
    freeze_this_setting = h3_recovery_attempts_in_window(fingerprint, now) >= 2
    if freeze_this_setting:
        blocked_until = now + timedelta(seconds=H3_RECOVERY_WINDOW_SECONDS)
        db_execute(
            "INSERT INTO h3_setting_cooldowns(settings_fingerprint,blocked_until,reason,created_at) VALUES(?,?,?,?) "
            "ON CONFLICT(settings_fingerprint) DO UPDATE SET blocked_until=excluded.blocked_until,reason=excluded.reason,created_at=excluded.created_at",
            (fingerprint, blocked_until.isoformat(), reason, utc_now()),
        )
        message = "MiniMax H3 stalled three times with the same runtime settings; those settings are paused for 30 minutes."
        db_execute("INSERT INTO h3_recovery_events(queue_id,record_id,settings_fingerprint,reason,created_at) VALUES(?,?,?,?,?)", (row["queue_id"], row["record_id"], fingerprint, reason, utc_now()))
        db_execute("UPDATE task_queue SET state='recovering',progress_phase='第三次卡死 · 正在恢复 ComfyUI 并冻结该参数',error_message=? WHERE queue_id=?", (message, row["queue_id"]))
        set_comfy_recovery_status("recovering", "MiniMax H3 同一参数第三次无响应，正在恢复 ComfyUI；仅该参数组合暂停 30 分钟。")
        try:
            await restart_comfyui_and_verify()
            current = queue_row(int(row["queue_id"])) or row
            old_prompt = str(current.get("prompt_id") or "")
            if old_prompt:
                db_execute("UPDATE jobs SET status='failed',completed_at=?,error_message=? WHERE prompt_id=? AND status IN ('queued','running','cancelling')", (utc_now(), message, old_prompt))
            db_execute("UPDATE generation_groups SET status='failed',completed_at=?,error_message=? WHERE group_id=?", (utc_now(), message, row["record_id"]))
            complete_queue_task(current, "failed", message)
            db_execute("UPDATE h3_recovery_events SET finished_at=?,outcome='blocked' WHERE recovery_id=(SELECT MAX(recovery_id) FROM h3_recovery_events)", (utc_now(),))
            set_comfy_recovery_status("idle", "")
        except Exception as exc:
            message = f"ComfyUI automatic recovery failed: {exc}"
            db_execute("UPDATE h3_recovery_events SET finished_at=?,outcome='failed' WHERE recovery_id=(SELECT MAX(recovery_id) FROM h3_recovery_events)", (utc_now(),))
            set_comfy_recovery_status("blocked", message)
            db_execute("UPDATE task_queue SET state='recovering',error_message=?,progress_phase='恢复失败 · 已冻结队列' WHERE queue_id=?", (message, row["queue_id"]))
        return

    db_execute("INSERT INTO h3_recovery_events(queue_id,record_id,settings_fingerprint,reason,created_at) VALUES(?,?,?,?,?)", (row["queue_id"], row["record_id"], fingerprint, reason, utc_now()))
    db_execute("UPDATE task_queue SET state='recovering',progress_phase='检测到无响应 · 正在恢复 ComfyUI',error_message=? WHERE queue_id=?", (reason, row["queue_id"]))
    set_comfy_recovery_status("recovering", "MiniMax H3 无响应，正在自动恢复 ComfyUI；WebUI 保持运行。")
    try:
        await restart_comfyui_and_verify()
        current = queue_row(int(row["queue_id"])) or row
        if row.get("state") == "cancelling":
            await finalize_cancelled_task(current)
        else:
            old_prompt = str(current.get("prompt_id") or "")
            if old_prompt:
                db_execute("UPDATE jobs SET superseded=1,status='failed',completed_at=?,error_message=? WHERE prompt_id=?", (utc_now(), reason, old_prompt))
            db_execute("UPDATE generation_groups SET status='queued',completed_at=NULL,error_message=NULL WHERE group_id=?", (row["record_id"],))
            db_execute("UPDATE task_queue SET state='waiting',prompt_id=NULL,started_at=NULL,item_started_at=NULL,cancel_requested_at=NULL,last_interrupt_at=NULL,recovery_retry_count=recovery_retry_count+1,interrupted_count=interrupted_count+1,progress_value=0,progress_phase='恢复完成 · 正在自动重排一次',progress_approximate=1,error_message=NULL WHERE queue_id=?", (row["queue_id"],))
        db_execute("UPDATE h3_recovery_events SET finished_at=?,outcome='recovered' WHERE recovery_id=(SELECT MAX(recovery_id) FROM h3_recovery_events)", (utc_now(),))
        set_comfy_recovery_status("idle", "")
    except Exception as exc:
        message = f"ComfyUI automatic recovery failed: {exc}"
        db_execute("UPDATE h3_recovery_events SET finished_at=?,outcome='failed' WHERE recovery_id=(SELECT MAX(recovery_id) FROM h3_recovery_events)", (utc_now(),))
        set_comfy_recovery_status("blocked", message)
        db_execute("UPDATE task_queue SET state='recovering',error_message=?,progress_phase='恢复失败 · 已冻结队列' WHERE queue_id=?", (message, row["queue_id"]))


async def adopt_orphaned_cancelled_prompt() -> None:
    """Recover legacy cancellations that cleared queue tracking too early."""
    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        candidates = db.execute(
            "SELECT j.prompt_id,j.group_id,q.queue_id FROM jobs j "
            "JOIN generation_groups g ON g.group_id=j.group_id "
            "LEFT JOIN task_queue q ON q.task_kind='generation' AND q.record_id=j.group_id "
            "WHERE g.status IN ('cancelled','cancelling') AND j.prompt_id IS NOT NULL "
            "AND (j.status IN ('cancelled','cancelling') OR q.state IN ('cancelled','cancelling'))"
        ).fetchall()
    if not candidates:
        return
    try:
        queue = await comfy_json("GET", "/queue")
    except HTTPException:
        return
    running_ids = queue_prompt_ids(queue.get("queue_running", []))
    if not running_ids:
        return
    orphan = next((row for row in candidates if str(row["prompt_id"]) in running_ids), None)
    if not orphan:
        return
    now = utc_now()
    if orphan["queue_id"] is not None:
        db_execute(
            "UPDATE task_queue SET state='cancelling',prompt_id=?,cancel_requested_at=COALESCE(cancel_requested_at,?),"
            "progress_phase='正在清理遗留 ComfyUI 任务',progress_approximate=1 WHERE queue_id=?",
            (orphan["prompt_id"], now, orphan["queue_id"]),
        )
    db_execute(
        "UPDATE generation_groups SET status='cancelling',completed_at=NULL,error_message=NULL WHERE group_id=?",
        (orphan["group_id"],),
    )
    db_execute(
        "UPDATE jobs SET status='cancelling',completed_at=NULL,error_message=NULL WHERE prompt_id=?",
        (orphan["prompt_id"],),
    )


async def scheduler_tick() -> None:
    token = CURRENT_USER.set(None)
    try:
        async with queue_lock():
            if performance_mode_enabled():
                return
            if comfy_recovery_status()["recovery_state"] != "idle":
                return
            await adopt_orphaned_cancelled_prompt()
            running = next((item for item in active_queue_rows() if item["state"] in {"running", "dispatching", "cancelling"}), None)
            if running:
                if running["state"] == "cancelling":
                    await reconcile_cancelling_task(running)
                elif running["state"] == "dispatching" and not running.get("prompt_id"):
                    await dispatch_claimed_task(running)
                else:
                    await reconcile_running_task(running)
                return
            try:
                comfy_queue = await comfy_json("GET", "/queue")
            except HTTPException:
                return
            if queue_prompt_ids(comfy_queue.get("queue_running", [])) or queue_prompt_ids(comfy_queue.get("queue_pending", comfy_queue.get("queue_queued", []))):
                return
            with sqlite3.connect(DATABASE) as db:
                db.row_factory = sqlite3.Row
                db.execute("BEGIN IMMEDIATE")
                candidate = db.execute(
                    "SELECT * FROM task_queue WHERE state IN ('waiting','paused') ORDER BY priority DESC,queue_id LIMIT 1"
                ).fetchone()
                if not candidate:
                    return
                db.execute("UPDATE task_queue SET state='dispatching' WHERE queue_id=?", (candidate["queue_id"],))
                claimed = dict(candidate)
                claimed["state"] = "dispatching"
            await dispatch_claimed_task(claimed)
    finally:
        CURRENT_USER.reset(token)


async def scheduler_loop() -> None:
    global QUEUE_RECOVERING
    while True:
        try:
            await scheduler_tick()
            QUEUE_RECOVERING = False
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(QUEUE_POLL_SECONDS)


def adopt_existing_active_tasks() -> None:
    """Register pre-dispatcher mobile records without disturbing their Comfy prompts."""
    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        for table, kind, id_column in (
            ("generation_groups", "generation", "group_id"),
            ("preprocess_jobs", "preprocess", "preprocess_id"),
            ("upscale_jobs", "upscale", "upscale_id"),
            ("prompt_tool_jobs", "prompt_tool", "tool_id"),
        ):
            rows = db.execute(
                f"SELECT * FROM {table} WHERE owner_id IS NOT NULL AND status IN ('queued','running') "
                f"AND NOT EXISTS(SELECT 1 FROM task_queue q WHERE q.task_kind=? AND q.record_id={table}.{id_column})",
                (kind,),
            ).fetchall()
            for raw in rows:
                record = dict(raw)
                if kind == "generation":
                    children = db.execute("SELECT * FROM jobs WHERE group_id=? AND COALESCE(superseded,0)=0 ORDER BY submitted_at", (record[id_column],)).fetchall()
                    active = next((child for child in children if child["status"] in {"queued", "running"}), None)
                    prompt_id = active["prompt_id"] if active else None
                    parameters = json.loads(record.get("parameters_json") or "{}")
                    profile = queue_profile(kind, parameters, record.get("workflow_key") or "")
                elif kind == "prompt_tool":
                    prompt_id = record.get("prompt_id")
                    parameters = {
                        "operation": record.get("operation"),
                        "target_workflow": record.get("target_workflow"),
                    }
                    profile = queue_profile(kind, parameters)
                else:
                    prompt_id = record.get("prompt_id")
                    parameters = json.loads(record.get("parameters_json") or "{}")
                    if kind == "upscale":
                        parameters["engine"] = record.get("engine") or "seedvr2"
                    profile = queue_profile(kind, parameters)
                state = "running" if prompt_id else "waiting"
                db.execute(
                    "INSERT INTO task_queue(task_kind,record_id,owner_id,state,queued_at,started_at,item_started_at,prompt_id,profile_key) VALUES(?,?,?,?,?,?,?,?,?)",
                    (kind, record[id_column], record["owner_id"], state, record.get("submitted_at") or utc_now(), record.get("submitted_at") if prompt_id else None, record.get("submitted_at") if prompt_id else None, prompt_id, profile),
                )
        db.execute("UPDATE task_queue SET state='waiting',prompt_id=NULL,item_started_at=NULL WHERE state='dispatching'")
        control = db.execute("SELECT performance_mode FROM server_control WHERE control_id=1").fetchone()
        if control and control[0]:
            db.execute("UPDATE task_queue SET state='paused' WHERE state='waiting'")


async def set_performance_mode(enabled: bool, changed_by: int) -> None:
    token = CURRENT_USER.set(None)
    try:
        async with queue_lock():
            db_execute(
                "UPDATE server_control SET performance_mode=?,changed_at=?,changed_by=? WHERE control_id=1",
                (int(enabled), utc_now(), changed_by),
            )
            if not enabled:
                db_execute("UPDATE task_queue SET state='waiting' WHERE state='paused'")
                return
            db_execute("UPDATE task_queue SET state='paused' WHERE state='waiting'")
            running = next((item for item in active_queue_rows() if item["state"] in {"running", "dispatching"}), None)
            if not running:
                return
            prompt_id = running.get("prompt_id")
            if prompt_id:
                try:
                    queue = await comfy_json("GET", "/queue")
                    running_ids = queue_prompt_ids(queue.get("queue_running", []))
                    pending_ids = queue_prompt_ids(queue.get("queue_pending", queue.get("queue_queued", [])))
                    if prompt_id in pending_ids:
                        await comfy_json("POST", "/queue", json={"delete": [prompt_id]})
                    elif prompt_id in running_ids:
                        await comfy_json("POST", "/interrupt", json={})
                except HTTPException:
                    pass
                COMFY_PROGRESS.pop(prompt_id, None)
            if running["task_kind"] == "generation" and prompt_id:
                db_execute("UPDATE jobs SET superseded=1,status='cancelled',completed_at=?,error_message='管理员暂停，稍后自动重跑' WHERE prompt_id=?", (utc_now(), prompt_id))
            table, id_column = {
                "generation": ("generation_groups", "group_id"),
                "preprocess": ("preprocess_jobs", "preprocess_id"),
                "upscale": ("upscale_jobs", "upscale_id"),
                "prompt_tool": ("prompt_tool_jobs", "tool_id"),
            }[running["task_kind"]]
            db_execute(f"UPDATE {table} SET status='queued',completed_at=NULL,error_message=NULL" + (",prompt_id=NULL" if running["task_kind"] != "generation" else "") + f" WHERE {id_column}=?", (running["record_id"],))
            db_execute(
                "UPDATE task_queue SET state='paused',prompt_id=NULL,item_started_at=NULL,interrupted_count=interrupted_count+1,error_message=NULL,progress_value=0,progress_phase='任务已重新开始',progress_approximate=1 WHERE queue_id=?",
                (running["queue_id"],),
            )
    finally:
        CURRENT_USER.reset(token)


def validate_image(upload: UploadFile, body: bytes) -> str:
    if upload.content_type not in ALLOWED_TYPES: raise HTTPException(415, "Only JPEG, PNG, and WebP are supported")
    try:
        with Image.open(BytesIO(body)) as image:
            if image.width * image.height > 67_108_864: raise HTTPException(413, "Image is too large")
            image.verify()
    except UnidentifiedImageError as exc:
        raise HTTPException(415, "Invalid image") from exc
    return {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[upload.content_type]


def normalize_crop(raw: str | None) -> dict[str, float] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
        crop = {key: float(value[key]) for key in ("x", "y", "width", "height")}
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(400, "Invalid crop selection") from exc
    if (
        crop["x"] < 0 or crop["y"] < 0
        or crop["width"] <= 0.005 or crop["height"] <= 0.005
        or crop["x"] + crop["width"] > 1.000001
        or crop["y"] + crop["height"] > 1.000001
    ):
        raise HTTPException(400, "Crop selection is outside the image")
    return crop


def crop_image_bytes(body: bytes, suffix: str, raw_crop: str | None) -> bytes:
    crop = normalize_crop(raw_crop)
    if not crop:
        return body
    try:
        with Image.open(BytesIO(body)) as raw:
            image = ImageOps.exif_transpose(raw)
            width, height = image.size
            left = max(0, min(width - 1, int(round(crop["x"] * width))))
            top = max(0, min(height - 1, int(round(crop["y"] * height))))
            right = max(left + 1, min(width, int(round((crop["x"] + crop["width"]) * width))))
            bottom = max(top + 1, min(height, int(round((crop["y"] + crop["height"]) * height))))
            image = image.crop((left, top, right, bottom))
            output = BytesIO()
            if suffix in {".jpg", ".jpeg"}:
                image.convert("RGB").save(output, format="JPEG", quality=95, subsampling=0)
            elif suffix == ".webp":
                image.save(output, format="WEBP", quality=95, method=6)
            else:
                image.save(output, format="PNG", optimize=True)
            return output.getvalue()
    except (OSError, UnidentifiedImageError) as exc:
        raise HTTPException(415, "Invalid source image") from exc


def write_cropped_upload(upload: UploadFile, body: bytes, prefix: str, raw_crop: str | None) -> tuple[str, dict[str, str]]:
    suffix = validate_image(upload, body)
    cropped = crop_image_bytes(body, suffix, raw_crop)
    filename = f"{prefix}_{secrets.token_hex(16)}{suffix}"
    (UPLOAD_DIR / filename).write_bytes(cropped)
    return f"mobile_uploads/{filename}", {"filename": filename, "subfolder": "mobile_uploads", "type": "input"}


def crop_stored_source(record: dict[str, Any] | None, raw_crop: str | None) -> None:
    crop = normalize_crop(raw_crop)
    if not crop or not record:
        return
    path = safe_source_path(record)
    if not path or not path.is_file():
        raise HTTPException(409, "Reusable source image is unavailable")
    path.write_bytes(crop_image_bytes(path.read_bytes(), path.suffix.lower(), json.dumps(crop)))


def prepare_krea_edit_mask(body: bytes, target_image: dict[str, Any], grow: int, feather: int) -> tuple[str, dict[str, str]]:
    target_path = safe_source_path(target_image)
    if not target_path or not target_path.is_file():
        raise HTTPException(409, "Krea identity reference is unavailable for masking")
    try:
        with Image.open(BytesIO(body)) as raw_mask, Image.open(target_path) as target:
            mask = ImageOps.exif_transpose(raw_mask).convert("L")
            target_size = ImageOps.exif_transpose(target).size
            source_ratio = mask.width / max(mask.height, 1)
            target_ratio = target_size[0] / max(target_size[1], 1)
            if abs(source_ratio / target_ratio - 1) > 0.02:
                raise HTTPException(400, "Reference boost mask aspect ratio must match image 2")
            mask = mask.resize(target_size, Image.Resampling.BILINEAR)
            if grow:
                mask = mask.filter(ImageFilter.MaxFilter(grow * 2 + 1))
            if feather:
                mask = mask.filter(ImageFilter.GaussianBlur(feather))
            filename = f"krea_edit_mask_{secrets.token_hex(16)}.png"
            mask.save(UPLOAD_DIR / filename, format="PNG", optimize=True)
    except HTTPException:
        raise
    except (OSError, UnidentifiedImageError) as exc:
        raise HTTPException(415, "Invalid edit mask") from exc
    record = {"filename": filename, "subfolder": "mobile_uploads", "type": "input"}
    return f"mobile_uploads/{filename}", record


def krea_safe_dimensions(
    width: int,
    height: int,
    max_pixels: int = KREA_IDENTITY_MAX_PIXELS,
) -> tuple[int, int]:
    """Preserve source aspect ratio at the largest safe 8-pixel grid."""
    if width <= 0 or height <= 0:
        raise HTTPException(415, "Invalid source image dimensions")
    scale = min(
        (max_pixels / (width * height)) ** 0.5,
        2048 / width,
        2048 / height,
    )
    # Avoid needlessly turning an already near-budget standard size such as
    # 1920x1080 into a slightly distorted 1928x1080 grid.
    if 1 < scale < 1.05:
        scale = 1.0
    target_width = max(64, int(round(width * scale / 8)) * 8)
    target_height = max(64, int(round(height * scale / 8)) * 8)
    while target_width * target_height > max_pixels:
        if target_width / width >= target_height / height and target_width > 64:
            target_width -= 8
        elif target_height > 64:
            target_height -= 8
        else:
            break
    return target_width, target_height


def image_dimensions(path: Path) -> tuple[int, int]:
    try:
        with Image.open(path) as image:
            oriented = ImageOps.exif_transpose(image)
            return int(oriented.width), int(oriented.height)
    except (OSError, UnidentifiedImageError) as exc:
        raise HTTPException(415, "Invalid source image") from exc


def chat_pair(first: int, second: int) -> tuple[int, int]:
    if first == second:
        raise HTTPException(400, "不能添加自己为好友")
    return (first, second) if first < second else (second, first)


def chat_conversation(conversation_id: int, user_id: int | None = None) -> dict[str, Any]:
    member_id = user_id if user_id is not None else int(current_user()["user_id"])
    with database_session(DATABASE, rows=True) as db:
        row = db.execute(
            "SELECT * FROM chat_conversations WHERE conversation_id=? AND (user_a_id=? OR user_b_id=?)",
            (conversation_id, member_id, member_id),
        ).fetchone()
    if not row:
        raise HTTPException(404, "会话不存在")
    return dict(row)


def chat_friendship_active(first: int, second: int, db: sqlite3.Connection | None = None) -> bool:
    user_a, user_b = chat_pair(first, second)
    if db is not None:
        return bool(db.execute("SELECT 1 FROM friendships WHERE user_a_id=? AND user_b_id=?", (user_a, user_b)).fetchone())
    with sqlite3.connect(DATABASE) as connection:
        return bool(connection.execute("SELECT 1 FROM friendships WHERE user_a_id=? AND user_b_id=?", (user_a, user_b)).fetchone())


def chat_manifest_path(file_entry: dict[str, Any]) -> Path | None:
    image = file_entry.get("original") or file_entry.get("image")
    if not isinstance(image, dict) or not image.get("filename"):
        return None
    if file_entry.get("storage") == "chat_media":
        return safe_chat_media_path(image)
    return safe_source_path(image) if file_entry.get("storage") == "input" else safe_output_path(image)


def chat_playback_path(file_entry: dict[str, Any]) -> Path | None:
    playback = file_entry.get("playback") or (file_entry.get("image") if file_entry.get("original") else None)
    if file_entry.get("storage") != "chat_media" or not isinstance(playback, dict) or not playback.get("filename"):
        return None
    return safe_chat_media_path(playback)


def _media_process_flags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def probe_chat_video(path: Path) -> dict[str, Any]:
    if not FFPROBE_PATH.is_file():
        raise HTTPException(503, "视频检查组件不可用，请联系管理员")
    try:
        result = subprocess.run(
            [str(FFPROBE_PATH), "-v", "error", "-show_entries",
             "stream=index,codec_type,codec_name,profile,pix_fmt,width,height,color_space,color_transfer,color_primaries:format=format_name,duration",
             "-of", "json", str(path)], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=90, creationflags=_media_process_flags(), check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise HTTPException(503, "暂时无法检查视频编码，请稍后再试") from exc
    if result.returncode:
        raise HTTPException(415, "视频文件无效或编码信息无法读取")
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(415, "视频编码信息无效") from exc
    video = next((stream for stream in payload.get("streams", []) if stream.get("codec_type") == "video"), None)
    if not video:
        raise HTTPException(415, "视频文件中没有可用的视频画面")
    transfer = str(video.get("color_transfer") or "").lower()
    pix_fmt = str(video.get("pix_fmt") or "").lower()
    codec = str(video.get("codec_name") or "").lower()
    compatible = codec == "h264" and pix_fmt == "yuv420p" and transfer not in {"smpte2084", "arib-std-b67"}
    return {
        "codec": codec, "profile": video.get("profile"), "pix_fmt": pix_fmt,
        "width": int(video.get("width") or 0), "height": int(video.get("height") or 0),
        "color_transfer": transfer, "color_primaries": str(video.get("color_primaries") or ""),
        "compatible": compatible,
    }


def create_chat_video_playback(source: Path, target: Path, metadata: dict[str, Any]) -> None:
    if not FFMPEG_PATH.is_file():
        raise HTTPException(503, "视频转换组件不可用，请联系管理员")
    hdr = metadata.get("color_transfer") in {"smpte2084", "arib-std-b67"} or "10" in str(metadata.get("pix_fmt") or "")
    scale = "scale=trunc(iw/2)*2:trunc(ih/2)*2"
    if hdr:
        video_filter = "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p," + scale
    else:
        video_filter = scale + ",format=yuv420p"
    common = [
        str(FFMPEG_PATH), "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
        "-map", "0:v:0", "-map", "0:a?", "-vf", video_filter,
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(target),
    ]
    encoders = [
        ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "21", "-b:v", "0"],
        ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"],
    ]
    errors: list[str] = []
    for encoder in encoders:
        target.unlink(missing_ok=True)
        command = common[:-1] + encoder + common[-1:]
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=60 * 30, creationflags=_media_process_flags(), check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append(str(exc))
            continue
        if result.returncode == 0 and target.is_file() and target.stat().st_size:
            return
        errors.append((result.stderr or "视频转换失败").strip()[-600:])
    target.unlink(missing_ok=True)
    raise HTTPException(415, "该视频无法生成浏览器兼容版本，请尝试转换为 H.264 MP4 后重新上传")


def prepare_chat_video(path: Path, relative_folder: Path) -> dict[str, Any]:
    metadata = probe_chat_video(path)
    result: dict[str, Any] = {"video_info": metadata, "playback_status": "original"}
    if metadata["compatible"]:
        return result
    playback = path.with_name(f"{path.stem}.playback.mp4")
    create_chat_video_playback(path, playback, metadata)
    result.update({
        "playback_status": "converted", "playback_media_type": "video/mp4",
        "playback_size": playback.stat().st_size,
        "playback": {"filename": playback.name, "subfolder": relative_folder.as_posix(), "type": "chat"},
    })
    return result


def backfill_chat_video_playbacks(attachment_id: str | None = None) -> dict[str, int]:
    """Create browser-compatible derivatives for chat videos stored before this feature."""
    query = "SELECT attachment_id,manifest_json FROM chat_attachments WHERE kind='media'"
    values: tuple[Any, ...] = ()
    if attachment_id:
        query += " AND attachment_id=?"
        values = (attachment_id,)
    with sqlite3.connect(DATABASE) as db:
        rows = db.execute(query, values).fetchall()
    converted = direct = failed = 0
    for stored_id, raw_manifest in rows:
        try:
            manifest = json.loads(raw_manifest or "{}")
        except json.JSONDecodeError:
            failed += 1
            continue
        changed = False
        for entry in manifest.get("files") or []:
            if not isinstance(entry, dict) or not str(entry.get("media_type") or "").startswith("video/"):
                continue
            path = chat_manifest_path(entry)
            if not path or not path.is_file():
                continue
            existing = chat_playback_path(entry)
            if existing and existing.is_file():
                if not entry.get("original") and entry.get("playback"):
                    entry["original"] = entry.get("image")
                    entry["image"] = entry.get("playback")
                    changed = True
                continue
            image = entry.get("image") or {}
            try:
                data = prepare_chat_video(path, Path(str(image.get("subfolder") or "")))
            except HTTPException:
                failed += 1
                continue
            entry.update(data)
            if data.get("playback"):
                entry["original"] = entry.get("image")
                entry["image"] = data["playback"]
            changed = True
            if data.get("playback"):
                converted += 1
            else:
                direct += 1
        if changed:
            with sqlite3.connect(DATABASE) as db:
                db.execute(
                    "UPDATE chat_attachments SET manifest_json=? WHERE attachment_id=?",
                    (json.dumps(manifest, ensure_ascii=False), stored_id),
                )
    return {"converted": converted, "direct": direct, "failed": failed}


def chat_workflow_name(workflow_key: Any) -> str:
    key = str(workflow_key or "").strip()
    try:
        return get_spec(key).label
    except WorkflowCompileError:
        return key or "生成任务"


def chat_attachment_record(attachment_id: str, user_id: int | None = None) -> dict[str, Any]:
    member_id = user_id if user_id is not None else int(current_user()["user_id"])
    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            """SELECT a.*,m.conversation_id,m.sender_id,m.sender_name,m.created_at AS message_created_at,
                      c.user_a_id,c.user_b_id
               FROM chat_attachments a
               JOIN chat_messages m ON m.message_id=a.message_id
               JOIN chat_conversations c ON c.conversation_id=m.conversation_id
               WHERE a.attachment_id=? AND (c.user_a_id=? OR c.user_b_id=?)""",
            (attachment_id, member_id, member_id),
        ).fetchone()
    if not row:
        raise HTTPException(404, "分享内容不存在")
    result = dict(row)
    try:
        result["manifest"] = json.loads(result.get("manifest_json") or "{}")
        result["snapshot"] = json.loads(result.get("snapshot_json") or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(409, "分享内容已损坏") from exc
    return result


def chat_attachment_public(row: dict[str, Any]) -> dict[str, Any]:
    manifest = row.get("manifest")
    snapshot = row.get("snapshot")
    if not isinstance(manifest, dict):
        try: manifest = json.loads(row.get("manifest_json") or "{}")
        except json.JSONDecodeError: manifest = {}
    if not isinstance(snapshot, dict):
        try: snapshot = json.loads(row.get("snapshot_json") or "{}")
        except json.JSONDecodeError: snapshot = {}
    files = manifest.get("files") if isinstance(manifest.get("files"), list) else []
    available = [entry for entry in files if isinstance(entry, dict) and (path := chat_manifest_path(entry)) and path.is_file()]
    if row["kind"] in {"task", "image"}:
        preview = next((entry for entry in available if entry.get("role") in {"final", "image"}), None)
    else:
        preview = next((entry for entry in available if str(entry.get("media_type") or "").startswith("image/")), None)
    if not preview:
        preview = available[0] if available else None
    workflow = str(snapshot.get("workflow") or "")
    workflow_name = str(snapshot.get("workflow_name") or "")
    if not workflow_name or workflow_name == workflow:
        workflow_name = chat_workflow_name(workflow)
    image_count = sum(1 for entry in files if str(entry.get("media_type") or "").startswith("image/"))
    video_count = sum(1 for entry in files if str(entry.get("media_type") or "").startswith("video/"))
    result_count = len({str(entry.get("item_id")) for entry in files if entry.get("role") in {"final", "image"} and entry.get("item_id") is not None})
    if row["kind"] == "image" and not result_count:
        result_count = int(any(entry.get("role") == "image" for entry in files))
    preview_media_type = str(preview.get("media_type") or "") if preview else ""
    preview_is_image = bool(preview) and not preview_media_type.startswith("video/")
    preview_url = None
    if preview_is_image:
        preview_base = f"/api/chat/attachments/{row['attachment_id']}/files/{preview['id']}"
        preview_url = f"{preview_base}?preview=512"
    elif preview and preview_media_type.startswith("video/"):
        preview_url = f"/api/chat/attachments/{row['attachment_id']}/files/{preview['id']}/poster?preview=512"
    return {
        "id": row["attachment_id"], "kind": row["kind"],
        "title": str(snapshot.get("title") or workflow_name or ("媒体附件" if row["kind"] == "media" else "生成图片" if row["kind"] == "image" else "生成任务")),
        "workflow": workflow, "workflow_name": workflow_name, "prompt": snapshot.get("prompt") or "",
        "sender": row.get("sender_name") or snapshot.get("sender_name") or "",
        "created_at": row.get("created_at") or row.get("message_created_at"),
        "available": bool(available), "available_files": len(available), "total_files": len(files),
        "image_count": image_count, "video_count": video_count,
        "result_count": result_count,
        "media_type": preview.get("media_type") if preview else None,
        "preview_url": preview_url,
        "primary_url": f"/api/chat/attachments/{row['attachment_id']}/files/{preview['id']}" if preview else None,
        "playback_status": preview.get("playback_status") if preview else None,
        "files": [
            {"id": entry.get("id"), "role": entry.get("role"), "item_id": entry.get("item_id"),
             "media_type": "video/mp4" if chat_playback_path(entry) else entry.get("media_type"),
             "original_media_type": entry.get("media_type"), "playback_status": entry.get("playback_status"),
             "width": int(entry.get("width") or 0) or None, "height": int(entry.get("height") or 0) or None,
             "available": bool((path := chat_manifest_path(entry)) and path.is_file())}
            for entry in files if isinstance(entry, dict)
        ],
    }


def chat_attachment_detail_public(row: dict[str, Any]) -> dict[str, Any]:
    summary = chat_attachment_public(row)
    snapshot = row.get("snapshot") if isinstance(row.get("snapshot"), dict) else {}
    manifest = row.get("manifest") if isinstance(row.get("manifest"), dict) else {}
    public_files: list[dict[str, Any]] = []
    for entry in manifest.get("files") or []:
        if not isinstance(entry, dict):
            continue
        path = chat_manifest_path(entry)
        available = bool(path and path.is_file())
        original_media_type = str(entry.get("media_type") or ("image/" + (path.suffix.lower().lstrip(".") if path else "jpeg")))
        playback = chat_playback_path(entry)
        playback_available = bool(playback and playback.is_file())
        media_type = "video/mp4" if playback_available else original_media_type
        file_id = str(entry.get("id") or "")
        public_files.append({
            "id": file_id, "role": entry.get("role"), "item_id": entry.get("item_id"),
            "record_id": entry.get("record_id"), "parameters": entry.get("parameters") or {},
            "engine": entry.get("engine"), "source_kind": entry.get("source_kind"),
            "submitted_at": entry.get("submitted_at"), "completed_at": entry.get("completed_at"),
            "media_type": media_type, "original_media_type": original_media_type,
            "playback_status": entry.get("playback_status") or ("converted" if playback_available else "original"),
            "width": int(entry.get("width") or 0) or None, "height": int(entry.get("height") or 0) or None,
            "name": entry.get("original_name") or (path.name if path else ""),
            "size": int(entry.get("size") or (path.stat().st_size if available and path else 0)),
            "playback_size": int(entry.get("playback_size") or (playback.stat().st_size if playback_available and playback else 0)),
            "available": available,
            "url": f"/api/chat/attachments/{row['attachment_id']}/files/{file_id}" if available else None,
            "download_url": f"/api/chat/attachments/{row['attachment_id']}/files/{file_id}?download=true" if available else None,
            "preview_url": (
                f"/api/chat/attachments/{row['attachment_id']}/files/{file_id}?preview=512"
                if available and media_type.startswith("image/") else
                f"/api/chat/attachments/{row['attachment_id']}/files/{file_id}/poster?preview=512"
                if available and media_type.startswith("video/") else None
            ),
        })
    items: list[dict[str, Any]] = []
    snapshot_items = snapshot.get("items") if isinstance(snapshot.get("items"), list) else []
    item_ids = [str(item.get("id")) for item in snapshot_items if isinstance(item, dict) and item.get("id") is not None]
    if not item_ids:
        item_ids = list(dict.fromkeys(str(file.get("item_id")) for file in public_files if file.get("item_id") is not None))
    for item_id_value in item_ids:
        item_files = [file for file in public_files if str(file.get("item_id")) == item_id_value]
        items.append({
            "id": item_id_value,
            "final": next((file for file in item_files if file.get("role") in {"final", "image"}), None),
            "stage1": next((file for file in item_files if file.get("role") == "stage1"), None),
            "details": [file for file in item_files if file.get("role") == "detail"],
            "upscales": [file for file in item_files if file.get("role") == "upscale"],
        })
    sources = {str(file.get("role")): file for file in public_files if file.get("role") in {"source", "edit_source", "reference", "preprocess"}}
    owned_by_me = int(row.get("origin_owner_id") or 0) == int(current_user()["user_id"])
    return {
        **summary,
        "source_prompt": snapshot.get("source_prompt") or "",
        "parameters": snapshot.get("parameters") if isinstance(snapshot.get("parameters"), dict) else {},
        "submitted_at": snapshot.get("submitted_at"), "completed_at": snapshot.get("completed_at"),
        "owned_by_me": owned_by_me,
        "local_group_id": str(row.get("origin_group_id") or "") if owned_by_me else "",
        "files": public_files, "items": items, "sources": sources,
    }


def chat_message_public(row: dict[str, Any]) -> dict[str, Any]:
    sender = None
    if row.get("sender_username") is not None:
        sender = {
            "user_id": int(row["sender_id"]),
            "username": row["sender_username"],
            "avatar_version": int(row.get("sender_avatar_version") or 0),
        }
    if sender is None:
        sender = db_user(int(row["sender_id"]))
    public_sender = public_chat_user(sender) if sender else {
        "id": int(row["sender_id"]), "username": row["sender_name"],
        "avatar_url": None, "avatar_version": 0,
    }
    result = {
        "id": int(row["message_id"]), "conversation_id": int(row["conversation_id"]),
        "sender_id": int(row["sender_id"]), "sender_name": row["sender_name"],
        "sender": public_sender,
        "body": row.get("body") or "", "created_at": row["created_at"], "attachment": None,
    }
    if row.get("attachment_id"):
        result["attachment"] = chat_attachment_public(dict(row))
    return result


def chat_attachment_manifest(owner_id: int, descriptor: dict[str, Any]) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    kind = str(descriptor.get("kind") or "").strip()
    group_id = str(descriptor.get("group_id") or "").strip()
    group = unrestricted_group(group_id)
    if not group or int(group.get("owner_id") or 0) != owner_id or group.get("storage_scope") != "mobile":
        raise HTTPException(404, "历史任务不存在")
    if group.get("status") not in QUEUE_TERMINAL_STATES:
        raise HTTPException(409, "只能分享已经结束的任务")
    items = stored_items(group)
    if not any(isinstance(item.get("final"), dict) for item in items):
        raise HTTPException(409, "任务没有可分享的输出")
    try:
        parameters = json.loads(group.get("parameters_json") or "{}")
    except json.JSONDecodeError:
        parameters = {}
    snapshot = {
        "workflow": group["workflow_key"], "workflow_name": chat_workflow_name(group["workflow_key"]),
        "title": group.get("title") or "", "prompt": group.get("prompt_text") or "",
        "source_prompt": group.get("source_prompt_text") or "", "parameters": parameters,
        "submitted_at": group.get("submitted_at"), "completed_at": group.get("completed_at"),
    }
    files: list[dict[str, Any]] = []
    if kind == "image":
        item_id_value = str(descriptor.get("item_id") or "")
        source_kind = str(descriptor.get("source_kind") or "edit")
        record_id = str(descriptor.get("record_id") or "")
        image = reusable_result_image(group_id, item_id_value, source_kind, record_id)
        files.append({"id": "image", "role": "image", "item_id": item_id_value, "storage": "output", "image": image})
        snapshot["title"] = snapshot["title"] or "生成图片"
    elif kind == "task":
        for column, role in (("source_image_json", "source"), ("edit_source_image_json", "edit_source"), ("reference_image_json", "reference"), ("edit_mask_json", "mask")):
            image = json_image_from(group, column)
            if image:
                files.append({"id": role, "role": role, "storage": "input", "image": image})
        preprocess = group_preprocess(group)
        if preprocess and isinstance(preprocess.get("output"), dict):
            files.append({"id": "preprocess", "role": "preprocess", "storage": "output", "image": preprocess["output"]})
        _, group_upscales, group_details = db_group_relations(group_id)
        for index, item in enumerate(items):
            item_key = str(item.get("id") or index)
            for role in ("final", "stage1"):
                image = item.get(role)
                if isinstance(image, dict) and image.get("filename"):
                    files.append({"id": f"{role}-{index}", "role": role, "item_id": item_key, "storage": "output", "image": image})
            for post_kind, entries in (
                ("upscale", [public_upscale(entry) for entry in group_upscales.get(item_key, [])]),
                ("detail", [public_detail(entry) for entry in group_details.get(item_key, [])]),
            ):
                for post_index, entry in enumerate(entries):
                    image = entry.get("output") if isinstance(entry, dict) else None
                    if isinstance(image, dict) and image.get("filename"):
                        files.append({
                            "id": f"{post_kind}-{index}-{post_index}", "role": post_kind,
                            "item_id": item_key, "record_id": entry.get("id"), "storage": "output",
                            "parameters": entry.get("parameters") or {}, "engine": entry.get("engine"),
                            "source_kind": entry.get("source_kind"), "submitted_at": entry.get("submitted_at"),
                            "completed_at": entry.get("completed_at"), "image": image,
                        })
        snapshot["items"] = [{"id": str(item.get("id") or index)} for index, item in enumerate(items)]
    else:
        raise HTTPException(400, "不支持的分享类型")
    for entry in files:
        media = entry.get("image")
        if isinstance(media, dict):
            entry["media_type"] = output_media_type(media)
    return kind, group_id, {"version": 1, "files": files}, snapshot


async def emit_chat_event(user_ids: set[int] | list[int] | tuple[int, ...], event: dict[str, Any]) -> None:
    payload = json.dumps(event, ensure_ascii=False)
    for user_id in set(user_ids):
        for socket in list(CHAT_CONNECTIONS.get(user_id, set())):
            try:
                await socket.send_text(payload)
            except Exception:
                CHAT_CONNECTIONS.get(user_id, set()).discard(socket)


async def emit_chat_message_event(user_ids: set[int] | list[int] | tuple[int, ...], row: dict[str, Any]) -> None:
    message = chat_message_public(dict(row))
    for user_id in set(user_ids):
        overview = chat_overview_for_user(int(user_id))
        conversation = next(
            (entry for entry in overview["conversations"] if entry["id"] == message["conversation_id"]),
            None,
        )
        await emit_chat_event({int(user_id)}, {
            "type": "message",
            "conversation_id": message["conversation_id"],
            "message_id": message["id"],
            "message": message,
            "conversation": conversation,
            "unread_total": overview["unread"],
        })


def cleanup_expired_chat_assets() -> int:
    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(row) for row in db.execute(
            "SELECT * FROM chat_prepared_assets WHERE consumed_at IS NULL AND expires_at<=?", (utc_now(),),
        ).fetchall()]
        for row in rows:
            image = json_image_from(row, "source_image_json")
            path = safe_source_path(image) if image else None
            if path and path.is_file():
                path.unlink()
        db.execute("DELETE FROM chat_prepared_assets WHERE consumed_at IS NULL AND expires_at<=?", (utc_now(),))
    return len(rows)


def prepared_chat_asset(token: str, consume: bool = False) -> dict[str, str]:
    token_hash = token_digest(token)
    owner_id = int(current_user()["user_id"])
    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT * FROM chat_prepared_assets WHERE token_hash=? AND user_id=? AND consumed_at IS NULL AND expires_at>?",
            (token_hash, owner_id, utc_now()),
        ).fetchone()
        if not row:
            raise HTTPException(404, "临时编辑图片已失效")
        image = json_image_from(dict(row), "source_image_json")
        path = safe_source_path(image) if image else None
        if not path or not path.is_file():
            raise HTTPException(404, "临时编辑图片已不可用")
        if consume:
            db.execute("UPDATE chat_prepared_assets SET consumed_at=? WHERE token_hash=?", (utc_now(), token_hash))
    return image or {}


def copy_chat_manifest_file(entry: dict[str, Any], group_id: str) -> dict[str, str]:
    source = chat_manifest_path(entry)
    if not source or not source.is_file():
        raise HTTPException(409, "分享文件已不可用")
    suffix = source.suffix.lower() if source.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".mp4", ".webm", ".mov"} else ".png"
    if entry.get("storage") == "input":
        filename = f"chat_{group_id}_{secrets.token_hex(5)}{suffix}"
        target = UPLOAD_DIR / filename
        shutil.copy2(source, target)
        return {"filename": filename, "subfolder": "mobile_uploads", "type": "input"}
    folder = MOBILE_OUTPUT_DIR / "imported" / group_id
    folder.mkdir(parents=True, exist_ok=True)
    filename = f"{entry.get('role') or 'image'}_{secrets.token_hex(5)}{suffix}"
    target = folder / filename
    shutil.copy2(source, target)
    copied = {"filename": filename, "subfolder": f"mobile/imported/{group_id}", "type": "output"}
    if entry.get("media_type"):
        copied["media_type"] = str(entry["media_type"])
    return copied


def prepare_krea_identity_source(image_name: str) -> tuple[str, dict[str, str]]:
    """Create a bounded processing copy while preserving the original upload."""
    source = (COMFY_ROOT / "input" / image_name).resolve()
    input_root = (COMFY_ROOT / "input").resolve()
    if not source.is_relative_to(input_root) or not source.is_file():
        raise HTTPException(409, "Krea Identity Edit source image is unavailable")
    suffix = source.suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise HTTPException(415, "Only JPEG, PNG, and WebP are supported")
    filename = f"krea_identity_{secrets.token_hex(16)}{suffix}"
    target = UPLOAD_DIR / filename
    try:
        with Image.open(source) as raw:
            image = ImageOps.exif_transpose(raw)
            width, height = image.size
            scale = min(
                1.0,
                (KREA_IDENTITY_MAX_PIXELS / (width * height)) ** 0.5,
                2048 / width,
                2048 / height,
            )
            if scale < 1:
                resized = (
                    max(1, int(width * scale)),
                    max(1, int(height * scale)),
                )
                image = image.resize(resized, Image.Resampling.LANCZOS)
            save_options: dict[str, Any] = {}
            if suffix in {".jpg", ".jpeg"}:
                if image.mode not in {"RGB", "L"}:
                    image = image.convert("RGB")
                save_options = {"quality": 95, "subsampling": 0}
            elif suffix == ".webp":
                save_options = {"quality": 95, "method": 6}
            image.save(target, **save_options)
    except (OSError, UnidentifiedImageError) as exc:
        target.unlink(missing_ok=True)
        raise HTTPException(415, "Invalid source image") from exc
    record = {"filename": filename, "subfolder": "mobile_uploads", "type": "input"}
    return f"mobile_uploads/{filename}", record


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_runtime_logging(DATA_DIR / "logs")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    MOBILE_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    CHAT_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    if not (STATIC_DIR / "index.html").is_file():
        raise RuntimeError(r"Frontend build is missing. Run: npm install; npm run build")
    # Runtime workflow files are canonical assets. Rebuilding them at startup
    # used to overwrite reviewed files and tied startup to unrelated desktop
    # workflows and hard-coded external drive paths.
    validate_mobile_workflows(MOBILE_WORKFLOW_DIR, ENABLED_WORKFLOW_KEYS)
    init_database()
    now = utc_now()
    queue_cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    with closing(sqlite3.connect(DATABASE)) as db:
        expired_sessions = db.execute("DELETE FROM sessions WHERE expires_at<=?", (now,)).rowcount
        expired_queue = db.execute(
            "DELETE FROM task_queue WHERE state IN ('completed','failed','cancelled') "
            "AND COALESCE(finished_at,queued_at)<?",
            (queue_cutoff,),
        ).rowcount
        expired_cooldowns = db.execute(
            "DELETE FROM h3_setting_cooldowns WHERE blocked_until<=?",
            (now,),
        ).rowcount
        db.commit()
    if expired_sessions or expired_queue or expired_cooldowns:
        RUNTIME_LOGGER.info(
            "runtime housekeeping completed",
            extra={"status": expired_sessions + expired_queue + expired_cooldowns},
        )
    purge_expired_recycle_entries()
    synchronize_lora_registry(True)
    prune_h3_sampler_heartbeats()
    with sqlite3.connect(DATABASE) as db:
        if not db.execute("SELECT 1 FROM users WHERE role='admin' AND disabled=0").fetchone():
            raise RuntimeError(r"No administrator account. Run: .venv\Scripts\python.exe -m mobile_server.admin create")
    recover_stuck_groups()
    adopt_existing_active_tasks()
    with closing(sqlite3.connect(DATABASE)) as db:
        seed_repair = db.execute(
            "SELECT value FROM app_metadata WHERE key='terminal_seed_repair_version'"
        ).fetchone()
    if not seed_repair:
        repaired = repair_terminal_group_seed_lists()
        with closing(sqlite3.connect(DATABASE)) as db:
            db.execute(
                "INSERT INTO app_metadata(key,value) VALUES('terminal_seed_repair_version','1') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )
        RUNTIME_LOGGER.info("startup migration completed", extra={"status": repaired})
    await restore_running_comfy_progress_plans()
    cancelled_recovery = asyncio.create_task(recover_cancelled_generation_history())
    lora_registry = asyncio.create_task(lora_registry_loop())
    recycle_cleaner = asyncio.create_task(recycle_cleanup_loop())
    listener = asyncio.create_task(comfy_progress_listener())
    task_events = asyncio.create_task(task_event_loop())
    preprocess_cleaner = asyncio.create_task(preprocess_cleanup_loop())
    scheduler = asyncio.create_task(scheduler_loop())
    try:
        yield
    finally:
        listener.cancel()
        task_events.cancel()
        preprocess_cleaner.cancel()
        scheduler.cancel()
        cancelled_recovery.cancel()
        lora_registry.cancel()
        recycle_cleaner.cancel()
        await asyncio.gather(listener, task_events, preprocess_cleaner, scheduler, cancelled_recovery, lora_registry, recycle_cleaner, return_exceptions=True)


app = FastAPI(title="Comfy Canvas", version=__version__, lifespan=lifespan)
app.mount("/static", PrecompressedStaticFiles(directory=STATIC_DIR), name="static")


def is_versioned_static_request(request: Request) -> bool:
    filename = Path(request.url.path).name
    return bool(
        VERSIONED_STATIC_FILE_RE.search(filename)
        or HASHED_STATIC_FILE_RE.search(filename)
    )


@app.middleware("http")
async def observability_middleware(request: Request, call_next):
    started = time.perf_counter()
    request_id = request.headers.get("x-request-id", "").strip()[:64] or secrets.token_hex(8)
    try:
        response = await call_next(request)
    except Exception:
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        RUNTIME_LOGGER.exception(
            "request failed",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "duration_ms": duration_ms,
            },
        )
        raise
    duration_ms = round((time.perf_counter() - started) * 1000, 1)
    response.headers["X-Request-ID"] = request_id
    apply_security_headers(response)
    if request.url.path.startswith("/api/") or response.status_code >= 400 or duration_ms >= 500:
        RUNTIME_LOGGER.info(
            "request completed",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": duration_ms,
            },
        )
    return response


@app.middleware("http")
async def authentication_middleware(request: Request, call_next):
    path = request.url.path
    if path == "/next" or path.startswith("/next/"):
        canonical_path = path[5:] or "/"
        canonical_url = canonical_path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(canonical_url, status_code=308)
    public = path.startswith("/static/") or path in {
        "/login", "/register", "/api/auth/login", "/api/auth/register",
        "/favicon.ico", "/favicon.png",
        "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png", "/apple-touch-icon-180x180.png",
    }
    user = session_user(request.cookies.get(SESSION_COOKIE))
    if not public and not user:
        if path.startswith("/api/"):
            return JSONResponse({"detail": "请先登录"}, status_code=401)
        next_target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(next_target, safe='')}", status_code=303)
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        expected = f"{request.url.scheme}://{request.url.netloc}"
        if origin and origin.rstrip("/") != expected.rstrip("/"):
            return JSONResponse({"detail": "请求来源验证失败"}, status_code=403)
    token = CURRENT_USER.set(user)
    try:
        response = await call_next(request)
        if request.method not in {"GET", "HEAD", "OPTIONS"} and response.status_code < 500:
            invalidate_storage_usage_cache()
            invalidate_diagnostics_cache()
        if request.method in {"GET", "HEAD"}:
            if path.startswith("/static/"):
                if is_versioned_static_request(request):
                    response.headers["Cache-Control"] = f"public, max-age={STATIC_IMMUTABLE_MAX_AGE}, immutable"
                else:
                    response.headers["Cache-Control"] = "no-cache, max-age=0, must-revalidate"
            elif not path.startswith("/api/"):
                response.headers["Cache-Control"] = "no-store, max-age=0"
        return response
    finally:
        CURRENT_USER.reset(token)


def require_admin() -> dict[str, Any]:
    user = current_user()
    if user["role"] != "admin":
        raise HTTPException(403, "需要管理员权限")
    return user


@app.get("/apple-touch-icon.png", include_in_schema=False)
@app.get("/apple-touch-icon-precomposed.png", include_in_schema=False)
@app.get("/apple-touch-icon-180x180.png", include_in_schema=False)
async def apple_touch_icon():
    return FileResponse(STATIC_DIR / "icons" / "apple-touch-icon.png", media_type="image/png")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon_ico():
    return FileResponse(STATIC_DIR / "icons" / "fraunces-favicon-v2.ico", media_type="image/x-icon")


@app.get("/favicon.png", include_in_schema=False)
async def favicon_png():
    return FileResponse(STATIC_DIR / "icons" / "fraunces-favicon-v2-64.png", media_type="image/png")


@app.get("/login")
@app.get("/register")
async def auth_page(request: Request):
    return precompressed_file_response(request, STATIC_DIR / "auth.html", "text/html")


def write_avatar_file(user_id: int, contents: bytes, crop: str | None) -> None:
    write_avatar_file_service(
        user_id,
        contents,
        crop,
        avatar_dir=AVATAR_DIR,
        avatar_path=avatar_path,
        max_pixels=MAX_AVATAR_PIXELS,
        avatar_size=AVATAR_SIZE,
    )


account_router = create_account_router(AccountRouteDependencies(
    current_user=current_user,
    normalize_username=normalize_username,
    utc_now=utc_now,
    database_path=lambda: DATABASE,
    db_user=db_user,
    public_user=public_user,
    broadcast_profile_update=broadcast_profile_update,
    avatar_path=avatar_path,
    write_avatar_file=write_avatar_file,
    db_execute=db_execute,
    verify_password=verify_password,
    password_hash=password_hash,
    token_digest=token_digest,
    allowed_avatar_types=ALLOWED_TYPES,
    max_avatar_bytes=MAX_AVATAR_BYTES,
    session_cookie=SESSION_COOKIE,
))
app.router.routes.extend(account_router.routes)
update_account_profile = account_router.update_account_profile
update_account_avatar = account_router.update_account_avatar
delete_account_avatar = account_router.delete_account_avatar
update_account_password = account_router.update_account_password
user_avatar = account_router.user_avatar


@app.websocket("/api/chat/ws")
async def chat_websocket(websocket: WebSocket):
    user = session_user(websocket.cookies.get(SESSION_COOKIE))
    origin = websocket.headers.get("origin")
    host = websocket.headers.get("host")
    if not user or (origin and urlparse(origin).netloc.casefold() != str(host or "").casefold()):
        await websocket.close(code=4403 if user else 4401)
        return
    user_id = int(user["user_id"])
    await websocket.accept()
    CHAT_CONNECTIONS.setdefault(user_id, set()).add(websocket)
    await websocket.send_json({"type": "ready"})
    try:
        while True:
            message = await websocket.receive_text()
            if message == "ping":
                await websocket.send_text('{"type":"pong"}')
    except WebSocketDisconnect:
        pass
    finally:
        CHAT_CONNECTIONS.get(user_id, set()).discard(websocket)


@app.websocket("/api/tasks/ws")
async def task_websocket(websocket: WebSocket):
    user = session_user(websocket.cookies.get(SESSION_COOKIE))
    origin = websocket.headers.get("origin")
    host = websocket.headers.get("host")
    if not user or (origin and urlparse(origin).netloc.casefold() != str(host or "").casefold()):
        await websocket.close(code=4403 if user else 4401)
        return
    user_id = int(user["user_id"])
    await websocket.accept()
    TASK_CONNECTIONS.setdefault(user_id, set()).add(websocket)
    TASK_CONNECTION_ROLES[user_id] = user.get("role") == "admin"
    await websocket.send_json({
        "type": "queue",
        "snapshot": queue_snapshot(user_id, TASK_CONNECTION_ROLES[user_id]),
    })
    try:
        while True:
            message = await websocket.receive_text()
            if message == "ping":
                await websocket.send_text('{"type":"pong"}')
    except WebSocketDisconnect:
        pass
    finally:
        TASK_CONNECTIONS.get(user_id, set()).discard(websocket)
        if not TASK_CONNECTIONS.get(user_id):
            TASK_CONNECTIONS.pop(user_id, None)
            TASK_CONNECTION_ROLES.pop(user_id, None)


@app.get("/api/chat/search")
async def chat_search(username: str):
    _, username_key = normalize_username(username)
    me = current_user()
    target = db_user_by_name(username_key)
    if not target or target["disabled"] or int(target["user_id"]) == int(me["user_id"]):
        return {"user": None}
    me_id, target_id = int(me["user_id"]), int(target["user_id"])
    user_a, user_b = chat_pair(me_id, target_id)
    with sqlite3.connect(DATABASE) as db:
        friendship = db.execute("SELECT 1 FROM friendships WHERE user_a_id=? AND user_b_id=?", (user_a, user_b)).fetchone()
        request = db.execute(
            "SELECT request_id,sender_id,status FROM friend_requests WHERE status='pending' AND ((sender_id=? AND receiver_id=?) OR (sender_id=? AND receiver_id=?)) ORDER BY request_id DESC LIMIT 1",
            (me_id, target_id, target_id, me_id),
        ).fetchone()
    relationship = "friend" if friendship else "outgoing" if request and request[1] == me_id else "incoming" if request else "none"
    return {"user": public_chat_user(target), "relationship": relationship, "request_id": int(request[0]) if request else None}


def chat_overview_for_user(me_id: int) -> dict[str, Any]:
    with database_session(DATABASE, rows=True) as db:
        requests = [dict(row) for row in db.execute(
            """SELECT r.*,su.username AS sender_name,su.avatar_version AS sender_avatar_version,
                      ru.username AS receiver_name,ru.avatar_version AS receiver_avatar_version
               FROM friend_requests r JOIN users su ON su.user_id=r.sender_id JOIN users ru ON ru.user_id=r.receiver_id
               WHERE r.status='pending' AND (r.sender_id=? OR r.receiver_id=?) ORDER BY r.created_at DESC""",
            (me_id, me_id),
        ).fetchall()]
        conversations = [dict(row) for row in db.execute(
            """WITH last_messages AS (
                   SELECT m.*,a.attachment_id,a.kind AS attachment_kind,
                          ROW_NUMBER() OVER (PARTITION BY m.conversation_id ORDER BY m.message_id DESC) AS row_number
                   FROM chat_messages m LEFT JOIN chat_attachments a ON a.message_id=m.message_id
               ), unread_counts AS (
                   SELECT m.conversation_id,COUNT(*) AS unread,MIN(m.message_id) AS first_unread_message_id
                   FROM chat_messages m
                   LEFT JOIN chat_reads r ON r.conversation_id=m.conversation_id AND r.user_id=?
                   WHERE m.sender_id<>? AND m.message_id>COALESCE(r.last_opened_message_id,0)
                   GROUP BY m.conversation_id
               )
               SELECT c.*,u.user_id AS peer_id,u.username AS peer_name,u.avatar_version AS peer_avatar_version,
                       COALESCE(r.last_opened_message_id,0) AS last_opened_message_id,
                       COALESCE(uc.unread,0) AS unread,
                       uc.first_unread_message_id AS first_unread_message_id,
                      lm.message_id AS last_message_id,lm.sender_id AS last_sender_id,
                      lm.body AS last_body,lm.created_at AS last_created_at,
                      lm.attachment_id AS last_attachment_id,lm.attachment_kind AS last_attachment_kind,
                      CASE WHEN f.user_a_id IS NULL THEN 0 ELSE 1 END AS friend
               FROM chat_conversations c
               JOIN users u ON u.user_id=CASE WHEN c.user_a_id=? THEN c.user_b_id ELSE c.user_a_id END
               LEFT JOIN chat_reads r ON r.conversation_id=c.conversation_id AND r.user_id=?
               LEFT JOIN unread_counts uc ON uc.conversation_id=c.conversation_id
               LEFT JOIN last_messages lm ON lm.conversation_id=c.conversation_id AND lm.row_number=1
               LEFT JOIN friendships f ON f.user_a_id=c.user_a_id AND f.user_b_id=c.user_b_id
               WHERE c.user_a_id=? OR c.user_b_id=?
               ORDER BY COALESCE(c.last_message_at,c.created_at) DESC""",
            (me_id, me_id, me_id, me_id, me_id, me_id),
        ).fetchall()]
        result_conversations = []
        unread_total = 0
        for conversation in conversations:
            unread = int(conversation.get("unread") or 0)
            unread_total += unread
            last = None
            if conversation.get("last_message_id") is not None:
                last = {
                    "id": int(conversation["last_message_id"]),
                    "sender_id": int(conversation["last_sender_id"]),
                    "body": conversation.get("last_body") or "",
                    "created_at": conversation.get("last_created_at"),
                    "attachment": (
                        {"id": conversation["last_attachment_id"], "kind": conversation.get("last_attachment_kind")}
                        if conversation.get("last_attachment_id") else None
                    ),
                }
            peer = {
                "user_id": int(conversation["peer_id"]),
                "username": conversation["peer_name"],
                "avatar_version": int(conversation.get("peer_avatar_version") or 0),
            }
            public_peer = public_chat_user(peer)
            result_conversations.append({
                "id": int(conversation["conversation_id"]),
                "kind": "direct", "peer": public_peer,
                "display": {"title": public_peer["username"], "avatar_url": public_peer["avatar_url"]},
                "friend": bool(conversation["friend"]), "read_only": not bool(conversation["friend"]), "unread": unread,
                "last_opened_message_id": int(conversation.get("last_opened_message_id") or 0),
                "first_unread_message_id": (
                    int(conversation["first_unread_message_id"])
                    if conversation.get("first_unread_message_id") is not None else None
                ),
                "last_message": last,
                "updated_at": conversation.get("last_message_at") or conversation["created_at"],
            })
    incoming = [{
        "id": int(row["request_id"]),
        "user": public_chat_user({
            "user_id": int(row["sender_id"]), "username": row["sender_name"],
            "avatar_version": int(row.get("sender_avatar_version") or 0),
        }),
        "created_at": row["created_at"],
    } for row in requests if int(row["receiver_id"]) == me_id]
    outgoing = [{
        "id": int(row["request_id"]),
        "user": public_chat_user({
            "user_id": int(row["receiver_id"]), "username": row["receiver_name"],
            "avatar_version": int(row.get("receiver_avatar_version") or 0),
        }),
        "created_at": row["created_at"],
    } for row in requests if int(row["sender_id"]) == me_id]
    return {"incoming": incoming, "outgoing": outgoing, "conversations": result_conversations, "unread": unread_total}


@app.get("/api/chat/overview")
async def chat_overview():
    return chat_overview_for_user(int(current_user()["user_id"]))


@app.post("/api/chat/friend-requests")
async def create_friend_request(payload: dict[str, Any] = Body(...)):
    _, key = normalize_username(payload.get("username"))
    me = current_user()
    target = db_user_by_name(key)
    if not target or target["disabled"]:
        raise HTTPException(404, "没有找到该用户")
    me_id, target_id = int(me["user_id"]), int(target["user_id"])
    user_a, user_b = chat_pair(me_id, target_id)
    with database_session(DATABASE, rows=True, write=True) as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM friendships WHERE user_a_id=? AND user_b_id=?", (user_a, user_b)).fetchone():
            raise HTTPException(409, "你们已经是好友")
        existing = db.execute(
            "SELECT * FROM friend_requests WHERE status='pending' AND ((sender_id=? AND receiver_id=?) OR (sender_id=? AND receiver_id=?)) ORDER BY request_id DESC LIMIT 1",
            (me_id, target_id, target_id, me_id),
        ).fetchone()
        if existing:
            if int(existing["sender_id"]) == target_id:
                raise HTTPException(409, "对方已经向你发送了好友请求")
            raise HTTPException(409, "好友请求已发送")
        cursor = db.execute(
            "INSERT INTO friend_requests(sender_id,receiver_id,status,created_at) VALUES(?,?,'pending',?)",
            (me_id, target_id, utc_now()),
        )
        request_id = int(cursor.lastrowid)
    await emit_chat_event({target_id, me_id}, {"type": "friend_request", "request_id": request_id})
    return {"id": request_id, "user": public_chat_user(target), "status": "pending"}


async def act_friend_request(request_id: int, action: str) -> dict[str, Any]:
    me_id = int(current_user()["user_id"])
    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM friend_requests WHERE request_id=? AND receiver_id=? AND status='pending'", (request_id, me_id)).fetchone()
        if not row:
            raise HTTPException(404, "好友请求不存在")
        sender_id = int(row["sender_id"])
        now = utc_now()
        db.execute("UPDATE friend_requests SET status=?,acted_at=? WHERE request_id=?", (action, now, request_id))
        conversation_id = None
        if action == "accepted":
            user_a, user_b = chat_pair(sender_id, me_id)
            db.execute("INSERT OR IGNORE INTO friendships(user_a_id,user_b_id,created_at) VALUES(?,?,?)", (user_a, user_b, now))
            db.execute("INSERT OR IGNORE INTO chat_conversations(user_a_id,user_b_id,created_at) VALUES(?,?,?)", (user_a, user_b, now))
            conversation_id = int(db.execute("SELECT conversation_id FROM chat_conversations WHERE user_a_id=? AND user_b_id=?", (user_a, user_b)).fetchone()[0])
            db.execute("INSERT OR IGNORE INTO chat_reads(conversation_id,user_id) VALUES(?,?),(?,?)", (conversation_id, sender_id, conversation_id, me_id))
    await emit_chat_event({sender_id, me_id}, {"type": "friendship", "request_id": request_id, "status": action, "conversation_id": conversation_id})
    return {"status": action, "conversation_id": conversation_id}


@app.post("/api/chat/friend-requests/{request_id}/accept")
async def accept_friend_request(request_id: int):
    return await act_friend_request(request_id, "accepted")


@app.post("/api/chat/friend-requests/{request_id}/reject")
async def reject_friend_request(request_id: int):
    return await act_friend_request(request_id, "rejected")


@app.delete("/api/chat/friend-requests/{request_id}")
async def cancel_friend_request(request_id: int):
    me_id = int(current_user()["user_id"])
    with sqlite3.connect(DATABASE) as db:
        row = db.execute("SELECT receiver_id FROM friend_requests WHERE request_id=? AND sender_id=? AND status='pending'", (request_id, me_id)).fetchone()
        if not row:
            raise HTTPException(404, "好友请求不存在")
        receiver_id = int(row[0])
        db.execute("UPDATE friend_requests SET status='cancelled',acted_at=? WHERE request_id=?", (utc_now(), request_id))
    await emit_chat_event({receiver_id, me_id}, {"type": "friendship", "request_id": request_id, "status": "cancelled"})
    return {"cancelled": True}


@app.delete("/api/chat/friends/{friend_id}")
async def delete_chat_friend(friend_id: int):
    me_id = int(current_user()["user_id"])
    user_a, user_b = chat_pair(me_id, friend_id)
    with sqlite3.connect(DATABASE) as db:
        cursor = db.execute("DELETE FROM friendships WHERE user_a_id=? AND user_b_id=?", (user_a, user_b))
        if not cursor.rowcount:
            raise HTTPException(404, "好友关系不存在")
        conversation = db.execute("SELECT conversation_id FROM chat_conversations WHERE user_a_id=? AND user_b_id=?", (user_a, user_b)).fetchone()
    await emit_chat_event({friend_id, me_id}, {"type": "friendship", "status": "removed", "conversation_id": int(conversation[0]) if conversation else None})
    return {"deleted": True, "conversation_retained": True}


@app.get("/api/chat/conversations/{conversation_id}/messages")
async def chat_messages(
    conversation_id: int,
    before: int | None = None,
    after: int | None = None,
    anchor: int | None = None,
    limit: int = 50,
):
    if not 1 <= limit <= 100:
        raise HTTPException(400, "每页消息数量必须为 1 到 100")
    if sum(value is not None for value in (before, after, anchor)) > 1:
        raise HTTPException(400, "before, after and anchor cannot be used together")
    me_id = int(current_user()["user_id"])
    conversation = chat_conversation(conversation_id, me_id)
    peer_id = int(conversation["user_b_id"] if int(conversation["user_a_id"]) == me_id else conversation["user_a_id"])
    select_messages = """SELECT m.*,a.attachment_id,a.kind,a.manifest_json,a.snapshot_json,a.created_at AS attachment_created_at,
                                su.username AS sender_username,su.avatar_version AS sender_avatar_version
                         FROM chat_messages m LEFT JOIN chat_attachments a ON a.message_id=m.message_id
                         JOIN users su ON su.user_id=m.sender_id
                         WHERE m.conversation_id=?"""
    with database_session(DATABASE, rows=True) as db:
        if anchor is not None:
            target = db.execute(
                "SELECT 1 FROM chat_messages WHERE conversation_id=? AND message_id=?",
                (conversation_id, anchor),
            ).fetchone()
            if not target:
                raise HTTPException(404, "Message anchor was not found")
            context_limit = min(12, max(1, limit // 4))
            older_rows = [dict(row) for row in db.execute(
                select_messages + " AND m.message_id<? ORDER BY m.message_id DESC LIMIT ?",
                (conversation_id, anchor, context_limit),
            ).fetchall()]
            newer_rows = [dict(row) for row in db.execute(
                select_messages + " AND m.message_id>=? ORDER BY m.message_id ASC LIMIT ?",
                (conversation_id, anchor, limit - len(older_rows)),
            ).fetchall()]
            rows = list(reversed(older_rows)) + newer_rows
        else:
            query = select_messages
            values: list[Any] = [conversation_id]
            if before is not None:
                query += " AND m.message_id<?"
                values.append(before)
            if after is not None:
                query += " AND m.message_id>?"
                values.append(after)
            query += f" ORDER BY m.message_id {'ASC' if after is not None else 'DESC'} LIMIT ?"
            values.append(limit + 1)
            rows = [dict(row) for row in db.execute(query, tuple(values)).fetchall()]
            rows = rows[:limit]
            if after is None:
                rows.reverse()
        peer = db.execute("SELECT * FROM users WHERE user_id=?", (peer_id,)).fetchone()
        first_id = int(rows[0]["message_id"]) if rows else 0
        last_id = int(rows[-1]["message_id"]) if rows else 0
        has_older = bool(first_id and db.execute(
            "SELECT 1 FROM chat_messages WHERE conversation_id=? AND message_id<? LIMIT 1",
            (conversation_id, first_id),
        ).fetchone())
        has_newer = bool(last_id and db.execute(
            "SELECT 1 FROM chat_messages WHERE conversation_id=? AND message_id>? LIMIT 1",
            (conversation_id, last_id),
        ).fetchone())
    public_peer = public_chat_user(dict(peer))
    return {
        "conversation": {
            "id": conversation_id, "kind": "direct", "peer": public_peer,
            "display": {"title": public_peer["username"], "avatar_url": public_peer["avatar_url"]},
            "read_only": not chat_friendship_active(me_id, peer_id),
        },
        "messages": [chat_message_public(row) for row in rows],
        "has_more": has_newer if after is not None else has_older,
        "has_older": has_older, "has_newer": has_newer,
    }


@app.post("/api/chat/conversations/{conversation_id}/messages")
async def send_chat_message(conversation_id: int, payload: dict[str, Any] = Body(...)):
    me = current_user()
    me_id = int(me["user_id"])
    conversation = chat_conversation(conversation_id, me_id)
    peer_id = int(conversation["user_b_id"] if int(conversation["user_a_id"]) == me_id else conversation["user_a_id"])
    if not chat_friendship_active(me_id, peer_id):
        raise HTTPException(409, "重新添加好友后才能继续发送消息")
    body = unicodedata.normalize("NFKC", str(payload.get("body") or "")).strip()
    if len(body) > CHAT_MESSAGE_LIMIT:
        raise HTTPException(400, "消息不能超过 4000 个字符")
    attachment_descriptor = payload.get("attachment")
    if attachment_descriptor is not None and not isinstance(attachment_descriptor, dict):
        raise HTTPException(400, "分享内容格式无效")
    if not body and not attachment_descriptor:
        raise HTTPException(400, "消息不能为空")
    attachment_data = chat_attachment_manifest(me_id, attachment_descriptor) if attachment_descriptor else None
    nonce = str(payload.get("client_nonce") or "").strip()[:100] or None
    now = utc_now()
    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        if nonce:
            existing = db.execute(
                """SELECT m.*,a.attachment_id,a.kind,a.manifest_json,a.snapshot_json,a.created_at AS attachment_created_at,
                          su.username AS sender_username,su.avatar_version AS sender_avatar_version
                   FROM chat_messages m LEFT JOIN chat_attachments a ON a.message_id=m.message_id
                   JOIN users su ON su.user_id=m.sender_id
                   WHERE m.sender_id=? AND m.client_nonce=?""", (me_id, nonce),
            ).fetchone()
            if existing:
                return chat_message_public(dict(existing))
        cursor = db.execute(
            "INSERT INTO chat_messages(conversation_id,sender_id,sender_name,body,client_nonce,created_at) VALUES(?,?,?,?,?,?)",
            (conversation_id, me_id, me["username"], body, nonce, now),
        )
        message_id = int(cursor.lastrowid)
        attachment_id = None
        if attachment_data:
            kind, group_id, manifest, snapshot = attachment_data
            attachment_id = f"share-{secrets.token_hex(16)}"
            snapshot["sender_name"] = me["username"]
            db.execute(
                "INSERT INTO chat_attachments(attachment_id,message_id,kind,origin_owner_id,origin_group_id,manifest_json,snapshot_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (attachment_id, message_id, kind, me_id, group_id, json.dumps(manifest), json.dumps(snapshot), now),
            )
        db.execute("UPDATE chat_conversations SET last_message_at=? WHERE conversation_id=?", (now, conversation_id))
        row = db.execute(
            """SELECT m.*,a.attachment_id,a.kind,a.manifest_json,a.snapshot_json,a.created_at AS attachment_created_at,
                      su.username AS sender_username,su.avatar_version AS sender_avatar_version
               FROM chat_messages m LEFT JOIN chat_attachments a ON a.message_id=m.message_id
               JOIN users su ON su.user_id=m.sender_id WHERE m.message_id=?""",
            (message_id,),
        ).fetchone()
    await emit_chat_message_event({peer_id, me_id}, dict(row))
    return chat_message_public(dict(row))


def validate_chat_media_body(body: bytes, media_type: str) -> tuple[int | None, int | None]:
    if media_type in CHAT_IMAGE_TYPES:
        try:
            with Image.open(BytesIO(body)) as image:
                image.verify()
            with Image.open(BytesIO(body)) as image:
                return int(image.width), int(image.height)
        except (OSError, UnidentifiedImageError) as exc:
            raise HTTPException(415, "图片文件无效或已损坏") from exc
    if media_type == "video/webm":
        if not body.startswith(b"\x1a\x45\xdf\xa3"):
            raise HTTPException(415, "WebM 文件无效")
    elif media_type in {"video/mp4", "video/quicktime"}:
        if len(body) < 12 or body[4:8] != b"ftyp":
            raise HTTPException(415, "MP4/MOV 文件无效")
    else:
        raise HTTPException(415, "不支持这种媒体格式")
    return None, None


def validate_chat_media_file(path: Path, media_type: str) -> tuple[int | None, int | None]:
    if media_type in CHAT_IMAGE_TYPES:
        try:
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                return int(image.width), int(image.height)
        except (OSError, UnidentifiedImageError) as exc:
            raise HTTPException(415, "图片文件无效或已损坏") from exc
    with path.open("rb") as source:
        header = source.read(16)
    return validate_chat_media_body(header, media_type)


@app.post("/api/chat/conversations/{conversation_id}/media")
async def send_chat_media(
    conversation_id: int,
    files: list[UploadFile] = File(...),
    body: str = Form(default=""),
    client_nonce: str = Form(default=""),
):
    me = current_user()
    me_id = int(me["user_id"])
    conversation = chat_conversation(conversation_id, me_id)
    peer_id = int(conversation["user_b_id"] if int(conversation["user_a_id"]) == me_id else conversation["user_a_id"])
    if not chat_friendship_active(me_id, peer_id):
        raise HTTPException(409, "重新添加好友后才能继续发送消息")
    message_body = unicodedata.normalize("NFKC", str(body or "")).strip()
    if len(message_body) > CHAT_MESSAGE_LIMIT:
        raise HTTPException(400, "消息不能超过 4000 个字符")
    if not files:
        raise HTTPException(400, "请选择要发送的图片或视频")
    media_types = [str(upload.content_type or "").lower() for upload in files]
    all_images = all(media_type in CHAT_IMAGE_TYPES for media_type in media_types)
    all_videos = all(media_type in CHAT_VIDEO_TYPES for media_type in media_types)
    if not all_images and not all_videos:
        raise HTTPException(415, "一条消息只能发送图片组或一个视频，不能混合")
    if all_images and len(files) > MAX_CHAT_IMAGES:
        raise HTTPException(400, f"一条消息最多发送 {MAX_CHAT_IMAGES} 张图片")
    if all_videos and len(files) != 1:
        raise HTTPException(400, "一条消息只能发送一个视频")
    nonce = str(client_nonce or "").strip()[:100] or None
    if nonce:
        with sqlite3.connect(DATABASE) as db:
            db.row_factory = sqlite3.Row
            existing = db.execute(
                """SELECT m.*,a.attachment_id,a.kind,a.manifest_json,a.snapshot_json,a.created_at AS attachment_created_at,
                          su.username AS sender_username,su.avatar_version AS sender_avatar_version
                   FROM chat_messages m LEFT JOIN chat_attachments a ON a.message_id=m.message_id
                   JOIN users su ON su.user_id=m.sender_id
                   WHERE m.sender_id=? AND m.client_nonce=?""", (me_id, nonce),
            ).fetchone()
        if existing:
            return chat_message_public(dict(existing))
    attachment_id = f"media-{secrets.token_hex(16)}"
    relative_folder = Path(str(me_id)) / attachment_id
    target_folder = CHAT_MEDIA_DIR / relative_folder
    target_folder.mkdir(parents=True, exist_ok=False)
    manifest_files: list[dict[str, Any]] = []
    try:
        total_bytes = 0
        for index, (upload, media_type) in enumerate(zip(files, media_types)):
            limit = MAX_UPLOAD_BYTES if media_type in CHAT_IMAGE_TYPES else MAX_CHAT_VIDEO_BYTES
            suffix = CHAT_MEDIA_SUFFIXES[media_type]
            filename = f"{index + 1}-{secrets.token_hex(8)}{suffix}"
            target = target_folder / filename
            size = 0
            with target.open("wb") as destination:
                while chunk := await upload.read(1024 * 1024):
                    size += len(chunk)
                    if size > limit:
                        raise HTTPException(413, "图片不能超过 50 MB，视频不能超过 500 MB")
                    destination.write(chunk)
            width, height = validate_chat_media_file(target, media_type)
            total_bytes += size
            entry = {
                "id": f"media-{index + 1}", "role": "media", "storage": "chat_media",
                "media_type": media_type, "original_name": Path(upload.filename or filename).name,
                "size": size, "width": width, "height": height,
                "image": {"filename": filename, "subfolder": relative_folder.as_posix(), "type": "chat"},
            }
            if media_type in CHAT_VIDEO_TYPES:
                video_data = await asyncio.to_thread(prepare_chat_video, target, relative_folder)
                entry.update(video_data)
                if video_data.get("playback"):
                    entry["original"] = entry.get("image")
                    entry["image"] = video_data["playback"]
                total_bytes += int(video_data.get("playback_size") or 0)
                info = video_data.get("video_info") or {}
                entry["width"] = int(info.get("width") or 0) or None
                entry["height"] = int(info.get("height") or 0) or None
            manifest_files.append(entry)
        if me.get("role") != "admin" and user_storage_bytes(me_id) + total_bytes >= USER_QUOTA_BYTES:
            raise HTTPException(413, "个人存储将超过 20GB，请先清理部分历史或聊天附件")
        media_kind = "images" if all_images else "video"
        snapshot = {
            "title": (f"{len(files)} 张图片" if all_images and len(files) > 1 else Path(files[0].filename or "").name or ("图片" if all_images else "视频")),
            "media_kind": media_kind, "sender_name": me["username"],
        }
        now = utc_now()
        with sqlite3.connect(DATABASE) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            if nonce:
                existing = db.execute("SELECT message_id FROM chat_messages WHERE sender_id=? AND client_nonce=?", (me_id, nonce)).fetchone()
                if existing:
                    raise HTTPException(409, "这条媒体消息已经发送")
            cursor = db.execute(
                "INSERT INTO chat_messages(conversation_id,sender_id,sender_name,body,client_nonce,created_at) VALUES(?,?,?,?,?,?)",
                (conversation_id, me_id, me["username"], message_body, nonce, now),
            )
            message_id = int(cursor.lastrowid)
            db.execute(
                "INSERT INTO chat_attachments(attachment_id,message_id,kind,origin_owner_id,origin_group_id,manifest_json,snapshot_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (attachment_id, message_id, "media", me_id, f"media:{attachment_id}", json.dumps({"version": 2, "files": manifest_files}), json.dumps(snapshot), now),
            )
            db.execute("UPDATE chat_conversations SET last_message_at=? WHERE conversation_id=?", (now, conversation_id))
            row = db.execute(
                """SELECT m.*,a.attachment_id,a.kind,a.manifest_json,a.snapshot_json,a.created_at AS attachment_created_at,
                          su.username AS sender_username,su.avatar_version AS sender_avatar_version
                   FROM chat_messages m LEFT JOIN chat_attachments a ON a.message_id=m.message_id
                   JOIN users su ON su.user_id=m.sender_id WHERE m.message_id=?""",
                (message_id,),
            ).fetchone()
    except Exception:
        shutil.rmtree(target_folder, ignore_errors=True)
        raise
    await emit_chat_message_event({peer_id, me_id}, dict(row))
    return chat_message_public(dict(row))


@app.post("/api/chat/conversations/{conversation_id}/opened")
async def open_chat_conversation(conversation_id: int, payload: dict[str, Any] = Body(default={})):
    me_id = int(current_user()["user_id"])
    chat_conversation(conversation_id, me_id)
    requested = int(payload.get("last_message_id") or 0)
    with sqlite3.connect(DATABASE) as db:
        maximum = int(db.execute("SELECT COALESCE(MAX(message_id),0) FROM chat_messages WHERE conversation_id=?", (conversation_id,)).fetchone()[0])
        opened = min(max(0, requested or maximum), maximum)
        db.execute(
            """INSERT INTO chat_reads(conversation_id,user_id,last_opened_message_id,last_opened_at) VALUES(?,?,?,?)
               ON CONFLICT(conversation_id,user_id) DO UPDATE SET
               last_opened_message_id=MAX(last_opened_message_id,excluded.last_opened_message_id),last_opened_at=excluded.last_opened_at""",
            (conversation_id, me_id, opened, utc_now()),
        )
    overview = chat_overview_for_user(me_id)
    summary = next(
        (item for item in overview["conversations"] if item["id"] == conversation_id),
        None,
    )
    return {"opened": opened, "conversation": summary, "unread_total": overview["unread"]}


@app.get("/api/chat/attachments/{attachment_id}")
async def chat_attachment_detail(attachment_id: str):
    return chat_attachment_detail_public(chat_attachment_record(attachment_id))


@app.get("/api/chat/attachments/{attachment_id}/files/{file_id}")
async def chat_attachment_file(attachment_id: str, file_id: str, preview: int | None = None, download: bool = False):
    attachment = chat_attachment_record(attachment_id)
    files = attachment["manifest"].get("files") or []
    entry = next((item for item in files if isinstance(item, dict) and item.get("id") == file_id), None)
    path = chat_manifest_path(entry) if entry else None
    if not path or not path.is_file():
        raise HTTPException(404, "分享文件已不可用")
    original_media_type = str(entry.get("media_type") or "") if entry else ""
    playback = chat_playback_path(entry) if entry else None
    if not download and original_media_type.startswith("video/") and playback and playback.is_file():
        path = playback
        media_type = "video/mp4"
    else:
        media_type = original_media_type
    if media_type.startswith("video/") or download:
        headers = private_media_cache_headers(3600, accept_ranges=True)
        disposition = "attachment" if download else "inline"
        filename = Path(str(entry.get("original_name") or path.name)).name if entry else path.name
        headers["Content-Disposition"] = f"{disposition}; filename*=UTF-8''{quote(filename)}"
        return FileResponse(path, media_type=media_type or None, headers=headers)
    return await private_image_response(path, preview)


@app.get("/api/chat/attachments/{attachment_id}/files/{file_id}/poster")
async def chat_attachment_video_poster(attachment_id: str, file_id: str, preview: int = 512):
    """Return a private first-frame poster for a shared video attachment."""
    attachment = chat_attachment_record(attachment_id)
    files = attachment["manifest"].get("files") or []
    entry = next((item for item in files if isinstance(item, dict) and item.get("id") == file_id), None)
    path = chat_manifest_path(entry) if entry else None
    media_type = str(entry.get("media_type") or "") if entry else ""
    if not path or not path.is_file():
        raise HTTPException(404, "Shared file is no longer available")
    if not media_type.startswith("video/"):
        raise HTTPException(400, "Poster previews are only available for videos")
    return await private_video_poster_response(path, preview, create_thumbnail=video_thumbnail_for)


@app.post("/api/chat/attachments/{attachment_id}/prepare-edit")
async def prepare_chat_attachment_edit(attachment_id: str, payload: dict[str, Any] = Body(default={})):
    me = current_user()
    attachment = chat_attachment_record(attachment_id, int(me["user_id"]))
    if attachment["kind"] != "media" and int(attachment["origin_owner_id"]) == int(me["user_id"]):
        raise HTTPException(403, "请从自己的历史记录继续编辑")
    files = attachment["manifest"].get("files") or []
    requested = str(payload.get("file_id") or "")
    entry = next((item for item in files if isinstance(item, dict) and item.get("id") == requested), None) if requested else None
    if not entry:
        entry = next((item for item in files if isinstance(item, dict) and item.get("role") in {"image", "final"}), None)
    path = chat_manifest_path(entry) if entry else None
    media_type = str(entry.get("media_type") or "") if entry else ""
    if not path or not path.is_file() or (entry.get("storage") != "output" and not (entry.get("storage") == "chat_media" and media_type.startswith("image/"))):
        raise HTTPException(409, "这张分享图片已不可用")
    enforce_user_capacity(path.stat().st_size)
    if entry.get("storage") == "chat_media":
        suffix = path.suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
            raise HTTPException(415, "只能编辑 JPEG、PNG 和 WebP 图片")
        filename = f"chat_edit_{secrets.token_hex(16)}{suffix}"
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, UPLOAD_DIR / filename)
        record = {"filename": filename, "subfolder": "mobile_uploads", "type": "input"}
    else:
        _, record = copy_image_for_processing(entry["image"], "chat_edit")
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    db_execute(
        "INSERT INTO chat_prepared_assets(token_hash,user_id,attachment_id,file_id,source_image_json,created_at,expires_at) VALUES(?,?,?,?,?,?,?)",
        (token_digest(token), int(me["user_id"]), attachment_id, entry["id"], json.dumps(record), now.isoformat(), (now + CHAT_PREPARED_RETENTION).isoformat()),
    )
    return {"token": token, "expires_at": (now + CHAT_PREPARED_RETENTION).isoformat(), "preview_url": f"/api/chat/prepared/{token}/image?preview=512"}


@app.get("/api/chat/prepared/{token}")
async def chat_prepared_info(token: str):
    prepared_chat_asset(token)
    return {"token": token, "preview_url": f"/api/chat/prepared/{token}/image?preview=512"}


@app.get("/api/chat/prepared/{token}/image")
async def chat_prepared_image(token: str, preview: int | None = None):
    image = prepared_chat_asset(token)
    path = safe_source_path(image)
    if not path or not path.is_file():
        raise HTTPException(404, "临时编辑图片已不可用")
    return await private_image_response(path, preview)


@app.post("/api/chat/attachments/{attachment_id}/import")
async def import_chat_attachment(attachment_id: str):
    me = current_user()
    me_id = int(me["user_id"])
    attachment = chat_attachment_record(attachment_id, me_id)
    if int(attachment["origin_owner_id"]) == me_id:
        raise HTTPException(403, "这项内容已经在你的历史记录中")
    with sqlite3.connect(DATABASE) as db:
        existing = db.execute("SELECT imported_group_id FROM chat_imports WHERE user_id=? AND attachment_id=?", (me_id, attachment_id)).fetchone()
    if existing:
        group = db_group(str(existing[0]))
        if group:
            return {"job": public_group(group), "existing": True}
    files = attachment["manifest"].get("files") or []
    available = [entry for entry in files if isinstance(entry, dict) and (path := chat_manifest_path(entry)) and path.is_file()]
    essential = [entry for entry in available if entry.get("role") in {"image", "final"}]
    if not essential:
        raise HTTPException(409, "发送方的原始输出已经删除，无法保存")
    extra_bytes = sum({chat_manifest_path(entry).resolve(): chat_manifest_path(entry).stat().st_size for entry in available if chat_manifest_path(entry)}.values())
    if me.get("role") != "admin" and user_storage_bytes(me_id) + extra_bytes >= USER_QUOTA_BYTES:
        raise HTTPException(413, "个人存储将超过 20GB，请先清理部分历史")
    group_id = f"group-chat-{secrets.token_hex(12)}"
    copied: dict[str, dict[str, str]] = {}
    copied_paths: list[Path] = []
    try:
        for entry in available:
            record = copy_chat_manifest_file(entry, group_id)
            copied[str(entry["id"])] = record
            path = safe_source_path(record) if record["type"] == "input" else safe_output_path(record)
            if path:
                copied_paths.append(path)
        snapshot = attachment["snapshot"]
        item_ids = [str(item.get("id")) for item in snapshot.get("items", []) if isinstance(item, dict) and item.get("id")]
        if attachment["kind"] == "image":
            item_ids = ["shared-image"]
        imported_items: list[dict[str, Any]] = []
        for item_id_value in item_ids:
            final_entry = next((entry for entry in available if entry.get("item_id") == item_id_value and entry.get("role") == "final"), None)
            if attachment["kind"] == "image":
                final_entry = next((entry for entry in available if entry.get("role") == "image"), None)
            if not final_entry:
                continue
            stage_entry = next((entry for entry in available if entry.get("item_id") == item_id_value and entry.get("role") == "stage1"), None)
            imported_items.append({
                "id": item_id_value, "prompt_id": "", "final": copied[str(final_entry["id"])],
                "stage1": copied.get(str(stage_entry["id"])) if stage_entry else None,
            })
        if not imported_items:
            raise HTTPException(409, "分享任务的结果已经不可用")
        source_entry = next((entry for entry in available if entry.get("role") == "source"), None)
        edit_source_entry = next((entry for entry in available if entry.get("role") == "edit_source"), None)
        reference_entry = next((entry for entry in available if entry.get("role") == "reference"), None)
        mask_entry = next((entry for entry in available if entry.get("role") == "mask"), None)
        preprocess_entry = next((entry for entry in available if entry.get("role") == "preprocess"), None)
        now = utc_now()
        missing = [str(entry.get("id")) for entry in files if isinstance(entry, dict) and str(entry.get("id")) not in copied]
        origin = {
            "type": "chat", "attachment_id": attachment_id, "message_id": int(attachment["message_id"]),
            "sender_id": int(attachment["sender_id"]), "sender_name": attachment["sender_name"],
            "missing_files": missing,
        }
        parameters = snapshot.get("parameters") if isinstance(snapshot.get("parameters"), dict) else {}
        workflow = str(snapshot.get("workflow") or "shared-image")
        with sqlite3.connect(DATABASE) as db:
            db.execute("BEGIN IMMEDIATE")
            duplicate = db.execute("SELECT imported_group_id FROM chat_imports WHERE user_id=? AND attachment_id=?", (me_id, attachment_id)).fetchone()
            if duplicate:
                raise HTTPException(409, "分享内容已经保存")
            db.execute(
                """INSERT INTO generation_groups(
                    group_id,workflow_key,prompt_text,source_prompt_text,title,status,submitted_at,completed_at,
                    outputs_json,parameters_json,items_json,source_image_json,edit_source_image_json,
                    reference_image_json,edit_mask_json,preprocess_json,storage_scope,owner_id,origin_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    group_id, workflow, str(snapshot.get("prompt") or ""), str(snapshot.get("source_prompt") or ""),
                    str(snapshot.get("title") or f"来自 {attachment['sender_name']} 的分享"), "completed",
                    str(snapshot.get("submitted_at") or now), str(snapshot.get("completed_at") or now),
                    json.dumps(outputs_from_items(imported_items)), json.dumps(parameters), json.dumps(imported_items),
                    json.dumps(copied.get(str(source_entry["id"]), {}) if source_entry else {}),
                    json.dumps(copied.get(str(edit_source_entry["id"]), {}) if edit_source_entry else {}),
                    json.dumps(copied.get(str(reference_entry["id"]), {}) if reference_entry else {}),
                    json.dumps(copied.get(str(mask_entry["id"]), {}) if mask_entry else {}),
                    json.dumps({"output": copied[str(preprocess_entry["id"])], "parameters": {}} if preprocess_entry else {}),
                    "mobile", me_id, json.dumps(origin),
                ),
            )
            item_map = {str(item["id"]): item for item in imported_items}
            for entry in available:
                role = entry.get("role")
                parent = item_map.get(str(entry.get("item_id")))
                if role not in {"upscale", "detail"} or not parent:
                    continue
                record_id = f"chat-{role}-{secrets.token_hex(10)}"
                if role == "upscale":
                    db.execute(
                        """INSERT INTO upscale_jobs(upscale_id,parent_group_id,parent_item_id,status,submitted_at,completed_at,
                           output_json,parameters_json,source_image_json,source_kind,engine,owner_id)
                           VALUES(?,?,?,'completed',?,?,?,?,?,?,?,?)""",
                        (
                            record_id, group_id, parent["id"], str(entry.get("submitted_at") or now),
                            str(entry.get("completed_at") or now), json.dumps(copied[str(entry["id"])]),
                            json.dumps(entry.get("parameters") or {}), json.dumps(parent["final"]),
                            str(entry.get("source_kind") or "edit"), str(entry.get("engine") or "shared"), me_id,
                        ),
                    )
                else:
                    db.execute(
                        """INSERT INTO detail_jobs(detail_id,parent_group_id,parent_item_id,status,submitted_at,completed_at,
                           output_json,parameters_json,source_image_json,owner_id)
                           VALUES(?,?,?,'completed',?,?,?,?,?,?)""",
                        (record_id, group_id, parent["id"], str(entry.get("submitted_at") or now), str(entry.get("completed_at") or now), json.dumps(copied[str(entry["id"])]), json.dumps(entry.get("parameters") or {}), json.dumps(parent["final"]), me_id),
                    )
            db.execute(
                "INSERT INTO chat_imports(user_id,attachment_id,imported_group_id,created_at) VALUES(?,?,?,?)",
                (me_id, attachment_id, group_id, now),
            )
    except Exception:
        for path in copied_paths:
            path.unlink(missing_ok=True)
        folder = MOBILE_OUTPUT_DIR / "imported" / group_id
        if folder.is_dir():
            try: folder.rmdir()
            except OSError: pass
        raise
    await emit_chat_event({me_id}, {"type": "attachment_imported", "attachment_id": attachment_id, "group_id": group_id})
    return {"job": public_group(db_group(group_id) or {}), "existing": False}


@app.get("/api/admin/invites")
async def admin_invites():
    require_admin()
    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(row) for row in db.execute("SELECT invite_id,created_at,expires_at,used_at,revoked_at FROM invites ORDER BY invite_id DESC").fetchall()]
    return rows


@app.post("/api/admin/invites")
async def admin_create_invite(payload: dict[str, Any] = Body(default={})):
    admin = require_admin()
    days = int(payload.get("days", 7))
    if not 1 <= days <= 30:
        raise HTTPException(400, "邀请码有效期必须为 1 到 30 天")
    token = secrets.token_urlsafe(24)
    now = datetime.now(timezone.utc)
    with sqlite3.connect(DATABASE) as db:
        cursor = db.execute(
            "INSERT INTO invites(token_hash,created_by,created_at,expires_at) VALUES(?,?,?,?)",
            (token_digest(token), admin["user_id"], now.isoformat(), (now + timedelta(days=days)).isoformat()),
        )
        invite_id = cursor.lastrowid
    return {"id": invite_id, "token": token, "expires_at": (now + timedelta(days=days)).isoformat()}


@app.delete("/api/admin/invites/{invite_id}")
async def admin_revoke_invite(invite_id: int):
    require_admin()
    db_execute("UPDATE invites SET revoked_at=? WHERE invite_id=? AND used_at IS NULL", (utc_now(), invite_id))
    return {"revoked": True}


@app.get("/api/admin/users")
async def admin_users():
    require_admin()
    with database_session(DATABASE, rows=True) as db:
        rows = [dict(row) for row in db.execute("SELECT * FROM users ORDER BY created_at").fetchall()]
    def rows_with_storage() -> list[dict[str, Any]]:
        usage = user_storage_snapshot()
        return [
            public_user(row) | {
                "created_at": row["created_at"],
                "storage_bytes": int(usage.get(int(row["user_id"]), 0)),
            }
            for row in rows
        ]

    return await asyncio.to_thread(rows_with_storage)


def directory_metrics(path: Path) -> dict[str, int]:
    total_bytes = 0
    file_count = 0
    if path.is_dir():
        for candidate in path.rglob("*"):
            try:
                if candidate.is_file():
                    file_count += 1
                    total_bytes += candidate.stat().st_size
            except OSError:
                continue
    return {"bytes": total_bytes, "files": file_count}


_DIAGNOSTICS_CACHE_LOCK = threading.Lock()
_DIAGNOSTICS_CACHE: dict[str, Any] | None = None
_DIAGNOSTICS_CACHE_EXPIRES_AT = 0.0
_DIAGNOSTICS_CACHE_KEY: tuple[str, ...] | None = None
_COMFY_DIAGNOSTICS_CACHE_LOCK = threading.Lock()
_COMFY_DIAGNOSTICS_CACHE: dict[str, Any] | None = None
_COMFY_DIAGNOSTICS_CACHE_EXPIRES_AT = 0.0


def invalidate_diagnostics_cache() -> None:
    global _DIAGNOSTICS_CACHE_EXPIRES_AT
    with _DIAGNOSTICS_CACHE_LOCK:
        _DIAGNOSTICS_CACHE_EXPIRES_AT = 0.0


def runtime_diagnostics_snapshot() -> dict[str, Any]:
    with database_session(DATABASE) as db:
        quick_check = str(db.execute("PRAGMA quick_check").fetchone()[0])
        tables = {
            table: int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("users", "generation_groups", "jobs", "task_queue", "chat_messages", "sessions")
        }
        queue = {
            str(row[0]): int(row[1])
            for row in db.execute("SELECT state, COUNT(*) FROM task_queue GROUP BY state")
        }
    database_files = {}
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{DATABASE}{suffix}")
        database_files[candidate.name] = candidate.stat().st_size if candidate.is_file() else 0
    return {
        "generated_at": utc_now(),
        "database": {"quick_check": quick_check, "files": database_files, "rows": tables},
        "queue": queue,
        "storage": {
            "mobile_output": directory_metrics(MOBILE_OUTPUT_DIR),
            "thumbnails": directory_metrics(THUMBNAIL_DIR),
            "chat_media": directory_metrics(CHAT_MEDIA_DIR),
            "avatars": directory_metrics(AVATAR_DIR),
            "logs": directory_metrics(DATA_DIR / "logs"),
        },
        "frontend": directory_metrics(STATIC_DIR),
    }


def cached_runtime_diagnostics_snapshot(force: bool = False) -> dict[str, Any]:
    global _DIAGNOSTICS_CACHE, _DIAGNOSTICS_CACHE_EXPIRES_AT, _DIAGNOSTICS_CACHE_KEY
    with _DIAGNOSTICS_CACHE_LOCK:
        now = time.monotonic()
        cache_key = tuple(str(path.resolve()) for path in (
            DATABASE, MOBILE_OUTPUT_DIR, THUMBNAIL_DIR, CHAT_MEDIA_DIR, AVATAR_DIR, DATA_DIR, STATIC_DIR,
        ))
        if not force and _DIAGNOSTICS_CACHE_KEY == cache_key and _DIAGNOSTICS_CACHE is not None and now < _DIAGNOSTICS_CACHE_EXPIRES_AT:
            return dict(_DIAGNOSTICS_CACHE) | {"stale": False}
        try:
            snapshot = runtime_diagnostics_snapshot()
        except Exception:
            if _DIAGNOSTICS_CACHE_KEY == cache_key and _DIAGNOSTICS_CACHE is not None:
                return dict(_DIAGNOSTICS_CACHE) | {"stale": True}
            raise
        _DIAGNOSTICS_CACHE = snapshot
        _DIAGNOSTICS_CACHE_KEY = cache_key
        _DIAGNOSTICS_CACHE_EXPIRES_AT = time.monotonic() + DIAGNOSTICS_CACHE_SECONDS
        return dict(snapshot) | {"stale": False}


async def cached_comfy_diagnostics(force: bool = False) -> dict[str, Any]:
    global _COMFY_DIAGNOSTICS_CACHE, _COMFY_DIAGNOSTICS_CACHE_EXPIRES_AT
    with _COMFY_DIAGNOSTICS_CACHE_LOCK:
        now = time.monotonic()
        if not force and _COMFY_DIAGNOSTICS_CACHE is not None and now < _COMFY_DIAGNOSTICS_CACHE_EXPIRES_AT:
            return dict(_COMFY_DIAGNOSTICS_CACHE)
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            response = await client.get(f"{COMFY_URL}/system_stats")
            result = {
                "reachable": response.is_success,
                "status": response.status_code,
                "checked_at": utc_now(),
            }
    except httpx.HTTPError:
        result = {"reachable": False, "checked_at": utc_now()}
    with _COMFY_DIAGNOSTICS_CACHE_LOCK:
        _COMFY_DIAGNOSTICS_CACHE = result
        _COMFY_DIAGNOSTICS_CACHE_EXPIRES_AT = time.monotonic() + COMFY_DIAGNOSTICS_CACHE_SECONDS
    return dict(result)


diagnostics_router = create_diagnostics_router(
    require_admin,
    cached_runtime_diagnostics_snapshot,
    cached_comfy_diagnostics,
)
# Keep ``app.routes`` compatible with the existing route-introspection tools.
# FastAPI's current ``include_router`` implementation adds a private marker
# without a ``path`` attribute, while this router contains ordinary API routes
# and does not need nested-router lifecycle handling.
app.router.routes.extend(diagnostics_router.routes)

queue_router = create_queue_router(QueueRouteDependencies(
    current_user=current_user,
    require_admin=require_admin,
    queue_snapshot=queue_snapshot,
    queue_row=queue_row,
    cancel_queued_task=cancel_queued_task,
    queue_lock=queue_lock,
    recovery_status=comfy_recovery_status,
    set_recovery_status=set_comfy_recovery_status,
    restart_comfyui=restart_comfyui_and_verify,
    current_user_context=CURRENT_USER,
    set_performance_mode=set_performance_mode,
    database_path=lambda: DATABASE,
    db_user=db_user,
    db_queue_record=db_queue_record,
    cancel_postprocess=cancel_postprocess,
    cancel_group=cancel_group,
))
app.router.routes.extend(queue_router.routes)
cancel_shared_queue_task = queue_router.cancel_shared_queue_task
admin_activity = queue_router.admin_activity

workflow_router = create_workflow_router(WorkflowRouteDependencies(
    synchronize_lora_registry=synchronize_lora_registry,
    enabled_workflows=enabled_workflows,
    workflow_descriptor=workflow_descriptor,
    get_spec=get_spec,
    normalize_settings=normalize_settings,
    source_dimensions=h3_source_dimensions,
    apply_history_estimate=apply_h3_history_estimate,
))
app.router.routes.extend(workflow_router.routes)
workflows = workflow_router.workflows
minimax_h3_estimate = workflow_router.minimax_h3_estimate

auth_router = create_auth_router(AuthRouteDependencies(
    normalize_username=normalize_username,
    password_hash=password_hash,
    verify_password=verify_password,
    token_digest=token_digest,
    db_user=db_user,
    db_user_by_name=db_user_by_name,
    issue_session=issue_session,
    current_user=current_user,
    public_user=public_user,
    utc_now=utc_now,
    db_execute=db_execute,
    database_path=lambda: DATABASE,
    login_failures=LOGIN_FAILURES,
    default_collection_name=DEFAULT_COLLECTION_NAME,
    session_cookie=SESSION_COOKIE,
    session_days=SESSION_DAYS,
))
app.router.routes.extend(auth_router.routes)
auth_login = auth_router.auth_login
auth_register = auth_router.auth_register
auth_me = auth_router.auth_me
auth_logout = auth_router.auth_logout
auth_logout_all = auth_router.auth_logout_all


@app.get("/api/account/storage", response_model=AccountStorageResponse)
async def account_storage():
    user = current_user()
    used = await asyncio.to_thread(user_storage_bytes, int(user["user_id"]))
    return {"used_bytes": used, "quota_bytes": None if user["role"] == "admin" else USER_QUOTA_BYTES}


@app.post("/api/device-task-status")
async def device_task_status(payload: dict[str, Any] = Body(...)):
    """Resolve a bounded list of locally tracked image tasks for the current user."""
    raw_tasks = payload.get("tasks")
    if not isinstance(raw_tasks, list):
        raise HTTPException(400, "tasks must be a list")
    if len(raw_tasks) > 50:
        raise HTTPException(400, "At most 50 tasks can be checked at once")
    allowed = {
        "generation": ("generation_groups", "group_id"),
        "upscale": ("upscale_jobs", "upscale_id"),
        "detail": ("detail_jobs", "detail_id"),
    }
    normalized: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in raw_tasks:
        if not isinstance(raw, dict):
            raise HTTPException(400, "Each task must be an object")
        task_type = str(raw.get("type") or "")
        record_id = str(raw.get("id") or "").strip()
        if task_type not in allowed or not record_id or len(record_id) > 160:
            raise HTTPException(400, "Invalid task type or id")
        key = (task_type, record_id)
        if key not in seen:
            normalized.append(key)
            seen.add(key)
    owner_id = current_owner_id()
    results: list[dict[str, Any]] = []
    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        for task_type, record_id in normalized:
            table, id_column = allowed[task_type]
            row = db.execute(
                f"SELECT status,completed_at,error_message FROM {table} "
                f"WHERE {id_column}=? AND owner_id=?",
                (record_id, owner_id),
            ).fetchone()
            results.append({
                "type": task_type,
                "id": record_id,
                "exists": bool(row),
                "status": row["status"] if row else None,
                "finished_at": row["completed_at"] if row else None,
                "error": row["error_message"] if row else None,
            })
    return {"tasks": results}


@app.patch("/api/admin/users/{user_id}")
async def admin_update_user(user_id: int, payload: dict[str, Any] = Body(...)):
    admin = require_admin()
    target = db_user(user_id)
    if not target:
        raise HTTPException(404, "账号不存在")
    if "disabled" in payload:
        disabled = int(bool(payload["disabled"]))
        if user_id == admin["user_id"] and disabled:
            raise HTTPException(400, "不能停用当前管理员")
        db_execute("UPDATE users SET disabled=?,updated_at=? WHERE user_id=?", (disabled, utc_now(), user_id))
        if disabled:
            db_execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    if payload.get("password"):
        db_execute("UPDATE users SET password_hash=?,updated_at=? WHERE user_id=?", (password_hash(str(payload["password"])), utc_now(), user_id))
        db_execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    return public_user(db_user(user_id) or target)


@app.delete("/api/admin/users/{user_id}")
async def admin_delete_user(user_id: int, purge: bool = False):
    admin = require_admin()
    if user_id == admin["user_id"]:
        raise HTTPException(400, "不能删除当前管理员")
    target = db_user(user_id)
    if not target:
        raise HTTPException(404, "账号不存在")
    if purge and active_task_count(user_id):
        raise HTTPException(409, "请先停止该用户正在运行或排队的任务")
    db_execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    if not purge:
        anonymous = f"deleted-{user_id}-{secrets.token_hex(4)}"
        avatar_path(user_id).unlink(missing_ok=True)
        db_execute(
            "UPDATE users SET username=?,username_key=?,password_hash=?,disabled=1,avatar_version=0,updated_at=? WHERE user_id=?",
            (anonymous, anonymous, password_hash(secrets.token_urlsafe(24)), utc_now(), user_id),
        )
        return {"deleted": True, "content_preserved": True}
    with sqlite3.connect(DATABASE) as db:
        group_ids = [row[0] for row in db.execute("SELECT group_id FROM generation_groups WHERE owner_id=?", (user_id,))]
        job_ids = [row[0] for row in db.execute("SELECT prompt_id FROM jobs WHERE owner_id=? AND group_id IS NULL", (user_id,))]
        standalone_ids = [row[0] for row in db.execute("SELECT upscale_id FROM upscale_jobs WHERE owner_id=? AND source_kind='standalone'", (user_id,))]
        preprocess_ids = [row[0] for row in db.execute("SELECT preprocess_id FROM preprocess_jobs WHERE owner_id=?", (user_id,))]
        prompt_tools = [dict(zip(("tool_id", "source_image_json"), row)) for row in db.execute(
            "SELECT tool_id,source_image_json FROM prompt_tool_jobs WHERE owner_id=?", (user_id,)
        ).fetchall()]
    token = CURRENT_USER.set(target)
    try:
        for record_id in group_ids + job_ids:
            hard_delete_job(record_id)
        for record_id in standalone_ids:
            row = db_upscale(record_id)
            if row and row["status"] not in {"queued", "running"}:
                await delete_upscale(record_id)
        for record_id in preprocess_ids:
            row = db_preprocess(record_id)
            if row and row["status"] not in {"queued", "running"}:
                await delete_preprocess(record_id)
        for row in prompt_tools:
            remove_prompt_tool_source(row)
    finally:
        CURRENT_USER.reset(token)
    with sqlite3.connect(DATABASE) as db:
        collection_ids = [row[0] for row in db.execute("SELECT collection_id FROM favorite_collections WHERE owner_id=?", (user_id,))]
        db.row_factory = sqlite3.Row
        prepared_assets = [dict(row) for row in db.execute("SELECT * FROM chat_prepared_assets WHERE user_id=?", (user_id,)).fetchall()]
        conversation_ids = [row[0] for row in db.execute("SELECT conversation_id FROM chat_conversations WHERE user_a_id=? OR user_b_id=?", (user_id, user_id)).fetchall()]
        media_attachments = [dict(row) for row in db.execute("SELECT manifest_json FROM chat_attachments WHERE origin_owner_id=? AND kind='media'", (user_id,)).fetchall()]
        for prepared in prepared_assets:
            image = json_image_from(prepared, "source_image_json")
            path = safe_source_path(image) if image else None
            if path and path.is_file():
                path.unlink()
        for attachment in media_attachments:
            try:
                manifest = json.loads(attachment.get("manifest_json") or "{}")
            except json.JSONDecodeError:
                manifest = {}
            for entry in manifest.get("files") or []:
                path = chat_manifest_path(entry) if isinstance(entry, dict) else None
                if path and path.is_file():
                    path.unlink()
        user_media_dir = (CHAT_MEDIA_DIR / str(user_id)).resolve()
        if user_media_dir.is_relative_to(CHAT_MEDIA_DIR.resolve()) and user_media_dir.is_dir():
            shutil.rmtree(user_media_dir, ignore_errors=True)
        for conversation_id in conversation_ids:
            db.execute("DELETE FROM chat_attachments WHERE message_id IN (SELECT message_id FROM chat_messages WHERE conversation_id=?)", (conversation_id,))
            db.execute("DELETE FROM chat_messages WHERE conversation_id=?", (conversation_id,))
            db.execute("DELETE FROM chat_reads WHERE conversation_id=?", (conversation_id,))
            db.execute("DELETE FROM chat_conversations WHERE conversation_id=?", (conversation_id,))
        db.execute("DELETE FROM chat_prepared_assets WHERE user_id=?", (user_id,))
        db.execute("DELETE FROM chat_imports WHERE user_id=?", (user_id,))
        db.execute("DELETE FROM friend_requests WHERE sender_id=? OR receiver_id=?", (user_id, user_id))
        db.execute("DELETE FROM friendships WHERE user_a_id=? OR user_b_id=?", (user_id, user_id))
        for collection_id in collection_ids:
            db.execute("DELETE FROM collection_memberships WHERE collection_id=?", (collection_id,))
        db.execute("DELETE FROM favorite_collections WHERE owner_id=?", (user_id,))
        db.execute("DELETE FROM prompt_templates WHERE owner_id=?", (user_id,))
        db.execute("DELETE FROM prompt_tool_jobs WHERE owner_id=?", (user_id,))
        db.execute("DELETE FROM invites WHERE created_by=? OR used_by=?", (user_id, user_id))
        db.execute("DELETE FROM task_queue WHERE owner_id=?", (user_id,))
        db.execute("DELETE FROM users WHERE user_id=?", (user_id,))
    avatar_path(user_id).unlink(missing_ok=True)
    return {"deleted": True, "content_preserved": False}

@app.get("/")
async def index(request: Request):
    return precompressed_file_response(request, STATIC_DIR / "index.html", "text/html")

@app.get("/create")
@app.get("/flux-upscale")
@app.get("/history")
@app.get("/history/trash")
@app.get("/messages")
@app.get("/messages/{conversation_id}")
@app.get("/messages/{conversation_id}/info")
@app.get("/messages/{conversation_id}/attachments/{attachment_id}")
@app.get("/settings")
@app.get("/settings/account")
@app.get("/settings/templates")
@app.get("/settings/help")
@app.get("/settings/collections")
@app.get("/settings/storage")
@app.get("/settings/admin")
async def app_page(request: Request):
    return precompressed_file_response(request, STATIC_DIR / "index.html", "text/html")

@app.get("/settings/tips")
async def help_alias_page(): return RedirectResponse("/settings/help", status_code=307)

@app.get("/jobs/{prompt_id}")
async def job_detail_page(request: Request, prompt_id: str):
    return precompressed_file_response(request, STATIC_DIR / "index.html", "text/html")


def public_tip(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["tip_id"], "placement": row["placement"],
        "workflow": row.get("workflow_key"), "title": row["title"],
        "body": row["body"], "enabled": bool(row["enabled"]),
        "sort_order": row["sort_order"], "updated_at": row["updated_at"],
    }


def tip_values(payload: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    current = existing or {}
    placement = str(payload.get("placement", current.get("placement", ""))).strip()
    workflow_key = payload.get("workflow", current.get("workflow_key"))
    workflow_key = str(workflow_key).strip() if workflow_key not in {None, ""} else None
    title = str(payload.get("title", current.get("title", ""))).strip()
    body = str(payload.get("body", current.get("body", ""))).strip()
    enabled = as_bool(payload.get("enabled", current.get("enabled", True)), "tip enabled")
    sort_order = as_number(payload.get("sort_order", current.get("sort_order", 0)), "tip sort order", 0, 999, True)
    if placement not in TIP_PLACEMENTS:
        raise HTTPException(400, "Invalid tip placement")
    if workflow_key is not None and workflow_key not in ENABLED_WORKFLOW_KEYS:
        raise HTTPException(400, "Invalid tip workflow")
    if not 1 <= len(title) <= 60:
        raise HTTPException(400, "Tip title must contain 1 to 60 characters")
    if not 1 <= len(body) <= 600:
        raise HTTPException(400, "Tip body must contain 1 to 600 characters")
    return {"placement": placement, "workflow_key": workflow_key, "title": title, "body": body, "enabled": int(enabled), "sort_order": sort_order}


def all_usage_tips(include_disabled: bool = False) -> list[dict[str, Any]]:
    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        query = "SELECT * FROM usage_tips" + ("" if include_disabled else " WHERE enabled=1") + " ORDER BY sort_order,tip_id"
        rows = [dict(row) for row in db.execute(query).fetchall()]
    return [public_tip(row) for row in rows]


def public_help_entry(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["help_id"], "scope": row["scope"], "placement": row["scope"],
        "workflow": row.get("workflow_key"), "topic": row["topic_key"],
        "title": row["title"], "body": row["body"],
        "enabled": bool(row["enabled"]), "sort_order": row["sort_order"],
        "updated_at": row["updated_at"],
    }


def all_help_entries(include_disabled: bool = False, workflow_key: str | None = None) -> list[dict[str, Any]]:
    clauses, values = [], []
    if not include_disabled:
        clauses.append("enabled=1")
    if workflow_key:
        clauses.append("(workflow_key IS NULL OR workflow_key=?)")
        values.append(workflow_key)
    query = "SELECT * FROM help_entries"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY sort_order,help_id"
    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(row) for row in db.execute(query, values).fetchall()]
    return [public_help_entry(row) for row in rows]


def page_help_entries(workflow_key: str | None = None, include_disabled: bool = False) -> dict[str, dict[str, Any] | None]:
    clauses = ["scope='page'"]
    values: list[Any] = []
    if not include_disabled:
        clauses.append("enabled=1")
    if workflow_key:
        clauses.append("workflow_key=?")
        values.append(workflow_key)
        topics = ("create_step_1", "create_step_2")
    else:
        clauses.append("workflow_key IS NULL")
        topics = ("flux_upscale",)
    query = "SELECT * FROM help_entries WHERE " + " AND ".join(clauses) + " ORDER BY help_id"
    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        rows = {row["topic_key"]: public_help_entry(dict(row)) for row in db.execute(query, values).fetchall()}
    return {topic: rows.get(topic) for topic in topics}


def public_lora_metadata(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": row["lora_name"], "family": row["family"],
        "description": row.get("description") or "",
        "recommended_min": row.get("recommended_min"),
        "recommended_max": row.get("recommended_max"),
        "category_id": row.get("category_id"),
        "enabled": bool(row.get("enabled", 1)), "updated_at": row.get("updated_at"),
    }


def lora_category_rows() -> list[dict[str, Any]]:
    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT category_id,name,sort_order,created_at,updated_at FROM lora_categories ORDER BY sort_order,category_id",
        ).fetchall()
    return [dict(row) for row in rows]


def lora_category_id(value: Any, *, required: bool = False) -> int | None:
    if value is None or value == "":
        if required:
            raise HTTPException(400, "LoRA category is required")
        return None
    category_id = as_number(value, "LoRA category", 1, 2_147_483_647, True)
    with sqlite3.connect(DATABASE) as db:
        if not db.execute("SELECT 1 FROM lora_categories WHERE category_id=?", (category_id,)).fetchone():
            raise HTTPException(400, "LoRA category does not exist")
    return int(category_id)


def lora_category_name(value: Any) -> str:
    name = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not 1 <= len(name) <= 30:
        raise HTTPException(400, "LoRA category name must contain 1 to 30 characters")
    return name


def lora_metadata_rows(family: str | None = None, include_disabled: bool = False) -> list[dict[str, Any]]:
    clauses, values = [], []
    if family:
        clauses.append("family=?")
        values.append(family)
    if not include_disabled:
        clauses.append("enabled=1")
    query = "SELECT * FROM lora_metadata" + ((" WHERE " + " AND ".join(clauses)) if clauses else "") + " ORDER BY family,lora_name"
    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        return [public_lora_metadata(dict(row)) for row in db.execute(query, values).fetchall()]


def lora_favorite_names(family: str | None = None) -> list[str]:
    owner = current_owner_id()
    if owner is None:
        return []
    clauses, values = ["owner_id=?"], [owner]
    if family:
        clauses.append("family=?")
        values.append(family)
    query = "SELECT lora_name FROM lora_favorites WHERE " + " AND ".join(clauses) + " ORDER BY created_at,lora_name"
    with sqlite3.connect(DATABASE) as db:
        return [str(row[0]) for row in db.execute(query, values).fetchall()]


def available_lora_favorite(family: str, lora_name: str) -> str:
    canonical_name = str(lora_name or "").replace("\\", "/")
    synchronize_lora_registry()
    if not any(entry_family == family and name == canonical_name for entry_family, name, _ in lora_catalog_entries()):
        raise HTTPException(404, "LoRA is not available in this workflow family")
    return canonical_name


@app.get("/api/help")
async def help_content(workflow: str | None = None):
    if workflow and workflow not in ENABLED_WORKFLOW_KEYS:
        raise HTTPException(400, "Invalid help workflow")
    family = lora_family_name(get_spec(workflow)) if workflow else None
    pages = page_help_entries(workflow) if workflow else page_help_entries(None)
    return {
        "pages": pages,
        "loras": lora_metadata_rows(family),
        "lora_categories": lora_category_rows(),
        "lora_favorites": lora_favorite_names(family),
    }


@app.put("/api/lora-favorites/{family}/{lora_name:path}")
async def favorite_lora(family: str, lora_name: str):
    owner = int(current_user()["user_id"])
    canonical_name = available_lora_favorite(family, lora_name)
    db_execute(
        "INSERT OR IGNORE INTO lora_favorites(owner_id,family,lora_name,created_at) VALUES(?,?,?,?)",
        (owner, family, canonical_name, utc_now()),
    )
    return {"favorite": True, "family": family, "name": canonical_name}


@app.delete("/api/lora-favorites/{family}/{lora_name:path}")
async def unfavorite_lora(family: str, lora_name: str):
    owner = int(current_user()["user_id"])
    canonical_name = str(lora_name or "").replace("\\", "/")
    db_execute(
        "DELETE FROM lora_favorites WHERE owner_id=? AND family=? AND lora_name=?",
        (owner, family, canonical_name),
    )
    return {"favorite": False, "family": family, "name": canonical_name}


@app.get("/api/admin/page-help")
async def admin_page_help_content():
    require_admin()
    result = []
    for spec in enabled_workflows():
        pages = page_help_entries(spec.key, True)
        for stage in ("create_step_1", "create_step_2"):
            result.append({"target": spec.key, "target_label": spec.label, "stage": stage, "entry": pages.get(stage)})
    result.append({"target": "flux-upscale", "target_label": "Flux 高清放大", "stage": "flux_upscale", "entry": page_help_entries(None, True).get("flux_upscale")})
    return result


@app.put("/api/admin/page-help/{target}/{stage}")
async def update_page_help(target: str, stage: str, payload: dict[str, Any] = Body(...)):
    require_admin()
    workflow_key = None if target == "flux-upscale" else target
    valid_stage = stage == "flux_upscale" if workflow_key is None else stage in {"create_step_1", "create_step_2"}
    if (workflow_key is not None and workflow_key not in ENABLED_WORKFLOW_KEYS) or not valid_stage:
        raise HTTPException(400, "Invalid page help target")
    title = str(payload.get("title") or "").strip()
    body = str(payload.get("body") or "").strip()
    enabled = int(as_bool(payload.get("enabled", True), "page help enabled"))
    if not 1 <= len(title) <= 60 or not 1 <= len(body) <= 4000:
        raise HTTPException(400, "Page help title or body is invalid")
    now = utc_now()
    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        if workflow_key is None:
            row = db.execute("SELECT help_id FROM help_entries WHERE scope='page' AND workflow_key IS NULL AND topic_key=? ORDER BY help_id LIMIT 1", (stage,)).fetchone()
        else:
            row = db.execute("SELECT help_id FROM help_entries WHERE scope='page' AND workflow_key=? AND topic_key=? ORDER BY help_id LIMIT 1", (workflow_key, stage)).fetchone()
        if row:
            help_id = int(row["help_id"])
            db.execute("UPDATE help_entries SET title=?,body=?,enabled=?,updated_at=? WHERE help_id=?", (title, body, enabled, now, help_id))
        else:
            cursor = db.execute(
                "INSERT INTO help_entries(scope,workflow_key,topic_key,title,body,enabled,sort_order,created_at,updated_at) VALUES('page',?,?,?,?,?,0,?,?)",
                (workflow_key, stage, title, body, enabled, now, now),
            )
            help_id = int(cursor.lastrowid)
        saved = dict(db.execute("SELECT * FROM help_entries WHERE help_id=?", (help_id,)).fetchone())
    return public_help_entry(saved)


@app.get("/api/admin/help")
async def admin_help_content():
    require_admin()
    return all_help_entries(True)


def help_values(payload: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    current = existing or {}
    scope = str(payload.get("scope", payload.get("placement", current.get("scope", "")))).strip()
    workflow_key = payload.get("workflow", current.get("workflow_key"))
    workflow_key = str(workflow_key).strip() if workflow_key not in {None, ""} else None
    topic = str(payload.get("topic", current.get("topic_key", ""))).strip() or f"custom:{secrets.token_hex(6)}"
    title = str(payload.get("title", current.get("title", ""))).strip()
    body = str(payload.get("body", current.get("body", ""))).strip()
    enabled = as_bool(payload.get("enabled", current.get("enabled", True)), "help enabled")
    sort_order = as_number(payload.get("sort_order", current.get("sort_order", 0)), "help sort order", 0, 999, True)
    if scope not in TIP_PLACEMENTS and scope not in {"workflow_intro", "parameter", "feature", "lora"}:
        raise HTTPException(400, "Invalid help scope")
    if workflow_key is not None and workflow_key not in ENABLED_WORKFLOW_KEYS:
        raise HTTPException(400, "Invalid help workflow")
    if not 1 <= len(title) <= 60 or not 1 <= len(body) <= 1200 or len(topic) > 120:
        raise HTTPException(400, "Invalid help content")
    return {"scope": scope, "workflow_key": workflow_key, "topic_key": topic, "title": title, "body": body, "enabled": int(enabled), "sort_order": sort_order}


@app.post("/api/admin/help")
async def create_help_entry(payload: dict[str, Any] = Body(...)):
    require_admin(); values = help_values(payload); now = utc_now()
    with sqlite3.connect(DATABASE) as db:
        cursor = db.execute(
            "INSERT INTO help_entries(scope,workflow_key,topic_key,title,body,enabled,sort_order,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (*values.values(), now, now),
        )
        help_id = int(cursor.lastrowid)
        db.row_factory = sqlite3.Row
        row = dict(db.execute("SELECT * FROM help_entries WHERE help_id=?", (help_id,)).fetchone())
    return public_help_entry(row)


@app.patch("/api/admin/help/{help_id}")
async def update_help_entry(help_id: int, payload: dict[str, Any] = Body(...)):
    require_admin()
    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM help_entries WHERE help_id=?", (help_id,)).fetchone()
    if not row: raise HTTPException(404, "Help entry not found")
    values = help_values(payload, dict(row))
    db_execute("UPDATE help_entries SET scope=?,workflow_key=?,topic_key=?,title=?,body=?,enabled=?,sort_order=?,updated_at=? WHERE help_id=?", (*values.values(), utc_now(), help_id))
    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        updated = dict(db.execute("SELECT * FROM help_entries WHERE help_id=?", (help_id,)).fetchone())
    return public_help_entry(updated)


@app.delete("/api/admin/help/{help_id}")
async def delete_help_entry(help_id: int):
    require_admin(); db_execute("DELETE FROM help_entries WHERE help_id=?", (help_id,)); return {"deleted": True}


@app.get("/api/admin/lora-metadata")
async def admin_lora_metadata():
    require_admin()
    synchronize_lora_registry(True)
    saved = {(item["family"], item["name"]): item for item in lora_metadata_rows(None, True)}
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for spec in enabled_workflows():
        for name in lora_options(spec.key):
            family = lora_family_name(spec)
            key = (family, name)
            item = saved.get(key) or {"name": name, "family": family, "description": "", "recommended_min": None, "recommended_max": None, "category_id": None, "enabled": True, "updated_at": None}
            result[key] = {**item, "available": True}
    for key, item in saved.items():
        result.setdefault(key, {**item, "available": False})
    return list(result.values())


@app.get("/api/admin/lora-categories")
async def admin_lora_categories():
    require_admin()
    return lora_category_rows()


@app.post("/api/admin/lora-categories")
async def create_lora_category(payload: dict[str, Any] = Body(...)):
    require_admin()
    name = lora_category_name(payload.get("name"))
    now = utc_now()
    with sqlite3.connect(DATABASE) as db:
        maximum = db.execute("SELECT COALESCE(MAX(sort_order),-1) FROM lora_categories").fetchone()[0]
        try:
            cursor = db.execute(
                "INSERT INTO lora_categories(name,sort_order,created_at,updated_at) VALUES(?,?,?,?)",
                (name, int(maximum) + 1, now, now),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "LoRA category name already exists") from exc
        category_id = int(cursor.lastrowid)
    return next(item for item in lora_category_rows() if item["category_id"] == category_id)


@app.patch("/api/admin/lora-categories/{category_id}")
async def update_lora_category(category_id: int, payload: dict[str, Any] = Body(...)):
    require_admin()
    lora_category_id(category_id, required=True)
    if "name" in payload:
        name = lora_category_name(payload.get("name"))
        try:
            db_execute("UPDATE lora_categories SET name=?,updated_at=? WHERE category_id=?", (name, utc_now(), category_id))
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "LoRA category name already exists") from exc
    if "sort_order" in payload:
        requested = as_number(payload.get("sort_order"), "LoRA category order", 0, 10_000, True)
        categories = lora_category_rows()
        moving = next(item for item in categories if item["category_id"] == category_id)
        categories.remove(moving)
        categories.insert(min(int(requested), len(categories)), moving)
        now = utc_now()
        with sqlite3.connect(DATABASE) as db, db:
            for index, item in enumerate(categories):
                db.execute(
                    "UPDATE lora_categories SET sort_order=?,updated_at=? WHERE category_id=?",
                    (index, now, item["category_id"]),
                )
    return next(item for item in lora_category_rows() if item["category_id"] == category_id)


@app.delete("/api/admin/lora-categories/{category_id}")
async def delete_lora_category(category_id: int):
    require_admin()
    lora_category_id(category_id, required=True)
    with sqlite3.connect(DATABASE) as db, db:
        affected = int(db.execute("SELECT COUNT(*) FROM lora_metadata WHERE category_id=?", (category_id,)).fetchone()[0])
        db.execute("UPDATE lora_metadata SET category_id=NULL,updated_at=? WHERE category_id=?", (utc_now(), category_id))
        db.execute("DELETE FROM lora_categories WHERE category_id=?", (category_id,))
        for index, row in enumerate(db.execute("SELECT category_id FROM lora_categories ORDER BY sort_order,category_id")):
            db.execute("UPDATE lora_categories SET sort_order=? WHERE category_id=?", (index, row[0]))
    return {"deleted": True, "category_id": category_id, "uncategorized_loras": affected}


@app.get("/api/admin/lora-scan")
async def admin_lora_scan():
    require_admin()
    return await asyncio.to_thread(lora_scan_report)


def rename_managed_lora_file(family: str, lora_name: str, requested_name: Any) -> dict[str, Any]:
    global LORA_REGISTRY_LAST_SCAN
    canonical_old_name = str(lora_name).replace("\\", "/")
    filename = normalized_lora_filename(requested_name)
    with LORA_REGISTRY_LOCK:
        synchronize_lora_registry(True)
        entries = lora_catalog_entries()
        source_entry = next(
            (entry for entry in entries if entry[0] == family and entry[1] == canonical_old_name), None,
        )
        if not source_entry:
            raise HTTPException(404, "LoRA file is not in the managed catalog")
        source = source_entry[2]
        if not source.is_file():
            raise HTTPException(404, "LoRA file no longer exists")
        target = source.with_name(filename)
        parent_name = Path(canonical_old_name).parent.as_posix()
        canonical_new_name = filename if parent_name == "." else f"{parent_name}/{filename}"
        if canonical_new_name == canonical_old_name:
            metadata = next(
                (item for item in lora_metadata_rows(family, True) if item["name"] == canonical_old_name),
                {"name": canonical_old_name, "family": family, "description": "", "recommended_min": None, "recommended_max": None, "category_id": None, "enabled": True, "updated_at": None},
            )
            return {"old_name": canonical_old_name, "new_name": canonical_old_name, "family": family, "item": metadata, "migrations": []}
        for sibling in source.parent.iterdir():
            if sibling.is_file() and sibling.name.casefold() == filename.casefold() and sibling.name != source.name:
                raise HTTPException(409, "A LoRA with that filename already exists in this directory")
        if active_task_references_lora(canonical_old_name):
            raise HTTPException(409, "This LoRA is used by a queued or running task. Rename it after the task finishes.")

        source_resolved = source.resolve()
        affected_families = {
            entry_family for entry_family, _, path in entries if path.resolve() == source_resolved
        }
        moved = False
        db = sqlite3.connect(DATABASE)
        try:
            rename_file_preserving_case(source, target)
            moved = source.name != target.name
            renamed_entries = lora_catalog_entries()
            renamed_families = {
                entry_family for entry_family, name, path in renamed_entries
                if name == canonical_new_name and path.resolve() == target.resolve()
            }
            if family not in renamed_families or not affected_families.issubset(renamed_families):
                raise HTTPException(409, "The new filename is not recognized by the current LoRA workflow family")
            migrations = synchronize_lora_registry_entries(db, renamed_entries)
            migrated_families = {
                item["family"] for item in migrations
                if item["old_name"] == canonical_old_name and item["new_name"] == canonical_new_name
            }
            if not affected_families.issubset(migrated_families):
                raise RuntimeError("LoRA registry did not migrate every affected workflow family")
            db.commit()
        except Exception as exc:
            db.rollback()
            if moved and target.exists():
                try:
                    rename_file_preserving_case(target, source)
                except OSError as rollback_error:
                    raise HTTPException(500, f"LoRA rename failed and the file rollback also failed: {rollback_error}") from exc
            if isinstance(exc, HTTPException):
                raise
            if isinstance(exc, OSError):
                raise HTTPException(409, f"Unable to rename LoRA file: {exc}") from exc
            raise HTTPException(500, f"Unable to synchronize renamed LoRA: {exc}") from exc
        finally:
            db.close()
        LORA_REGISTRY_LAST_SCAN = time.monotonic()
    metadata = next(
        (item for item in lora_metadata_rows(family, True) if item["name"] == canonical_new_name),
        {"name": canonical_new_name, "family": family, "description": "", "recommended_min": None, "recommended_max": None, "category_id": None, "enabled": True, "updated_at": None},
    )
    return {"old_name": canonical_old_name, "new_name": canonical_new_name, "family": family, "item": metadata, "migrations": migrations}


@app.patch("/api/admin/lora-files/{family}/{lora_name:path}")
async def rename_lora_file(family: str, lora_name: str, payload: dict[str, Any] = Body(...)):
    require_admin()
    return await asyncio.to_thread(rename_managed_lora_file, family, lora_name, payload.get("name"))


@app.put("/api/admin/lora-metadata/{family}/{lora_name:path}")
async def update_lora_metadata(family: str, lora_name: str, payload: dict[str, Any] = Body(...)):
    require_admin()
    description = str(payload.get("description") or "").strip()
    minimum = payload.get("recommended_min"); maximum = payload.get("recommended_max")
    minimum = None if minimum in {None, ""} else as_number(minimum, "recommended minimum", -LORA_WEIGHT_LIMIT, LORA_WEIGHT_LIMIT)
    maximum = None if maximum in {None, ""} else as_number(maximum, "recommended maximum", -LORA_WEIGHT_LIMIT, LORA_WEIGHT_LIMIT)
    if minimum is not None and maximum is not None and minimum > maximum: raise HTTPException(400, "Recommended minimum exceeds maximum")
    category_id = lora_category_id(payload.get("category_id"))
    enabled = int(as_bool(payload.get("enabled", True), "LoRA metadata enabled")); now = utc_now()
    db_execute("""INSERT INTO lora_metadata(lora_name,family,description,recommended_min,recommended_max,category_id,enabled,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(lora_name,family) DO UPDATE SET description=excluded.description,recommended_min=excluded.recommended_min,recommended_max=excluded.recommended_max,category_id=excluded.category_id,enabled=excluded.enabled,updated_at=excluded.updated_at""",
        (lora_name, family, description, minimum, maximum, category_id, enabled, now, now))
    return next(item for item in lora_metadata_rows(family, True) if item["name"] == lora_name)


@app.delete("/api/admin/lora-metadata/{family}/{lora_name:path}")
async def delete_stale_lora_metadata(family: str, lora_name: str):
    require_admin()
    canonical_name = str(lora_name or "").replace("\\", "/")
    synchronize_lora_registry(True)
    if any(entry_family == family and name == canonical_name for entry_family, name, _ in lora_catalog_entries()):
        raise HTTPException(409, "LoRA file still exists; only missing-file metadata can be removed")
    with closing(sqlite3.connect(DATABASE)) as db, db:
        if not db.execute(
            "SELECT 1 FROM lora_metadata WHERE family=? AND lora_name=?", (family, canonical_name),
        ).fetchone():
            raise HTTPException(404, "LoRA metadata not found")
        db.execute("DELETE FROM lora_metadata WHERE family=? AND lora_name=?", (family, canonical_name))
        db.execute("DELETE FROM lora_favorites WHERE family=? AND lora_name=?", (family, canonical_name))
    return {"deleted": True, "family": family, "name": canonical_name}


@app.get("/api/tips")
async def usage_tips():
    return all_help_entries()


@app.get("/api/admin/tips")
async def admin_usage_tips():
    require_admin()
    return all_usage_tips(True)


@app.post("/api/admin/tips")
async def create_usage_tip(payload: dict[str, Any] = Body(...)):
    require_admin()
    values = tip_values(payload)
    now = utc_now()
    with sqlite3.connect(DATABASE) as db:
        cursor = db.execute(
            "INSERT INTO usage_tips(placement,workflow_key,title,body,enabled,sort_order,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (values["placement"], values["workflow_key"], values["title"], values["body"], values["enabled"], values["sort_order"], now, now),
        )
        tip_id = int(cursor.lastrowid)
    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        row = dict(db.execute("SELECT * FROM usage_tips WHERE tip_id=?", (tip_id,)).fetchone())
    return public_tip(row)


@app.patch("/api/admin/tips/{tip_id}")
async def update_usage_tip(tip_id: int, payload: dict[str, Any] = Body(...)):
    require_admin()
    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM usage_tips WHERE tip_id=?", (tip_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Tip not found")
    values = tip_values(payload, dict(row))
    db_execute(
        "UPDATE usage_tips SET placement=?,workflow_key=?,title=?,body=?,enabled=?,sort_order=?,updated_at=? WHERE tip_id=?",
        (values["placement"], values["workflow_key"], values["title"], values["body"], values["enabled"], values["sort_order"], utc_now(), tip_id),
    )
    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        updated = dict(db.execute("SELECT * FROM usage_tips WHERE tip_id=?", (tip_id,)).fetchone())
    return public_tip(updated)


@app.delete("/api/admin/tips/{tip_id}")
async def delete_usage_tip(tip_id: int):
    require_admin()
    db_execute("DELETE FROM usage_tips WHERE tip_id=?", (tip_id,))
    return {"deleted": True}


@app.get("/api/prompt-templates")
async def list_prompt_templates(scope: str = PROMPT_TEMPLATE_SCOPE):
    return all_prompt_templates(scope)


@app.post("/api/prompt-templates")
async def create_prompt_template(payload: dict[str, Any] = Body(...)):
    name = template_name(payload.get("name"))
    body = template_body(payload.get("text"))
    scope = template_scope(payload.get("scope", PROMPT_TEMPLATE_SCOPE))
    now = utc_now()
    try:
        with closing(sqlite3.connect(DATABASE)) as db, db:
            owner = current_owner_id()
            duplicate = db.execute(
                "SELECT 1 FROM prompt_templates WHERE name=? AND " + ("owner_id=?" if owner else "owner_id IS NULL"),
                (name, owner) if owner else (name,),
            ).fetchone()
            if duplicate:
                raise HTTPException(409, "Template name already exists")
            sort_order = db.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM prompt_templates WHERE scope=?", (scope,)).fetchone()[0]
            cursor = db.execute(
                "INSERT INTO prompt_templates(name, body, scope, sort_order, created_at, updated_at, owner_id, is_system) VALUES(?,?,?,?,?,?,?,0)",
                (name, body, scope, sort_order, now, now, current_owner_id()),
            )
            template_id = cursor.lastrowid
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "Template name already exists") from exc
    return prompt_template(int(template_id))


@app.patch("/api/prompt-templates/{template_id}")
async def update_prompt_template(template_id: int, payload: dict[str, Any] = Body(...)):
    current = prompt_template(template_id)
    name = template_name(payload.get("name", current["name"]))
    body = template_body(payload.get("text", current["text"]))
    try:
        with closing(sqlite3.connect(DATABASE)) as db, db:
            owner = current_owner_id()
            duplicate = db.execute(
                "SELECT 1 FROM prompt_templates WHERE name=? AND template_id<>? AND " + ("owner_id=?" if owner else "owner_id IS NULL"),
                (name, template_id, owner) if owner else (name, template_id),
            ).fetchone()
            if duplicate:
                raise HTTPException(409, "Template name already exists")
            if current.get("system") and current_owner_id():
                now = utc_now()
                cursor = db.execute(
                    "INSERT INTO prompt_templates(name,body,scope,sort_order,created_at,updated_at,owner_id,is_system) VALUES(?,?,?,?,?,?,?,0)",
                    (name, body, current["scope"], 0, now, now, current_owner_id()),
                )
                template_id = int(cursor.lastrowid)
            else:
                owner = current_owner_id()
                db.execute(
                    "UPDATE prompt_templates SET name=?, body=?, updated_at=? WHERE template_id=?" + (" AND owner_id=?" if owner else ""),
                    (name, body, utc_now(), template_id, owner) if owner else (name, body, utc_now(), template_id),
                )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "Template name already exists") from exc
    return prompt_template(template_id)


@app.delete("/api/prompt-templates/{template_id}")
async def delete_prompt_template(template_id: int):
    current = prompt_template(template_id)
    if current.get("system") and current_owner_id():
        raise HTTPException(400, "系统模板不能删除；编辑后会生成私人副本")
    with closing(sqlite3.connect(DATABASE)) as db, db:
        owner = current_owner_id()
        db.execute("DELETE FROM prompt_templates WHERE template_id=?" + (" AND owner_id=?" if owner else ""), (template_id, owner) if owner else (template_id,))
    return {"deleted": True}


@app.get("/api/prompt-tools/options")
async def prompt_tool_options():
    raise HTTPException(410, "提示词助手已从发布版移除")


@app.get("/api/prompt-tools")
async def list_prompt_tools():
    raise HTTPException(410, "提示词助手已从发布版移除")


@app.get("/api/prompt-tools/{tool_id}")
async def get_prompt_tool(tool_id: str):
    raise HTTPException(410, "提示词助手已从发布版移除")


@app.post("/api/prompt-tools")
async def submit_prompt_tool(
    operation: Annotated[str, Form()],
    target_workflow: Annotated[str, Form()],
    mode: Annotated[str, Form()] = "pure",
    style: Annotated[str, Form()] = "detailed",
    input_text: Annotated[str, Form()] = "",
    image: Annotated[UploadFile | None, File()] = None,
    source_group_id: Annotated[str | None, Form()] = None,
    result_group_id: Annotated[str | None, Form()] = None,
    result_item_id: Annotated[str | None, Form()] = None,
    result_source_kind: Annotated[str | None, Form()] = None,
    result_record_id: Annotated[str | None, Form()] = None,
    preprocess_id: Annotated[str | None, Form()] = None,
    crop: Annotated[str | None, Form()] = None,
):
    raise HTTPException(410, "提示词助手已从发布版移除")


@app.delete("/api/prompt-tools/{tool_id}")
async def delete_prompt_tool(tool_id: str):
    raise HTTPException(410, "提示词助手已从发布版移除")


@app.get("/api/preprocess/{preprocess_id}")
async def preprocess_job(preprocess_id: str):
    queued = db_queue_record("preprocess", preprocess_id)
    if queued:
        await scheduler_tick()
        return public_preprocess(db_preprocess(preprocess_id) or {})
    return await refresh_preprocess(preprocess_id)


@app.get("/api/preprocess/{preprocess_id}/image")
async def preprocess_image(preprocess_id: str, preview: int | None = None):
    row = db_preprocess(preprocess_id)
    if not row or row["status"] != "completed":
        raise HTTPException(404, "Pre-upscale image not found")
    image = json.loads(row.get("output_json") or "{}")
    path = safe_output_path(image) if isinstance(image, dict) else None
    if not path or not path.is_file():
        raise HTTPException(404, "Pre-upscale image was removed")
    return await private_image_response(path, preview)


@app.get("/api/preprocess/{preprocess_id}/source-image")
async def preprocess_source_image(preprocess_id: str, preview: int | None = None):
    row = db_preprocess(preprocess_id)
    image = source_image_from(row) if row else None
    path = safe_source_path(image) if image else None
    if not path or not path.is_file():
        raise HTTPException(404, "Pre-upscale source image is unavailable")
    return await private_image_response(path, preview)


@app.post("/api/preprocess/{preprocess_id}/cancel")
async def cancel_preprocess(preprocess_id: str):
    return await cancel_postprocess("preprocess", preprocess_id)


@app.delete("/api/preprocess/{preprocess_id}")
async def delete_preprocess(preprocess_id: str):
    row = db_preprocess(preprocess_id)
    if not row:
        raise HTTPException(404, "Pre-upscale job not found")
    if row["status"] in {"queued", "running"}:
        raise HTTPException(409, "Stop pre-upscale before deleting it")
    source = source_image_from(row)
    source_path = safe_source_path(source) if source else None
    if source_path and source_path.is_file():
        source_path.unlink()
    output = json_image_from(row, "output_json")
    if output:
        remove_output_files([output])
    db_execute("DELETE FROM preprocess_jobs WHERE preprocess_id=?", (preprocess_id,))
    return {"deleted": True}

@app.post("/api/jobs")
async def submit_job(
    workflow: Annotated[str, Form()],
    prompt: Annotated[str, Form()],
    settings: Annotated[str, Form()] = "{}",
    image: Annotated[UploadFile | None, File()] = None,
    source_group_id: Annotated[str | None, Form()] = None,
    rerun_group_id: Annotated[str | None, Form()] = None,
    h3_lottery_count: Annotated[str | None, Form()] = None,
    preprocess_id: Annotated[str | None, Form()] = None,
    result_group_id: Annotated[str | None, Form()] = None,
    result_item_id: Annotated[str | None, Form()] = None,
    result_source_kind: Annotated[str | None, Form()] = None,
    result_record_id: Annotated[str | None, Form()] = None,
    chat_asset_token: Annotated[str | None, Form()] = None,
    title: Annotated[str, Form()] = "",
    source_prompt: Annotated[str, Form()] = "",
    reference_image: Annotated[UploadFile | None, File()] = None,
    reference_result_group_id: Annotated[str | None, Form()] = None,
    reference_result_item_id: Annotated[str | None, Form()] = None,
    reference_result_source_kind: Annotated[str | None, Form()] = None,
    reference_result_record_id: Annotated[str | None, Form()] = None,
    reference_source_group_id: Annotated[str | None, Form()] = None,
    image_crop: Annotated[str | None, Form()] = None,
    reference_image_crop: Annotated[str | None, Form()] = None,
    edit_mask: Annotated[UploadFile | None, File()] = None,
    mask_grow_px: Annotated[int, Form()] = 16,
    mask_feather_px: Annotated[int, Form()] = 12,
):
    if preprocess_id and preprocess_id.strip():
        raise HTTPException(410, "编辑前 SeedVR2 预放大已下线；请直接提交原图")
    try: spec = get_spec(workflow)
    except WorkflowCompileError as exc: raise HTTPException(400, str(exc)) from exc
    if spec.key not in ENABLED_WORKFLOW_KEYS:
        raise HTTPException(410, "This workflow is no longer available for new tasks")
    prompt = prompt.strip()
    if not prompt or len(prompt) > 6000: raise HTTPException(400, "Prompt must contain 1 to 6000 characters")
    source_prompt = unicodedata.normalize("NFKC", source_prompt).strip()
    if len(source_prompt) > 6000:
        raise HTTPException(400, "Source prompt must contain no more than 6000 characters")
    if source_prompt == prompt:
        source_prompt = ""
    title = unicodedata.normalize("NFKC", title).strip()
    if any(unicodedata.category(char) == "Cc" for char in title) or len(title) > 80:
        raise HTTPException(400, "Title must be a single line of no more than 80 characters")
    lottery_count = h3_rerun_lottery_count(h3_lottery_count)
    h3_lottery = lottery_count > 1
    if h3_lottery:
        values = h3_rerun_lottery_values(spec, settings, rerun_group_id, lottery_count)
        # A multi-draw lottery is a strict rerun: use the original first frame
        # when applicable, and do not permit uploads or result substitutions.
        if (
            image is not None and image.filename
        ) or result_group_id or chat_asset_token or preprocess_id or (
            source_group_id and source_group_id.strip() != str(rerun_group_id or "").strip()
        ):
            raise HTTPException(422, "MiniMax H3 lottery must use the historical task source and locked settings")
        if values.get("mode") == "i2v":
            source_group_id = str(rerun_group_id or "").strip()
    else:
        values = normalize_settings(spec, settings)
    values["prompt"] = prompt
    dual_reference = (
        spec.variant == "krea_identity"
        and values.get("reference_mode") in {"dual_reference", "face_swap"}
    )
    has_reference_input = bool(
        (reference_image is not None and reference_image.filename)
        or reference_result_group_id
        or reference_source_group_id
    )
    if spec.variant == "krea_identity" and not dual_reference and has_reference_input:
        raise HTTPException(400, "图2仅用于双参考图模式")
    if spec.variant == "krea_identity" and edit_mask is not None and edit_mask.filename:
        raise HTTPException(410, "Krea face mask input has been retired")
    enforce_user_capacity()
    image_name = None
    source_image = None
    edit_source_image = None
    reference_image_name = None
    reference_image_record = None
    edit_mask_name = None
    edit_mask_record = None
    preprocess_data: dict[str, Any] = {}
    needs_image = bool(spec.image_node) or (
        spec.variant == "minimax_h3" and values.get("mode") == "i2v"
    )
    if needs_image:
        pre_row = db_preprocess(preprocess_id.strip()) if preprocess_id and preprocess_id.strip() else None
        if preprocess_id and not pre_row:
            raise HTTPException(404, "Pre-upscale job not found")
        if pre_row:
            if pre_row["status"] != "completed":
                raise HTTPException(409, "Pre-upscale has not completed")
            source_image = source_image_from(pre_row)
            pre_output = json_image_from(pre_row, "output_json")
            if not source_image or not pre_output:
                raise HTTPException(409, "Pre-upscale files are unavailable")
            source_path = safe_source_path(source_image)
            output_path = safe_output_path(pre_output)
            if not source_path or not source_path.is_file() or not output_path or not output_path.is_file():
                raise HTTPException(409, "Pre-upscale files are unavailable")
            image_name, edit_source_image = copy_image_for_processing(pre_output, "pre_edit")
            preprocess_data = {
                "output": pre_output,
                "parameters": json.loads(pre_row.get("parameters_json") or "{}"),
            }
        elif chat_asset_token and chat_asset_token.strip():
            source_image = prepared_chat_asset(chat_asset_token.strip())
            image_name = f"{source_image.get('subfolder', '')}/{source_image.get('filename', '')}".strip("/")
            crop_stored_source(source_image, image_crop)
        elif image is not None and image.filename:
            body = await image.read(MAX_UPLOAD_BYTES + 1)
            if len(body) > MAX_UPLOAD_BYTES: raise HTTPException(413, "Image exceeds 50 MB")
            enforce_user_capacity(len(body))
            image_name, source_image = write_cropped_upload(image, body, "mobile", image_crop)
        elif result_group_id and result_group_id.strip():
            if not result_item_id or not result_source_kind:
                raise HTTPException(400, "Reusable result reference is incomplete")
            image_name, source_image = copy_reusable_result(
                result_group_id.strip(), result_item_id.strip(), result_source_kind.strip(), (result_record_id or "").strip(),
            )
            crop_stored_source(source_image, image_crop)
        elif source_group_id and source_group_id.strip():
            image_name, source_image = copy_group_source_image(source_group_id.strip())
            crop_stored_source(source_image, image_crop)
        else:
            raise HTTPException(400, "This workflow requires an image")
    if dual_reference:
        if reference_image is not None and reference_image.filename:
            body = await reference_image.read(MAX_UPLOAD_BYTES + 1)
            if len(body) > MAX_UPLOAD_BYTES:
                raise HTTPException(413, "Second reference image exceeds 50 MB")
            enforce_user_capacity(len(body))
            reference_image_name, reference_image_record = write_cropped_upload(
                reference_image, body, "krea_reference", reference_image_crop,
            )
        elif reference_result_group_id and reference_result_group_id.strip():
            if not reference_result_item_id or not reference_result_source_kind:
                raise HTTPException(400, "Reusable second reference is incomplete")
            reference_image_name, reference_image_record = copy_reusable_result(
                reference_result_group_id.strip(),
                reference_result_item_id.strip(),
                reference_result_source_kind.strip(),
                (reference_result_record_id or "").strip(),
            )
            crop_stored_source(reference_image_record, reference_image_crop)
        elif reference_source_group_id and reference_source_group_id.strip():
            reference_image_name, reference_image_record = copy_group_reference_image(
                reference_source_group_id.strip(),
            )
            crop_stored_source(reference_image_record, reference_image_crop)
        else:
            raise HTTPException(400, "双参考图模式需要上传图2人物参考图")
    if spec.variant == "minimax_h3" and values.get("mode") == "i2v":
        source_path = safe_source_path(source_image) if source_image else None
        if not source_path or not source_path.is_file():
            raise HTTPException(409, "MiniMax H3 first-frame image is unavailable")
        source_width, source_height = image_dimensions(source_path)
        values["source_width"], values["source_height"] = source_width, source_height
        values["aspect_ratio"] = "source"
        values["width"], values["height"] = h3_source_dimensions(
            source_width, source_height, values["megapixels"],
        )
        apply_h3_history_estimate(values)
    if spec.variant == "krea_identity":
        original_path = safe_source_path(source_image) if source_image else None
        if not original_path or not original_path.is_file() or not image_name:
            raise HTTPException(409, "Krea Identity Edit source image is unavailable")
        source_width, source_height = image_dimensions(original_path)
        if values["size_mode"] == "source_ratio":
            output_pixels = int(round(values["output_megapixels"] * 1024 * 1024))
            values["width"], values["height"] = krea_safe_dimensions(
                source_width, source_height, output_pixels,
            )
        previous_edit_source = edit_source_image
        image_name, edit_source_image = prepare_krea_identity_source(image_name)
        previous_path = safe_source_path(previous_edit_source) if previous_edit_source else None
        if previous_path and previous_path.is_file() and previous_path != safe_source_path(edit_source_image):
            previous_path.unlink()
        if dual_reference and reference_image_name:
            previous_reference = reference_image_record
            reference_image_name, reference_image_record = prepare_krea_identity_source(reference_image_name)
            previous_reference_path = safe_source_path(previous_reference) if previous_reference else None
            if (
                previous_reference_path
                and previous_reference_path.is_file()
                and previous_reference_path != safe_source_path(reference_image_record)
            ):
                previous_reference_path.unlink()
    group_id = f"group-{secrets.token_hex(12)}"
    group_parameters = dict(values)
    actual_seeds = generation_seed_sequence(spec, values)
    seed_list_field = "stage1_seeds" if spec.kind == "image_edit" else "generation_seeds"
    group_parameters[seed_list_field] = actual_seeds
    db_execute(
        "INSERT INTO generation_groups(group_id,workflow_key,prompt_text,source_prompt_text,title,status,submitted_at,parameters_json,source_image_json,edit_source_image_json,reference_image_json,edit_mask_json,preprocess_json,storage_scope,owner_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (group_id, spec.key, prompt, source_prompt, title, "queued", utc_now(), json.dumps(group_parameters), json.dumps(source_image or {}), json.dumps(edit_source_image or {}), json.dumps(reference_image_record or {}), json.dumps(edit_mask_record or {}), json.dumps(preprocess_data), "mobile", current_owner_id()),
    )
    if chat_asset_token and chat_asset_token.strip():
        prepared_chat_asset(chat_asset_token.strip(), consume=True)
    owner_id = current_owner_id()
    job_count = values["count"]
    if owner_id is not None:
        db_execute("UPDATE generation_groups SET parameters_json=? WHERE group_id=?", (json.dumps(group_parameters), group_id))
        if preprocess_id and preprocess_data:
            db_execute("DELETE FROM preprocess_jobs WHERE preprocess_id=?", (preprocess_id.strip(),))
        enqueue_task("generation", group_id, owner_id, queue_profile("generation", group_parameters, spec.key))
        await scheduler_tick()
        return public_group(db_group(group_id) or {})
    if preprocess_id and preprocess_data:
        db_execute("DELETE FROM preprocess_jobs WHERE preprocess_id=?", (preprocess_id.strip(),))
    try:
        for index in range(job_count):
            job_values = dict(values)
            if spec.kind == "image_edit":
                job_values["stage1_seed"] = actual_seeds[index]
                job_values["stage1_random_seed"] = False
            else:
                job_values["seed"] = actual_seeds[index]
                job_values["random_seed"] = False
            graph, editor_workflow = build_graph(
                spec, job_values, image_name, reference_image_name, edit_mask_name,
            )
            if preprocess_data and source_image:
                original_image_name = f"{source_image.get('subfolder', '')}/{source_image.get('filename', '')}".strip("/")
                apply_pre_edit_color_match(graph, original_image_name)
            response = await comfy_json("POST", "/prompt", json={"prompt": graph, "client_id": COMFY_CLIENT_ID, "extra_data": {"extra_pnginfo": {"workflow": editor_workflow}}})
            prompt_id = response.get("prompt_id")
            if not prompt_id:
                raise HTTPException(502, "ComfyUI did not return a job id")
            db_execute(
                "INSERT INTO jobs(prompt_id,workflow_key,prompt_text,source_prompt_text,title,status,submitted_at,parameters_json,storage_scope,group_id,owner_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (prompt_id, spec.key, prompt, source_prompt, title, "queued", utc_now(), json.dumps(job_values), "mobile", group_id, current_owner_id()),
            )
    except HTTPException as exc:
        db_execute("UPDATE generation_groups SET status='failed', completed_at=?, error_message=? WHERE group_id=?", (utc_now(), str(exc.detail), group_id))
        return public_group(db_group(group_id) or {})
    db_execute("UPDATE generation_groups SET parameters_json=? WHERE group_id=?", (json.dumps(group_parameters), group_id))
    return public_group(db_group(group_id) or {})

@app.get("/api/jobs/{prompt_id}")
async def job(prompt_id: str):
    group = db_group(prompt_id)
    if group:
        if recycle_task_entry(group):
            raise HTTPException(410, "任务已移入回收站")
        queued = db_queue_record("generation", prompt_id)
        if queued:
            return public_group(group)
        if group["status"] in {"completed", "failed", "cancelled"}:
            if group["status"] == "cancelled":
                await reconcile_cancelled_generation(prompt_id)
                group = db_group(prompt_id) or group
            result = public_group(group)
            if stored_items(group) and not result["items"]:
                raise HTTPException(410, "任务中的图片均在回收站")
            return result
        result = await refresh_group(prompt_id)
        await refresh_group_details(prompt_id)
        await refresh_group_upscales(prompt_id)
        return public_group(db_group(prompt_id) or result)
    row = db_job(prompt_id)
    if row and recycle_task_entry(row):
        raise HTTPException(410, "任务已移入回收站")
    result = await refresh_job(prompt_id)
    if row and row.get("status") in {"completed", "failed", "cancelled"} and stored_items(row) and not result.get("items"):
        raise HTTPException(410, "任务中的图片均在回收站")
    return result


@app.post("/api/jobs/{group_id}/cancel")
async def cancel_job(group_id: str):
    return await cancel_group(group_id)


@app.post("/api/flux-upscales")
async def submit_standalone_flux_upscale(
    settings: Annotated[str, Form()] = "{}",
    image: Annotated[UploadFile | None, File()] = None,
    crop: Annotated[str | None, Form()] = None,
):
    """Run the Flux2 Klein upscale without requiring an edit job first."""
    try:
        payload = json.loads(settings or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "Invalid Flux upscale settings") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "Invalid Flux upscale settings")
    values = normalize_enhanced_upscale_settings(payload)
    # A standalone upload can never use a Flux Detail result as its source.
    values["use_detail"] = False
    if image is None or not image.filename:
        raise HTTPException(400, "Flux upscale requires an image")
    body = await image.read(MAX_UPLOAD_BYTES + 1)
    if len(body) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Image exceeds 50 MB")
    enforce_user_capacity(len(body))
    image_name, source_record = write_cropped_upload(image, body, "flux_upscale", crop)
    upscale_id = f"flux-upscale-{secrets.token_hex(12)}"
    try:
        owner_id = current_owner_id()
        if owner_id is not None:
            db_execute(
                "INSERT INTO upscale_jobs(upscale_id,prompt_id,parent_group_id,parent_item_id,status,submitted_at,parameters_json,source_image_json,source_kind,engine,owner_id) VALUES(?,NULL,?,?,'queued',?,?,?,?,?,?)",
                (upscale_id, upscale_id, "source", utc_now(), json.dumps(values), json.dumps(source_record), "standalone", "flux2", owner_id),
            )
            enqueue_task("upscale", upscale_id, owner_id, queue_profile("upscale", values | {"engine": "flux2"}))
            await scheduler_tick()
            return public_upscale(db_upscale(upscale_id) or {})
        graph, editor_workflow = build_enhanced_upscale_graph(values, image_name)
        response = await comfy_json(
            "POST", "/prompt",
            json={"prompt": graph, "client_id": COMFY_CLIENT_ID, "extra_data": {"extra_pnginfo": {"workflow": editor_workflow}}},
        )
        prompt_id = response.get("prompt_id")
        if not prompt_id:
            raise HTTPException(502, "ComfyUI did not return a Flux upscale job id")
        register_comfy_prompt(str(prompt_id), graph)
        db_execute(
            "INSERT INTO upscale_jobs(upscale_id,prompt_id,parent_group_id,parent_item_id,status,submitted_at,parameters_json,source_image_json,source_kind,engine,owner_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (upscale_id, prompt_id, upscale_id, "source", "queued", utc_now(), json.dumps(values), json.dumps(source_record), "standalone", "flux2", current_owner_id()),
        )
    except Exception:
        if source_path.is_file():
            source_path.unlink()
        raise
    return public_upscale(db_upscale(upscale_id) or {})


@app.get("/api/flux-upscales")
async def standalone_flux_upscales(limit: int = 12):
    if not 1 <= limit <= 50:
        raise HTTPException(400, "Limit must be between 1 and 50")
    with closing(sqlite3.connect(DATABASE)) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(row) for row in db.execute(
            "SELECT * FROM upscale_jobs WHERE source_kind='standalone' AND engine='flux2' AND owner_id=? ORDER BY submitted_at DESC LIMIT ?",
            (current_owner_id(), limit),
        ).fetchall()]
    return [public_upscale(row) for row in rows]


@app.get("/api/flux-upscales/{upscale_id}/source-image")
async def standalone_flux_upscale_source(upscale_id: str, preview: int | None = None):
    row = db_upscale(upscale_id)
    if not row or row.get("source_kind") != "standalone":
        raise HTTPException(404, "Flux upscale source image not found")
    source = json.loads(row.get("source_image_json") or "{}")
    path = safe_source_path(source) if isinstance(source, dict) else None
    if not path or not path.is_file():
        raise HTTPException(404, "Flux upscale source image was removed")
    return await private_image_response(path, preview)


@app.post("/api/jobs/{group_id}/items/{item_id_value}/upscale")
async def submit_upscale(group_id: str, item_id_value: str, payload: dict[str, Any] = Body(default={})):
    enforce_user_capacity()
    group = db_group(group_id)
    if not group or group.get("storage_scope") != "mobile":
        raise HTTPException(404, "Generation group not found")
    if group.get("workflow_key") not in ENABLED_WORKFLOW_KEYS:
        raise HTTPException(410, "Disabled workflow history cannot start new post-processing")
    item = next((entry for entry in stored_items(group) if entry.get("id") == item_id_value), None)
    if not item or not item.get("final"):
        raise HTTPException(404, "Image not found")
    active = next((entry for entry in db_item_upscales(group_id, item_id_value) if entry["status"] in {"queued", "running"}), None)
    active_detail = next((entry for entry in db_item_details(group_id, item_id_value) if entry["status"] in {"queued", "running"}), None)
    if active or active_detail:
        raise HTTPException(409, "This image already has post-processing running")
    engine = str(payload.get("engine", "seedvr2")).strip().lower()
    if engine == "seedvr2":
        raise HTTPException(410, "独立 SeedVR2 后放大已下线；请使用 Flux 增强放大")
    if engine == "flux2":
        settings = normalize_enhanced_upscale_settings(payload)
        settings["use_detail"] = False
    else:
        raise HTTPException(400, "Upscale engine must be flux2")
    source_kind = "edit"
    source_image = item["final"]
    image_name, source_record = copy_image_for_processing(source_image, "upscale")
    upscale_id = f"upscale-{secrets.token_hex(12)}"
    try:
        owner_id = current_owner_id()
        if owner_id is not None:
            db_execute(
                "INSERT INTO upscale_jobs(upscale_id,prompt_id,parent_group_id,parent_item_id,status,submitted_at,parameters_json,source_image_json,source_kind,engine,owner_id) VALUES(?,NULL,?,?,'queued',?,?,?,?,?,?)",
                (upscale_id, group_id, item_id_value, utc_now(), json.dumps(settings), json.dumps(source_record), source_kind, engine, owner_id),
            )
            enqueue_task("upscale", upscale_id, owner_id, queue_profile("upscale", settings | {"engine": engine}))
            await scheduler_tick()
            return public_upscale(db_upscale(upscale_id) or {})
        graph, editor_workflow = build_enhanced_upscale_graph(settings, image_name)
        response = await comfy_json(
            "POST", "/prompt",
            json={"prompt": graph, "client_id": COMFY_CLIENT_ID, "extra_data": {"extra_pnginfo": {"workflow": editor_workflow}}},
        )
        prompt_id = response.get("prompt_id")
        if not prompt_id:
            raise HTTPException(502, "ComfyUI did not return an upscale job id")
        register_comfy_prompt(str(prompt_id), graph)
        db_execute(
            "INSERT INTO upscale_jobs(upscale_id,prompt_id,parent_group_id,parent_item_id,status,submitted_at,parameters_json,source_image_json,source_kind,engine,owner_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (upscale_id, prompt_id, group_id, item_id_value, "queued", utc_now(), json.dumps(settings), json.dumps(source_record), source_kind, engine, current_owner_id()),
        )
    except Exception:
        source_path = safe_source_path(source_record)
        if source_path and source_path.is_file():
            source_path.unlink()
        raise
    return public_upscale(db_upscale(upscale_id) or {})


@app.get("/api/upscales/{upscale_id}")
async def upscale_job(upscale_id: str):
    queued = db_queue_record("upscale", upscale_id)
    if queued:
        await scheduler_tick()
        return public_upscale(db_upscale(upscale_id) or {})
    return await refresh_upscale(upscale_id)


@app.get("/api/upscales/{upscale_id}/image")
async def upscale_image(upscale_id: str, preview: int | None = None):
    row = db_upscale(upscale_id)
    if not row or row["status"] != "completed":
        raise HTTPException(404, "Upscale image not found")
    image = json.loads(row.get("output_json") or "{}")
    path = safe_output_path(image) if isinstance(image, dict) else None
    if not path or not path.is_file():
        raise HTTPException(404, "Upscale image was removed")
    return await private_image_response(path, preview)


@app.get("/api/details/{detail_id}")
async def detail_job(detail_id: str):
    return await refresh_detail(detail_id)


@app.get("/api/details/{detail_id}/image")
async def detail_image(detail_id: str, preview: int | None = None):
    row = db_detail(detail_id)
    if not row or row["status"] != "completed":
        raise HTTPException(404, "Flux detail image not found")
    image = json.loads(row.get("output_json") or "{}")
    path = safe_output_path(image) if isinstance(image, dict) else None
    if not path or not path.is_file():
        raise HTTPException(404, "Flux detail image was removed")
    return await private_image_response(path, preview)


@app.post("/api/details/{detail_id}/cancel")
async def cancel_detail(detail_id: str):
    return await cancel_postprocess("detail", detail_id)


@app.post("/api/upscales/{upscale_id}/cancel")
async def cancel_upscale(upscale_id: str):
    return await cancel_postprocess("upscale", upscale_id)


@app.delete("/api/details/{detail_id}")
async def delete_detail(detail_id: str):
    row = db_detail(detail_id)
    if not row:
        raise HTTPException(404, "Flux detail job not found")
    if row["status"] in {"queued", "running"}:
        raise HTTPException(409, "Stop Flux detail before deleting it")
    remove_upscale_records(row["parent_group_id"], row["parent_item_id"], "detail")
    remove_detail_records(row["parent_group_id"], row["parent_item_id"])
    return {"deleted": True}


@app.delete("/api/upscales/{upscale_id}")
async def delete_upscale(upscale_id: str):
    row = db_upscale(upscale_id)
    if not row:
        raise HTTPException(404, "Upscale job not found")
    if row["status"] in {"queued", "running"}:
        raise HTTPException(409, "Stop upscale before deleting it")
    remove_upscale_records(row["parent_group_id"], row["parent_item_id"], engine=row.get("engine") or "seedvr2")
    return {"deleted": True}


@app.get("/api/activity")
async def activity():
    """Return active mobile generation groups for dashboard and global progress UI."""
    recover_stuck_groups()
    with closing(sqlite3.connect(DATABASE)) as db:
        db.row_factory = sqlite3.Row
        groups = [dict(row) for row in db.execute(
            "SELECT * FROM generation_groups WHERE status IN ('queued','running') AND storage_scope='mobile'" + (" AND owner_id=?" if current_owner_id() else "") + " ORDER BY submitted_at DESC",
            (current_owner_id(),) if current_owner_id() else (),
        ).fetchall()]
    refreshed: list[dict[str, Any]] = []
    for row in groups:
        try:
            item = await refresh_group(row["group_id"])
        except HTTPException:
            item = public_group(row)
        if item["status"] in {"queued", "running"}:
            refreshed.append(item)
    return sorted(refreshed, key=lambda item: item["submitted_at"] or "", reverse=True)

@app.get("/api/collections")
async def collections():
    return all_collections()

@app.post("/api/collections")
async def create_collection(name: Annotated[str, Form()]):
    try:
        with sqlite3.connect(DATABASE) as db:
            cursor = db.execute("INSERT INTO favorite_collections(name, created_at, owner_id) VALUES(?, ?, ?)", (collection_name(name), utc_now(), current_owner_id()))
            collection_id = cursor.lastrowid
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "Collection name already exists") from exc
    return next(item for item in all_collections() if item["id"] == collection_id)

@app.patch("/api/collections/{collection_id}")
async def rename_collection(collection_id: int, name: Annotated[str, Form()]):
    with sqlite3.connect(DATABASE) as db:
        row = db.execute("SELECT name FROM favorite_collections WHERE collection_id=? AND owner_id=?", (collection_id, current_owner_id())).fetchone()
        if not row:
            raise HTTPException(404, "Collection not found")
        if row[0] == DEFAULT_COLLECTION_NAME:
            raise HTTPException(400, "The default collection cannot be renamed")
        try:
            db.execute("UPDATE favorite_collections SET name=? WHERE collection_id=? AND owner_id=?", (collection_name(name), collection_id, current_owner_id()))
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "Collection name already exists") from exc
    return next(item for item in all_collections() if item["id"] == collection_id)

@app.delete("/api/collections/{collection_id}")
async def delete_collection(collection_id: int):
    with sqlite3.connect(DATABASE) as db:
        row = db.execute("SELECT name FROM favorite_collections WHERE collection_id=? AND owner_id=?", (collection_id, current_owner_id())).fetchone()
        if not row:
            raise HTTPException(404, "Collection not found")
        if row[0] == DEFAULT_COLLECTION_NAME:
            raise HTTPException(400, "The default collection cannot be deleted")
        record_ids = [entry[0] for entry in db.execute("SELECT record_id FROM collection_memberships WHERE collection_id=?", (collection_id,))]
        db.execute("DELETE FROM collection_memberships WHERE collection_id=?", (collection_id,))
        db.execute("DELETE FROM favorite_collections WHERE collection_id=?", (collection_id,))
        for record_id in record_ids:
            has_collection = db.execute("SELECT 1 FROM collection_memberships WHERE record_id=?", (record_id,)).fetchone()
            db.execute("UPDATE generation_groups SET is_favorite=? WHERE group_id=?", (int(bool(has_collection)), record_id))
            db.execute("UPDATE jobs SET is_favorite=? WHERE prompt_id=?", (int(bool(has_collection)), record_id))
    return {"deleted": True}

@app.put("/api/jobs/{prompt_id}/collections")
async def set_job_collections(prompt_id: str, collection_ids: list[int] = Body(...)):
    row = db_group(prompt_id) or db_job(prompt_id)
    if not row or row.get("storage_scope") != "mobile":
        raise HTTPException(404, "Mobile job not found")
    if row.get("workflow_key") not in ENABLED_WORKFLOW_KEYS:
        raise HTTPException(410, "Disabled workflow history is read-only")
    return {"collections": update_record_collections(prompt_id, collection_ids)}


def display_job_title(row: dict[str, Any]) -> str:
    def workflow_label(workflow_key: str) -> str:
        try:
            return get_spec(workflow_key).label
        except WorkflowCompileError:
            return workflow_key or "Untitled"

    return display_job_title_service(row, workflow_label)


@app.patch("/api/jobs/{prompt_id}/title")
async def rename_job(prompt_id: str, payload: dict[str, Any] = Body(...)):
    group = db_group(prompt_id)
    row = group or db_job(prompt_id)
    if not row or row.get("storage_scope") != "mobile":
        raise HTTPException(404, "Mobile job not found")
    title = normalize_job_title(payload.get("title"))
    if group:
        db_execute("UPDATE generation_groups SET title=? WHERE group_id=?", (title, prompt_id))
        db_execute("UPDATE jobs SET title=? WHERE group_id=?", (title, prompt_id))
        updated = db_group(prompt_id) or {}
        return {"title": title, "display_title": display_job_title(updated)}
    db_execute("UPDATE jobs SET title=? WHERE prompt_id=?", (title, prompt_id))
    updated = db_job(prompt_id) or {}
    return {"title": title, "display_title": display_job_title(updated)}

GALLERY_REPOSITORY = GalleryRepository(
    database_path=lambda: DATABASE,
    current_owner_id=current_owner_id,
    recycle_record_id=recycle_record_id,
    stored_items=stored_items,
    display_job_title=display_job_title,
)


def gallery_record_rows(
    favorites: bool = False,
    collection_id: int | None = None,
    workflow_key: str | None = None,
    include_active: bool = False,
    sort: str = "newest",
) -> tuple[list[tuple[str, dict[str, Any]]], dict[str, set[str]]]:
    return GALLERY_REPOSITORY.list_rows(
        favorites=favorites,
        collection_id=collection_id,
        workflow_key=workflow_key,
        include_active=include_active,
        sort=sort,
    )


def workflow_exists(workflow_key: str) -> bool:
    try:
        get_spec(workflow_key)
    except WorkflowCompileError:
        return False
    return True


gallery_router = create_gallery_router(GalleryRouteDependencies(
    workflow_exists=workflow_exists,
    gallery_record_rows=gallery_record_rows,
    load_gallery_hydration=load_gallery_hydration,
    public_group=public_group,
    public_job=public_job,
    recycle_record_id=recycle_record_id,
))
app.router.routes.extend(gallery_router.routes)
gallery = gallery_router.gallery
gallery_context = gallery_router.gallery_context

def _hard_delete_item(prompt_id: str, item_id_value: str) -> dict[str, Any]:
    group = db_group(prompt_id)
    row = group or db_job(prompt_id)
    if not row or row.get("storage_scope") != "mobile":
        raise HTTPException(404, "Image not found")
    queued = db_queue_record("generation", prompt_id) if group else None
    group_active = bool(queued and queued["state"] in QUEUE_ACTIVE_STATES)
    task_trashed = bool(recycle_task_entry(row))
    items = stored_items(row)
    item = next((entry for entry in items if entry.get("id") == item_id_value), None)
    if not item or not item.get("final"):
        raise HTTPException(404, "Image not found")
    item_index = items.index(item)
    child = None
    if group:
        child = db_job(str(item.get("prompt_id") or ""))
        if not child or child.get("group_id") != prompt_id:
            raise HTTPException(404, "Image owner not found")
        remove_upscale_records(prompt_id, item_id_value)
        remove_detail_records(prompt_id, item_id_value)
    targets: dict[str, set[tuple[str, str, str]]] = {"final": {image_key(item["final"])}}
    files_to_remove = [item["final"]]
    if item.get("stage1"):
        targets["stage1"] = {image_key(item["stage1"])}
        files_to_remove.append(item["stage1"])
    remove_output_files(files_to_remove)
    if group:
        child_outputs = output_sets(child["outputs_json"])
        db_execute("UPDATE jobs SET outputs_json=? WHERE prompt_id=?", (json.dumps(remove_images(child_outputs, targets)), child["prompt_id"]))
        remaining_items = [entry for entry in items if entry.get("id") != item_id_value]
        if remaining_items or group_active or task_trashed:
            remove_group_item_seed(group, child, item_index, len(items), group_active)
        if not remaining_items:
            if group_active:
                # A batch may have produced only its first visible result while
                # later children are still running. Keep the group and its
                # completed child as an empty slot so dispatch accounting and
                # subsequent results continue on the same history page.
                db_execute(
                    "UPDATE generation_groups SET outputs_json=?,items_json=? WHERE group_id=?",
                    (json.dumps(outputs_from_items([])), json.dumps([]), prompt_id),
                )
                return {"deleted": True, "group_deleted": False, "remaining": 0}
            if task_trashed:
                # An independently trashed task keeps its full restore window
                # even when the last separately trashed image expires first.
                db_execute(
                    "UPDATE generation_groups SET outputs_json=?,items_json=? WHERE group_id=?",
                    (json.dumps(outputs_from_items([])), json.dumps([]), prompt_id),
                )
                return {"deleted": True, "group_deleted": False, "remaining": 0}
            for child in db_group_jobs(prompt_id):
                remove_output_files([image for images in output_sets(child["outputs_json"]).values() for image in images])
            remove_source_image(group)
            remove_record_collections(prompt_id)
            db_execute("DELETE FROM jobs WHERE group_id=?", (prompt_id,))
            db_execute("DELETE FROM generation_groups WHERE group_id=?", (prompt_id,))
            return {"deleted": True, "group_deleted": True, "remaining": 0}
        db_execute("UPDATE generation_groups SET outputs_json=?, items_json=? WHERE group_id=?", (json.dumps(outputs_from_items(remaining_items)), json.dumps(remaining_items), prompt_id))
        if not group_active:
            synchronize_terminal_group_seeds(prompt_id)
    else:
        outputs = remove_images(output_sets(row["outputs_json"]), targets)
        if not outputs["final"]:
            if task_trashed:
                db_execute("UPDATE jobs SET outputs_json=? WHERE prompt_id=?", (json.dumps(outputs), prompt_id))
                return {"deleted": True, "group_deleted": False, "remaining": 0}
            remove_record_collections(prompt_id)
            db_execute("DELETE FROM jobs WHERE prompt_id=?", (prompt_id,))
            return {"deleted": True, "group_deleted": True, "remaining": 0}
        db_execute("UPDATE jobs SET outputs_json=? WHERE prompt_id=?", (json.dumps(outputs), prompt_id))
        remaining_items = [entry for entry in items if entry.get("id") != item_id_value]
    return {"deleted": True, "group_deleted": False, "remaining": len(remaining_items)}


def hard_delete_item(prompt_id: str, item_id_value: str) -> dict[str, Any]:
    result = _hard_delete_item(prompt_id, item_id_value)
    with sqlite3.connect(DATABASE) as db:
        if result.get("group_deleted"):
            db.execute("DELETE FROM recycle_bin WHERE record_id=?", (prompt_id,))
        else:
            db.execute("DELETE FROM recycle_bin WHERE kind='item' AND record_id=? AND item_id=?", (prompt_id, item_id_value))
    return result


def _hard_delete_job(prompt_id: str) -> dict[str, Any]:
    group = db_group(prompt_id)
    if group:
        if group.get("storage_scope") != "mobile": raise HTTPException(404, "Mobile job not found")
        queued = db_queue_record("generation", prompt_id)
        if queued and queued["state"] in QUEUE_ACTIVE_STATES:
            raise HTTPException(409, "任务仍在运行或排队，请先停止任务再删除")
        remove_upscale_records(prompt_id)
        remove_detail_records(prompt_id)
        for child in db_group_jobs(prompt_id):
            remove_output_files([image for image_list in output_sets(child["outputs_json"]).values() for image in image_list])
        remove_source_image(group)
        remove_record_collections(prompt_id)
        db_execute("DELETE FROM jobs WHERE group_id=?", (prompt_id,))
        db_execute("DELETE FROM generation_groups WHERE group_id=?", (prompt_id,))
        return {"deleted": True}
    row = db_job(prompt_id)
    if not row or row.get("storage_scope") != "mobile": raise HTTPException(404, "Mobile job not found")
    remove_output_files([image for image_list in output_sets(row["outputs_json"]).values() for image in image_list])
    remove_record_collections(prompt_id)
    db_execute("DELETE FROM jobs WHERE prompt_id=?", (prompt_id,))
    return {"deleted": True}


def hard_delete_job(prompt_id: str) -> dict[str, Any]:
    result = _hard_delete_job(prompt_id)
    db_execute("DELETE FROM recycle_bin WHERE record_id=?", (prompt_id,))
    return result


def parse_utc_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def create_recycle_entry(owner_id: int, kind: str, record_id: str, item_id_value: str = "") -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    deleted_at = now.isoformat()
    purge_at = (now + RECYCLE_RETENTION).isoformat()
    with RECYCLE_LOCK, closing(db_connect()) as db, db:
        db.row_factory = sqlite3.Row
        db.execute(
            """INSERT OR IGNORE INTO recycle_bin(owner_id,kind,record_id,item_id,deleted_at,purge_at)
               VALUES(?,?,?,?,?,?)""",
            (owner_id, kind, record_id, item_id_value, deleted_at, purge_at),
        )
        row = db.execute(
            "SELECT * FROM recycle_bin WHERE owner_id=? AND kind=? AND record_id=? AND item_id=?",
            (owner_id, kind, record_id, item_id_value),
        ).fetchone()
    return dict(row)


def trash_item(prompt_id: str, item_id_value: str) -> dict[str, Any]:
    group = db_group(prompt_id)
    row = group or db_job(prompt_id)
    if not row or row.get("storage_scope") != "mobile":
        raise HTTPException(404, "Image not found")
    if recycle_task_entry(row):
        raise HTTPException(409, "任务已在回收站中")
    item = next((entry for entry in stored_items(row) if str(entry.get("id") or "") == item_id_value), None)
    if not item or not item.get("final"):
        raise HTTPException(404, "Image not found")
    owner_id = int(row.get("owner_id") or current_owner_id() or 0)
    if not owner_id:
        raise HTTPException(404, "Image owner not found")
    entry = create_recycle_entry(owner_id, "item", prompt_id, item_id_value)
    remaining = len(visible_stored_items(row))
    return {
        "trashed": True, "entry_id": int(entry["entry_id"]), "purge_at": entry["purge_at"],
        "group_deleted": False, "group_hidden": remaining == 0, "remaining": remaining,
    }


def trash_job(prompt_id: str) -> dict[str, Any]:
    group = db_group(prompt_id)
    row = group or db_job(prompt_id)
    if not row or row.get("storage_scope") != "mobile":
        raise HTTPException(404, "Mobile job not found")
    if group:
        queued = db_queue_record("generation", prompt_id)
        if queued and queued["state"] in QUEUE_ACTIVE_STATES:
            raise HTTPException(409, "任务仍在运行或排队，请先停止任务再删除")
    owner_id = int(row.get("owner_id") or current_owner_id() or 0)
    if not owner_id:
        raise HTTPException(404, "Mobile job owner not found")
    entry = create_recycle_entry(owner_id, recycle_record_kind(row), prompt_id)
    return {"trashed": True, "entry_id": int(entry["entry_id"]), "purge_at": entry["purge_at"]}


def purge_recycle_entry(entry: dict[str, Any]) -> None:
    try:
        if entry["kind"] == "item":
            hard_delete_item(str(entry["record_id"]), str(entry["item_id"]))
        else:
            hard_delete_job(str(entry["record_id"]))
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
    finally:
        db_execute("DELETE FROM recycle_bin WHERE entry_id=?", (int(entry["entry_id"]),))


def purge_expired_recycle_entries(owner_id: int | None = None) -> int:
    now = utc_now()
    with RECYCLE_LOCK:
        query = "SELECT * FROM recycle_bin WHERE purge_at<=?"
        values: tuple[Any, ...] = (now,)
        if owner_id is not None:
            query += " AND owner_id=?"
            values += (owner_id,)
        query += " ORDER BY purge_at,CASE kind WHEN 'item' THEN 0 ELSE 1 END,entry_id"
        with sqlite3.connect(DATABASE) as db:
            db.row_factory = sqlite3.Row
            entries = [dict(row) for row in db.execute(query, values).fetchall()]
        for entry in entries:
            purge_recycle_entry(entry)
    return len(entries)


async def recycle_cleanup_loop() -> None:
    while True:
        await asyncio.sleep(RECYCLE_CLEANUP_INTERVAL_SECONDS)
        try:
            await asyncio.to_thread(purge_expired_recycle_entries)
        except Exception:
            pass


def recycle_entry_payload(entry: dict[str, Any]) -> dict[str, Any] | None:
    record_id = str(entry["record_id"])
    if entry["kind"] == "group":
        row = db_group(record_id)
        record = public_group(row) if row else None
    elif entry["kind"] == "job":
        row = db_job(record_id)
        record = public_job(row) if row else None
    else:
        row = db_group(record_id) or db_job(record_id)
        if not row:
            record = None
        elif recycle_record_kind(row) == "group":
            record = public_group(row, True, str(entry["item_id"]))
        else:
            record = public_job(row, True, str(entry["item_id"]))
    if not record:
        return None
    remaining = max(0, math.ceil((parse_utc_timestamp(entry["purge_at"]) - datetime.now(timezone.utc)).total_seconds()))
    return {
        "id": int(entry["entry_id"]), "kind": "image" if entry["kind"] == "item" else "task",
        "record_kind": entry["kind"], "record_id": record_id,
        "item_id": str(entry.get("item_id") or ""), "deleted_at": entry["deleted_at"],
        "purge_at": entry["purge_at"], "remaining_seconds": remaining, "record": record,
    }


@app.get("/api/recycle-bin")
async def recycle_bin_entries():
    owner_id = int(current_user()["user_id"])
    purge_expired_recycle_entries(owner_id)
    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(row) for row in db.execute(
            "SELECT * FROM recycle_bin WHERE owner_id=? ORDER BY purge_at,entry_id", (owner_id,),
        ).fetchall()]
    entries = [payload for row in rows if (payload := recycle_entry_payload(row))]
    return entries


@app.post("/api/recycle-bin/{entry_id}/restore")
async def restore_recycle_entry(entry_id: int):
    owner_id = int(current_user()["user_id"])
    with RECYCLE_LOCK:
        with sqlite3.connect(DATABASE) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT * FROM recycle_bin WHERE entry_id=? AND owner_id=?", (entry_id, owner_id),
            ).fetchone()
        if not row:
            raise HTTPException(404, "回收站条目不存在")
        entry = dict(row)
        if parse_utc_timestamp(entry["purge_at"]) <= datetime.now(timezone.utc):
            purge_recycle_entry(entry)
            raise HTTPException(410, "恢复时间已结束，内容已永久清理")
        db_execute("DELETE FROM recycle_bin WHERE entry_id=? AND owner_id=?", (entry_id, owner_id))
    return {"restored": True, "kind": "image" if entry["kind"] == "item" else "task", "record_id": entry["record_id"]}


@app.delete("/api/jobs/{prompt_id}/items/{item_id_value}")
async def delete_job_item(prompt_id: str, item_id_value: str):
    return trash_item(prompt_id, item_id_value)


@app.delete("/api/jobs/{prompt_id}/images/{index}")
async def delete_job_image(prompt_id: str, index: int):
    row = db_group(prompt_id) or db_job(prompt_id)
    items = visible_stored_items(row) if row else []
    if index < 0 or index >= len(items):
        raise HTTPException(404, "Image not found")
    return trash_item(prompt_id, str(items[index]["id"]))


@app.delete("/api/jobs/{prompt_id}")
async def delete_job(prompt_id: str):
    return trash_job(prompt_id)

@app.post("/api/jobs/{prompt_id}/favorite")
async def set_favorite(prompt_id: str, value: bool):
    row = db_group(prompt_id) or db_job(prompt_id)
    if not row or row.get("storage_scope") != "mobile": raise HTTPException(404, "Mobile job not found")
    if row.get("workflow_key") not in ENABLED_WORKFLOW_KEYS:
        raise HTTPException(410, "Disabled workflow history is read-only")
    if value:
        with sqlite3.connect(DATABASE) as db:
            default_id = db.execute("SELECT collection_id FROM favorite_collections WHERE name=? AND owner_id=?", (DEFAULT_COLLECTION_NAME, current_owner_id())).fetchone()[0]
        current = [item["id"] for item in collections_for_record(prompt_id)]
        collections = update_record_collections(prompt_id, current + [default_id])
    else:
        collections = update_record_collections(prompt_id, [])
    return {"favorite": bool(collections), "collections": collections}

@app.delete("/api/workflows/{workflow_key}/outputs")
async def clear_workflow_outputs(workflow_key: str):
    try: get_spec(workflow_key)
    except WorkflowCompileError as exc: raise HTTPException(404, "Workflow not found") from exc
    if workflow_key not in ENABLED_WORKFLOW_KEYS:
        raise HTTPException(410, "Disabled workflows must be removed from history individually")
    target = MOBILE_OUTPUT_DIR / workflow_key
    if target.exists(): shutil.rmtree(target)
    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        groups = [dict(row) for row in db.execute("SELECT * FROM generation_groups WHERE workflow_key=? AND storage_scope='mobile' AND owner_id=?", (workflow_key, current_owner_id()))]
        jobs = [dict(row) for row in db.execute("SELECT * FROM jobs WHERE workflow_key=? AND storage_scope='mobile' AND group_id IS NULL AND owner_id=?", (workflow_key, current_owner_id()))]
    for group in groups:
        remove_upscale_records(group["group_id"])
        remove_detail_records(group["group_id"])
        remove_source_image(group)
        remove_record_collections(group["group_id"])
        db_execute("DELETE FROM recycle_bin WHERE owner_id=? AND record_id=?", (current_owner_id(), group["group_id"]))
    for row in jobs:
        remove_record_collections(row["prompt_id"])
        db_execute("DELETE FROM recycle_bin WHERE owner_id=? AND record_id=?", (current_owner_id(), row["prompt_id"]))
    owner = current_owner_id()
    db_execute("DELETE FROM jobs WHERE group_id IN (SELECT group_id FROM generation_groups WHERE workflow_key=? AND storage_scope='mobile' AND owner_id=?)", (workflow_key, owner))
    db_execute("DELETE FROM generation_groups WHERE workflow_key=? AND storage_scope='mobile' AND owner_id=?", (workflow_key, owner))
    db_execute("DELETE FROM jobs WHERE workflow_key=? AND storage_scope='mobile' AND owner_id=?", (workflow_key, owner))
    return {"deleted": True}

@app.get("/api/jobs/{prompt_id}/source-image")
async def source_image_file(prompt_id: str, preview: int | None = None):
    group = db_group(prompt_id)
    image = source_image_from(group) if group else None
    path = safe_source_path(image) if image else None
    if not path or not path.is_file():
        raise HTTPException(404, "Source image is unavailable")
    return await private_image_response(path, preview)


@app.get("/api/jobs/{prompt_id}/reference-image")
async def reference_image_file(prompt_id: str, preview: int | None = None):
    group = db_group(prompt_id)
    image = json_image_from(group, "reference_image_json") if group else None
    path = safe_source_path(image) if image else None
    if not path or not path.is_file():
        raise HTTPException(404, "Second reference image is unavailable")
    return await private_image_response(path, preview)


@app.get("/api/jobs/{prompt_id}/edit-mask")
async def edit_mask_file(prompt_id: str, preview: int | None = None):
    group = db_group(prompt_id)
    image = json_image_from(group, "edit_mask_json") if group else None
    path = safe_source_path(image) if image else None
    if not path or not path.is_file():
        raise HTTPException(404, "Edit mask is unavailable")
    return await private_image_response(path, preview)


@app.get("/api/jobs/{prompt_id}/preprocess-image")
async def group_preprocess_image_file(prompt_id: str, preview: int | None = None):
    group = db_group(prompt_id)
    preprocess = group_preprocess(group) if group else None
    image = preprocess.get("output") if preprocess else None
    path = safe_output_path(image) if isinstance(image, dict) else None
    if not path or not path.is_file():
        raise HTTPException(404, "Pre-upscale image is unavailable")
    return await private_image_response(path, preview)


@app.get("/api/jobs/{group_id}/items/{item_id_value}/reuse-image")
async def reusable_result_file(group_id: str, item_id_value: str, kind: str = "edit", record_id: str = "", preview: int | None = None):
    image = reusable_result_image(group_id, item_id_value, kind, record_id)
    path = safe_output_path(image)
    if not path or not path.is_file():
        raise HTTPException(404, "Reusable result image was removed")
    return await private_image_response(path, preview)


@app.get("/api/images/{prompt_id}/items/{item_id_value}/poster")
async def item_video_poster(prompt_id: str, item_id_value: str, preview: int = 512):
    row = db_group(prompt_id) or db_job(prompt_id)
    if not row or row.get("storage_scope") != "mobile" or row.get("workflow_key") != "minimax-h3":
        raise HTTPException(404, "Video poster not found")
    try:
        parameters = json.loads(row.get("parameters_json") or "{}")
    except json.JSONDecodeError:
        parameters = {}
    if parameters.get("mode") == "i2v":
        source = source_image_from(row)
        source_path = safe_source_path(source) if source else None
        if source_path and source_path.is_file():
            return await private_image_response(source_path, preview)
    item = next((entry for entry in stored_items(row) if entry.get("id") == item_id_value), None)
    video = item.get("final") if item else None
    path = safe_output_path(video) if isinstance(video, dict) else None
    if not path or not path.is_file() or not output_media_type(video).startswith("video/"):
        raise HTTPException(404, "Video poster not found")
    return await private_video_poster_response(path, preview, create_thumbnail=video_thumbnail_for)


@app.get("/api/images/{prompt_id}/items/{item_id_value}/{kind}")
async def item_image_file(prompt_id: str, item_id_value: str, kind: str, preview: int | None = None):
    row = db_group(prompt_id) or db_job(prompt_id)
    if not row or row.get("storage_scope") != "mobile" or kind not in {"final", "stage1"}:
        raise HTTPException(404, "Image not found")
    item = next((entry for entry in stored_items(row) if entry.get("id") == item_id_value), None)
    image = item.get(kind) if item else None
    path = safe_output_path(image) if image else None
    if not path or not path.is_file():
        raise HTTPException(404, "Image file was removed")
    return await private_image_response(path, preview)

@app.get("/api/images/{prompt_id}/{kind}/{index}")
async def image_file(prompt_id: str, kind: str, index: int, preview: int | None = None):
    row = db_group(prompt_id) or db_job(prompt_id)
    if not row or row.get("storage_scope") != "mobile": raise HTTPException(404, "Image not found")
    images = output_sets(row["outputs_json"]).get(kind)
    if images is None: raise HTTPException(404, "Image not found")
    if index < 0 or index >= len(images): raise HTTPException(404, "Image not found")
    path = safe_output_path(images[index])
    if not path or not path.is_file(): raise HTTPException(404, "Image file was removed")
    return await private_image_response(path, preview)
