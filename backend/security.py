"""Opt-in public deployment policy; ordinary local development keeps its defaults."""

import os


def production() -> bool:
    return os.getenv('FRIDGE_ENV', 'development').strip().lower() in ('prod', 'production')


def public_mode() -> bool:
    return production() or os.getenv('DEMO_MODE', 'false').lower() == 'true'


def private_guest(user: dict) -> bool:
    return public_mode() and user.get('role') == 'guest'
