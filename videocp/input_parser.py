from __future__ import annotations

import re

import requests

from videocp.models import ParsedInput
from videocp.providers import resolve_provider
from videocp.runtime_log import full_url, log_info, log_warn

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/134.0.0.0 Safari/537.36"
)

URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
URL_TRAILING_PUNCTUATION = "，。！？；：、“”\"'`()[]{}<>）】》"


def extract_first_url(text: str) -> str:
    match = URL_RE.search(text)
    if not match:
        raise ValueError("No URL found in input.")
    return match.group(0).rstrip(URL_TRAILING_PUNCTUATION)


def resolve_url(url: str, timeout_secs: int = 15) -> str:
    session = requests.Session()
    response = session.get(
        url,
        allow_redirects=True,
        headers={"User-Agent": DEFAULT_UA},
        timeout=timeout_secs,
    )
    response.raise_for_status()
    return response.url


def parse_input(raw_input: str, timeout_secs: int = 15) -> ParsedInput:
    log_info("input.parse.start", raw_input=raw_input)
    extracted_url = extract_first_url(raw_input)
    log_info("input.url.extracted", url=full_url(extracted_url))
    try:
        canonical_url = resolve_url(extracted_url, timeout_secs=timeout_secs)
        log_info(
            "input.url.resolved",
            extracted_url=full_url(extracted_url),
            canonical_url=full_url(canonical_url),
        )
    except requests.RequestException:
        canonical_url = extracted_url
        log_warn(
            "input.url.resolve_failed",
            extracted_url=full_url(extracted_url),
            fallback="use_extracted_url",
        )
    provider = resolve_provider(canonical_url)
    log_info(
        "input.parse.complete",
        provider=provider.key,
        canonical_url=full_url(canonical_url),
    )
    return ParsedInput(
        raw_input=raw_input,
        extracted_url=extracted_url,
        canonical_url=canonical_url,
        provider_key=provider.key,
    )
