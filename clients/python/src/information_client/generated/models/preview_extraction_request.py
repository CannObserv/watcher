from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.preview_extraction_request_document import PreviewExtractionRequestDocument


T = TypeVar("T", bound="PreviewExtractionRequest")


@_attrs_define
class PreviewExtractionRequest:
    """Request body for POST /api/v1/tools/preview-extraction.

    Attributes:
        document (PreviewExtractionRequestDocument): Candidate InfoSpec document. Validated against the v1 schema before
            any fetch is attempted; a validation failure returns 422 with the per-field issue list and no fetch is
            performed.
        url (str): Target URL to fetch and extract from.
    """

    document: PreviewExtractionRequestDocument
    url: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        document = self.document.to_dict()

        url = self.url

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "document": document,
                "url": url,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.preview_extraction_request_document import PreviewExtractionRequestDocument

        d = dict(src_dict)
        document = PreviewExtractionRequestDocument.from_dict(d.pop("document"))

        url = d.pop("url")

        preview_extraction_request = cls(
            document=document,
            url=url,
        )

        preview_extraction_request.additional_properties = d
        return preview_extraction_request

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
