"""Anthropic Provider: the only package allowed to import the `anthropic` SDK."""

from football_data_ai_platform.agent.providers.anthropic.client import ClaudeClient

__all__ = ["ClaudeClient"]
