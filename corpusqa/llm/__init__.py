"""LLM access layer. The only package that invokes LiteLLM."""

from corpusqa.llm.tasks import LLMTaskClient

__all__ = ["LLMTaskClient"]
