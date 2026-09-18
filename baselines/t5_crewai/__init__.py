"""Thin adapter entry point; inject a real executor in the integration phase."""

from common.adapters import get_adapter

__all__ = ["get_adapter"]

