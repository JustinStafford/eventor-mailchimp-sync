"""Minimal Mailchimp Marketing API v3 client covering what the sync needs.

Safety rules baked in here rather than left to callers:

* :meth:`MailchimpClient.upsert_member` never sends ``status``. New contacts
  get ``status_if_new`` (``subscribed``); existing contacts keep whatever
  status they have, so unsubscribed, cleaned (bounced) or archived people are
  never reverted.
* There is no delete or archive method at all.
"""

from __future__ import annotations

import hashlib
import logging
import random
import time
from collections.abc import Iterable
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self

import httpx

log = logging.getLogger(__name__)

# 520-524 are Cloudflare's transient origin errors; Eventor sits behind Cloudflare.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504, 520, 521, 522, 523, 524})
PAGE_SIZE = 1000
MEMBER_FIELDS = "members.email_address,members.status,members.merge_fields,members.tags,total_items"
# Contacts in these states are never written to by the sync.
PROTECTED_STATUSES = frozenset({"unsubscribed", "cleaned", "archived"})


# 400 titles Mailchimp uses when it refuses one particular contact rather than the request.
CONTACT_REJECTION_TITLES = frozenset(
    {"Member In Compliance State", "Forgotten Email Not Subscribed", "Member Exists"}
)


class MailchimpError(Exception):
    """An error response from Mailchimp, or a transport failure."""

    def __init__(self, message: str, *, status_code: int | None = None, detail: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail

    @property
    def is_contact_rejection(self) -> bool:
        """True when Mailchimp refused this one contact (fake-looking address, compliance
        state, forgotten email) rather than failing the request as a whole.

        A 400 that names a field other than ``email_address`` (for example a merge
        field) is a problem with the run, not the contact, and is *not* a rejection.
        """
        if self.status_code != 400 or not isinstance(self.detail, dict):
            return False
        if self.detail.get("title") in CONTACT_REJECTION_TITLES:
            return True
        fields = {
            str(e.get("field", "")) for e in self.detail.get("errors") or [] if isinstance(e, dict)
        }
        if fields - {"email_address", ""}:
            return False
        text = str(self.detail.get("detail") or "").lower()
        return "email" in text or "looks fake" in text


def normalise_email(email: str) -> str:
    return email.strip().lower()


def subscriber_hash(email: str) -> str:
    """MD5 of the lower-cased email, as Mailchimp addresses members."""
    return hashlib.md5(normalise_email(email).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class MergeField:
    tag: str
    name: str
    type: str
    phone_format: str | None = None  # "US" or "none" for phone fields


@dataclass(frozen=True, slots=True)
class AudienceMember:
    """A contact as it currently exists in the audience."""

    email: str
    status: str
    merge_fields: dict[str, Any]
    tags: frozenset[str]
    email_original: str = ""

    @property
    def is_protected(self) -> bool:
        return self.status in PROTECTED_STATUSES


class MailchimpClient:
    def __init__(
        self,
        api_key: str,
        server_prefix: str,
        list_id: str,
        *,
        timeout: float = 60.0,
        max_retries: int = 4,
        backoff_base: float = 1.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key or not server_prefix or not list_id:
            raise MailchimpError("Mailchimp API key, server prefix and list ID are all required")
        self.list_id = list_id
        self.base_url = f"https://{server_prefix}.api.mailchimp.com/3.0"
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._sleep = time.sleep
        self._http = httpx.Client(
            base_url=self.base_url,
            auth=("anystring", api_key),
            headers={"Accept": "application/json"},
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- transport -----------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        attempt = 0
        while True:
            try:
                response = self._http.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                if attempt >= self.max_retries:
                    raise MailchimpError(f"{method} {path} failed: {exc}") from exc
                self._backoff(attempt, None)
                attempt += 1
                continue
            if response.status_code in RETRY_STATUSES and attempt < self.max_retries:
                self._backoff(attempt, response)
                attempt += 1
                continue
            if response.status_code >= 400:
                raise self._error(method, path, response)
            return response

    def _backoff(self, attempt: int, response: httpx.Response | None) -> None:
        delay = self.backoff_base * (2**attempt) + random.uniform(0, self.backoff_base)
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                delay = max(delay, float(retry_after))
        log.warning("Mailchimp request failed; retrying in %.1fs", delay)
        self._sleep(delay)

    @staticmethod
    def _error(method: str, path: str, response: httpx.Response) -> MailchimpError:
        detail: Any = None
        message = f"Mailchimp {method} {path} returned HTTP {response.status_code}"
        try:
            detail = response.json()
        except ValueError:
            detail = response.text[:300]
        if isinstance(detail, dict):
            title = detail.get("title")
            text = detail.get("detail")
            if title:
                message = f"{message}: {title}"
            if text:
                message = f"{message} - {text}"
            for err in detail.get("errors") or []:
                if isinstance(err, dict):
                    message = f"{message} ({err.get('field')}: {err.get('message')})"
        return MailchimpError(message, status_code=response.status_code, detail=detail)

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request("GET", path, params=params).json()

    # -- reads ---------------------------------------------------------------

    def ping(self) -> None:
        self._get_json("/ping")

    def list_info(self) -> dict[str, Any]:
        return self._get_json(
            f"/lists/{self.list_id}",
            {"fields": "id,name,stats.member_count,stats.unsubscribe_count,stats.cleaned_count"},
        )

    def merge_fields(self) -> list[MergeField]:
        fields: list[MergeField] = []
        offset = 0
        while True:
            data = self._get_json(
                f"/lists/{self.list_id}/merge-fields",
                {
                    "count": PAGE_SIZE,
                    "offset": offset,
                    "fields": (
                        "merge_fields.tag,merge_fields.name,merge_fields.type,"
                        "merge_fields.options,total_items"
                    ),
                },
            )
            for item in data.get("merge_fields", []):
                options = item.get("options") or {}
                fields.append(
                    MergeField(
                        tag=item["tag"],
                        name=item.get("name", ""),
                        type=item.get("type", "text"),
                        phone_format=options.get("phone_format"),
                    )
                )
            offset += PAGE_SIZE
            if offset >= int(data.get("total_items", 0)):
                break
        return fields

    def all_members(self) -> list[AudienceMember]:
        """Every contact in the audience, including archived ones.

        Mailchimp hides archived contacts from the default listing, so a second
        pass with ``status=archived`` is made; the sync needs to know about them
        so it never un-archives anyone by accident.
        """
        members: dict[str, AudienceMember] = {}
        for status_filter in (None, "archived"):
            offset = 0
            while True:
                params: dict[str, Any] = {
                    "count": PAGE_SIZE,
                    "offset": offset,
                    "fields": MEMBER_FIELDS,
                }
                if status_filter:
                    params["status"] = status_filter
                data = self._get_json(f"/lists/{self.list_id}/members", params)
                for item in data.get("members", []):
                    member = _member_from_json(item)
                    members.setdefault(member.email, member)
                offset += PAGE_SIZE
                if offset >= int(data.get("total_items", 0)):
                    break
        return list(members.values())

    # -- writes --------------------------------------------------------------

    def upsert_member(
        self,
        email: str,
        *,
        merge_fields: dict[str, Any] | None = None,
        tags: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """``PUT /lists/{list_id}/members/{hash}`` without ever sending ``status``.

        For a new contact this creates a subscribed member. For an existing
        contact only ``merge_fields`` (and additive ``tags``) are applied; the
        subscription status is left exactly as it was.
        """
        body: dict[str, Any] = {
            "email_address": normalise_email(email),
            "status_if_new": "subscribed",
        }
        if merge_fields:
            body["merge_fields"] = merge_fields
        if tags:
            body["tags"] = sorted(set(tags))
        assert "status" not in body  # the never-resubscribe guarantee
        response = self._request(
            "PUT", f"/lists/{self.list_id}/members/{subscriber_hash(email)}", json=body
        )
        return response.json()

    def update_tags(
        self, email: str, *, add: Iterable[str] = (), remove: Iterable[str] = ()
    ) -> None:
        """``POST /lists/{list_id}/members/{hash}/tags``: add and/or remove tags."""
        ops = [{"name": t, "status": "active"} for t in sorted(set(add))]
        ops += [{"name": t, "status": "inactive"} for t in sorted(set(remove))]
        if not ops:
            return
        self._request(
            "POST",
            f"/lists/{self.list_id}/members/{subscriber_hash(email)}/tags",
            json={"tags": ops, "is_syncing": False},
        )


def _member_from_json(item: dict[str, Any]) -> AudienceMember:
    raw_email = item.get("email_address", "")
    tags = frozenset(t.get("name", "") for t in item.get("tags", []) if t.get("name"))
    return AudienceMember(
        email=normalise_email(raw_email),
        email_original=raw_email,
        status=item.get("status", ""),
        merge_fields=dict(item.get("merge_fields") or {}),
        tags=tags,
    )
