from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="SelectorCandidateOut")


@_attrs_define
class SelectorCandidateOut:
    """One ranked selector candidate.

    Attributes:
        sample_text (str): Visible text from the matched element (truncated to 200 chars).
        selector (str): CSS selector for the proposed element.
        stability_score (float): Heuristic score in [0, 1]: higher == more stable. Combines id/class structure, text-
            length proximity to the description, and a volatility penalty for hash-looking class names.
    """

    sample_text: str
    selector: str
    stability_score: float
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        sample_text = self.sample_text

        selector = self.selector

        stability_score = self.stability_score

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "sample_text": sample_text,
                "selector": selector,
                "stability_score": stability_score,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        sample_text = d.pop("sample_text")

        selector = d.pop("selector")

        stability_score = d.pop("stability_score")

        selector_candidate_out = cls(
            sample_text=sample_text,
            selector=selector,
            stability_score=stability_score,
        )

        selector_candidate_out.additional_properties = d
        return selector_candidate_out

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
