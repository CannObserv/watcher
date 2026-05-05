from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="FetchAndRenderRequest")


@_attrs_define
class FetchAndRenderRequest:
    """Request body for POST /api/v1/tools/fetch-and-render.

    Attributes:
        url (str): Target URL to fetch (http/https only).
        render (bool | Unset): If True, render the page via Playwright before returning. v1 returns 501 — wired in once
            the Playwright fetcher (#3) lands. Default: False.
    """

    url: str
    render: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        url = self.url

        render = self.render

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "url": url,
            }
        )
        if render is not UNSET:
            field_dict["render"] = render

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        url = d.pop("url")

        render = d.pop("render", UNSET)

        fetch_and_render_request = cls(
            url=url,
            render=render,
        )

        fetch_and_render_request.additional_properties = d
        return fetch_and_render_request

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
