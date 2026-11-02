"""warn_insecure_session_cookie warns ONLY in production with a non-Secure cookie.

Dev/non-prod (the default FRIDGE_ENV) is silent and needs no TLS — local keeps working over plain
http://localhost with a non-Secure cookie. The gate never refuses (a fresh prod box runs HTTP until
TLS is added), it just makes a sniffable-session prod deploy impossible to ship silently.
"""

import logging

from backend.crypto import warn_insecure_session_cookie


def _warned(caplog) -> bool:
    return any("SESSION_COOKIE_SECURE" in r.message for r in caplog.records)


def test_warns_in_prod_with_insecure_cookie(monkeypatch, caplog):
    monkeypatch.setenv("FRIDGE_ENV", "production")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    with caplog.at_level(logging.WARNING):
        warn_insecure_session_cookie()
    assert _warned(caplog)


def test_silent_in_dev_even_with_insecure_cookie(monkeypatch, caplog):
    monkeypatch.delenv("FRIDGE_ENV", raising=False)  # defaults to development
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    with caplog.at_level(logging.WARNING):
        warn_insecure_session_cookie()
    assert not _warned(caplog)


def test_silent_in_prod_when_cookie_secure(monkeypatch, caplog):
    monkeypatch.setenv("FRIDGE_ENV", "production")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "true")
    with caplog.at_level(logging.WARNING):
        warn_insecure_session_cookie()
    assert not _warned(caplog)
