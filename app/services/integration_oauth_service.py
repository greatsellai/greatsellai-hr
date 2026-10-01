"""OAuth 2.1 public-client AS using Authlib validation and the shared grant chain.

No network fetches, client secrets, implicit/password grants, JWTs or CIMD.
Authlib's default issuance methods log token dictionaries at DEBUG; our overrides
intentionally never call those methods and never log request or token material.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import wraps
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID

from authlib.oauth2.rfc6749 import AuthorizationServer, OAuth2Request
from authlib.oauth2.rfc6749.errors import InvalidGrantError, InvalidRequestError, InvalidScopeError, OAuth2Error
from authlib.oauth2.rfc6749.grants import AuthorizationCodeGrant, RefreshTokenGrant
from authlib.oauth2.rfc6749.requests import BasicOAuth2Payload
from authlib.oauth2.rfc7636 import CodeChallenge, create_s256_code_challenge
from sqlalchemy import and_, case, delete, func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.config import AppSettings
from app.models import (IntegrationCredential, IntegrationGrant, IntegrationOAuthClient,
    IntegrationOAuthCode, IntegrationOAuthConsent, IntegrationOAuthFamily,
    IntegrationOAuthRateBucket, IntegrationOAuthRefresh)
from app.services.identity_service import AuthPrincipal
from app.services.integration_auth_service import (DEFAULT_INTEGRATION_SCOPES, INTEGRATION_SCOPES, IntegrationAccessError,
    aware, ensure_integration_entitlement, integration_features, integration_now, lock_integration_policy,
    record_integration_audit, reload_bound_auth)
from app.services.integration_management_service import _ensure_issuance, integration_csrf_token
from app.services.integration_oauth_retention_service import (
    cleanup_terminal_oauth_families_global,
    cleanup_unused_oauth_clients,
)

OPAQUE_ID = re.compile(r"[A-Za-z0-9_-]{43}\Z")
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache", "Referrer-Policy": "no-referrer"}
OAUTH_CLIENT_LIMIT_PER_USER = 20
OAUTH_CLIENT_LIMIT_PER_WORKSPACE = 200
OAUTH_REGISTRATION_CAPACITY_KEY = "register:capacity"
OAUTH_REGISTRATION_CAPACITY_WINDOW = datetime(1970, 1, 1, tzinfo=timezone.utc)


def audited_transaction(operation):
    @wraps(operation)
    def wrapped(session, *args, **kwargs):
        try:
            return operation(session, *args, **kwargs)
        except (IntegrationAccessError, OAuth2Error):
            session.rollback()
            raise
        except Exception:
            session.rollback()
            raise IntegrationAccessError("oauth_audit_unavailable", 503) from None
    return wrapped


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def oauth_issuer(settings: AppSettings) -> str:
    """Canonical root base for endpoint/resource construction, without '/'."""
    base = (settings.public_app_url or "").rstrip("/")
    parsed = urlsplit(base)
    if (not base or parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password):
        raise IntegrationAccessError("integrations_public_app_url_required", 503)
    return base


def oauth_issuer_identifier(settings: AppSettings) -> str:
    """Match the SDK's AnyHttpUrl root issuer exactly, including its final '/'."""
    return oauth_issuer(settings) + "/"


def require_oauth(settings: AppSettings) -> None:
    if not settings.integrations_enabled or not settings.integrations_oauth_enabled:
        raise IntegrationAccessError("integrations_disabled", 404)
    oauth_issuer(settings)


def resource_audience(resource: str, settings: AppSettings) -> str:
    base = oauth_issuer(settings)
    if resource == base + "/v1/integrations":
        return "rest"
    if resource == base + "/v1/mcp" and settings.integrations_mcp_enabled:
        return "mcp"
    raise InvalidRequestError("Invalid resource.")


def validate_redirect(value: str) -> str:
    """Literal loopback HTTP or HTTPS. Wildcards, fragments and credentials fail."""
    try:
        parsed = urlsplit(value)
        if (not value or len(value) > 2048 or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.fragment or "\\" in value
                or any(ord(char) < 33 or ord(char) == 127 for char in value)
                or any(char in parsed.netloc for char in "*%")
                or parsed.port == 0
                or parsed.scheme not in {"http", "https"}
                or (parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "::1", "localhost"})
                or any(key in {"code", "state", "error", "iss"} for key, _ in parse_qsl(parsed.query))):
            raise ValueError()
    except (ValueError, TypeError):
        raise IntegrationAccessError("oauth_redirect_invalid", 400) from None
    return value


def redirect_matches(registered: str, actual: str) -> bool:
    try:
        validate_redirect(actual)
        if hmac.compare_digest(registered, actual):
            return True
        left, right = urlsplit(registered), urlsplit(actual)
        # Native loopback ports may be ephemeral; every other component is exact.
        return (left.scheme == right.scheme == "http"
            and left.hostname in {"127.0.0.1", "::1", "localhost"}
            and left.hostname == right.hostname and left.path == right.path
            and left.query == right.query and left.fragment == right.fragment
            and left.username == right.username is None and left.password == right.password is None)
    except IntegrationAccessError:
        return False


def oauth_supported_scopes(settings: AppSettings) -> list[str]:
    """Product capabilities to advertise, never a token's required/granted set."""
    return sorted(scope for scope in INTEGRATION_SCOPES
        if settings.integrations_analysis_enabled or not scope.startswith("analyses:"))


def oauth_metadata(settings: AppSettings) -> dict:
    require_oauth(settings)
    base = oauth_issuer(settings)
    return {"issuer": oauth_issuer_identifier(settings), "authorization_endpoint": base + "/v1/oauth/authorize",
        "token_endpoint": base + "/v1/oauth/token", "registration_endpoint": base + "/v1/oauth/register",
        "revocation_endpoint": base + "/v1/oauth/revoke", "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["none"],
        "revocation_endpoint_auth_methods_supported": ["none"],
        "code_challenge_methods_supported": ["S256"],
        "authorization_response_iss_parameter_supported": True,
        "scopes_supported": oauth_supported_scopes(settings)}


def preauth_budget(session: Session, *, settings: AppSettings, operation: str, peer: str) -> None:
    """Atomic DB upserts, independent of application replicas. No forwarded-IP trust."""
    if operation != "revoke":
        require_oauth(settings)
    limits = {"register": (10, 100), "authorize": (30, 300), "token": (120, 1000), "revoke": (60, 600)}
    per_peer, global_limit = limits[operation]
    now = integration_now()
    window = now.replace(second=0, microsecond=0)
    peer_digest = hmac.new(settings.session_signing_secret().encode(), peer.encode(), hashlib.sha256).hexdigest()
    factory = pg_insert if session.bind.dialect.name == "postgresql" else sqlite_insert
    try:
        expired = select(IntegrationOAuthRateBucket.key, IntegrationOAuthRateBucket.window_started_at).where(
            IntegrationOAuthRateBucket.expires_at <= now).order_by(IntegrationOAuthRateBucket.expires_at).limit(1000)
        session.execute(delete(IntegrationOAuthRateBucket).where(tuple_(IntegrationOAuthRateBucket.key,
            IntegrationOAuthRateBucket.window_started_at).in_(expired)).execution_options(synchronize_session=False))
        for suffix, limit in ((peer_digest, per_peer), ("global", global_limit)):
            key = f"{operation}:{suffix}"
            stmt = factory(IntegrationOAuthRateBucket).values(key=key, window_started_at=window,
                request_count=1, expires_at=window + timedelta(minutes=2))
            stmt = stmt.on_conflict_do_update(index_elements=["key", "window_started_at"],
                set_={"request_count": IntegrationOAuthRateBucket.request_count + 1},
                where=IntegrationOAuthRateBucket.request_count < limit).returning(IntegrationOAuthRateBucket.request_count)
            if session.scalar(stmt) is None:
                raise IntegrationAccessError("oauth_rate_limited", 429, retry_after=60)
        session.commit()
    except Exception:
        session.rollback()
        raise


def register_public_client(session: Session, payload: dict) -> dict:
    allowed = {"client_name", "redirect_uris", "token_endpoint_auth_method", "grant_types", "response_types", "scope", "application_type"}
    if set(payload) - allowed or payload.get("token_endpoint_auth_method", "none") != "none":
        raise IntegrationAccessError("invalid_client_metadata", 400)
    name = payload.get("client_name", "External MCP client")
    redirects = payload.get("redirect_uris")
    if (not isinstance(name, str) or not 1 <= len(name.strip()) <= 80
            or any(ord(char) < 32 or ord(char) == 127 for char in name)
            or not isinstance(redirects, list) or not 1 <= len(redirects) <= 5
            or not all(isinstance(value, str) for value in redirects)
            or payload.get("application_type", "native") not in {"native", "web"}
            or payload.get("grant_types", ["authorization_code", "refresh_token"]) != ["authorization_code", "refresh_token"]
            or payload.get("response_types", ["code"]) != ["code"]):
        raise IntegrationAccessError("invalid_client_metadata", 400)
    if "scope" in payload and (not isinstance(payload["scope"], str) or not set(payload["scope"].split()) <= INTEGRATION_SCOPES):
        raise IntegrationAccessError("invalid_client_metadata", 400)
    redirects = list(dict.fromkeys(validate_redirect(value) for value in redirects))
    _lock_registration_capacity(session)
    client_count = session.scalar(select(func.count()).select_from(IntegrationOAuthClient)) or 0
    if client_count >= 10000:
        # Expensive cross-workspace cleanup is only needed at the hard pool
        # boundary. Keep the lock order policy -> consent/client consistent
        # with browser consent; commit maintenance independently so a capacity
        # response does not roll back reclamation.
        cleanup_terminal_oauth_families_global(session)
        cleanup_unused_oauth_clients(session)
        session.commit()
        _lock_registration_capacity(session)
        client_count = session.scalar(select(func.count()).select_from(IntegrationOAuthClient)) or 0
    if client_count >= 10000:
        raise IntegrationAccessError("oauth_registration_capacity_reached", 429, retry_after=60)
    client = IntegrationOAuthClient(name=name.strip(), redirect_uris=redirects)
    session.add(client)
    session.flush()
    response = {"client_id": client.id, "client_name": client.name, "redirect_uris": client.redirect_uris,
        "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"], "client_id_issued_at": int(aware(client.created_at).timestamp())}
    session.commit()
    return response


def _lock_registration_capacity(session: Session) -> None:
    """Serialize global count/insert admission across replicas and minutes."""
    factory = pg_insert if session.bind.dialect.name == "postgresql" else sqlite_insert
    row = factory(IntegrationOAuthRateBucket).values(
        key=OAUTH_REGISTRATION_CAPACITY_KEY,
        window_started_at=OAUTH_REGISTRATION_CAPACITY_WINDOW,
        request_count=0,
        expires_at=datetime(9999, 12, 31, tzinfo=timezone.utc),
    ).on_conflict_do_update(
        index_elements=["key", "window_started_at"],
        set_={"request_count": IntegrationOAuthRateBucket.request_count},
    )
    session.execute(row)
    session.scalar(select(IntegrationOAuthRateBucket.request_count).where(
        IntegrationOAuthRateBucket.key == OAUTH_REGISTRATION_CAPACITY_KEY,
        IntegrationOAuthRateBucket.window_started_at == OAUTH_REGISTRATION_CAPACITY_WINDOW,
    ).with_for_update())


class PublicClient:
    def __init__(self, model):
        self.model = model
        self.client_id = model.id

    def get_client_id(self):
        return self.client_id

    def get_default_redirect_uri(self):
        return self.model.redirect_uris[0]

    def check_redirect_uri(self, value):
        return any(redirect_matches(uri, value) for uri in self.model.redirect_uris)

    def get_allowed_scope(self, scope):
        return scope

    def check_client_secret(self, value):
        return False

    def check_endpoint_auth_method(self, method, endpoint):
        return method == "none"

    def check_response_type(self, response_type):
        return response_type == "code"

    def check_grant_type(self, grant_type):
        return grant_type in {"authorization_code", "refresh_token"}


class ParsedOAuthRequest(OAuth2Request):
    def __init__(self, method: str, path: str, data: dict[str, str]):
        # Canonical transport is checked by configuration/proxy, not attacker Host.
        # The parser has already rejected duplicate parameters and oversized bodies.
        super().__init__(method, "https://oauth-adapter.invalid" + path)
        self.payload = BasicOAuth2Payload(data)

    @property
    def args(self):
        return self.payload.data if self.method == "GET" else {}

    @property
    def form(self):
        return self.payload.data if self.method == "POST" else {}


class S256Only(CodeChallenge):
    SUPPORTED_CODE_CHALLENGE_METHOD = ["S256"]
    CODE_CHALLENGE_METHODS = {"S256": lambda verifier, challenge: hmac.compare_digest(create_s256_code_challenge(verifier), challenge)}

    def validate_code_challenge(self, grant, redirect_uri):
        data = grant.request.payload.data
        if data.get("code_challenge_method") != "S256" or not OPAQUE_ID.fullmatch(data.get("code_challenge", "")):
            raise InvalidRequestError("PKCE S256 is required.")
        return super().validate_code_challenge(grant, redirect_uri)


@dataclass
class RefreshHandle:
    row: IntegrationOAuthRefresh
    family: IntegrationOAuthFamily
    grant: IntegrationGrant

    def check_client(self, client):
        return self.family.client_id == client.client_id

    def get_scope(self):
        return " ".join(self.grant.scopes)


class CodeGrant(AuthorizationCodeGrant):
    TOKEN_ENDPOINT_AUTH_METHODS = ["none"]

    def save_authorization_code(self, code, request):
        self.server.save_code(code, request)

    def query_authorization_code(self, code, client):
        row = self.server.lookup_bound(IntegrationOAuthCode, "code_digest", code, client)
        if row is None or (row.consumed_at is None and integration_now() >= aware(row.expires_at)):
            return None
        return row

    def authenticate_user(self, code):
        return self.server.auth

    def delete_authorization_code(self, code):
        code.consumed_at = integration_now()

    def create_token_response(self):
        # Do not invoke Authlib's base implementation: it DEBUG-logs raw tokens.
        code = self.request.authorization_code
        if self.request.payload.data.get("resource") != self.server.family.resource:
            raise InvalidGrantError()
        if code.consumed_at is not None:
            self.server.revoke_family("oauth.code_replay")
            raise InvalidGrantError()
        token = self.server.issue(self.request, "oauth.authorized")
        self.delete_authorization_code(code)
        return 200, token, self.TOKEN_RESPONSE_HEADER


class RotatingRefreshGrant(RefreshTokenGrant):
    TOKEN_ENDPOINT_AUTH_METHODS = ["none"]
    INCLUDE_NEW_REFRESH_TOKEN = True

    def authenticate_refresh_token(self, token):
        row = self.server.lookup_bound(IntegrationOAuthRefresh, "token_digest", token, self.request.client)
        if row is None:
            return None
        return RefreshHandle(row, self.server.family, self.server.grant)

    def validate_token_request(self):
        super().validate_token_request()
        requested = self.request.payload.scope
        if requested is not None and set(requested.split()) != set(self.server.grant.scopes):
            # Credentials deliberately share an immutable grant scope ceiling.
            raise InvalidScopeError()

    def create_token_response(self):
        refresh = self.request.refresh_token.row
        if self.request.payload.data.get("resource") != self.server.family.resource:
            raise InvalidGrantError()
        if refresh.consumed_at is not None:
            self.server.revoke_family("oauth.refresh_replay")
            raise InvalidGrantError()
        token = self.server.issue(self.request, "oauth.refreshed")
        refresh.consumed_at = integration_now()
        return 200, token, self.TOKEN_RESPONSE_HEADER


class IntegrationAuthorizationServer(AuthorizationServer):
    """A per-request adapter; no DB sessions or principals in process globals."""
    def __init__(self, session: Session, settings: AppSettings):
        super().__init__(scopes_supported=sorted(INTEGRATION_SCOPES))
        self.session, self.settings = session, settings
        self.family = self.grant = self.auth = self.consent = None
        self.security_state_changed = False
        self.register_grant(CodeGrant, [S256Only(required=True)])
        self.register_grant(RotatingRefreshGrant)

    def query_client(self, client_id):
        client = self.session.get(IntegrationOAuthClient, client_id, populate_existing=True)
        return PublicClient(client) if client and client.disabled_at is None else None

    def create_oauth2_request(self, request):
        return request

    def send_signal(self, name, *args, **kwargs):
        pass

    def handle_response(self, status, body, headers):
        headers = dict(headers)
        if "Location" in headers:
            parsed = urlsplit(headers["Location"])
            params = parse_qsl(parsed.query, keep_blank_values=True)
            params.append(("iss", oauth_issuer_identifier(self.settings)))
            headers["Location"] = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(params), ""))
        return status, body, headers

    def handle_error_response(self, request, error):
        # Do not echo untrusted request values in error descriptions/logs.
        error.description = "Authorization request failed."
        return super().handle_error_response(request, error)

    def save_code(self, raw_code, request):
        auth, consent = self.auth, self.consent
        now = integration_now()
        grant = IntegrationGrant(organization_id=auth.organization_id, user_id=auth.user.id,
            membership_id=auth.membership.id, audience=consent.audience, name=request.client.model.name,
            kind="oauth", scopes=consent.scopes, created_at=now, updated_at=now)
        self.session.add(grant)
        self.session.flush()
        family = IntegrationOAuthFamily(organization_id=auth.organization_id, grant_id=grant.id,
            client_id=consent.client_id, resource=consent.resource,
            auth_session_version=auth.user.auth_session_version, expires_at=now + timedelta(days=30))
        self.session.add(family)
        self.session.flush()
        # Preserve a finite post-authorization recovery window for client IDs
        # cached by desktop MCP hosts. Use an atomic maximum for approvals that
        # share the same public client across workspaces.
        self.session.execute(update(IntegrationOAuthClient).where(
            IntegrationOAuthClient.id == consent.client_id,
        ).values(last_authorized_at=case(
            (IntegrationOAuthClient.last_authorized_at.is_(None), now),
            (IntegrationOAuthClient.last_authorized_at < now, now),
            else_=IntegrationOAuthClient.last_authorized_at,
        )))
        self.session.add(IntegrationOAuthCode(organization_id=auth.organization_id, family_id=family.id,
            code_digest=digest(raw_code), redirect_uri=consent.redirect_uri, code_challenge=consent.code_challenge,
            scopes=consent.scopes, expires_at=now + timedelta(minutes=5)))
        record_integration_audit(self.session, organization_id=auth.organization_id, actor_user_id=auth.user.id,
            grant_id=grant.id, action="oauth.consent_approved", resource_type="grant", resource_id=grant.id, resource_count=1)

    def lookup_bound(self, model, digest_column, raw, client):
        if not isinstance(raw, str) or not 32 <= len(raw) <= 256:
            return None
        table = model.__table__
        binding = self.session.execute(select(table.c.id, table.c.family_id, table.c.organization_id).where(
            table.c[digest_column] == digest(raw))).first()
        if binding is None:
            return None
        family_table, grant_table = IntegrationOAuthFamily.__table__, IntegrationGrant.__table__
        family_row = self.session.execute(select(family_table).where(family_table.c.id == binding.family_id,
            family_table.c.organization_id == binding.organization_id)).mappings().first()
        if family_row is None or family_row["client_id"] != client.client_id:
            return None
        grant_row = self.session.execute(select(grant_table).where(grant_table.c.id == family_row["grant_id"],
            grant_table.c.organization_id == binding.organization_id)).mappings().first()
        if grant_row is None:
            return None
        self.auth = reload_bound_auth(self.session, organization_id=binding.organization_id,
            user_id=grant_row["user_id"], membership_id=grant_row["membership_id"],
            auth_session_version=family_row["auth_session_version"], lock=True)
        policy = lock_integration_policy(self.session, binding.organization_id)
        self.family = self.session.get(IntegrationOAuthFamily, binding.family_id, populate_existing=True)
        self.grant = self.session.get(IntegrationGrant, family_row["grant_id"], populate_existing=True)
        if self.family is None or self.grant is None:
            # A maintenance transaction may have removed this terminal family
            # while this request waited for the shared workspace policy lock.
            return None
        now = integration_now()
        if (self.family.revoked_at is not None or self.grant.revoked_at is not None
                or self.grant.kind != "oauth" or now >= aware(self.family.expires_at)
                or not integration_features(self.settings, binding.organization_id)["oauth"]):
            return None
        _ensure_issuance(self.auth, policy, settings=self.settings, audience=self.grant.audience, scopes=self.grant.scopes, now=now)
        return self.session.get(model, binding.id, populate_existing=True)

    def issue(self, request, action):
        if request.payload.data.get("resource") != self.family.resource:
            raise InvalidGrantError()
        now = integration_now()
        if now >= aware(self.family.expires_at):
            raise InvalidGrantError()
        access = f"gs_oat_{secrets.token_hex(8)}_{secrets.token_urlsafe(32)}"
        refresh = secrets.token_urlsafe(48)
        expires_at = min(now + timedelta(minutes=15), aware(self.family.expires_at))
        credential = IntegrationCredential(organization_id=self.auth.organization_id, grant_id=self.grant.id,
            token_digest=digest(access), token_prefix=access[:23], kind="oauth",
            auth_session_version=self.auth.user.auth_session_version, expires_at=expires_at, created_at=now)
        self.session.add(credential)
        self.session.add(IntegrationOAuthRefresh(organization_id=self.auth.organization_id,
            family_id=self.family.id, token_digest=digest(refresh), created_at=now))
        self.session.flush()
        record_integration_audit(self.session, organization_id=self.auth.organization_id, actor_user_id=self.auth.user.id,
            grant_id=self.grant.id, credential_id=credential.id, action=action, resource_type="grant", resource_id=self.grant.id, resource_count=1)
        return {"access_token": access, "token_type": "Bearer", "expires_in": max(1, int((expires_at - now).total_seconds())),
            "refresh_token": refresh, "scope": " ".join(self.grant.scopes)}

    def revoke_family(self, action):
        now = integration_now()
        self.family.revoked_at = self.grant.revoked_at = now
        self.session.execute(update(IntegrationCredential).where(IntegrationCredential.organization_id == self.auth.organization_id,
            IntegrationCredential.grant_id == self.grant.id).values(revoked_at=now))
        record_integration_audit(self.session, organization_id=self.auth.organization_id, actor_user_id=self.auth.user.id,
            grant_id=self.grant.id, action=action, resource_type="grant", resource_id=self.grant.id, resource_count=1)
        self.security_state_changed = True


def start_consent(session: Session, *, settings: AppSettings, data: dict[str, str]) -> str:
    require_oauth(settings)
    if (set(data) - {"client_id", "redirect_uri", "response_type", "scope", "state", "code_challenge", "code_challenge_method", "resource"}
            or not data.get("redirect_uri") or len(data.get("state", "")) > 1024):
        raise InvalidRequestError()
    # RFC 6749 section 3.3 permits a predefined default only when omitted.
    # Optional evidence/analysis permissions always require an explicit request.
    data = {"scope": " ".join(sorted(DEFAULT_INTEGRATION_SCOPES)), **data}
    if not isinstance(data["scope"], str) or not data["scope"].split():
        raise InvalidScopeError()
    audience = resource_audience(data.get("resource", ""), settings)
    try:
        if str(UUID(data.get("client_id", ""))) != data.get("client_id"):
            raise ValueError()
    except (ValueError, TypeError):
        # Prevent untrusted strings from entering Authlib's client-id debug log.
        raise InvalidRequestError("Invalid client.") from None
    # Serialize this first consent with abandoned-registration reclamation.
    # Do not add a client-first lock to token/approval paths with other lock orders.
    session.execute(select(IntegrationOAuthClient.id).where(
        IntegrationOAuthClient.id == data["client_id"]).with_for_update()).scalar_one_or_none()
    server = IntegrationAuthorizationServer(session, settings)
    server.get_consent_grant(ParsedOAuthRequest("GET", "/v1/oauth/authorize", data))
    now, raw = integration_now(), secrets.token_urlsafe(32)
    expired = select(IntegrationOAuthConsent.id).where(IntegrationOAuthConsent.expires_at < now - timedelta(hours=24)).order_by(IntegrationOAuthConsent.expires_at).limit(1000)
    session.execute(delete(IntegrationOAuthConsent).where(IntegrationOAuthConsent.id.in_(expired)).execution_options(synchronize_session=False))
    session.add(IntegrationOAuthConsent(request_digest=digest(raw), client_id=data["client_id"],
        redirect_uri=data["redirect_uri"], resource=data["resource"], audience=audience, scopes=sorted(set(data["scope"].split())),
        state=data.get("state"), code_challenge=data["code_challenge"], expires_at=now + timedelta(minutes=10)))
    session.commit()
    return oauth_issuer(settings) + "/settings/integrations/authorize?request_id=" + raw


def _claim_consent(session, auth, *, settings, values, request_id, claim):
    require_oauth(settings)
    if not OPAQUE_ID.fullmatch(request_id):
        raise IntegrationAccessError("oauth_consent_not_found", 404)
    auth = reload_bound_auth(session, organization_id=auth.organization_id, user_id=auth.user.id,
        membership_id=auth.membership.id, auth_session_version=auth.user.auth_session_version, lock=True)
    policy = lock_integration_policy(session, auth.organization_id)
    consent = session.scalar(select(IntegrationOAuthConsent).where(IntegrationOAuthConsent.request_digest == digest(request_id))
        .with_for_update().execution_options(populate_existing=True))
    if consent is None or consent.consumed_at is not None or integration_now() >= aware(consent.expires_at):
        raise IntegrationAccessError("oauth_consent_expired", 410)
    csrf = integration_csrf_token(settings, auth, values)
    if consent.user_id is None and claim:
        consent.organization_id, consent.user_id, consent.membership_id = auth.organization_id, auth.user.id, auth.membership.id
        consent.auth_session_version, consent.session_digest = auth.user.auth_session_version, digest(csrf)
    if (consent.organization_id != auth.organization_id or consent.user_id != auth.user.id
            or consent.membership_id != auth.membership.id or consent.auth_session_version != auth.user.auth_session_version
            or consent.session_digest != digest(csrf)):
        raise IntegrationAccessError("oauth_consent_session_mismatch", 403)
    features = integration_features(settings, auth.organization_id)
    if (not features["oauth"] or not features["api"] or not policy.enabled
            or (consent.audience == "mcp" and not features["mcp"])):
        raise IntegrationAccessError("integrations_disabled", 403)
    ensure_integration_entitlement(auth, now=integration_now())
    if consent.audience not in {"rest", "mcp"} or not consent.scopes or not set(consent.scopes) <= INTEGRATION_SCOPES:
        raise IntegrationAccessError("integration_scope_forbidden", 403)
    # A public client's requested ceiling can exceed this user's policy. Keep
    # displaying the request, but make only the current intersection selectable.
    available = sorted(set(consent.scopes) & set(policy.allowed_scopes) & set(oauth_supported_scopes(settings)))
    client = session.get(IntegrationOAuthClient, consent.client_id, populate_existing=True)
    if client is None or client.disabled_at is not None or not any(redirect_matches(uri, consent.redirect_uri) for uri in client.redirect_uris):
        raise IntegrationAccessError("oauth_client_unavailable", 403)
    return auth, consent, client, csrf, policy, available


def ensure_oauth_client_capacity(
    session: Session, *, organization_id: str, user_id: str, client_id: str,
) -> None:
    """Limit retained public-client authorizations before creating a family.

    A reused client already occupying a workspace/user slot remains usable for
    normal reauthorization. Counts include terminal families until retention
    cleanup reclaims them, so revoke/reapprove churn cannot immediately recycle
    capacity.
    """
    family = IntegrationOAuthFamily.__table__
    grant = IntegrationGrant.__table__
    binding = and_(grant.c.id == family.c.grant_id,
        grant.c.organization_id == family.c.organization_id)
    existing = session.scalar(select(family.c.id).select_from(
        family.join(grant, binding)).where(
            family.c.organization_id == organization_id,
            grant.c.user_id == user_id,
            family.c.client_id == client_id,
        ).limit(1))
    if existing is not None:
        return
    client_in_workspace = session.scalar(select(family.c.id).select_from(
        family.join(grant, binding)).where(
            family.c.organization_id == organization_id,
            family.c.client_id == client_id,
        ).limit(1)) is not None
    workspace_count = session.scalar(select(func.count(func.distinct(family.c.client_id)))
        .select_from(family.join(grant, binding))
        .where(family.c.organization_id == organization_id)) or 0
    user_clients = select(family.c.client_id).select_from(
        family.join(grant, binding),
    ).where(grant.c.user_id == user_id).distinct().subquery()
    user_count = session.scalar(select(func.count()).select_from(user_clients)) or 0
    user_has_client = session.scalar(select(family.c.id).select_from(
        family.join(grant, binding),
    ).where(
        grant.c.user_id == user_id,
        family.c.client_id == client_id,
    ).limit(1)) is not None
    if ((user_count >= OAUTH_CLIENT_LIMIT_PER_USER and not user_has_client)
            or (workspace_count >= OAUTH_CLIENT_LIMIT_PER_WORKSPACE and not client_in_workspace)):
        raise IntegrationAccessError("oauth_connection_limit_reached", 429, retry_after=60)


@audited_transaction
def consent_view(session, auth, *, settings, values, request_id):
    auth, consent, client, csrf, _, available = _claim_consent(session, auth, settings=settings, values=values, request_id=request_id, claim=True)
    redirect = urlsplit(consent.redirect_uri)
    response = {"request_id": request_id, "client": {"name": client.name, "redirect_origin": f"{redirect.scheme}://{redirect.netloc}"},
        "workspace": {"organization_id": auth.organization_id, "name": auth.organization.name},
        "audience": consent.audience, "scopes": consent.scopes, "available_scopes": available,
        "default_scopes": sorted(set(available) & DEFAULT_INTEGRATION_SCOPES),
        "expires_at": aware(consent.expires_at), "csrf_token": csrf}
    record_integration_audit(session, organization_id=auth.organization_id, actor_user_id=auth.user.id,
        action="oauth.consent_viewed", resource_type="oauth_consent", resource_id=consent.id, resource_count=1)
    session.commit()
    return response


@audited_transaction
def decide_consent(session, auth, *, settings, values, request_id, approve, approved_scopes=None):
    auth, consent, _, _, policy, available = _claim_consent(session, auth, settings=settings, values=values, request_id=request_id, claim=False)
    if approve:
        selected = sorted(set(available) & DEFAULT_INTEGRATION_SCOPES) if approved_scopes is None else approved_scopes
        if (not isinstance(selected, list) or not selected or not all(isinstance(scope, str) for scope in selected)
                or len(selected) != len(set(selected)) or not set(selected) <= set(available)):
            raise IntegrationAccessError("integration_scope_forbidden", 403)
        _ensure_issuance(auth, policy, settings=settings, audience=consent.audience, scopes=selected, now=integration_now())
        ensure_oauth_client_capacity(
            session, organization_id=auth.organization_id,
            user_id=auth.user.id, client_id=consent.client_id,
        )
        # Persist only the explicit selection after checking the fresh locked
        # policy. save_code() binds both the authorization code and grant to it.
        consent.scopes = sorted(selected)
    server = IntegrationAuthorizationServer(session, settings)
    server.auth, server.consent = auth, consent
    data = {"client_id": consent.client_id, "redirect_uri": consent.redirect_uri, "response_type": "code",
        "scope": " ".join(consent.scopes), "resource": consent.resource,
        "code_challenge": consent.code_challenge, "code_challenge_method": "S256"}
    if consent.state is not None:
        data["state"] = consent.state
    request = ParsedOAuthRequest("GET", "/v1/oauth/authorize", data)
    grant = server.get_consent_grant(request)
    status, body, headers = server.create_authorization_response(request, grant_user=auth if approve else None, grant=grant)
    if status != 302 or "Location" not in headers:
        raise IntegrationAccessError("oauth_authorization_failed", 400)
    consent.consumed_at = integration_now()
    if not approve:
        record_integration_audit(session, organization_id=auth.organization_id, actor_user_id=auth.user.id,
            action="oauth.consent_denied", resource_type="oauth_consent", resource_id=consent.id, resource_count=1)
    session.commit()
    return {"redirect_url": headers["Location"]}


@audited_transaction
def exchange_token(session, *, settings, data):
    require_oauth(settings)
    if set(data) - {"grant_type", "client_id", "code", "redirect_uri", "code_verifier", "refresh_token", "resource", "scope"}:
        raise InvalidRequestError()
    resource_audience(data.get("resource", ""), settings)
    server = IntegrationAuthorizationServer(session, settings)
    try:
        status, body, headers = server.create_token_response(ParsedOAuthRequest("POST", "/v1/oauth/token", data))
        if status == 200 or server.security_state_changed:
            session.commit()
        else:
            session.rollback()
        return status, body, headers
    except IntegrationAccessError as exc:
        session.rollback()
        if exc.status_code in {401, 402, 403}:
            raise InvalidGrantError() from None
        raise
    except Exception:
        session.rollback()
        raise


@audited_transaction
def revoke_oauth_token(session, *, settings, data):
    """RFC7009 non-enumerating revocation; disabled services still permit revoke."""
    if set(data) - {"token", "client_id", "token_type_hint"} or not data.get("client_id") or not data.get("token"):
        raise InvalidRequestError()
    server = IntegrationAuthorizationServer(session, settings)
    client = server.query_client(data["client_id"])
    if client is None:
        return
    raw = data["token"]
    refresh_table = IntegrationOAuthRefresh.__table__
    family_table = IntegrationOAuthFamily.__table__
    family_id = session.scalar(select(refresh_table.c.family_id).where(refresh_table.c.token_digest == digest(raw)))
    if family_id is None:
        credential = IntegrationCredential.__table__
        family_id = session.scalar(select(family_table.c.id).join(credential,
            (credential.c.grant_id == family_table.c.grant_id) & (credential.c.organization_id == family_table.c.organization_id))
            .where(credential.c.token_digest == digest(raw), credential.c.kind == "oauth"))
    if family_id is None:
        return
    binding = session.execute(select(family_table).where(family_table.c.id == family_id,
        family_table.c.client_id == client.client_id)).mappings().first()
    if binding is None:
        return
    grants = IntegrationGrant.__table__
    grant = session.execute(select(grants).where(grants.c.id == binding["grant_id"],
        grants.c.organization_id == binding["organization_id"])).mappings().first()
    # Retention can remove a terminal family/grant after the raw token lookup
    # and before this owner lookup. Revocation is deliberately non-enumerating.
    if grant is None:
        return
    # Revocation is also possible after account expiry. Browser revocation is the
    # fallback after logout/reset, where old session versions may no longer load.
    try:
        server.auth = reload_bound_auth(session, organization_id=binding["organization_id"],
            user_id=grant["user_id"], membership_id=grant["membership_id"],
            auth_session_version=binding["auth_session_version"], lock=True)
        lock_integration_policy(session, binding["organization_id"])
    except IntegrationAccessError:
        session.rollback()
        return
    server.family = session.get(IntegrationOAuthFamily, family_id, populate_existing=True)
    server.grant = session.get(IntegrationGrant, binding["grant_id"], populate_existing=True)
    # Retention may have won the policy lock after the raw digest binding was
    # read. RFC 7009 is non-enumerating: a now-absent family remains a success.
    if server.family is None or server.grant is None:
        return
    server.revoke_family("oauth.revoked")
    session.commit()
