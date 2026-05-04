from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from dateutil.parser import isoparse

T = TypeVar("T", bound="InfoItemOut")


@_attrs_define
class InfoItemOut:
    """
    Attributes:
        created_at (datetime.datetime):
        description (None | str):
        info_item_id (str):
        name (str):
        owner (None | str):
        updated_at (datetime.datetime):
    """

    created_at: datetime.datetime
    description: None | str
    info_item_id: str
    name: str
    owner: None | str
    updated_at: datetime.datetime
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        created_at = self.created_at.isoformat()

        description: None | str
        description = self.description

        info_item_id = self.info_item_id

        name = self.name

        owner: None | str
        owner = self.owner

        updated_at = self.updated_at.isoformat()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "created_at": created_at,
                "description": description,
                "info_item_id": info_item_id,
                "name": name,
                "owner": owner,
                "updated_at": updated_at,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        created_at = isoparse(d.pop("created_at"))

        def _parse_description(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        description = _parse_description(d.pop("description"))

        info_item_id = d.pop("info_item_id")

        name = d.pop("name")

        def _parse_owner(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        owner = _parse_owner(d.pop("owner"))

        updated_at = isoparse(d.pop("updated_at"))

        info_item_out = cls(
            created_at=created_at,
            description=description,
            info_item_id=info_item_id,
            name=name,
            owner=owner,
            updated_at=updated_at,
        )

        info_item_out.additional_properties = d
        return info_item_out

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
