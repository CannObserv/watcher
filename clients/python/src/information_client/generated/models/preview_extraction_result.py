from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.chunk_preview_out import ChunkPreviewOut


T = TypeVar("T", bound="PreviewExtractionResult")


@_attrs_define
class PreviewExtractionResult:
    """Response body for POST /api/v1/tools/preview-extraction.

    Attributes:
        chunks (list[ChunkPreviewOut]): Extracted chunks in order; empty when extraction yields nothing.
        computed_fingerprint (str): Fingerprint of the joined extracted text under the spec's algorithm. sha256 →
            64-char hex; simhash → decimal int as a string.
        fingerprint_algorithm (str): Algorithm used for ``computed_fingerprint`` (mirrors the spec).
        total_chars (int): Sum of ``char_count`` across all chunks.
    """

    chunks: list[ChunkPreviewOut]
    computed_fingerprint: str
    fingerprint_algorithm: str
    total_chars: int
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        chunks = []
        for chunks_item_data in self.chunks:
            chunks_item = chunks_item_data.to_dict()
            chunks.append(chunks_item)

        computed_fingerprint = self.computed_fingerprint

        fingerprint_algorithm = self.fingerprint_algorithm

        total_chars = self.total_chars

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "chunks": chunks,
                "computed_fingerprint": computed_fingerprint,
                "fingerprint_algorithm": fingerprint_algorithm,
                "total_chars": total_chars,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.chunk_preview_out import ChunkPreviewOut

        d = dict(src_dict)
        chunks = []
        _chunks = d.pop("chunks")
        for chunks_item_data in _chunks:
            chunks_item = ChunkPreviewOut.from_dict(chunks_item_data)

            chunks.append(chunks_item)

        computed_fingerprint = d.pop("computed_fingerprint")

        fingerprint_algorithm = d.pop("fingerprint_algorithm")

        total_chars = d.pop("total_chars")

        preview_extraction_result = cls(
            chunks=chunks,
            computed_fingerprint=computed_fingerprint,
            fingerprint_algorithm=fingerprint_algorithm,
            total_chars=total_chars,
        )

        preview_extraction_result.additional_properties = d
        return preview_extraction_result

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
