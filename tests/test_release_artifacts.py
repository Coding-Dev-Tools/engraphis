from __future__ import annotations

import hashlib

import pytest

from scripts import verify_release_artifacts
from scripts.verify_release_artifacts import (
    ArtifactIncomplete,
    ArtifactMismatch,
    local_artifacts,
    pypi_artifacts,
    validate_artifacts,
)


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class _Response:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


def test_pypi_metadata_request_is_fixed_origin_and_refuses_redirects(monkeypatch):
    requests = []
    handlers = []

    class Opener:
        def open(self, request, *, timeout):
            requests.append((request, timeout))
            return _Response(b'{"urls":[]}')

    def build_opener(*received):
        handlers.extend(received)
        return Opener()

    monkeypatch.setattr(
        verify_release_artifacts.urllib.request, "build_opener", build_opener
    )

    assert pypi_artifacts("1.2.3") == {}
    assert len(requests) == 1
    assert requests[0][0].full_url == "https://pypi.org/pypi/engraphis/1.2.3/json"
    assert requests[0][0].get_header("Accept") == "application/json"
    assert requests[0][1] == 30
    assert len(handlers) == 1
    assert handlers[0].redirect_request(
        None, None, 302, "Found", {}, "https://example.test"
    ) is None


def test_pypi_metadata_redirect_is_not_followed(monkeypatch):
    calls = []

    class Opener:
        def open(self, request, *, timeout):
            calls.append((request.full_url, timeout))
            raise verify_release_artifacts.urllib.error.HTTPError(
                request.full_url, 302, "Found", {}, None,
            )

    monkeypatch.setattr(
        verify_release_artifacts.urllib.request,
        "build_opener",
        lambda *_handlers: Opener(),
    )

    with pytest.raises(ArtifactMismatch, match="metadata request failed"):
        pypi_artifacts("1.2.3")
    assert calls == [("https://pypi.org/pypi/engraphis/1.2.3/json", 30)]


@pytest.mark.parametrize(
    ("code", "payload", "message"),
    [
        (404, b"", None),
        (503, b"", "metadata request failed"),
        (200, b"not json", "metadata response was unavailable or malformed"),
    ],
)
def test_pypi_metadata_errors_are_redacted_and_deterministic(
    monkeypatch, code, payload, message
):
    class Opener:
        def open(self, request, *, timeout):
            if code != 200:
                raise verify_release_artifacts.urllib.error.HTTPError(
                    request.full_url, code, "untrusted detail", {}, None,
                )
            return _Response(payload)

    monkeypatch.setattr(
        verify_release_artifacts.urllib.request,
        "build_opener",
        lambda *_handlers: Opener(),
    )

    if code == 404:
        assert pypi_artifacts("1.2.3") == {}
    else:
        with pytest.raises(ArtifactMismatch, match=message) as caught:
            pypi_artifacts("1.2.3")
        assert "untrusted detail" not in str(caught.value)


@pytest.mark.parametrize(
    "payload",
    [
        b"[]",
        b'{"urls":[{"filename":"engraphis-1.2.3.whl","digests":{"sha256":"bad"}}]}',
        b'{"urls":[{"filename":"engraphis-1.2.3.whl","digests":{"sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}},{"filename":"engraphis-1.2.3.whl","digests":{"sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}}]}',
    ],
)
def test_pypi_metadata_rejects_malformed_or_duplicate_digests(monkeypatch, payload):
    class Opener:
        def open(self, request, *, timeout):
            return _Response(payload)

    monkeypatch.setattr(
        verify_release_artifacts.urllib.request,
        "build_opener",
        lambda *_handlers: Opener(),
    )

    with pytest.raises(
        ArtifactMismatch, match="malformed artifact metadata|duplicate artifact"
    ):
        pypi_artifacts("1.2.3")


def test_verified_pypi_subset_can_be_safely_resumed(tmp_path):
    wheel = tmp_path / "engraphis-1.0.0-cp311-cp311-win_amd64.whl"
    sdist = tmp_path / "engraphis-1.0.0.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")
    local = local_artifacts(tmp_path)

    validate_artifacts(local, {wheel.name: _digest(b"wheel")}, exact=False)
    with pytest.raises(ArtifactIncomplete):
        validate_artifacts(local, {wheel.name: _digest(b"wheel")}, exact=True)
    validate_artifacts(local, local, exact=True)


def test_pypi_duplicate_name_is_skipped_only_when_digest_matches(tmp_path):
    wheel = tmp_path / "engraphis-1.0.0-py3-none-any.whl"
    wheel.write_bytes(b"candidate")
    local = local_artifacts(tmp_path)

    with pytest.raises(ArtifactMismatch, match="digest conflicts"):
        validate_artifacts(local, {wheel.name: _digest(b"different")}, exact=False)


def test_unexpected_published_filename_fails_closed(tmp_path):
    wheel = tmp_path / "engraphis-1.0.0-py3-none-any.whl"
    wheel.write_bytes(b"candidate")
    local = local_artifacts(tmp_path)

    with pytest.raises(ArtifactMismatch, match="outside the candidate set"):
        validate_artifacts(
            local, {"engraphis-1.0.0-malicious.whl": _digest(b"candidate")},
            exact=False,
        )
