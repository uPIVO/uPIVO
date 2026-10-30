"""Signed clients for legacy business tests; security tests use plain TestClient.

These tests select the acting user through the existing path/query/body user_id.
Only the test client issues tokens; production has no identity shortcut.
"""

import re
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from app.auth.tokens import issue_token
from app.core.config import get_settings


def auth_headers(user_id: int) -> dict[str, str]:
    token = issue_token(
        user_id=user_id,
        secret=get_settings().auth_token_secret.get_secret_value(),
        ttl_seconds=3600,
    )
    return {"Authorization": f"Bearer {token}"}


class UserClient(TestClient):
    def request(self, method, url, **kwargs):
        match = re.search(r"/users/(\d+)/", str(url))
        query = parse_qs(urlsplit(str(url)).query)
        params = kwargs.get("params") or {}
        payload = kwargs.get("json") or {}
        user_id = (
            int(match[1])
            if match
            else int(query.get("user_id", [params.get("user_id", payload.get("user_id", 1))])[0])
        )
        headers = auth_headers(max(1, user_id))
        headers.update(kwargs.pop("headers", None) or {})
        return super().request(method, url, headers=headers, **kwargs)
