"""Shared plumbing for the four service clients."""

import re
from difflib import SequenceMatcher

import requests


class ServiceError(Exception):
    """Anything that should be shown to the user as 'this service call failed'."""


class BaseClient:
    timeout = 20

    def __init__(self, config):
        self.config = config
        self.base_url = (config.url or "").rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json", "User-Agent": "MediaThatFeelsLike/0.1"})
        if not self.base_url:
            raise ServiceError(f"{config.get_service_display()} has no URL configured")

    def _request(self, method, path, **kwargs):
        kwargs.setdefault("timeout", self.timeout)
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        try:
            resp = self.session.request(method, url, **kwargs)
        except requests.RequestException as exc:
            raise ServiceError(f"{self.config.get_service_display()}: {exc.__class__.__name__}: {exc}") from exc
        if resp.status_code == 401 or resp.status_code == 403:
            raise ServiceError(f"{self.config.get_service_display()}: authentication rejected (HTTP {resp.status_code}) -- check the API key / credentials")
        if resp.status_code >= 400:
            detail = ""
            try:
                body = resp.json()
                if isinstance(body, list) and body and isinstance(body[0], dict):
                    detail = body[0].get("errorMessage") or body[0].get("message") or ""
                elif isinstance(body, dict):
                    detail = body.get("message") or body.get("error") or body.get("title") or ""
            except ValueError:
                detail = resp.text[:200]
            raise ServiceError(f"{self.config.get_service_display()}: HTTP {resp.status_code} {detail}".strip(), )
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError as exc:
            raise ServiceError(f"{self.config.get_service_display()}: non-JSON response from {path}") from exc

    def get(self, path, **params):
        return self._request("GET", path, params=params)

    def post(self, path, json=None, **params):
        return self._request("POST", path, json=json, params=params)

    def test(self):
        """Return a short human-readable success string or raise ServiceError."""
        raise NotImplementedError


# -- fuzzy matching helpers -------------------------------------------------------------

_PUNCT = re.compile(r"[^a-z0-9]+")
_FEAT = re.compile(r"\b(feat\.?|ft\.?|featuring)\b.*$")
_PARENS = re.compile(r"\(.*?\)|\[.*?\]")


def normalise(text):
    text = (text or "").lower()
    text = _PARENS.sub(" ", text)
    text = _FEAT.sub(" ", text)
    text = _PUNCT.sub(" ", text).strip()
    return re.sub(r"^(the|a|an) ", "", text)


def similarity(a, b):
    """0..1 -- SequenceMatcher on normalised strings, boosted when one contains the other."""
    na, nb = normalise(a), normalise(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ratio = SequenceMatcher(None, na, nb).ratio()
    # "Fade Into You" vs "Fade Into You - 2006 Remaster": containment is a strong
    # signal, but only when the shorter side is most of the longer one ("Heat" is
    # not most of "Heathers").
    if (na in nb or nb in na) and min(len(na), len(nb)) / max(len(na), len(nb)) >= 0.6:
        ratio = max(ratio, 0.85)
    return ratio


def best_match(candidates, scorer, threshold):
    """Pick the highest-scoring candidate above `threshold`, or (None, score)."""
    best, best_score = None, 0.0
    for cand in candidates:
        score = scorer(cand)
        if score > best_score:
            best, best_score = cand, score
    if best_score < threshold:
        return None, best_score
    return best, best_score
