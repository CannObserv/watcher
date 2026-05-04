from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.http_validation_error import HTTPValidationError
from ...models.info_spec_out import InfoSpecOut
from ...models.info_spec_patch import InfoSpecPatch
from ...types import Response


def _get_kwargs(
    info_item_id: str,
    info_spec_id: str,
    *,
    body: InfoSpecPatch,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "patch",
        "url": "/api/v1/info-items/{info_item_id}/info-specs/{info_spec_id}".format(
            info_item_id=quote(str(info_item_id), safe=""),
            info_spec_id=quote(str(info_spec_id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> HTTPValidationError | InfoSpecOut | None:
    if response.status_code == 200:
        response_200 = InfoSpecOut.from_dict(response.json())

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
) -> Response[HTTPValidationError | InfoSpecOut]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    info_item_id: str,
    info_spec_id: str,
    *,
    client: AuthenticatedClient,
    body: InfoSpecPatch,
) -> Response[HTTPValidationError | InfoSpecOut]:
    """Patch Info Spec

     Mutate placement metadata (priority, active). Document body is immutable.

    Args:
        info_item_id (str):
        info_spec_id (str):
        body (InfoSpecPatch):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[HTTPValidationError | InfoSpecOut]
    """

    kwargs = _get_kwargs(
        info_item_id=info_item_id,
        info_spec_id=info_spec_id,
        body=body,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    info_item_id: str,
    info_spec_id: str,
    *,
    client: AuthenticatedClient,
    body: InfoSpecPatch,
) -> HTTPValidationError | InfoSpecOut | None:
    """Patch Info Spec

     Mutate placement metadata (priority, active). Document body is immutable.

    Args:
        info_item_id (str):
        info_spec_id (str):
        body (InfoSpecPatch):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        HTTPValidationError | InfoSpecOut
    """

    return sync_detailed(
        info_item_id=info_item_id,
        info_spec_id=info_spec_id,
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    info_item_id: str,
    info_spec_id: str,
    *,
    client: AuthenticatedClient,
    body: InfoSpecPatch,
) -> Response[HTTPValidationError | InfoSpecOut]:
    """Patch Info Spec

     Mutate placement metadata (priority, active). Document body is immutable.

    Args:
        info_item_id (str):
        info_spec_id (str):
        body (InfoSpecPatch):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[HTTPValidationError | InfoSpecOut]
    """

    kwargs = _get_kwargs(
        info_item_id=info_item_id,
        info_spec_id=info_spec_id,
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    info_item_id: str,
    info_spec_id: str,
    *,
    client: AuthenticatedClient,
    body: InfoSpecPatch,
) -> HTTPValidationError | InfoSpecOut | None:
    """Patch Info Spec

     Mutate placement metadata (priority, active). Document body is immutable.

    Args:
        info_item_id (str):
        info_spec_id (str):
        body (InfoSpecPatch):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        HTTPValidationError | InfoSpecOut
    """

    return (
        await asyncio_detailed(
            info_item_id=info_item_id,
            info_spec_id=info_spec_id,
            client=client,
            body=body,
        )
    ).parsed
