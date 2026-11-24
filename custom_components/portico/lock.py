"""HA imports platforms as portico.lock; the door entities live in door/entities/ (a door entry only; nothing else forwards this platform)."""

from .door.entities.lock import async_setup_entry

__all__ = ["async_setup_entry"]
