"""LLM configuration and endpoint resolution for cloud and self-hosted models (vLLM, llama.cpp, Ollama)."""

import os
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field


class LLMConfig(BaseModel):
    """Configuration for LLM inference endpoint and parameters."""

    model: str = Field(
        default="gpt-4o",
        description="Model identifier (e.g. 'gpt-4o', 'openai/meta-llama/Meta-Llama-3-8B-Instruct', 'hosted_vllm/qwen2.5-coder').",
    )
    api_base: Optional[str] = Field(
        default=None,
        description="Base URL for OpenAI-compatible server (e.g. 'http://vllm:8000/v1' or 'http://localhost:8080/v1').",
    )
    api_key: Optional[str] = Field(
        default=None,
        description="API key or token (e.g. 'EMPTY' for local vLLM/llama.cpp).",
    )
    temperature: float = Field(
        default=0.2,
        description="Sampling temperature.",
    )
    max_tokens: Optional[int] = Field(
        default=4096,
        description="Maximum tokens per generation.",
    )


def resolve_llm_config(repo_llm_config: Optional[LLMConfig] = None) -> LLMConfig:
    """Resolves LLM configuration with environment variable fallbacks."""
    env_model = os.environ.get("LLM_MODEL") or os.environ.get("OPENAI_MODEL")
    env_api_base = os.environ.get("LLM_API_BASE") or os.environ.get("OPENAI_API_BASE") or os.environ.get("OPENAI_BASE_URL")
    env_api_key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")

    if repo_llm_config:
        model = repo_llm_config.model if repo_llm_config.model != "gpt-4o" or not env_model else env_model
        api_base = repo_llm_config.api_base or env_api_base
        api_key = repo_llm_config.api_key or env_api_key or "EMPTY"
        return LLMConfig(
            model=model,
            api_base=api_base,
            api_key=api_key,
            temperature=repo_llm_config.temperature,
            max_tokens=repo_llm_config.max_tokens,
        )

    return LLMConfig(
        model=env_model or "gpt-4o",
        api_base=env_api_base,
        api_key=env_api_key or "EMPTY",
    )
