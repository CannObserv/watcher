from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ProposeSelectorsRequest")


@_attrs_define
class ProposeSelectorsRequest:
    """Request body for POST /api/v1/tools/propose-selectors.

    Attributes:
        description (str): Plain-language description of the content the operator wants to extract. Matched against
            element text via case-insensitive substring search.
        url (str): Target URL to fetch and search.
        top_k (int | Unset): Maximum candidates to return; ranked by stability score (highest first). Default: 5.
    """

    description: str
    url: str
    top_k: int | Unset = 5
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        description = self.description

        url = self.url

        top_k = self.top_k

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "description": description,
                "url": url,
            }
        )
        if top_k is not UNSET:
            field_dict["top_k"] = top_k

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        description = d.pop("description")

        url = d.pop("url")

        top_k = d.pop("top_k", UNSET)

        propose_selectors_request = cls(
            description=description,
            url=url,
            top_k=top_k,
        )

        propose_selectors_request.additional_properties = d
        return propose_selectors_request

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
