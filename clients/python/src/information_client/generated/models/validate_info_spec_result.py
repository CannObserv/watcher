from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.validation_issue_out import ValidationIssueOut


T = TypeVar("T", bound="ValidateInfoSpecResult")


@_attrs_define
class ValidateInfoSpecResult:
    """Response body for POST /api/v1/tools/validate-info-spec.

    Attributes:
        valid (bool): True iff the document passed schema validation.
        errors (list[ValidationIssueOut] | Unset): Per-field validation issues; empty when ``valid`` is True.
    """

    valid: bool
    errors: list[ValidationIssueOut] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        valid = self.valid

        errors: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.errors, Unset):
            errors = []
            for errors_item_data in self.errors:
                errors_item = errors_item_data.to_dict()
                errors.append(errors_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "valid": valid,
            }
        )
        if errors is not UNSET:
            field_dict["errors"] = errors

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.validation_issue_out import ValidationIssueOut

        d = dict(src_dict)
        valid = d.pop("valid")

        _errors = d.pop("errors", UNSET)
        errors: list[ValidationIssueOut] | Unset = UNSET
        if _errors is not UNSET:
            errors = []
            for errors_item_data in _errors:
                errors_item = ValidationIssueOut.from_dict(errors_item_data)

                errors.append(errors_item)

        validate_info_spec_result = cls(
            valid=valid,
            errors=errors,
        )

        validate_info_spec_result.additional_properties = d
        return validate_info_spec_result

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
