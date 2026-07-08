"""Beak browser rendering service."""

from .client import BeakClient, BeakClientError

__all__ = ["BeakClient", "BeakClientError", "__version__"]

__version__ = "0.0.3"
