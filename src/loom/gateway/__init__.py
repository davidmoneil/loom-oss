"""Loom gateway — OpenAI/Anthropic-compatible proxy with routing, compression, and observability."""

from .app import create_app

__all__ = ["create_app"]
