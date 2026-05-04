from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.http_validation_error import HTTPValidationError
from ...models.info_spec_out import InfoSpecOut
from ...types import Response


def _get_kwargs(
    info_item_id: str,
) -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/info-items/{info_item_id}/info-specs".format(
            info_item_id=quote(str(info_item_id), safe=""),
        ),
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> HTTPValidationError | list[InfoSpecOut] | None:
    if response.status_code == 200:
        response_200 = []
        _response_200 = response.json()
        for response_200_item_data in _response_200:
            response_200_item = InfoSpecOut.from_dict(response_200_item_data)

            response_200.append(response_200_item)

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
) -> Response[HTTPValidationError | list[InfoSpecOut]]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    info_item_id: str,
    *,
    client: AuthenticatedClient,
) -> Response[HTTPValidationError | list[InfoSpecOut]]:
    """List Info Specs

    Args:
        info_item_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[HTTPValidationError | list[InfoSpecOut]]
    """

    kwargs = _get_kwargs(
        info_item_id=info_item_id,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    info_item_id: str,
    *,
    client: AuthenticatedClient,
) -> HTTPValidationError | list[InfoSpecOut] | None:
    """List Info Specs

    Args:
        info_item_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        HTTPValidationError | list[InfoSpecOut]
    """

    return sync_detailed(
        info_item_id=info_item_id,
        client=client,
    ).parsed


async def asyncio_detailed(
    info_item_id: str,
    *,
    client: AuthenticatedClient,
) -> Response[HTTPValidationError | list[InfoSpecOut]]:
    """List Info Specs

    Args:
        info_item_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[HTTPValidationError | list[InfoSpecOut]]
    """

    kwargs = _get_kwargs(
        info_item_id=info_item_id,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    info_item_id: str,
    *,
    client: AuthenticatedClient,
) -> HTTPValidationError | list[InfoSpecOut] | None:
    """List Info Specs

    Args:
        info_item_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        HTTPValidationError | list[InfoSpecOut]
    """

    return (
        await asyncio_detailed(
            info_item_id=info_item_id,
            client=client,
        )
    ).parsed
