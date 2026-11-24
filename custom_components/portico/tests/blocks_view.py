"""Test helpers: read a page of a reply through its `blocks` (the wire carries nothing else since B8)."""
from __future__ import annotations

from typing import Any


def served(pages: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """The "home" page (else the first) of a reply's `pages` as `{cameras, actions}`: its blocks' items, in order."""
    page = next((p for p in pages if p["id"] == "home"), pages[0] if pages else None)
    if page is None:
        return {"cameras": [], "actions": []}
    return {"cameras": [c for b in page["blocks"] for c in b["cameras"]],
            "actions": [a for b in page["blocks"] for a in b["actions"]]}


def resolved(store, user_id: str) -> dict[str, Any]:
    """`{rev, cameras, actions}` of a user's endpoint as `content/get` serves it, items read from the blocks."""
    full = store.resolve_for_endpoint(store._endpoint_id(user_id))
    return {**served(full["pages"]), "rev": full["rev"]}
