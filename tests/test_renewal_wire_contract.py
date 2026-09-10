"""The renewal guard's field list, pinned against the wire schema it stands in for.

``_renew_blob_reference`` refuses to overwrite a queued outbox row with
provenance missing a field ``SourceRevisionObservedEmit`` requires (#293, CR 1)
— that is the one writer that can *degrade* a row rather than create one, so a
gap there loses a real revision to a refresh.

The guard names those fields directly, because the wire coupling belongs in the
drain (#253) rather than in the pipeline. This test is what keeps the copy
honest: it derives the answer from co-core's own model, so promoting a field to
required there fails here instead of silently reopening the vector (CR 9).
"""

from co_core.pure.models.changes import SourceRevisionObservedEmit

from src.workers.pipeline import (
    WIRE_REQUIRED_PROVENANCE_FIELDS,
    BlobProvenance,
    ExtractionOutcome,
    _provenance_columns,
)


def _provenance_keys() -> set[str]:
    """The wire field names the renewal supplies, from the mapping itself."""
    return set(
        _provenance_columns(
            BlobProvenance(command_id="c", blob_uri="gs://b/k", source_media_type="text/html"),
            ExtractionOutcome(
                content_fingerprint="sha256:x", content_size_bytes=1, schema_version=1
            ),
        )
    )


def test_the_guard_covers_every_required_field_the_renewal_supplies():
    """Exactly the intersection: required on the wire AND written by a renewal.

    Under-covering reopens the degradation vector. Over-covering would refuse a
    renewal over a field Archiver records as absent, which is a different bug in
    the same guard.
    """
    required = {
        name for name, f in SourceRevisionObservedEmit.model_fields.items() if f.is_required()
    }
    assert set(WIRE_REQUIRED_PROVENANCE_FIELDS) == _provenance_keys() & required


def test_the_provenance_mapping_speaks_the_wire_s_field_names():
    """The guard can compare the two only because the names coincide.

    ``_provenance_columns`` keys are outbox column names that happen to be the
    emit model's field names one-for-one. If a column is ever renamed away from
    its wire field, the test above starts comparing unrelated strings and
    silently passes — so assert the property it rests on.
    """
    assert _provenance_keys() <= set(SourceRevisionObservedEmit.model_fields)
