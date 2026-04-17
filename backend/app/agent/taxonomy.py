"""
Feature Taxonomy — predefined knowledge base of GPU-related features
per project category. The Agent uses this as a scaffold, then dynamically
discovers project-specific features via README analysis.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FeatureSpec:
    id: str
    dimension: str  # model_support | feature_availability | performance_parity | kernel_backend | engineering_maturity
    description: str
    check_keywords: list[str] = field(default_factory=list)
    nvidia_default: str = "available"
    amd_default: str = "unknown"


INFERENCE_FEATURES: dict[str, dict[str, Any]] = {
    "attention_backends": {
        "dimension": "kernel_backend",
        "description": "Attention computation backends (FlashAttention, FlashInfer, xFormers, PagedAttention, FlashMLA, SDPA)",
        "check_keywords": [
            "flashattention", "flash_attn", "flash_attention", "xformers",
            "paged_attention", "flash_mla", "flashinfer", "sdpa", "aiter",
        ],
    },
    "quantization_schemes": {
        "dimension": "kernel_backend",
        "description": "Weight/activation quantization (FP8, INT8, INT4, AWQ, GPTQ, Marlin, mxfp4, GGUF, compressed-tensors)",
        "check_keywords": [
            "fp8", "int8", "int4", "awq", "gptq", "marlin", "mxfp4",
            "gguf", "quantiz", "compressed-tensor", "w4a16", "w8a8",
        ],
    },
    "model_architectures": {
        "dimension": "model_support",
        "description": "Supported model families and architectures (LLaMA, Mistral, Gemma, Qwen, DeepSeek, etc.)",
        "check_keywords": [
            "llama", "mistral", "gemma", "qwen", "deepseek", "phi",
            "mixtral", "moe", "model support", "supported model",
        ],
    },
    "distributed_inference": {
        "dimension": "feature_availability",
        "description": "Multi-GPU/multi-node inference: tensor parallelism, pipeline parallelism, expert parallelism",
        "check_keywords": [
            "tensor parallel", "pipeline parallel", "data parallel",
            "expert parallel", "distributed", "multi-gpu", "nccl", "rccl",
        ],
    },
    "speculative_decoding": {
        "dimension": "feature_availability",
        "description": "Speculative decoding methods (EAGLE, Medusa, n-gram, draft model)",
        "check_keywords": [
            "speculative", "eagle", "medusa", "draft model", "lookahead",
        ],
    },
    "kv_cache_management": {
        "dimension": "feature_availability",
        "description": "KV cache strategies (paged, prefix caching, automatic, FP8 KV cache)",
        "check_keywords": [
            "kv cache", "prefix cach", "paged", "fp8 kv", "kvcache",
        ],
    },
    "continuous_batching": {
        "dimension": "feature_availability",
        "description": "Continuous/dynamic batching and chunked prefill",
        "check_keywords": [
            "continuous batch", "dynamic batch", "chunked prefill",
        ],
    },
    "disaggregated_serving": {
        "dimension": "feature_availability",
        "description": "Disaggregated prefill/decode and heterogeneous serving",
        "check_keywords": [
            "disaggregat", "prefill decode", "heterogeneous",
        ],
    },
    "structured_output": {
        "dimension": "feature_availability",
        "description": "Structured/constrained generation (JSON mode, grammar, regex)",
        "check_keywords": [
            "structured output", "json mode", "grammar", "constrained",
        ],
    },
    "vision_multimodal": {
        "dimension": "model_support",
        "description": "Vision-language and multimodal model support",
        "check_keywords": [
            "vision", "multimodal", "vlm", "image", "video",
        ],
    },
    "lora_adapters": {
        "dimension": "feature_availability",
        "description": "LoRA/multi-LoRA adapter serving",
        "check_keywords": [
            "lora", "multi-lora", "adapter", "peft",
        ],
    },
    "torch_compile": {
        "dimension": "feature_availability",
        "description": "torch.compile / graph compilation integration",
        "check_keywords": [
            "torch.compile", "torch compile", "graph compile", "inductor",
        ],
    },
    "gemm_kernels": {
        "dimension": "kernel_backend",
        "description": "GEMM/matmul kernel backends (CUTLASS, cuBLAS, hipBLAS, Composable Kernel, Triton)",
        "check_keywords": [
            "cutlass", "cublas", "hipblas", "composable kernel",
            "gemm", "matmul", "triton kernel",
        ],
    },
    "communication_backend": {
        "dimension": "kernel_backend",
        "description": "GPU communication libraries (NCCL, RCCL, custom all-reduce)",
        "check_keywords": [
            "nccl", "rccl", "all-reduce", "all_reduce", "collective",
        ],
    },
}

TRAINING_FEATURES: dict[str, dict[str, Any]] = {
    "mixed_precision": {
        "dimension": "feature_availability",
        "description": "Mixed precision training (FP16, BF16, FP8 training)",
        "check_keywords": [
            "fp16", "bf16", "fp8", "mixed precision", "amp",
        ],
    },
    "zero_optimization": {
        "dimension": "feature_availability",
        "description": "ZeRO optimizer stages and memory optimization",
        "check_keywords": [
            "zero", "zero-3", "offload", "memory optim",
        ],
    },
    "distributed_training": {
        "dimension": "feature_availability",
        "description": "Distributed training (DDP, FSDP, model parallelism, pipeline parallelism)",
        "check_keywords": [
            "ddp", "fsdp", "model parallel", "pipeline parallel",
            "data parallel", "tensor parallel", "3d parallel",
        ],
    },
    "optimizer_backends": {
        "dimension": "kernel_backend",
        "description": "Fused/custom optimizer kernels (fused Adam, LAMB, LION)",
        "check_keywords": [
            "fused adam", "fused optimizer", "lamb", "lion", "apex",
        ],
    },
    "gradient_checkpointing": {
        "dimension": "feature_availability",
        "description": "Activation/gradient checkpointing for memory efficiency",
        "check_keywords": [
            "gradient checkpoint", "activation checkpoint", "recompute",
        ],
    },
    "flash_attention_training": {
        "dimension": "kernel_backend",
        "description": "FlashAttention for training workloads",
        "check_keywords": [
            "flash attention", "flash_attn", "flashattention",
        ],
    },
    "model_architectures_training": {
        "dimension": "model_support",
        "description": "Supported model architectures for training/fine-tuning",
        "check_keywords": [
            "llama", "gpt", "transformer", "moe", "fine-tun",
        ],
    },
    "rlhf_alignment": {
        "dimension": "feature_availability",
        "description": "RLHF, DPO, PPO and alignment training",
        "check_keywords": [
            "rlhf", "dpo", "ppo", "reward model", "alignment",
        ],
    },
    "communication_training": {
        "dimension": "kernel_backend",
        "description": "Communication backend for distributed training (NCCL/RCCL)",
        "check_keywords": [
            "nccl", "rccl", "collective", "all-reduce",
        ],
    },
}

TOOL_FEATURES: dict[str, dict[str, Any]] = {
    "kernel_compilation": {
        "dimension": "feature_availability",
        "description": "GPU kernel compilation and code generation",
        "check_keywords": [
            "compile", "codegen", "jit", "kernel",
        ],
    },
    "gpu_primitives": {
        "dimension": "kernel_backend",
        "description": "Low-level GPU primitives (warp, shared memory, tensor core, matrix core)",
        "check_keywords": [
            "warp", "shared memory", "tensor core", "matrix core",
            "wmma", "mfma", "dpetha",
        ],
    },
    "data_types": {
        "dimension": "feature_availability",
        "description": "Numeric data type support (FP8, BF16, FP4, block-scaled)",
        "check_keywords": [
            "fp8", "bf16", "fp4", "mxfp", "block scale", "e4m3", "e5m2",
        ],
    },
    "model_hub_integration": {
        "dimension": "feature_availability",
        "description": "Model loading, Hub integration, format support",
        "check_keywords": [
            "from_pretrained", "safetensors", "hub", "model card",
        ],
    },
    "attention_ops": {
        "dimension": "kernel_backend",
        "description": "Attention operator implementations",
        "check_keywords": [
            "attention", "flash", "sdpa", "cross_attention",
        ],
    },
    "convolution_ops": {
        "dimension": "kernel_backend",
        "description": "Convolution and image processing operations",
        "check_keywords": [
            "conv", "convolution", "depthwise", "image",
        ],
    },
    "async_operations": {
        "dimension": "kernel_backend",
        "description": "Async copy, TMA, prefetch, memory operations",
        "check_keywords": [
            "async", "tma", "prefetch", "tensor memory",
        ],
    },
    "python_dsl": {
        "dimension": "feature_availability",
        "description": "Python DSL or high-level API for kernel writing",
        "check_keywords": [
            "dsl", "python api", "autotune", "pythonic",
        ],
    },
}

CATEGORY_TAXONOMY = {
    "inference": INFERENCE_FEATURES,
    "training": TRAINING_FEATURES,
    "tool": TOOL_FEATURES,
}

CI_ENGINEERING_FEATURES: dict[str, dict[str, Any]] = {
    "rocm_ci_pipeline": {
        "dimension": "engineering_maturity",
        "description": "Dedicated ROCm/AMD CI test pipeline",
        "check_keywords": ["rocm", "hip", "amd", "mi300", "mi250"],
    },
    "rocm_docker": {
        "dimension": "engineering_maturity",
        "description": "ROCm Docker images or Dockerfiles",
        "check_keywords": ["rocm", "docker"],
    },
    "amd_install_docs": {
        "dimension": "engineering_maturity",
        "description": "AMD/ROCm installation documentation",
        "check_keywords": ["rocm", "install", "amd", "hip"],
    },
}


def get_taxonomy_for_project(category: str) -> dict[str, dict[str, Any]]:
    base = dict(CATEGORY_TAXONOMY.get(category, INFERENCE_FEATURES))
    base.update(CI_ENGINEERING_FEATURES)
    return base


def merge_dynamic_features(
    base: dict[str, dict[str, Any]],
    discovered: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    merged = dict(base)
    for feat in discovered:
        fid = feat.get("id", "").strip().lower().replace(" ", "_")
        if not fid or fid in merged:
            continue
        merged[fid] = {
            "dimension": feat.get("dimension", "feature_availability"),
            "description": feat.get("description", fid),
            "check_keywords": feat.get("check_keywords", []),
        }
    return merged


def taxonomy_to_prompt_text(taxonomy: dict[str, dict[str, Any]]) -> str:
    lines = []
    for fid, spec in taxonomy.items():
        dim = spec.get("dimension", "")
        desc = spec.get("description", "")
        lines.append(f"- {fid} [{dim}]: {desc}")
    return "\n".join(lines)
