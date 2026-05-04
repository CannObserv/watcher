from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from dateutil.parser import isoparse

if TYPE_CHECKING:
    from ..models.info_spec_out_document import InfoSpecOutDocument


T = TypeVar("T", bound="InfoSpecOut")


@_attrs_define
class InfoSpecOut:
    """
    Attributes:
        active (bool):
        created_at (datetime.datetime):
        document (InfoSpecOutDocument):
        info_item_id (str):
        info_spec_id (str):
        priority (int):
        schema_version (int):
    """

    active: bool
    created_at: datetime.datetime
    document: InfoSpecOutDocument
    info_item_id: str
    info_spec_id: str
    priority: int
    schema_version: int
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        active = self.active

        created_at = self.created_at.isoformat()

        document = self.document.to_dict()

        info_item_id = self.info_item_id

        info_spec_id = self.info_spec_id

        priority = self.priority

        schema_version = self.schema_version

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "active": active,
                "created_at": created_at,
                "document": document,
                "info_item_id": info_item_id,
                "info_spec_id": info_spec_id,
                "priority": priority,
                "schema_version": schema_version,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.info_spec_out_document import InfoSpecOutDocument

        d = dict(src_dict)
        active = d.pop("active")

        created_at = isoparse(d.pop("created_at"))

        document = InfoSpecOutDocument.from_dict(d.pop("document"))

        info_item_id = d.pop("info_item_id")

        info_spec_id = d.pop("info_spec_id")

        priority = d.pop("priority")

        schema_version = d.pop("schema_version")

        info_spec_out = cls(
            active=active,
            created_at=created_at,
            document=document,
            info_item_id=info_item_id,
            info_spec_id=info_spec_id,
            priority=priority,
            schema_version=schema_version,
        )

        info_spec_out.additional_properties = d
        return info_spec_out

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> Any:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
