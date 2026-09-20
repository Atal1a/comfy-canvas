"""Validate canonical mobile workflows and compile them to ComfyUI API prompts."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Iterable


QWEN_LIGHTNING_LORA = "Qwen-Image-Edit-2511-Lightning-8steps-V1.0-bf16.safetensors"


@dataclass(frozen=True)
class WorkflowSpec:
    key: str
    label: str
    desktop_source_name: str
    mobile_source_name: str
    prompt_node: str
    prompt_input: str
    image_node: str | None = None
    image_input: str | None = None
    kind: str = "image_generation"
    variant: str = ""
    stage1_output_node: str | None = None
    lora_family: str = "krea"
    max_lora_slots: int = 3
    description: str = ""


WORKFLOWS = (
    WorkflowSpec(
        "qwen2511-modular-flux2",
        "Qwen 2511 Modular FP8 Edit",
        "Qwen2511_Modular_Edit_FP8.json",
        "qwen2511-modular-flux2.mobile.json",
        "444",
        "string_b",
        "78",
        "image",
        "image_edit",
        "modular2511",
        None,
        "qwen2511",
        10,
        "Qwen 2511 FP8、Lightning 8-step 与最多十个可选 LoRA；完成后可选增强放大。",
    ),
    WorkflowSpec(
        "krea-identity-edit",
        "Krea2 图生图",
        "Krea2_Identity_Edit.json",
        "krea-identity-edit.mobile.json",
        "84",
        "prompt",
        "72",
        "image",
        "image_edit",
        "krea_identity",
        None,
        "krea",
        10,
        "基于官方 Krea2 Turbo 的 Identity Edit v1.2；支持单图编辑、身份重塑和双参考图编辑。",
    ),
    WorkflowSpec(
        key="minimax-h3",
        label="MiniMax H3 Video",
        desktop_source_name="MiniMax H3 Turbo.json",
        mobile_source_name="minimax-h3.mobile.json",
        prompt_node="131",
        prompt_input="prompt",
        kind="video_generation",
        variant="minimax_h3",
        lora_family="minimax_h3",
        max_lora_slots=10,
        description="MiniMax H3 Turbo text-to-video or first-frame image-to-video with native stereo audio.",
    ),
)

ENABLED_WORKFLOW_KEYS = frozenset(
    {"qwen2511-modular-flux2", "krea-identity-edit", "minimax-h3"}
)
WORKFLOW_DISPLAY_PRIORITY = {
    "krea-identity-edit": 10,
    "minimax-h3": 20,
    "qwen2511-modular-flux2": 30,
}


def enabled_workflows(
    keys: Iterable[str] = ENABLED_WORKFLOW_KEYS,
) -> tuple[WorkflowSpec, ...]:
    selected = frozenset(keys)
    return tuple(
        sorted(
            (spec for spec in WORKFLOWS if spec.key in selected),
            key=lambda spec: (WORKFLOW_DISPLAY_PRIORITY.get(spec.key, 1000), spec.label),
        )
    )


def is_two_stage_key(key: str) -> bool:
    """Return whether stored records may expose stage-specific seed fields."""
    return key == "qwen2511-modular-flux2"


class WorkflowCompileError(ValueError):
    pass


def get_spec(key: str) -> WorkflowSpec:
    for item in WORKFLOWS:
        if item.key == key:
            return item
    raise WorkflowCompileError("Unknown workflow")


def _refresh_links(graph: dict[str, Any]) -> None:
    nodes = {node["id"]: node for node in graph["nodes"]}
    graph["links"] = [
        link
        for link in graph.get("links", [])
        if link[1] in nodes and link[3] in nodes
    ]
    links = graph["links"]
    for node in nodes.values():
        inputs = node.get("inputs", [])
        for index, entry in enumerate(inputs):
            entry["link"] = next(
                (link[0] for link in links if link[3] == node["id"] and link[4] == index),
                None,
            )
        outputs = node.get("outputs", [])
        for index, output in enumerate(outputs):
            output["links"] = [
                link[0]
                for link in links
                if link[1] == node["id"] and link[2] == index
            ] or None


def _set_widget(node: dict[str, Any], value: Any) -> None:
    node["widgets_values"] = [value]


def ensure_enhanced_upscale_workflow(source: Path, destination: Path) -> None:
    """Prepare the reference SeedVR2 + tiled Flux2 Klein enhancement graph."""
    try:
        graph = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowCompileError(
            f"Cannot create enhanced upscale workflow: {source.name}"
        ) from exc
    required = {
        4, 5, 6, 17, 83, 120, 125, 128, 131, 132, 139, 143, 176, 177,
        181, 182, 183, 185, 186, 193, 196, 197, 198, 199, 201, 202, 203,
        211, 213, 222, 223,
    }
    graph["nodes"] = [
        node for node in graph.get("nodes", []) if node.get("id") in required
    ]
    nodes = {node["id"]: node for node in graph["nodes"]}
    if not required.issubset(nodes):
        missing = sorted(required - set(nodes))
        raise WorkflowCompileError(
            f"Enhanced upscale workflow is missing nodes: {missing}"
        )
    _set_widget(nodes[143], "mobile/enhanced-upscale/Flux2_Klein_Enhanced")
    nodes[211]["widgets_values"][4] = "res_2s"
    graph.setdefault("extra", {})["mobile_server_version"] = 1
    _refresh_links(graph)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def ensure_mobile_workflows(desktop_dir: Path, mobile_dir: Path) -> None:
    """Copy packaged canonical workflows without mutating desktop sources.

    The checked-in ``*.mobile.json`` files are authoritative. ``desktop_dir`` is
    accepted for compatibility with maintenance callers, but is intentionally
    never written or used as an implicit source of repository defaults.
    """
    del desktop_dir
    bundled_dir = Path(__file__).resolve().parent / "workflows"
    mobile_dir.mkdir(parents=True, exist_ok=True)
    for spec in enabled_workflows():
        source = bundled_dir / spec.mobile_source_name
        destination = mobile_dir / spec.mobile_source_name
        try:
            graph = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkflowCompileError(
                f"Cannot read packaged workflow: {spec.mobile_source_name}"
            ) from exc
        if source.resolve() != destination.resolve():
            destination.write_text(
                json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    validate_mobile_workflows(mobile_dir)


def validate_mobile_workflows(
    mobile_dir: Path,
    keys: Iterable[str] = ENABLED_WORKFLOW_KEYS,
) -> None:
    """Validate canonical mobile workflow files without rewriting them."""
    for spec in enabled_workflows(keys):
        source = mobile_dir / spec.mobile_source_name
        try:
            graph = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkflowCompileError(
                f"Cannot read canonical mobile workflow: {spec.mobile_source_name}"
            ) from exc
        if not graph.get("nodes") or not graph.get("links"):
            raise WorkflowCompileError(
                f"Incomplete canonical mobile workflow: {spec.mobile_source_name}"
            )
        if spec.variant == "modular2511" and graph.get("extra", {}).get(
            "mobile_server_version", 0
        ) < 12:
            raise WorkflowCompileError(
                f"Canonical mobile workflow requires rebuilding: {spec.mobile_source_name}"
            )
        nodes = {int(node["id"]): node for node in graph.get("nodes", [])}
        required_loras = {
            "krea-identity-edit": {109, 110, 111, 117, 118, 119},
        }.get(spec.key)
        if required_loras and not required_loras.issubset(nodes):
            raise WorkflowCompileError(
                f"Canonical mobile workflow requires six LoRA slots: {spec.mobile_source_name}"
            )
        if spec.key == "krea-identity-edit" and not any(
            item.get("name") == "target_latent"
            for item in nodes[79].get("inputs", [])
        ):
            raise WorkflowCompileError(
                "Canonical Krea2 Identity workflow is missing target_latent: "
                f"{spec.mobile_source_name}"
            )


def _input_values(node: dict[str, Any]) -> dict[str, Any]:
    names = [entry["name"] for entry in node.get("inputs", []) if "widget" in entry]
    values = list(node.get("widgets_values", []))
    if not names:
        node_type = node["type"]
        known_inputs = {
            "ResolutionSelector": ["aspect_ratio", "megapixels", "multiple"],
            "KSampler": [
                "seed", "__seed_action", "steps", "cfg", "sampler_name",
                "scheduler", "denoise",
            ],
            "LoraLoaderModelOnly": ["lora_name", "strength_model"],
            "CLIPLoader": ["clip_name", "type", "device"],
            "VAELoader": ["vae_name"],
            "SaveImage": ["filename_prefix"],
            "UNETLoader": ["unet_name", "weight_dtype"],
            "RandomNoise": ["noise_seed", "__seed_action"],
            "BasicScheduler": ["scheduler", "steps", "denoise"],
            "MiniMaxH3TurboLoRA": ["lora_name", "strength", "low_vram"],
            "MiniMaxH3ImageToVideo": ["prompt", "width", "height", "length"],
            "PrimitiveFloat": ["value"],
            "ComfyMathExpression": ["expression"],
            "CreateVideo": ["fps"],
            "SaveVideo": ["filename_prefix", "format", "codec"],
        }
        if node_type == "Power Lora Loader (rgthree)":
            loras = [value for value in values if isinstance(value, dict) and "lora" in value]
            return {
                f"lora_{index}": value
                for index, value in enumerate(loras, start=1)
            }
        names = known_inputs.get(node_type, [])
        if not names:
            return {}
    result: dict[str, Any] = {}
    index = 0
    for name in names:
        if index >= len(values):
            break
        result[name] = values[index]
        index += 1
        if (
            name in {"seed", "value"}
            and "__seed_action" not in names
            and index < len(values)
            and values[index] in {"fixed", "increment", "decrement", "randomize"}
        ):
            index += 1
    result.pop("__seed_action", None)
    return result


def compile_workflow(
    source: Path,
    overrides: dict[str, dict[str, Any]],
    bypass_nodes: Iterable[str] = (),
    output_node_types: Iterable[str] = ("SaveImage",),
) -> dict[str, Any]:
    try:
        graph = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowCompileError(f"Cannot read workflow: {source.name}") from exc
    nodes = {str(node["id"]): node for node in graph.get("nodes", [])}
    links = {str(link[0]): link for link in graph.get("links", [])}
    bypass = set(bypass_nodes)
    if not nodes or not links:
        raise WorkflowCompileError("Incomplete workflow graph")

    def is_bypassed(node_id: str) -> bool:
        return nodes[node_id].get("mode") == 4 or node_id in bypass

    def source_for(
        node_id: str,
        slot: int,
        seen: set[tuple[str, int]] | None = None,
    ) -> list[Any]:
        seen = seen or set()
        if (node_id, slot) in seen:
            raise WorkflowCompileError("Workflow contains a loop")
        seen.add((node_id, slot))
        node = nodes[node_id]
        if not is_bypassed(node_id):
            return [node_id, slot]
        inputs = node.get("inputs", [])
        if not inputs:
            raise WorkflowCompileError(f"Cannot bypass node: {node['type']}")
        passthrough_input = {
            "ColorMatch": 1,
            "ColorMatchV2": 0,
        }.get(node["type"], min(slot, len(inputs) - 1))
        link_id = inputs[passthrough_input].get("link")
        link = links.get(str(link_id)) if link_id is not None else None
        if link is None:
            raise WorkflowCompileError(f"Bypassed node has no input: {node['type']}")
        return source_for(str(link[1]), int(link[2]), seen)

    accepted_outputs = set(output_node_types)
    outputs = {
        node_id
        for node_id, node in nodes.items()
        if node["type"] in accepted_outputs and not is_bypassed(node_id)
    }
    if not outputs:
        raise WorkflowCompileError("No supported mobile output node")
    needed, pending = set(outputs), list(outputs)
    while pending:
        node = nodes[pending.pop()]
        for item in node.get("inputs", []):
            link = links.get(str(item.get("link")))
            if link and str(link[1]) not in needed:
                needed.add(str(link[1]))
                pending.append(str(link[1]))

    result: dict[str, Any] = {}
    for node_id, node in nodes.items():
        if node_id not in needed or is_bypassed(node_id):
            continue
        inputs = _input_values(node)
        for item in node.get("inputs", []):
            link = links.get(str(item.get("link")))
            if link:
                inputs[item["name"]] = source_for(str(link[1]), int(link[2]))
        inputs.update(deepcopy(overrides.get(node_id, {})))
        result[node_id] = {"class_type": node["type"], "inputs": inputs}
    return result
