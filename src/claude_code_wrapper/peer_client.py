"""Peer wire models and the signing outbound HTTP client (design D1, D5, D6, D13, D14).

The peer token is a signing key only: it signs every request and never appears on the
wire, in a URL, or in an error message.
"""

from __future__ import annotations

import secrets
from typing import Annotated, Any, Literal

import httpx
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, model_validator

from . import peer_auth
from .config import _PEER_NAME, _check_http_url
from .peers import ADDRESS_RE
from .turns import TERMINAL_STATES

_SESSION_ID_RE = r"^ses_[0-9a-f]{12}$"
_MSG_ID_RE = r"^msg_[0-9a-f]{16}$"
MAX_ENTRIES = 1024

# Receiving-side error codes -> HTTP status for the endpoint.
RECEIVER_STATUS: dict[str, int] = {
    "not_found": 404,
    "inbox_full": 429,
    "rate_limited": 429,
    "left": 409,
}

_PREFIX = {
    "unknown_peer": "unknown peer",
    "unreachable": "peer unreachable",
    "too_large": "message too large",
    "inbox_full": "inbox full",
    "rate_limited": "rate limited",
    "unauthorized": "peer rejected the request",
    "invalid": "invalid arguments",
}

_RETRYABLE = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
    httpx.ReadError,
    httpx.WriteError,
)


class PeerError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _address(value: str) -> str:
    m = ADDRESS_RE.match(value)
    if m is None or m.group(1).startswith("."):
        raise ValueError("must be <instance>/<session id>")
    return value


def _instance(value: str) -> str:
    if not _PEER_NAME.fullmatch(value) or value.startswith("."):
        raise ValueError("must match [A-Za-z0-9._-]{1,64} and not start with '.'")
    return value


def _url(value: str) -> str:
    if "{port}" in value:
        raise ValueError("must be an absolute http(s) URL")
    return _check_http_url(value)


Address = Annotated[str, AfterValidator(_address)]
SessionId = Annotated[str, Field(pattern=_SESSION_ID_RE)]
MsgId = Annotated[str, Field(pattern=_MSG_ID_RE)]
Instance = Annotated[str, AfterValidator(_instance)]
Url = Annotated[str, AfterValidator(_url)]


class _Wire(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=False)


class MessageBody(_Wire):
    from_: Address = Field(alias="from")
    to_session: SessionId
    message: str = Field(min_length=1)
    msg_id: MsgId
    notify_when_idle: bool = False


class NoticeBody(_Wire):
    from_: Address = Field(alias="from")
    to_session: SessionId
    msg_id: MsgId
    status: Literal["delivered", "rejected", "dropped"]
    state: str | None = None
    reason: str | None = Field(None, max_length=200)

    @model_validator(mode="after")
    def _state(self) -> NoticeBody:
        if self.status == "delivered":
            if self.state not in TERMINAL_STATES:
                raise ValueError(f"state must be one of {TERMINAL_STATES} when delivered")
        elif self.state is not None:
            raise ValueError("state must be null unless status is delivered")
        return self


class ViewSession(_Wire):
    session_id: SessionId
    name: str
    cwd: str


class ViewMember(_Wire):
    instance: Instance
    url: Url


class MemberView(_Wire):
    instance: Instance
    url: Url
    sessions: list[ViewSession] = Field(default_factory=list, max_length=MAX_ENTRIES)
    members: list[ViewMember] = Field(default_factory=list, max_length=MAX_ENTRIES)
    leaving: bool = False


def new_msg_id() -> str:
    return "msg_" + secrets.token_hex(8)


def _envelope_code(response: httpx.Response) -> str:
    try:
        data = response.json()
        code = data["error"]["code"]
    except (ValueError, KeyError, TypeError):
        return ""
    return code if isinstance(code, str) else ""


class PeerClient:
    def __init__(
        self,
        token: str,
        *,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = token
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport)

    def _err(self, code: str, detail: str = "") -> PeerError:
        text = _PREFIX[code] + (f": {detail}" if detail else "")
        return PeerError(code, text.replace(self._token, "***"))

    def _map_status(self, response: httpx.Response, url: str) -> PeerError:
        status = response.status_code
        if status == 404:
            return self._err("unknown_peer", url)
        if status == 413:
            return self._err("too_large")
        if status == 429:
            code = _envelope_code(response)
            return self._err("inbox_full" if code == "inbox_full" else "rate_limited")
        if status == 401:
            return self._err("unauthorized")
        if status == 422:
            return self._err("invalid")
        return self._err("unreachable", f"{url} answered {status}")

    async def _post(self, url: str, path: str, model: BaseModel, *, retry: bool) -> Any:
        target = url.rstrip("/") + path
        body = model.model_dump_json(by_alias=True).encode()
        attempts = 2 if retry else 1
        response: httpx.Response | None = None
        for attempt in range(attempts):
            headers = peer_auth.sign_request(self._token, "POST", path, body)
            headers["Content-Type"] = "application/json"
            try:
                response = await self._http.post(target, content=body, headers=headers)
                break
            except _RETRYABLE as e:
                if attempt + 1 < attempts:
                    continue
                reason = f"{type(e).__name__}: {e}".rstrip(": ")
                raise self._err("unreachable", f"{url} ({reason})") from None
            except httpx.HTTPError as e:
                raise self._err("unreachable", f"{url} ({type(e).__name__})") from None
        assert response is not None
        if response.status_code in (200, 202):
            try:
                return response.json()
            except ValueError:
                raise self._err("unreachable", f"{url} sent a non-JSON answer") from None
        raise self._map_status(response, url)

    async def send_message(self, url: str, body: MessageBody) -> dict:
        return await self._post(url, "/v1/peers/messages", body, retry=True)

    async def send_notice(self, url: str, body: NoticeBody) -> dict:
        return await self._post(url, "/v1/peers/notices", body, retry=True)

    async def heartbeat(self, url: str, view: MemberView) -> MemberView:
        data = await self._post(url, "/v1/peers/heartbeat", view, retry=False)
        try:
            return MemberView.model_validate(data)
        except ValidationError:
            raise self._err("unreachable", f"{url} sent an invalid heartbeat answer") from None

    async def aclose(self) -> None:
        await self._http.aclose()
