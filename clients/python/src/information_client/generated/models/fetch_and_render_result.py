from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.fetch_and_render_result_headers import FetchAndRenderResultHeaders


T = TypeVar("T", bound="FetchAndRenderResult")


@_attrs_define
class FetchAndRenderResult:
    """Response body for POST /api/v1/tools/fetch-and-render.

    Attributes:
        body (str): Decoded response body, truncated at 5 MiB. ``truncated`` is True when the original payload exceeded
            the cap.
        body_bytes_total (int): Original byte count before any truncation; useful for size sanity checks.
        headers (FetchAndRenderResultHeaders): Response headers from the target.
        status_code (int): HTTP status code from the target.
        truncated (bool): True when ``body`` was truncated to the 5 MiB cap.
        url (str): Echo of the requested URL.
        screenshot_url (None | str | Unset): Reserved for the Playwright fetcher path; always None in v1 since
            screenshot capture isn't wired.
    """

    body: str
    body_bytes_total: int
    headers: FetchAndRenderResultHeaders
    status_code: int
    truncated: bool
    url: str
    screenshot_url: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        body = self.body

        body_bytes_total = self.body_bytes_total

        headers = self.headers.to_dict()

        status_code = self.status_code

        truncated = self.truncated

        url = self.url

        screenshot_url: None | str | Unset
        if isinstance(self.screenshot_url, Unset):
            screenshot_url = UNSET
        else:
            screenshot_url = self.screenshot_url

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "body": body,
                "body_bytes_total": body_bytes_total,
                "headers": headers,
                "status_code": status_code,
                "truncated": truncated,
                "url": url,
            }
        )
        if screenshot_url is not UNSET:
            field_dict["screenshot_url"] = screenshot_url

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fetch_and_render_result_headers import FetchAndRenderResultHeaders

        d = dict(src_dict)
        body = d.pop("body")

        body_bytes_total = d.pop("body_bytes_total")

        headers = FetchAndRenderResultHeaders.from_dict(d.pop("headers"))

        status_code = d.pop("status_code")

        truncated = d.pop("truncated")

        url = d.pop("url")

        def _parse_screenshot_url(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        screenshot_url = _parse_screenshot_url(d.pop("screenshot_url", UNSET))

        fetch_and_render_result = cls(
            body=body,
            body_bytes_total=body_bytes_total,
            headers=headers,
            status_code=status_code,
            truncated=truncated,
            url=url,
            screenshot_url=screenshot_url,
        )

        fetch_and_render_result.additional_properties = d
        return fetch_and_render_result

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
