"""Structured, Engine-grounded AI explanations for critical positions."""

from server.core.explanation.service import (
    ExplanationError,
    ExplanationNotFoundError,
    generate_explanations,
)

__all__ = ["ExplanationError", "ExplanationNotFoundError", "generate_explanations"]
