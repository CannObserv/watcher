"""Resolve an Archiver InfoItem's bindings, partitioned by role."""

from dataclasses import dataclass

from archiver_client import ArchiverClient

from src.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class InfoItemBindings:
    """An InfoItem's active bindings, partitioned by role.

    `primary` is the unique root-shaped InfoSource bound with role IS NULL.
    `cross_checks` are fragment-shaped bindings with role='cross_check'.
    `sub_aspects` are fragment-shaped bindings with role='sub_aspect'.
    `primary_url` is the root URL the cycle fetches; all extractions run against
    its bytes.
    """

    primary: object  # InfoSourceOut for the primary (role IS NULL)
    cross_checks: list[object]
    sub_aspects: list[object]
    primary_url: str


async def fetch_info_item_bindings(
    info_client: ArchiverClient, info_item_id: str
) -> InfoItemBindings:
    """Fetch and partition an InfoItem's bindings.

    Issues `get_info_item` once for the binding list, then `get_info_source`
    per active binding to resolve full InfoSourceOut (URL + source_spec).
    Unknown roles are skipped with a warning — forward-compatible with future
    role values added by Archiver.

    Raises ``ValueError`` if no active primary binding exists or the primary
    has no URL.
    """
    info_item = await info_client.get_info_item(info_item_id)
    primary = None
    cross_checks: list[object] = []
    sub_aspects: list[object] = []
    for binding in info_item.info_item_sources:
        source = await info_client.get_info_source(str(binding.info_source_id))
        if binding.role is None:
            primary = source
        elif binding.role == "cross_check":
            cross_checks.append(source)
        elif binding.role == "sub_aspect":
            sub_aspects.append(source)
        else:
            logger.warning(
                "ignoring unknown binding role %r for InfoSource %s on InfoItem %s",
                binding.role,
                binding.info_source_id,
                info_item_id,
            )
    if primary is None:
        raise ValueError(f"InfoItem {info_item_id}: no active primary binding")

    # Per Archiver v3.0.0: InfoSourceOut.url is a first-class field, non-NULL
    # for root-shaped (primary) InfoSources, NULL for fragments. Read directly
    # rather than walking source_spec.additional_properties.
    primary_url = primary.url
    if not primary_url:
        raise ValueError(
            f"InfoItem {info_item_id}: primary InfoSource {primary.info_source_id} has no url "
            "(InfoItem's primary must be root-shaped per Archiver invariant)"
        )

    return InfoItemBindings(
        primary=primary,
        cross_checks=cross_checks,
        sub_aspects=sub_aspects,
        primary_url=primary_url,
    )
