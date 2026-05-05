from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.fetch_and_render_request import FetchAndRenderRequest
from ...models.fetch_and_render_result import FetchAndRenderResult
from ...models.http_validation_error import HTTPValidationError
from ...types import Response


def _get_kwargs(
    *,
    body: FetchAndRenderRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/tools/fetch-and-render",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> FetchAndRenderResult | HTTPValidationError | None:
    if response.status_code == 200:
        response_200 = FetchAndRenderResult.from_dict(response.json())

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
) -> Response[FetchAndRenderResult | HTTPValidationError]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    body: FetchAndRenderRequest,
) -> Response[FetchAndRenderResult | HTTPValidationError]:
    """Fetch And Render Route

     Fetch a target URL and return its body + headers for downstream tools.

    Use during InfoSpec authoring to inspect what the extractor will see (e.g.
    pipe the body into ``propose_selectors`` or ``preview_extraction``). Body
    payloads larger than 5 MiB are truncated; ``truncated`` flags the case.
    ``render=True`` returns 501 until the Playwright fetcher (#3) lands.

    Args:
        body (FetchAndRenderRequest): Request body for POST /api/v1/tools/fetch-and-render.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[FetchAndRenderResult | HTTPValidationError]
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
    body: FetchAndRenderRequest,
) -> FetchAndRenderResult | HTTPValidationError | None:
    """Fetch And Render Route

     Fetch a target URL and return its body + headers for downstream tools.

    Use during InfoSpec authoring to inspect what the extractor will see (e.g.
    pipe the body into ``propose_selectors`` or ``preview_extraction``). Body
    payloads larger than 5 MiB are truncated; ``truncated`` flags the case.
    ``render=True`` returns 501 until the Playwright fetcher (#3) lands.

    Args:
        body (FetchAndRenderRequest): Request body for POST /api/v1/tools/fetch-and-render.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        FetchAndRenderResult | HTTPValidationError
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    body: FetchAndRenderRequest,
) -> Response[FetchAndRenderResult | HTTPValidationError]:
    """Fetch And Render Route

     Fetch a target URL and return its body + headers for downstream tools.

    Use during InfoSpec authoring to inspect what the extractor will see (e.g.
    pipe the body into ``propose_selectors`` or ``preview_extraction``). Body
    payloads larger than 5 MiB are truncated; ``truncated`` flags the case.
    ``render=True`` returns 501 until the Playwright fetcher (#3) lands.

    Args:
        body (FetchAndRenderRequest): Request body for POST /api/v1/tools/fetch-and-render.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[FetchAndRenderResult | HTTPValidationError]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient,
    body: FetchAndRenderRequest,
) -> FetchAndRenderResult | HTTPValidationError | None:
    """Fetch And Render Route

     Fetch a target URL and return its body + headers for downstream tools.

    Use during InfoSpec authoring to inspect what the extractor will see (e.g.
    pipe the body into ``propose_selectors`` or ``preview_extraction``). Body
    payloads larger than 5 MiB are truncated; ``truncated`` flags the case.
    ``render=True`` returns 501 until the Playwright fetcher (#3) lands.

    Args:
        body (FetchAndRenderRequest): Request body for POST /api/v1/tools/fetch-and-render.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        FetchAndRenderResult | HTTPValidationError
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
