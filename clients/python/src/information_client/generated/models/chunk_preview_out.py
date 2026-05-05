from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="ChunkPreviewOut")


@_attrs_define
class ChunkPreviewOut:
    """One chunk in the preview response.

    Attributes:
        char_count (int): Character count of ``text``.
        chunk_type (str): Algorithm-specific type tag (e.g. 'page', 'section').
        index (int): Position of the chunk in extraction order.
        label (str): Operator-readable chunk identifier.
        text (str): Extracted chunk text.
    """

    char_count: int
    chunk_type: str
    index: int
    label: str
    text: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        char_count = self.char_count

        chunk_type = self.chunk_type

        index = self.index

        label = self.label

        text = self.text

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "char_count": char_count,
                "chunk_type": chunk_type,
                "index": index,
                "label": label,
                "text": text,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        char_count = d.pop("char_count")

        chunk_type = d.pop("chunk_type")

        index = d.pop("index")

        label = d.pop("label")

        text = d.pop("text")

        chunk_preview_out = cls(
            char_count=char_count,
            chunk_type=chunk_type,
            index=index,
            label=label,
            text=text,
        )

        chunk_preview_out.additional_properties = d
        return chunk_preview_out

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
