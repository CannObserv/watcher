from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.info_item_create_initial_info_spec_type_0 import (
        InfoItemCreateInitialInfoSpecType0,
    )


T = TypeVar("T", bound="InfoItemCreate")


@_attrs_define
class InfoItemCreate:
    """
    Attributes:
        name (str):
        description (None | str | Unset):
        initial_info_spec (InfoItemCreateInitialInfoSpecType0 | None | Unset): Optional InfoSpec document to atomically
            create alongside the new InfoItem at priority=1, active=True. Validated against the v1 schema before either row
            is written; on validation failure neither InfoItem nor InfoSpec is persisted.
        owner (None | str | Unset):
    """

    name: str
    description: None | str | Unset = UNSET
    initial_info_spec: InfoItemCreateInitialInfoSpecType0 | None | Unset = UNSET
    owner: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.info_item_create_initial_info_spec_type_0 import (
            InfoItemCreateInitialInfoSpecType0,
        )

        name = self.name

        description: None | str | Unset
        if isinstance(self.description, Unset):
            description = UNSET
        else:
            description = self.description

        initial_info_spec: dict[str, Any] | None | Unset
        if isinstance(self.initial_info_spec, Unset):
            initial_info_spec = UNSET
        elif isinstance(self.initial_info_spec, InfoItemCreateInitialInfoSpecType0):
            initial_info_spec = self.initial_info_spec.to_dict()
        else:
            initial_info_spec = self.initial_info_spec

        owner: None | str | Unset
        if isinstance(self.owner, Unset):
            owner = UNSET
        else:
            owner = self.owner

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "name": name,
            }
        )
        if description is not UNSET:
            field_dict["description"] = description
        if initial_info_spec is not UNSET:
            field_dict["initial_info_spec"] = initial_info_spec
        if owner is not UNSET:
            field_dict["owner"] = owner

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.info_item_create_initial_info_spec_type_0 import (
            InfoItemCreateInitialInfoSpecType0,
        )

        d = dict(src_dict)
        name = d.pop("name")

        def _parse_description(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        description = _parse_description(d.pop("description", UNSET))

        def _parse_initial_info_spec(
            data: object,
        ) -> InfoItemCreateInitialInfoSpecType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                initial_info_spec_type_0 = InfoItemCreateInitialInfoSpecType0.from_dict(data)

                return initial_info_spec_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(InfoItemCreateInitialInfoSpecType0 | None | Unset, data)

        initial_info_spec = _parse_initial_info_spec(d.pop("initial_info_spec", UNSET))

        def _parse_owner(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        owner = _parse_owner(d.pop("owner", UNSET))

        info_item_create = cls(
            name=name,
            description=description,
            initial_info_spec=initial_info_spec,
            owner=owner,
        )

        info_item_create.additional_properties = d
        return info_item_create

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
