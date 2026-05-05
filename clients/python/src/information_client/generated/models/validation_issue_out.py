from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="ValidationIssueOut")


@_attrs_define
class ValidationIssueOut:
    """Single validation problem with a structured path + message.

    Attributes:
        message (str): Human-readable error message from the validator.
        path (list[int | str]): JSON path to the offending field, as a list of segments.
    """

    message: str
    path: list[int | str]
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        message = self.message

        path = []
        for path_item_data in self.path:
            path_item: int | str
            path_item = path_item_data
            path.append(path_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "message": message,
                "path": path,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        message = d.pop("message")

        path = []
        _path = d.pop("path")
        for path_item_data in _path:

            def _parse_path_item(data: object) -> int | str:
                return cast(int | str, data)

            path_item = _parse_path_item(path_item_data)

            path.append(path_item)

        validation_issue_out = cls(
            message=message,
            path=path,
        )

        validation_issue_out.additional_properties = d
        return validation_issue_out

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
