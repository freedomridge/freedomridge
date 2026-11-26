"""How a tool calls the API: in process, as the user (inner.py), with the API's answers turned into the tools' errors.

A refusal keeps the API's own words. A busy server (429, 503, 504) is `busy`, retryable; for a read that counts
through the samples, the server's time limit for a statement (504) is `too_slow`, not retryable. A server fault (500,
an answer that isn't JSON) raises ToolFailed: nothing a caller did causes it. A mistake in filter text keeps the
server's line, column and suggestions (`details`).
"""

from typing import Any, Iterable, Mapping, Optional

from . import guard, inner
from .identity import Identity

# get_current_user's code when the assistant rule refuses an inner request (backend/routers/auth.py).
NOT_ALLOWED_CODE = 'assistant_not_allowed'
# The guard's code when a request questioned a private field (guard.py): a tool may word that refusal itself.
PRIVATE_FIELD_CODE = guard.CODE
# Its code when the route itself is refused: not one the tool lists, or an administrators' route. Only a tool's own
# fault can cause it, so it is a server fault (ToolFailed), never an answer to the model.
ROUTE_REFUSED_CODE = 'assistant_route_refused'
BUSY_STATUSES = (429, 503, 504)
TRY_AGAIN = 'The server is busy right now; try again in a moment.'
# What a tool that counts says when a read ran into that limit; the working-view tools say more (working.py:
# _too_slow_counting, _too_slow_filter).
TOO_SLOW = (
    'Counting this took longer than the server allows for one statement, so there is no answer, not even a partial '
    'one. Narrow the view first.'
)


class ToolError(Exception):
    """An answer a caller can cause: call_tool returns it as {'ok': False, 'error': ...}."""

    def __init__(
        self,
        code: str,
        message: str,
        retryable: bool = False,
        details: Optional[dict[str, Any]] = None,
        reason: Optional[str] = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details
        self.reason = reason  # the API's own code for the refusal, when it gave one (never in the answer)

    def answer(self) -> dict[str, Any]:
        """The error as a tool answers it: its message cut to MESSAGE_LIMIT characters, and its suggestions to
        SUGGESTIONS of SUGGESTION_LIMIT characters each, so no error is large, whatever a route's words repeat (an
        unknown field echoes filter text)."""
        answer: dict[str, Any] = {
            'code': self.code,
            'message': cut(self.message, MESSAGE_LIMIT),
            'retryable': self.retryable,
        }
        if self.details is None:
            return answer
        details = dict(self.details)
        if isinstance(details.get('suggestions'), list):
            details['suggestions'] = [
                cut(item, SUGGESTION_LIMIT) if isinstance(item, str) else item
                for item in details['suggestions'][:SUGGESTIONS]
            ]
        return {**answer, 'details': details}


# The most an error's message holds, and its suggestions (count, and characters each).
MESSAGE_LIMIT = 2000
SUGGESTIONS = 20
SUGGESTION_LIMIT = 200


def cut(text: str, limit: int) -> str:
    """`text` within `limit` characters: past it, its start, an ellipsis and how many characters were left out."""
    if len(text) <= limit:
        return text
    kept = limit - 40
    return f'{text[:kept]}… ({len(text) - kept:,} more characters)'


class ToolFailed(Exception):
    """A server fault while a tool ran (an answer 500, an answer that isn't JSON): not the caller's doing."""


def detail_text(detail: Any) -> str:
    """The API's `detail` as a sentence: a string as it is; FastAPI's validation errors as "where: what"."""
    if isinstance(detail, str):
        return detail
    if isinstance(detail, dict) and isinstance(detail.get('message'), str):
        return detail['message']
    if isinstance(detail, list):
        parts = []
        for error in detail:
            if isinstance(error, dict):
                where = '.'.join(str(part) for part in error.get('loc', ()) if part not in ('body', 'query', 'path'))
                parts.append(f"{where}: {error.get('msg', 'invalid')}" if where else str(error.get('msg', 'invalid')))
            else:
                parts.append(str(error))
        return '; '.join(parts) or 'The request was invalid'
    return 'The request was refused' if detail is None else str(detail)


class Api:
    """The API as one tool may call it: only the routes it lists (inner.request refuses any other)."""

    def __init__(self, identity: Identity, routes: Iterable[tuple[str, str]]):
        self.identity = identity
        self.routes = tuple(routes)
        # Tokens in the filter values sent that didn't open (sealed for another field or dataset, or under an earlier
        # key): they matched nothing, which a tool says.
        self.unreadable = 0
        # Every read of the tool counts through the samples (the working-view tools): the time limit for a statement
        # (504) is too_slow on any of them, never busy.
        self.counting = False
        # What one tool call learns once and its requests share (working._images: whether a dataset has object rows).
        self.facts: dict[str, Any] = {}
        # The headers of the last answer (a search says in one that more may match).
        self.headers: Mapping[str, str] = {}

    async def call(
        self,
        method: str,
        template: str,
        *,
        missing: Optional[str] = None,
        path_params: Optional[Mapping[str, Any]] = None,
        query: Optional[Mapping[str, Any]] = None,
        body: Any = None,
        counting: bool = False,
    ) -> Any:
        """The JSON answer of a 2xx; otherwise ToolError (ToolFailed for a server fault). `missing`: the sentence a
        404 answers with, naming what wasn't found (the API's own words without it). `counting`: a read that counts
        through the samples, whose time limit is `too_slow`."""
        try:
            answer = await inner.request(
                self.identity,
                self.routes,
                method,
                template,
                path_params=path_params,
                query=query,
                body=body,
            )
        except inner.NotJson as error:
            raise ToolFailed(str(error)) from None
        self.unreadable += answer.unreadable
        self.headers = answer.headers
        if 200 <= answer.status < 300:
            return answer.body
        body_detail = answer.body.get('detail') if isinstance(answer.body, dict) else None
        text = detail_text(body_detail)
        code = answer.body.get('code') if isinstance(answer.body, dict) else None
        if answer.status == 403 and code == ROUTE_REFUSED_CODE:
            raise ToolFailed(f'{method} {template} was refused: {text}')
        if answer.status == 401 or (answer.status == 403 and code == NOT_ALLOWED_CODE):
            raise ToolError('not_allowed', text)
        if answer.status in (403, 409):
            reason = body_detail.get('code') if isinstance(body_detail, dict) else code
            raise ToolError('refused', text, reason=reason if isinstance(reason, str) else None)
        if answer.status == 404:
            raise ToolError('not_found', missing or text)
        if answer.status in (400, 413, 414, 422):
            errors = body_detail.get('errors') if isinstance(body_detail, dict) else None
            first = errors[0] if isinstance(errors, list) and errors and isinstance(errors[0], dict) else None
            details = None if first is None else {key: first.get(key) for key in ('line', 'column', 'suggestions')}
            raise ToolError('invalid', text, details=details)
        if answer.status == 501:
            # What this server can't do, in its own words: a text prompt where no text model is installed (the
            # encoders' answer). An argument the server can't take, not a fault.
            raise ToolError('invalid', text)
        if answer.status == 504 and (counting or self.counting):
            raise ToolError('too_slow', TOO_SLOW)
        if answer.status in BUSY_STATUSES:
            said = text if body_detail else ''
            message = said if 'try again' in said.lower() else f'{said} {TRY_AGAIN}'.strip()
            raise ToolError('busy', message, retryable=True)
        raise ToolFailed(f'{method} {template} answered {answer.status}: {text}')

    async def get(self, template: str, *, missing: Optional[str] = None, **params: Any) -> Any:
        """GET `template`; keyword arguments fill its path parameters, `query` its query string."""
        query = params.pop('query', None)
        return await self.call('GET', template, missing=missing, path_params=params, query=query)

    async def post(self, template: str, body: Any, *, counting: bool = False, **params: Any) -> Any:
        """POST `body` to `template` (a read: the tool lists only POSTs that read)."""
        return await self.call('POST', template, path_params=params, body=body, counting=counting)
