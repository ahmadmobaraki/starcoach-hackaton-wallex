from __future__ import annotations

from wallex_trader.config import Settings


def private_headers(settings: Settings) -> dict[str, str]:
    key = settings.wallex_api_key.get_secret_value()
    if not key:
        raise ValueError("WALLEX_API_KEY is not configured")
    headers = {"x-api-key": key, "Accept": "application/json"}
    sub_account = (settings.wallex_sub_account_client_id or "").strip()
    if sub_account:
        headers["sub-account-client-id"] = sub_account
    return headers


def public_headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate",
        "User-Agent": "argus-wallex-trader/0.1",
    }
