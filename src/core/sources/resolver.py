"""Resolve a Watch's info_source_id to root + fragment SourceSpec docs."""

from dataclasses import dataclass, field
from typing import Any

from archiver_client import ArchiverClient


@dataclass(frozen=True)
class ResolvedFragmentSource:
    info_source_id: str
    parent_info_source_id: str
    source_spec: dict[str, Any]


@dataclass(frozen=True)
class ResolvedRootSource:
    info_source_id: str
    url: str
    source_spec: dict[str, Any]
    children: list[ResolvedFragmentSource] = field(default_factory=list)


def _spec_to_dict(spec: Any) -> dict[str, Any]:
    if hasattr(spec, "to_dict"):
        return dict(spec.to_dict())
    if hasattr(spec, "additional_properties"):
        return dict(spec.additional_properties)
    return dict(spec)


async def resolve_root_sources_with_children(
    client: ArchiverClient,
    info_source_id: str,
) -> ResolvedRootSource:
    """Walk parent chain to root; list children of the root."""
    current = await client.get_info_source(info_source_id)
    while current.parent_info_source_id is not None:
        current = await client.get_info_source(str(current.parent_info_source_id))

    root = current
    root_spec = _spec_to_dict(root.source_spec)
    url = root_spec.get("target", {}).get("url")
    if not url:
        raise ValueError(f"root source {root.info_source_id} has no target.url")

    page = await client.list_info_sources(parent_info_source_id=str(root.info_source_id))
    children = [
        ResolvedFragmentSource(
            info_source_id=str(c.info_source_id),
            parent_info_source_id=str(c.parent_info_source_id),
            source_spec=_spec_to_dict(c.source_spec),
        )
        for c in page.items
    ]

    return ResolvedRootSource(
        info_source_id=str(root.info_source_id),
        url=url,
        source_spec=root_spec,
        children=children,
    )
