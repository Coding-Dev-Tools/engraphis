from __future__ import annotations

import http.client
from io import BytesIO
import threading
from concurrent.futures import ThreadPoolExecutor
import urllib.error

import pytest

from engraphis import cloud_session


@pytest.fixture(autouse=True)
def _isolated_cloud_state(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ENGRAPHIS_STATE_DIR", str(tmp_path))


def test_refresh_rotates_saved_credential_and_binds_client_workspace(monkeypatch) -> None:
    monkeypatch.delenv("ENGRAPHIS_CLOUD_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("ENGRAPHIS_CLOUD_REFRESH_CREDENTIAL", raising=False)
    saved = {
        "control_url": "https://control.example.test",
        "compute_url": "https://compute.example.test",
        "organization_id": "org_1",
        "refresh_credential": "old-refresh",
        "token_subject": "member",
    }
    writes = []
    requests = []
    monkeypatch.setattr(cloud_session, "_load", lambda: dict(saved))
    monkeypatch.setattr(cloud_session, "_save", writes.append)
    monkeypatch.setattr(
        cloud_session,
        "validate_cloud_base_url",
        lambda value: value.rstrip("/"),
    )

    def refresh(control_url, credential, workspace_id, token_subject):
        requests.append((control_url, credential, workspace_id, token_subject))
        return {
            "access_token": "short-lived-access",
            "organization_id": "org_1",
            "refresh_credential": "rotated-refresh",
            "refresh_expires_at": "2026-08-21T00:00:00Z",
            "token_subject": "member",
        }

    monkeypatch.setattr(cloud_session, "_post_refresh", refresh)
    result = cloud_session.access_for_workspace("ws_client_1")
    assert result == (
        "short-lived-access",
        "org_1",
        "https://compute.example.test",
    )
    assert requests == [(
        "https://control.example.test",
        "old-refresh",
        "ws_client_1",
        "member",
    )]
    assert writes[0]["refresh_credential"] == "rotated-refresh"


def test_direct_access_token_path_never_reads_refresh_state(monkeypatch) -> None:
    monkeypatch.setenv("ENGRAPHIS_CLOUD_ACCESS_TOKEN", "direct-token")
    monkeypatch.setenv("ENGRAPHIS_CLOUD_ORGANIZATION_ID", "org_direct")
    monkeypatch.setenv("ENGRAPHIS_CLOUD_COMPUTE_URL", "https://compute.example.test")
    monkeypatch.setattr(cloud_session, "validate_cloud_base_url", lambda value: value)
    monkeypatch.setattr(
        cloud_session,
        "_load",
        lambda: (_ for _ in ()).throw(AssertionError("refresh state must not be read")),
    )
    assert cloud_session.access_for_workspace("ws") == (
        "direct-token",
        "org_direct",
        "https://compute.example.test",
    )


def test_environment_refresh_honors_explicit_device_subject(monkeypatch) -> None:
    monkeypatch.setenv("ENGRAPHIS_CLOUD_REFRESH_CREDENTIAL", "env-refresh")
    monkeypatch.setenv("ENGRAPHIS_CLOUD_CONTROL_URL", "https://control.example.test")
    monkeypatch.setenv("ENGRAPHIS_CLOUD_TOKEN_SUBJECT", "device")
    monkeypatch.setattr(cloud_session, "_load", lambda: {})
    writes = []
    monkeypatch.setattr(cloud_session, "_save", writes.append)
    monkeypatch.setattr(
        cloud_session, "validate_cloud_base_url", lambda value: value.rstrip("/")
    )
    subjects = []

    def refresh(control_url, credential, workspace_id, token_subject):
        subjects.append(token_subject)
        return {
            "access_token": "device-access",
            "organization_id": "org_device",
            "refresh_credential": "rotated-but-env-owned",
            "token_subject": "device",
        }

    monkeypatch.setattr(cloud_session, "_post_refresh", refresh)
    assert cloud_session.access_for_workspace("ws", require_compute=False) == (
        "device-access",
        "org_device",
        "",
    )
    assert subjects == ["device"]
    assert writes[0]["refresh_credential"] == "rotated-but-env-owned"


def test_persisted_refresh_subject_cannot_be_overridden_by_environment(monkeypatch) -> None:
    """A bootstrap-only setting must not invalidate an already-bound credential."""

    monkeypatch.setenv("ENGRAPHIS_CLOUD_TOKEN_SUBJECT", "device")
    saved = {"token_subject": "member", "refresh_credential": "engr_rt_saved"}

    assert cloud_session._token_subject(saved) == "member"


def test_environment_bootstrap_persists_and_reuses_rotated_credential(monkeypatch) -> None:
    monkeypatch.setenv("ENGRAPHIS_CLOUD_REFRESH_CREDENTIAL", "env-bootstrap")
    monkeypatch.setenv("ENGRAPHIS_CLOUD_CONTROL_URL", "https://control.example.test")
    state = {}
    requests = []
    monkeypatch.setattr(cloud_session, "_load", lambda: dict(state))
    monkeypatch.setattr(cloud_session, "_save", lambda value: state.update(value))
    monkeypatch.setattr(
        cloud_session, "validate_cloud_base_url", lambda value: value.rstrip("/")
    )

    def refresh(control_url, credential, workspace_id, token_subject):
        requests.append(credential)
        return {
            "access_token": "access-%d" % len(requests),
            "organization_id": "org_device",
            "refresh_credential": "rotated-%d" % len(requests),
            "token_subject": "member",
        }

    monkeypatch.setattr(cloud_session, "_post_refresh", refresh)
    first = cloud_session.access_for_workspace("ws", require_compute=False)
    second = cloud_session.access_for_workspace("ws", require_compute=False)

    assert first[0] == "access-1"
    assert second[0] == "access-2"
    assert requests == ["env-bootstrap", "rotated-1"]
    assert state["refresh_credential"] == "rotated-2"


def test_a_possibly_spent_refresh_is_marked_unusable_and_never_replayed(
    monkeypatch,
) -> None:
    """A truncated/revoked rotation must force reconnect on every later request."""

    monkeypatch.setenv("ENGRAPHIS_CLOUD_REFRESH_CREDENTIAL", "env-bootstrap")
    monkeypatch.setenv("ENGRAPHIS_CLOUD_CONTROL_URL", "https://control.example.test")
    state = {}
    requests = []
    monkeypatch.setattr(cloud_session, "_load", lambda: dict(state))
    monkeypatch.setattr(
        cloud_session, "_save", lambda value: (state.clear(), state.update(value))
    )
    monkeypatch.setattr(
        cloud_session, "validate_cloud_base_url", lambda value: value.rstrip("/")
    )

    def spent(*args):
        requests.append(args[1])
        raise cloud_session.CloudSessionError(
            "Connect this installation again.",
            status=409,
            refresh_unusable=True,
        )

    monkeypatch.setattr(cloud_session, "_post_refresh", spent)
    with pytest.raises(cloud_session.CloudSessionError) as first:
        cloud_session.access_for_workspace("ws", require_compute=False)
    with pytest.raises(cloud_session.CloudSessionError) as second:
        cloud_session.access_for_workspace("ws", require_compute=False)

    assert first.value.status == 409
    assert second.value.status in {401, 409}
    assert requests == ["env-bootstrap"]
    assert state["refresh_unusable"] is True
    assert state["refresh_unusable_digest"] == cloud_session._refresh_digest("env-bootstrap")
    assert "refresh_credential" not in state


def test_replaced_environment_bootstrap_is_not_blocked_by_a_spent_predecessor(
    monkeypatch,
) -> None:
    """A persisted tombstone applies to its credential, not every future bootstrap."""

    monkeypatch.setenv("ENGRAPHIS_CLOUD_REFRESH_CREDENTIAL", "replacement-bootstrap")
    monkeypatch.setenv("ENGRAPHIS_CLOUD_CONTROL_URL", "https://control.example.test")
    state = {
        "refresh_unusable": True,
        "refresh_unusable_at": 1.0,
        "refresh_unusable_digest": cloud_session._refresh_digest("spent-bootstrap"),
    }
    requests = []
    monkeypatch.setattr(cloud_session, "_load", lambda: dict(state))
    monkeypatch.setattr(
        cloud_session, "_save", lambda value: (state.clear(), state.update(value))
    )
    monkeypatch.setattr(
        cloud_session, "validate_cloud_base_url", lambda value: value.rstrip("/")
    )

    def refresh(control_url, credential, workspace_id, token_subject):
        requests.append(credential)
        return {
            "access_token": "replacement-access",
            "organization_id": "org_1",
            "refresh_credential": "rotated-replacement",
            "token_subject": "member",
        }

    monkeypatch.setattr(cloud_session, "_post_refresh", refresh)
    assert cloud_session.configured(require_compute=False) is True
    assert cloud_session.access_for_workspace("ws", require_compute=False)[0] == "replacement-access"
    assert requests == ["replacement-bootstrap"]
    assert "refresh_unusable" not in state
    assert "refresh_unusable_digest" not in state


def test_concurrent_refreshes_spend_each_rotation_once(monkeypatch) -> None:
    monkeypatch.setenv("ENGRAPHIS_CLOUD_REFRESH_CREDENTIAL", "bootstrap")
    monkeypatch.setenv("ENGRAPHIS_CLOUD_CONTROL_URL", "https://control.example.test")
    state = {}
    calls = []
    authority_lock = threading.Lock()
    monkeypatch.setattr(cloud_session, "_load", lambda: dict(state))
    monkeypatch.setattr(cloud_session, "_save", lambda value: state.update(value))
    monkeypatch.setattr(
        cloud_session, "validate_cloud_base_url", lambda value: value.rstrip("/")
    )

    def refresh(control_url, credential, workspace_id, token_subject):
        with authority_lock:
            expected = "bootstrap" if not calls else "rotated-%d" % len(calls)
            if credential != expected:
                raise cloud_session.CloudSessionError("refresh replay rejected")
            calls.append((credential, workspace_id))
            sequence = len(calls)
            return {
                "access_token": "access-%d" % sequence,
                "organization_id": "org_device",
                "refresh_credential": "rotated-%d" % sequence,
                "token_subject": "member",
            }

    monkeypatch.setattr(cloud_session, "_post_refresh", refresh)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda workspace: cloud_session.access_for_workspace(
                workspace, require_compute=False
            ),
            ("ws-1", "ws-2"),
        ))

    assert {item[0] for item in results} == {"access-1", "access-2"}
    assert [item[0] for item in calls] == ["bootstrap", "rotated-1"]
    assert state["refresh_credential"] == "rotated-2"


def test_refresh_lock_oserror_is_normalized(monkeypatch) -> None:
    monkeypatch.setenv("ENGRAPHIS_CLOUD_REFRESH_CREDENTIAL", "bootstrap")
    monkeypatch.setenv("ENGRAPHIS_CLOUD_CONTROL_URL", "https://control.example.test")
    monkeypatch.setattr(
        cloud_session, "private_file_stat",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("lock failure")),
    )

    with pytest.raises(cloud_session.CloudSessionError, match="lock.*unsafe") as caught:
        cloud_session.access_for_workspace("ws", require_compute=False)
    assert caught.value.status == 409


def test_unreadable_state_mount_is_normalized_before_the_refresh_lock(monkeypatch) -> None:
    """A stale/unreadable state mount must not escape as a raw filesystem error.

    ``access_for_workspace`` calls ``configured()`` -> ``_load()`` in its preflight,
    before ``_refresh_lock()`` can normalize I/O failures, so an OSError there used to
    reach the route layer as an opaque 500 instead of a structured cloud error.
    """

    monkeypatch.setattr(
        cloud_session, "read_private_text",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("stale NFS handle")),
    )

    with pytest.raises(cloud_session.CloudSessionError, match="temporarily unreadable") as caught:
        cloud_session.access_for_workspace("ws", require_compute=False)
    assert 400 <= caught.value.status <= 599


def test_unconfigured_client_does_not_create_the_state_directory(monkeypatch) -> None:
    monkeypatch.delenv("ENGRAPHIS_CLOUD_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("ENGRAPHIS_CLOUD_ORGANIZATION_ID", raising=False)
    monkeypatch.delenv("ENGRAPHIS_CLOUD_COMPUTE_URL", raising=False)
    monkeypatch.delenv("ENGRAPHIS_CLOUD_REFRESH_CREDENTIAL", raising=False)
    monkeypatch.delenv("ENGRAPHIS_CLOUD_CONTROL_URL", raising=False)
    monkeypatch.setattr(cloud_session, "_load", lambda: {})
    monkeypatch.setattr(
        cloud_session.Path,
        "mkdir",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not lock")),
    )

    with pytest.raises(cloud_session.CloudSessionError, match="Connect this installation") as caught:
        cloud_session.access_for_workspace("ws")

    assert caught.value.status == 401


def test_refresh_http_error_response_is_closed(monkeypatch) -> None:
    error = urllib.error.HTTPError(
        "https://control.example.test/v1/tokens/refresh",
        503, "unavailable", {}, BytesIO(b'{"detail":"private"}'),
    )
    closed = []
    original_close = error.close

    def close():
        closed.append(True)
        original_close()

    error.close = close

    class _Opener:
        def open(self, request, timeout):
            raise error

    monkeypatch.setattr(
        cloud_session.urllib.request, "build_opener", lambda *handlers: _Opener()
    )
    with pytest.raises(cloud_session.CloudSessionError, match="could not refresh"):
        cloud_session._post_refresh(
            "https://control.example.test", "refresh", "ws", "member"
        )
    assert closed == [True]


@pytest.mark.parametrize("status", [401, 403])
def test_refresh_authorization_error_preserves_status(monkeypatch, status) -> None:
    error = urllib.error.HTTPError(
        "https://control.example.test/v1/tokens/refresh",
        status,
        "denied",
        {},
        BytesIO(b'{"detail":"private"}'),
    )

    class _Opener:
        def open(self, request, timeout):
            raise error

    monkeypatch.setattr(
        cloud_session.urllib.request, "build_opener", lambda *handlers: _Opener()
    )
    with pytest.raises(cloud_session.CloudSessionError) as caught:
        cloud_session._post_refresh(
            "https://control.example.test", "refresh", "ws", "member"
        )

    assert caught.value.status == status


@pytest.mark.parametrize("failure", [
    http.client.LineTooLong("header line"),
    http.client.BadStatusLine("garbage"),
])
def test_a_mangled_refresh_status_line_requires_reconnect(monkeypatch, failure) -> None:
    """The request was sent, so an unparsable reply may hide a spent credential.

    These ``HTTPException`` variants escape from ``getresponse()`` after urllib has sent the
    refresh POST.  A control plane or proxy may have consumed the one-time credential before
    the malformed reply prevented its rotation from reaching disk; classifying that as 503
    would retry the stale credential and can revoke its entire family.
    """

    class _Opener:
        def open(self, request, timeout):
            raise failure

    monkeypatch.setattr(
        cloud_session.urllib.request, "build_opener", lambda *handlers: _Opener()
    )
    with pytest.raises(cloud_session.CloudSessionError) as caught:
        cloud_session._post_refresh(
            "https://control.example.test", "refresh", "ws", "member"
        )

    assert caught.value.status == 409
    assert "Connect this installation again" in str(caught.value)


@pytest.mark.parametrize("failure", [
    http.client.IncompleteRead(b'{"access_'),
    TimeoutError("read timed out"),
    ConnectionResetError("reset mid-body"),
])
def test_a_truncated_refresh_body_is_not_reported_as_a_retryable_outage(
    monkeypatch, failure
) -> None:
    """A post-response failure must not invite a retry that replays a spent credential.

    Once the status line parses, the control plane consumed the submitted credential, but
    the rotation it returned only reaches disk after the body parses -- so the stale value
    is still there. ``_public_session_error`` maps 503 to ``transient=True``, which
    ``CloudFeatureClient.run_job`` acts on by retrying, and this module documents that the
    control plane answers a replayed credential by revoking the whole family. 409 is the
    existing non-transient "connect this installation again" bucket.
    """

    class _Truncated:
        def __enter__(self):
            return self

        def __exit__(self, *exc) -> bool:
            return False

        def read(self, size: int = -1) -> bytes:
            raise failure

    class _Opener:
        def open(self, request, timeout):
            return _Truncated()

    monkeypatch.setattr(
        cloud_session.urllib.request, "build_opener", lambda *handlers: _Opener()
    )
    with pytest.raises(cloud_session.CloudSessionError) as caught:
        cloud_session._post_refresh(
            "https://control.example.test", "refresh", "ws", "member"
        )

    assert caught.value.status == 409
    assert "Connect this installation again" in str(caught.value)

    # The status must survive translation to the public error as non-transient, or the
    # retry this whole change exists to prevent happens anyway.
    from engraphis.cloud_features import _public_session_error

    _message, transient = _public_session_error(caught.value.status)
    assert transient is False


@pytest.mark.parametrize("payload", [
    b"{not json",
    b'"a string"',
    b"[]",
])
def test_an_unparseable_refresh_body_is_also_non_transient(monkeypatch, payload) -> None:
    """Same hazard as a truncated body: answered, credential spent, rotation not saved."""

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc) -> bool:
            return False

        def read(self, size: int = -1) -> bytes:
            return payload

    class _Opener:
        def open(self, request, timeout):
            return _Response()

    monkeypatch.setattr(
        cloud_session.urllib.request, "build_opener", lambda *handlers: _Opener()
    )
    with pytest.raises(cloud_session.CloudSessionError) as caught:
        cloud_session._post_refresh(
            "https://control.example.test", "refresh", "ws", "member"
        )

    assert caught.value.status == 409
    assert "invalid session response" in str(caught.value)


def test_a_truncated_refresh_error_body_still_reports_the_status(monkeypatch) -> None:
    """The drain runs inside the ``except HTTPError`` block, so it needs its own guard.

    An exception raised there cannot reach the sibling ``except HTTPException`` clause of
    the same ``try``, so an ``(OSError, ValueError)`` guard let ``IncompleteRead`` replace
    the 401 copy with a traceback.
    """

    error = urllib.error.HTTPError(
        "https://control.example.test/v1/tokens/refresh",
        401, "denied", {}, BytesIO(b'{"detail":"private"}'),
    )

    def _boom(*args, **kwargs):
        raise http.client.IncompleteRead(b'{"detail":"pri')

    error.read = _boom
    error.close = _boom

    class _Opener:
        def open(self, request, timeout):
            raise error

    monkeypatch.setattr(
        cloud_session.urllib.request, "build_opener", lambda *handlers: _Opener()
    )
    with pytest.raises(cloud_session.CloudSessionError) as caught:
        cloud_session._post_refresh(
            "https://control.example.test", "refresh", "ws", "member"
        )

    assert caught.value.status == 401


def test_refresh_network_error_is_service_unavailable(monkeypatch) -> None:
    class _Opener:
        def open(self, request, timeout):
            raise urllib.error.URLError("private network detail")

    monkeypatch.setattr(
        cloud_session.urllib.request, "build_opener", lambda *handlers: _Opener()
    )
    with pytest.raises(cloud_session.CloudSessionError) as caught:
        cloud_session._post_refresh(
            "https://control.example.test", "refresh", "ws", "member"
        )

    assert caught.value.status == 503
    assert "private network detail" not in str(caught.value)


def _downgrade_state(monkeypatch, *, body: dict) -> dict:
    """A saved Team session, refreshed against a control plane that answers *body*."""

    monkeypatch.delenv("ENGRAPHIS_CLOUD_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("ENGRAPHIS_CLOUD_CONTROL_URL", raising=False)
    state = {
        "control_url": "https://control.example.test",
        "organization_id": "org_1",
        "refresh_credential": "old-refresh",
        "token_subject": "member",
        "plan": "team",
        "cloud_access_active": True,
        "cloud_features": ["analytics", "automation", "export", "sync", "team"],
    }

    def _replace(value: dict) -> None:
        state.clear()
        state.update(value)

    monkeypatch.setattr(cloud_session, "_load", lambda: dict(state))
    monkeypatch.setattr(cloud_session, "_save", _replace)
    monkeypatch.setattr(
        cloud_session, "validate_cloud_base_url", lambda value: value.rstrip("/")
    )
    monkeypatch.setattr(cloud_session, "_post_refresh", lambda *_args: dict({
        "access_token": "short-lived-access",
        "organization_id": "org_1",
        "refresh_credential": "rotated-refresh",
        "token_subject": "member",
    }, **body))
    cloud_session.access_for_workspace("ws", require_compute=False)
    return state


def test_a_plan_downgrade_does_not_carry_the_previous_plans_features(monkeypatch) -> None:
    """Team -> Pro must not leave ``team`` in the persisted grant list.

    ``_declared_entitlement`` omits ``cloud_features`` whenever the body carries no feature
    list, so merging it onto the saved record replaced ``plan`` while the *old* list
    survived. ``/api/license`` then reported ``plan: "pro"`` with ``team`` still granted and
    the Team tab stayed unlocked indefinitely — a paid capability handed out for free.
    """

    state = _downgrade_state(monkeypatch, body={
        "plan": "pro", "cloud_access_active": True,
    })

    assert state["plan"] == "pro"
    assert "cloud_features" not in state, "the Team grants outlived the Team plan"
    # With no stale list left, the client's own plan table answers for Pro.
    assert "team" not in cloud_session.saved_entitlement().get("cloud_features", [])


def test_a_downgrade_that_declares_its_own_features_is_taken_verbatim(monkeypatch) -> None:
    """A control plane that does send a list stays authoritative over the plan table."""

    state = _downgrade_state(monkeypatch, body={
        "plan": "pro", "cloud_access_active": True,
        "cloud_features": ["analytics", "sync"],
    })

    assert state["plan"] == "pro"
    assert state["cloud_features"] == ["analytics", "sync"]


def test_a_refresh_reconfirming_the_same_plan_keeps_its_saved_features(monkeypatch) -> None:
    """Only a plan *change* drops the list; re-confirming Team must not blank it.

    Otherwise every token rotation against a control plane that reports the plan but not
    the features would quietly demote a Team customer to the generic table.
    """

    state = _downgrade_state(monkeypatch, body={
        "plan": "team", "cloud_access_active": True,
    })

    assert state["plan"] == "team"
    assert "team" in state["cloud_features"]


@pytest.mark.parametrize("subject", ["admin", "", "device member"])
def test_environment_refresh_rejects_invalid_subject(monkeypatch, subject) -> None:
    monkeypatch.setenv("ENGRAPHIS_CLOUD_REFRESH_CREDENTIAL", "env-refresh")
    monkeypatch.setenv("ENGRAPHIS_CLOUD_CONTROL_URL", "https://control.example.test")
    monkeypatch.setenv("ENGRAPHIS_CLOUD_TOKEN_SUBJECT", subject)
    monkeypatch.setattr(cloud_session, "_load", lambda: {})

    if subject == "":
        # Empty means the documented member default, not an invalid override.
        assert cloud_session.configured(require_compute=False) is True
    else:
        with pytest.raises(cloud_session.CloudSessionError, match="device.*member"):
            cloud_session.configured(require_compute=False)


def test_record_billing_denial_stamps_every_denial_including_the_repeat() -> None:
    """A 402 is an authoritative entitlement read, so it has to advance the clock.

    ``entitlement_checked_at`` is what ``_session_entitlement`` reports as ``fetched_at``
    and what the dashboard's 15-minute refresh interval throttles on. The denial used to
    clear the grants without touching it -- and to write nothing whatsoever once the
    session was already denied -- so a lapsed account re-rotated its single-use refresh
    credential on every request, in every worker, forever.
    """

    cloud_session._save({
        "plan": "team",
        "cloud_access_active": True,
        "cloud_features": ["analytics", "team"],
        "entitlement_checked_at": 1.0,
        "organization_id": "org_1",
        "refresh_credential": "engr_rt_saved",
    })

    assert cloud_session.record_billing_denial() is True
    first = cloud_session._load()
    assert first["cloud_access_active"] is False
    assert first["cloud_features"] == []
    assert first["plan"] == "team", "the lapsed plan is still named for the UI"
    assert first["refresh_credential"] == "engr_rt_saved", "the denial is not a disconnect"
    stamped = first["entitlement_checked_at"]
    assert stamped > 1.0, "the denial was never stamped as checked"

    # The steady state for a lapsed account: already denied, and aged past the interval.
    aged = dict(first)
    aged["entitlement_checked_at"] = stamped - 3600.0
    cloud_session._save(aged)

    assert cloud_session.record_billing_denial() is False, "nothing new was revoked"
    repeated = cloud_session._load()
    assert repeated["entitlement_checked_at"] >= stamped, (
        "a repeat denial must advance the clock the refresh interval reads"
    )
    assert cloud_session.saved_entitlement()["entitlement_checked_at"] >= stamped

def test_record_billing_denial_writes_under_the_refresh_lock(tmp_path, monkeypatch) -> None:
    """The denial is a load-modify-save on the shared session file, so it must be serialized.

    Unguarded, it could read the old single-use refresh credential while another worker was
    mid-rotation and write that stale value back over the rotated one. The control plane
    treats a spent credential as replay and revokes the whole family, turning a lapsed
    subscription into a forced reconnect.
    """

    monkeypatch.setenv("ENGRAPHIS_STATE_DIR", str(tmp_path))
    cloud_session._save({
        "schema": "engraphis-cloud-session/v1",
        "control_url": "https://control.example.test",
        "compute_url": "",
        "organization_id": "org_paid",
        "refresh_credential": "engr_rt_live",
        "token_subject": "member",
        "plan": "team",
        "cloud_features": ["analytics", "team"],
        "cloud_access_active": True,
    })

    held = []
    real_lock = cloud_session._refresh_lock

    import contextlib

    @contextlib.contextmanager
    def _tracking_lock():
        with real_lock():
            held.append("in")
            try:
                yield
            finally:
                held.append("out")

    saved_while = []
    real_save = cloud_session._save
    monkeypatch.setattr(cloud_session, "_refresh_lock", _tracking_lock)
    monkeypatch.setattr(
        cloud_session, "_save",
        lambda v: (saved_while.append(held[-1] if held else None), real_save(v))[1],
    )

    assert cloud_session.record_billing_denial() is True
    assert held == ["in", "out"], "the refresh lock was not taken"
    assert saved_while == ["in"], "the session was written outside the refresh lock"

    record = cloud_session._load()
    assert record["cloud_access_active"] is False
    assert record["cloud_features"] == []
    assert record["refresh_credential"] == "engr_rt_live", "the credential was clobbered"


def test_refresh_save_failure_retires_the_spent_credential_without_replay(monkeypatch) -> None:
    """A local state failure after a successful refresh cannot leave a replayable token.

    The refresh credential is single-use.  A storage error occurs after the control plane
    has consumed it, so retrying the old value risks revoking its entire family.  The
    client must return a reconnect error and fence the value in-process even if the best
    effort persisted retirement is also unavailable.
    """

    state = {
        "control_url": "https://control.example.test",
        "organization_id": "org_1",
        "refresh_credential": "old-refresh",
        "token_subject": "member",
    }
    posts = []
    saves = []

    monkeypatch.setattr(cloud_session, "_UNUSABLE_REFRESHES", set())
    monkeypatch.setattr(cloud_session, "_load", lambda: dict(state))
    monkeypatch.setattr(
        cloud_session, "validate_cloud_base_url", lambda value: value.rstrip("/")
    )

    def _post(*args):
        posts.append(args)
        return {
            "access_token": "short-lived-access",
            "organization_id": "org_1",
            "refresh_credential": "rotated-refresh",
            "token_subject": "member",
        }

    def _save(_value):
        saves.append(dict(_value))
        raise cloud_session.UnsafeStateFile("state path changed during write")

    monkeypatch.setattr(cloud_session, "_post_refresh", _post)
    monkeypatch.setattr(cloud_session, "_save", _save)

    with pytest.raises(cloud_session.CloudSessionError) as caught:
        cloud_session.access_for_workspace("ws", require_compute=False)

    assert caught.value.status == 409
    assert "Connect this installation again" in str(caught.value)
    assert "old-refresh" not in str(caught.value)
    assert len(posts) == 1
    # The initial rotated-session write and the best-effort persisted retirement both
    # failed; the in-process fence must still stop a second POST with the spent value.
    with pytest.raises(cloud_session.CloudSessionError):
        cloud_session.access_for_workspace("ws", require_compute=False)
    assert len(posts) == 1
    assert len(saves) == 2
