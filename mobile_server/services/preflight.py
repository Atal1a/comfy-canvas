"""Small dependency checks using ComfyUI's own node descriptions.

ComfyUI remains responsible for full graph validation. This check prevents
known missing nodes and model/sampler options from starting a partial graph.
"""
from typing import Any

from fastapi import HTTPException


DEPENDENCY_INPUTS = frozenset({
    "unet_name", "clip_name", "vae_name", "lora_name", "sampler_name", "scheduler", "model",
})


def validate_dependencies(graph: dict[str, Any], node_info: dict[str, Any]) -> None:
    if not isinstance(node_info, dict) or not node_info:
        raise HTTPException(503, "无法读取 ComfyUI 节点清单，请确认 ComfyUI 已启动。")
    problems = []
    for node_id, node in graph.items():
        kind = node.get("class_type", "")
        info = node_info.get(kind)
        if not isinstance(info, dict):
            problems.append(f"节点 {node_id}：缺少 {kind}")
            continue
        inputs = info.get("input", {})
        schema = {**inputs.get("required", {}), **inputs.get("optional", {})}
        for name, value in node.get("inputs", {}).items():
            if name not in DEPENDENCY_INPUTS or not isinstance(value, str):
                continue
            definition = schema.get(name)
            if not isinstance(definition, (list, tuple)) or not definition:
                continue
            choices = definition[0]
            if isinstance(choices, (list, tuple)) and value not in choices:
                problems.append(f"节点 {node_id}（{kind}）：{name} 不可用：{value}")
    if problems:
        detail = "；".join(dict.fromkeys(problems))
        raise HTTPException(409, f"ComfyUI 依赖未就绪：{detail}。请检查模型配置或安装对应节点。")
