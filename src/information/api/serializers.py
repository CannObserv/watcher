"""ORM → Pydantic ``Out`` serialisers shared across route modules.

Lifted out of route files when more than one route module needs the same
mapping (e.g. ``tools.find-info-items`` reuses ``info-items``' serialiser).
"""

from src.information.api.schemas.info_item import InfoItemOut
from src.information.api.schemas.info_spec import InfoSpecOut
from src.information.core.models import InfoItem, InfoSpec


def info_item_to_out(item: InfoItem) -> InfoItemOut:
    return InfoItemOut(
        info_item_id=str(item.info_item_id),
        name=item.name,
        description=item.description,
        owner=item.owner,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def info_spec_to_out(spec: InfoSpec) -> InfoSpecOut:
    return InfoSpecOut(
        info_spec_id=str(spec.info_spec_id),
        info_item_id=str(spec.info_item_id),
        schema_version=spec.schema_version,
        document=spec.document,
        priority=spec.priority,
        active=spec.active,
        created_at=spec.created_at,
    )
