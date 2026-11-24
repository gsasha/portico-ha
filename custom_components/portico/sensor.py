"""HA imports platforms as portico.sensor; the implementation lives in entities/ (design doc §9.2)."""

from .entities.sensor import async_setup_entry

__all__ = ["async_setup_entry"]
