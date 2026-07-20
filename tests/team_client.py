"""Temporary test adapter for suites whose setup predates recipient-owned passwords.

It translates only successful admin fixture setup into the real invitation-store flow.
Production endpoints are untouched, and dedicated GA tests exercise the HTTP contract.
"""
from __future__ import annotations

from fastapi.testclient import TestClient as _TestClient


class InvitationTestClient(_TestClient):
    def post(self, url, *args, **kwargs):
        payload = kwargs.get("json") or {}
        if url != "/api/auth/users" or not payload.get("password"):
            return super().post(url, *args, **kwargs)
        state = super().get("/api/auth/state").json()
        admin = state.get("user") or {}
        if admin.get("role") != "admin":
            return super().post(url, *args, **kwargs)
        created = super().post("/api/auth/invitations", json={
            "email": payload.get("email", ""),
            "name": payload.get("name", ""),
            "role": payload.get("role", "member"),
        })
        if created.status_code != 200:
            return created

        # Production intentionally never returns a raw invitation token. The adapter
        # retrieves one through the store solely to bridge legacy fixtures, then drives
        # the real public acceptance endpoint with a separate recipient client. Thus
        # middleware, role, seat, password, cookie, and one-time-token behavior remain
        # production behavior; only email delivery is bypassed in these old fixtures.
        invitation = self.app.state.auth_store.resend_invitation(
            created.json()["invitation"]["id"])
        recipient = _TestClient(self.app)
        return recipient.post("/api/auth/invitations/accept", json={
            "token": invitation["token"], "password": payload["password"]})
