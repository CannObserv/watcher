from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.http_validation_error import HTTPValidationError
from ...models.preview_extraction_request import PreviewExtractionRequest
from ...models.preview_extraction_result import PreviewExtractionResult
from ...types import Response


def _get_kwargs(
    *,
    body: PreviewExtractionRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/tools/preview-extraction",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> HTTPValidationError | PreviewExtractionResult | None:
    if response.status_code == 200:
        response_200 = PreviewExtractionResult.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = HTTPValidationError.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[HTTPValidationError | PreviewExtractionResult]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    body: PreviewExtractionRequest,
) -> Response[HTTPValidationError | PreviewExtractionResult]:
    r"""Preview Extraction Route

     Validate, fetch, extract, and fingerprint with a candidate InfoSpec.

    Composes ``validate_info_spec`` + ``fetch_and_render`` + the HTML extractor
    + the spec's fingerprint algorithm so an authoring agent can verify the
    spec yields the expected content before persisting via ``create_info_spec``
    or ``create_info_item(initial_info_spec=…)``.

    Returns 422 with structured errors on schema validation failure
    (``error: \"validation_failed\"``) or target unreachability
    (``error: \"target_unreachable\"``).

    Args:
        body (PreviewExtractionRequest): Request body for POST /api/v1/tools/preview-extraction.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[HTTPValidationError | PreviewExtractionResult]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient,
    body: PreviewExtractionRequest,
) -> HTTPValidationError | PreviewExtractionResult | None:
    r"""Preview Extraction Route

     Validate, fetch, extract, and fingerprint with a candidate InfoSpec.

    Composes ``validate_info_spec`` + ``fetch_and_render`` + the HTML extractor
    + the spec's fingerprint algorithm so an authoring agent can verify the
    spec yields the expected content before persisting via ``create_info_spec``
    or ``create_info_item(initial_info_spec=…)``.

    Returns 422 with structured errors on schema validation failure
    (``error: \"validation_failed\"``) or target unreachability
    (``error: \"target_unreachable\"``).

    Args:
        body (PreviewExtractionRequest): Request body for POST /api/v1/tools/preview-extraction.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        HTTPValidationError | PreviewExtractionResult
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    body: PreviewExtractionRequest,
) -> Response[HTTPValidationError | PreviewExtractionResult]:
    r"""Preview Extraction Route

     Validate, fetch, extract, and fingerprint with a candidate InfoSpec.

    Composes ``validate_info_spec`` + ``fetch_and_render`` + the HTML extractor
    + the spec's fingerprint algorithm so an authoring agent can verify the
    spec yields the expected content before persisting via ``create_info_spec``
    or ``create_info_item(initial_info_spec=…)``.

    Returns 422 with structured errors on schema validation failure
    (``error: \"validation_failed\"``) or target unreachability
    (``error: \"target_unreachable\"``).

    Args:
        body (PreviewExtractionRequest): Request body for POST /api/v1/tools/preview-extraction.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[HTTPValidationError | PreviewExtractionResult]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient,
    body: PreviewExtractionRequest,
) -> HTTPValidationError | PreviewExtractionResult | None:
    r"""Preview Extraction Route

     Validate, fetch, extract, and fingerprint with a candidate InfoSpec.

    Composes ``validate_info_spec`` + ``fetch_and_render`` + the HTML extractor
    + the spec's fingerprint algorithm so an authoring agent can verify the
    spec yields the expected content before persisting via ``create_info_spec``
    or ``create_info_item(initial_info_spec=…)``.

    Returns 422 with structured errors on schema validation failure
    (``error: \"validation_failed\"``) or target unreachability
    (``error: \"target_unreachable\"``).

    Args:
        body (PreviewExtractionRequest): Request body for POST /api/v1/tools/preview-extraction.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        HTTPValidationError | PreviewExtractionResult
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
