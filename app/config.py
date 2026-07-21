from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from email.utils import parseaddr
from ipaddress import ip_network
from pathlib import Path

from cryptography.fernet import Fernet


logger = logging.getLogger(__name__)


def _environment_flag(name: str, *, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean environment flag")


@dataclass(frozen=True)
class AppSettings:
    project_dir: Path
    data_dir: Path
    upload_dir: Path
    database_url: str
    environment: str = "development"
    auto_create_schema: bool = True
    seed_registry_on_startup: bool = True
    database_pool_size: int = 5
    database_max_overflow: int = 10
    admin_token: str | None = field(default=None, repr=False)
    # Pre-tenant installations authenticated a shared legacy workspace with
    # one static token.  New deployments must keep this bridge disabled: it
    # has no human identity and cannot provide account-level attribution.
    legacy_admin_token_enabled: bool = False
    session_secret: str | None = field(default=None, repr=False)
    session_cookie_secure: bool = False
    allow_unauthenticated: bool = False
    deepseek_api_key: str | None = field(default=None, repr=False)
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_timeout_seconds: int = 90
    # One-time compatibility bridge for installations created before the AI
    # gateway. It is used only to create an initial platform route when none
    # exists; all later feature calls resolve their endpoint/model from DB.
    legacy_openai_compatible_endpoint: str = "https://api.deepseek.com/beta/chat/completions"
    # The provider/model used by business features is now resolved from the
    # database-backed AI route policy.  Credentials remain outside that
    # control plane: this map is loaded only from the server environment and
    # maps a non-secret reference (stored in the DB) to its actual API secret.
    ai_provider_credentials: dict[str, str] = field(default_factory=dict, repr=False)
    ai_extraction_job_max_attempts: int = 3
    ai_extraction_job_lease_seconds: int = 180
    ai_extraction_worker_poll_seconds: float = 2.0
    mailbox_sync_interval_seconds: float = 600.0
    # The scheduler checks a workspace at this cadence for expired short-lived
    # mail body/attachment cache entries. Candidate resume originals are not
    # part of this cleanup path.
    mailbox_retention_cleanup_interval_seconds: float = 3600.0
    # Candidate data has a deliberately separate lifecycle from transient
    # mailbox replicas.  Defaults are conservative and do not enable any
    # automatic deletion until a workspace administrator opts in.
    candidate_data_recovery_days: int = 7
    candidate_data_file_access_ttl_seconds: int = 300
    candidate_data_export_ttl_seconds: int = 24 * 60 * 60
    candidate_data_lifecycle_cleanup_interval_seconds: float = 3600.0
    candidate_data_purge_lease_seconds: int = 180
    candidate_data_export_lease_seconds: int = 300
    candidate_data_export_max_items: int = 1000
    candidate_data_export_max_original_bytes: int = 200 * 1024 * 1024
    # A dedicated server secret for privacy-preserving mailbox tombstones.
    # It is intentionally distinct from an attachment SHA-256 and is never
    # persisted or returned.  Deployments that ingest from mailboxes must set
    # it before deleting a mailbox-derived resume.
    candidate_data_tombstone_secret: str | None = field(default=None, repr=False)
    # This is a per-run message batch, not an unbounded mailbox scan. Each
    # following run resumes older unseen message UIDs after newer ones are
    # recorded, so the worker remains responsive on large mailboxes.
    mailbox_sync_attachment_limit: int = 20
    # IMAP endpoints are deployment-owned infrastructure, never arbitrary
    # destinations supplied by a workspace. The transport resolves these
    # exact names again for every connection and pins the verified address.
    mailbox_imap_allowed_hosts: tuple[str, ...] = ("imap.feishu.cn",)
    mailbox_imap_connect_timeout_seconds: int = 10
    mailbox_imap_max_resolved_addresses: int = 8
    # Every value below bounds one RFC822 message before MIME parsing or
    # document extraction can allocate unbounded process memory.
    mailbox_max_raw_message_bytes: int = 24 * 1024 * 1024
    mailbox_max_header_bytes: int = 128 * 1024
    mailbox_max_mime_parts: int = 64
    mailbox_max_mime_depth: int = 16
    mailbox_max_attachments_per_message: int = 5
    mailbox_max_search_response_bytes: int = 1024 * 1024
    mailbox_max_body_cache_bytes: int = 256 * 1024
    # A mailbox incident opens only after this many terminal sync failures in
    # the configured window. Configuration/security failures are escalated
    # immediately by the worker-owned alert service.
    mailbox_consecutive_failure_alert_threshold: int = 3
    mailbox_consecutive_failure_window_seconds: int = 60 * 60
    email_credentials_key: str | None = field(default=None, repr=False)
    # Account verification and password recovery use a transactional sender.
    # This is deliberately separate from the IMAP credentials used to ingest
    # resumes from a mailbox.
    transactional_email_provider: str = "disabled"
    transactional_email_from: str | None = None
    public_app_url: str | None = None
    tencent_ses_region: str = "ap-guangzhou"
    tencent_ses_verification_template_id: int | None = None
    # Password reset mail uses its own action and therefore its own approved
    # Tencent SES template. SMTP providers render the reset message directly.
    tencent_ses_password_reset_template_id: int | None = None
    # Temporary transactional sender backed by a dedicated Feishu public
    # mailbox.  Its app-specific SMTP password must remain environment-only.
    feishu_smtp_host: str = "smtp.feishu.cn"
    feishu_smtp_port: int = 465
    feishu_smtp_tls_mode: str = "ssl"
    feishu_smtp_username: str | None = None
    feishu_smtp_password: str | None = field(default=None, repr=False)
    feishu_smtp_timeout_seconds: int = 20
    email_verification_ttl_seconds: int = 24 * 60 * 60
    email_verification_resend_cooldown_seconds: int = 60
    email_verification_daily_limit: int = 5
    password_reset_ttl_seconds: int = 60 * 60
    # Every public recovery response waits to a randomized target budget. It
    # masks the extra database/outbox work for registered accounts without
    # blocking the async server event loop. This is a timing-noise control,
    # not a claim of mathematical constant-time behavior under load.
    password_reset_min_response_seconds: float = 0.75
    password_reset_response_jitter_seconds: float = 0.25
    # Public password recovery is intentionally throttled independently from
    # registration. The effective buckets use only client and opaque-email
    # dimensions; no raw address or IP is stored.
    #
    # These two values remain only as source-compatible deprecated settings.
    # A global recovery hard limit is not enforced because a distributed
    # attacker could otherwise deny recovery to every unrelated user.
    password_reset_rate_limit_global_limit: int = 60
    password_reset_rate_limit_global_window_seconds: int = 60 * 60
    password_reset_rate_limit_client_limit: int = 5
    password_reset_rate_limit_client_window_seconds: int = 15 * 60
    password_reset_rate_limit_email_limit: int = 3
    password_reset_rate_limit_email_window_seconds: int = 24 * 60 * 60
    # Login failures use only durable, privacy-preserving per-client and
    # per-client-account throttles. There is deliberately no global login
    # bucket: a public global hard block would let one hostile source deny
    # sign-in service to every unrelated user. Successful sign-ins never
    # consume this failure budget.
    login_rate_limit_client_limit: int = 10
    login_rate_limit_client_window_seconds: int = 15 * 60
    # Kept as EMAIL_* environment names for clear operator input, but the
    # persisted bucket is a trusted-client + normalized-account composite to
    # avoid cross-network account lockouts.
    login_rate_limit_email_limit: int = 8
    login_rate_limit_email_window_seconds: int = 15 * 60
    # Account-keyed progressive backpressure supplements the per-client hard
    # limiter. It is a bounded async delay before scrypt verification, never
    # a permanent account lockout or a global shared bucket.
    login_account_backpressure_window_seconds: int = 60 * 60
    login_account_backpressure_free_failures: int = 3
    login_account_backpressure_base_delay_seconds: float = 0.25
    login_account_backpressure_max_delay_seconds: float = 2.0
    # A password-reset request writes a durable, encrypted email outbox row.
    # The shared worker performs all SMTP/SES I/O after the HTTP response.
    transactional_email_outbox_max_attempts: int = 5
    transactional_email_outbox_lease_seconds: int = 90
    transactional_email_outbox_retry_base_seconds: int = 60
    # Public self-registration has separate global, client, and email limits.
    # The limits are persisted in the database so multiple API replicas share
    # the same budget.  Forwarded client headers are accepted only when the
    # direct peer is explicitly configured as a trusted proxy.
    registration_rate_limit_global_limit: int = 20
    registration_rate_limit_global_window_seconds: int = 60 * 60
    registration_rate_limit_client_limit: int = 3
    registration_rate_limit_client_window_seconds: int = 15 * 60
    registration_rate_limit_email_limit: int = 3
    registration_rate_limit_email_window_seconds: int = 24 * 60 * 60
    trusted_proxy_cidrs: tuple[str, ...] = ()
    max_upload_bytes: int = 15 * 1024 * 1024
    min_text_chars_per_page: int = 80
    tencent_secret_id: str | None = field(default=None, repr=False)
    tencent_secret_key: str | None = field(default=None, repr=False)
    tencent_ocr_region: str = "ap-guangzhou"
    tencent_ocr_timeout_seconds: int = 20
    ocr_sparse_text_chars_per_page: int = 500

    @classmethod
    def from_env(cls) -> "AppSettings":
        # These global public-auth knobs were removed in favor of scoped
        # throttles and account-keyed, capped progressive backpressure. Emit
        # a startup-visible warning rather than silently accepting stale
        # operator configuration.
        for deprecated_name in (
            "RESUME_V3_LOGIN_RATE_LIMIT_GLOBAL_LIMIT",
            "RESUME_V3_LOGIN_RATE_LIMIT_GLOBAL_WINDOW_SECONDS",
            "RESUME_V3_PASSWORD_RESET_RATE_LIMIT_GLOBAL_LIMIT",
            "RESUME_V3_PASSWORD_RESET_RATE_LIMIT_GLOBAL_WINDOW_SECONDS",
        ):
            if os.getenv(deprecated_name) is not None:
                logger.warning(
                    "%s is deprecated and ignored; global public-auth hard "
                    "blocks are intentionally disabled to avoid cross-user denial of service",
                    deprecated_name,
                )
        project_dir = Path(__file__).resolve().parents[1]
        data_dir = Path(os.getenv("RESUME_V3_DATA_DIR", project_dir / "data"))
        database_url = os.getenv(
            "RESUME_V3_DATABASE_URL",
            f"sqlite:///{(data_dir / 'resume_v3.db').as_posix()}",
        )
        environment = os.getenv("RESUME_V3_ENVIRONMENT", "development").strip().lower()
        default_bootstrap = database_url.startswith("sqlite")
        return cls(
            project_dir=project_dir,
            data_dir=data_dir,
            upload_dir=data_dir / "uploads",
            database_url=database_url,
            environment=environment,
            auto_create_schema=_environment_flag(
                "RESUME_V3_AUTO_CREATE_SCHEMA",
                default=default_bootstrap,
            ),
            seed_registry_on_startup=_environment_flag(
                "RESUME_V3_SEED_REGISTRY_ON_STARTUP",
                default=default_bootstrap,
            ),
            database_pool_size=int(os.getenv("RESUME_V3_DATABASE_POOL_SIZE", "5")),
            database_max_overflow=int(
                os.getenv("RESUME_V3_DATABASE_MAX_OVERFLOW", "10")
            ),
            admin_token=os.getenv("RESUME_V3_ADMIN_TOKEN") or None,
            legacy_admin_token_enabled=_environment_flag(
                "RESUME_V3_LEGACY_ADMIN_TOKEN_ENABLED",
                default=False,
            ),
            session_secret=os.getenv("RESUME_V3_SESSION_SECRET") or None,
            session_cookie_secure=_environment_flag(
                "RESUME_V3_SESSION_COOKIE_SECURE",
                default=environment in {"production", "prod"},
            ),
            allow_unauthenticated=os.getenv("RESUME_V3_ALLOW_UNAUTHENTICATED") == "1",
            deepseek_api_key=os.getenv("DEEPSEEK_API_KEY") or None,
            deepseek_model=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
            deepseek_timeout_seconds=int(
                os.getenv("DEEPSEEK_TIMEOUT_SECONDS", "90")
            ),
            legacy_openai_compatible_endpoint=os.getenv(
                "RESUME_V3_LEGACY_OPENAI_COMPATIBLE_ENDPOINT",
                "https://api.deepseek.com/beta/chat/completions",
            ).strip(),
            ai_provider_credentials=_secret_reference_map(
                "RESUME_V3_AI_PROVIDER_CREDENTIALS_JSON"
            ),
            ai_extraction_job_max_attempts=int(
                os.getenv("RESUME_V3_AI_EXTRACTION_JOB_MAX_ATTEMPTS", "3")
            ),
            ai_extraction_job_lease_seconds=int(
                os.getenv("RESUME_V3_AI_EXTRACTION_JOB_LEASE_SECONDS", "180")
            ),
            ai_extraction_worker_poll_seconds=float(
                os.getenv("RESUME_V3_AI_EXTRACTION_WORKER_POLL_SECONDS", "2")
            ),
            mailbox_sync_interval_seconds=float(
                os.getenv("RESUME_V3_MAILBOX_SYNC_INTERVAL_SECONDS", "600")
            ),
            mailbox_retention_cleanup_interval_seconds=float(
                os.getenv("RESUME_V3_MAILBOX_RETENTION_CLEANUP_INTERVAL_SECONDS", "3600")
            ),
            candidate_data_recovery_days=int(
                os.getenv("RESUME_V3_CANDIDATE_DATA_RECOVERY_DAYS", "7")
            ),
            candidate_data_file_access_ttl_seconds=int(
                os.getenv("RESUME_V3_CANDIDATE_DATA_FILE_ACCESS_TTL_SECONDS", "300")
            ),
            candidate_data_export_ttl_seconds=int(
                os.getenv("RESUME_V3_CANDIDATE_DATA_EXPORT_TTL_SECONDS", str(24 * 60 * 60))
            ),
            candidate_data_lifecycle_cleanup_interval_seconds=float(
                os.getenv("RESUME_V3_CANDIDATE_DATA_LIFECYCLE_CLEANUP_INTERVAL_SECONDS", "3600")
            ),
            candidate_data_purge_lease_seconds=int(
                os.getenv("RESUME_V3_CANDIDATE_DATA_PURGE_LEASE_SECONDS", "180")
            ),
            candidate_data_export_lease_seconds=int(
                os.getenv("RESUME_V3_CANDIDATE_DATA_EXPORT_LEASE_SECONDS", "300")
            ),
            candidate_data_export_max_items=int(
                os.getenv("RESUME_V3_CANDIDATE_DATA_EXPORT_MAX_ITEMS", "1000")
            ),
            candidate_data_export_max_original_bytes=int(
                os.getenv("RESUME_V3_CANDIDATE_DATA_EXPORT_MAX_ORIGINAL_BYTES", str(200 * 1024 * 1024))
            ),
            candidate_data_tombstone_secret=os.getenv("RESUME_V3_CANDIDATE_DATA_TOMBSTONE_SECRET") or None,
            mailbox_sync_attachment_limit=int(
                os.getenv("RESUME_V3_MAILBOX_SYNC_ATTACHMENT_LIMIT", "20")
            ),
            mailbox_imap_allowed_hosts=_comma_separated_values(
                os.getenv("RESUME_V3_MAILBOX_IMAP_ALLOWED_HOSTS", "imap.feishu.cn")
            ),
            mailbox_imap_connect_timeout_seconds=int(
                os.getenv("RESUME_V3_MAILBOX_IMAP_CONNECT_TIMEOUT_SECONDS", "10")
            ),
            mailbox_imap_max_resolved_addresses=int(
                os.getenv("RESUME_V3_MAILBOX_IMAP_MAX_RESOLVED_ADDRESSES", "8")
            ),
            mailbox_max_raw_message_bytes=int(
                os.getenv("RESUME_V3_MAILBOX_MAX_RAW_MESSAGE_BYTES", str(24 * 1024 * 1024))
            ),
            mailbox_max_header_bytes=int(
                os.getenv("RESUME_V3_MAILBOX_MAX_HEADER_BYTES", str(128 * 1024))
            ),
            mailbox_max_mime_parts=int(
                os.getenv("RESUME_V3_MAILBOX_MAX_MIME_PARTS", "64")
            ),
            mailbox_max_mime_depth=int(
                os.getenv("RESUME_V3_MAILBOX_MAX_MIME_DEPTH", "16")
            ),
            mailbox_max_attachments_per_message=int(
                os.getenv("RESUME_V3_MAILBOX_MAX_ATTACHMENTS_PER_MESSAGE", "5")
            ),
            mailbox_max_search_response_bytes=int(
                os.getenv("RESUME_V3_MAILBOX_MAX_SEARCH_RESPONSE_BYTES", str(1024 * 1024))
            ),
            mailbox_max_body_cache_bytes=int(
                os.getenv("RESUME_V3_MAILBOX_MAX_BODY_CACHE_BYTES", str(256 * 1024))
            ),
            mailbox_consecutive_failure_alert_threshold=int(
                os.getenv("RESUME_V3_MAILBOX_CONSECUTIVE_FAILURE_ALERT_THRESHOLD", "3")
            ),
            mailbox_consecutive_failure_window_seconds=int(
                os.getenv("RESUME_V3_MAILBOX_CONSECUTIVE_FAILURE_WINDOW_SECONDS", str(60 * 60))
            ),
            email_credentials_key=os.getenv("RESUME_V3_EMAIL_CREDENTIALS_KEY") or None,
            transactional_email_provider=os.getenv(
                "RESUME_V3_TRANSACTIONAL_EMAIL_PROVIDER", "disabled"
            ).strip().lower(),
            transactional_email_from=os.getenv("RESUME_V3_TRANSACTIONAL_EMAIL_FROM") or None,
            public_app_url=os.getenv("RESUME_V3_PUBLIC_APP_URL") or None,
            tencent_ses_region=os.getenv("TENCENT_SES_REGION", "ap-guangzhou"),
            tencent_ses_verification_template_id=_optional_positive_int(
                "TENCENT_SES_VERIFICATION_TEMPLATE_ID"
            ),
            tencent_ses_password_reset_template_id=_optional_positive_int(
                "TENCENT_SES_PASSWORD_RESET_TEMPLATE_ID"
            ),
            feishu_smtp_host=os.getenv("RESUME_V3_FEISHU_SMTP_HOST", "smtp.feishu.cn").strip(),
            feishu_smtp_port=int(os.getenv("RESUME_V3_FEISHU_SMTP_PORT", "465")),
            feishu_smtp_tls_mode=os.getenv("RESUME_V3_FEISHU_SMTP_TLS_MODE", "ssl").strip().lower(),
            feishu_smtp_username=os.getenv("RESUME_V3_FEISHU_SMTP_USERNAME") or None,
            feishu_smtp_password=os.getenv("RESUME_V3_FEISHU_SMTP_PASSWORD") or None,
            feishu_smtp_timeout_seconds=int(
                os.getenv("RESUME_V3_FEISHU_SMTP_TIMEOUT_SECONDS", "20")
            ),
            email_verification_ttl_seconds=int(
                os.getenv("RESUME_V3_EMAIL_VERIFICATION_TTL_SECONDS", str(24 * 60 * 60))
            ),
            email_verification_resend_cooldown_seconds=int(
                os.getenv("RESUME_V3_EMAIL_VERIFICATION_RESEND_COOLDOWN_SECONDS", "60")
            ),
            email_verification_daily_limit=int(
                os.getenv("RESUME_V3_EMAIL_VERIFICATION_DAILY_LIMIT", "5")
            ),
            password_reset_ttl_seconds=int(
                os.getenv("RESUME_V3_PASSWORD_RESET_TTL_SECONDS", str(60 * 60))
            ),
            password_reset_min_response_seconds=float(
                os.getenv("RESUME_V3_PASSWORD_RESET_MIN_RESPONSE_SECONDS", "0.75")
            ),
            password_reset_response_jitter_seconds=float(
                os.getenv("RESUME_V3_PASSWORD_RESET_RESPONSE_JITTER_SECONDS", "0.25")
            ),
            # Deprecated globals are deliberately ignored even when malformed;
            # from_env() emits a startup warning above when either variable is
            # present. Keep the fields only for source-compatible callers.
            password_reset_rate_limit_global_limit=60,
            password_reset_rate_limit_global_window_seconds=60 * 60,
            password_reset_rate_limit_client_limit=int(
                os.getenv("RESUME_V3_PASSWORD_RESET_RATE_LIMIT_CLIENT_LIMIT", "5")
            ),
            password_reset_rate_limit_client_window_seconds=int(
                os.getenv(
                    "RESUME_V3_PASSWORD_RESET_RATE_LIMIT_CLIENT_WINDOW_SECONDS",
                    str(15 * 60),
                )
            ),
            password_reset_rate_limit_email_limit=int(
                os.getenv("RESUME_V3_PASSWORD_RESET_RATE_LIMIT_EMAIL_LIMIT", "3")
            ),
            password_reset_rate_limit_email_window_seconds=int(
                os.getenv(
                    "RESUME_V3_PASSWORD_RESET_RATE_LIMIT_EMAIL_WINDOW_SECONDS",
                    str(24 * 60 * 60),
                )
            ),
            login_rate_limit_client_limit=int(
                os.getenv("RESUME_V3_LOGIN_RATE_LIMIT_CLIENT_LIMIT", "10")
            ),
            login_rate_limit_client_window_seconds=int(
                os.getenv(
                    "RESUME_V3_LOGIN_RATE_LIMIT_CLIENT_WINDOW_SECONDS",
                    str(15 * 60),
                )
            ),
            login_rate_limit_email_limit=int(
                os.getenv("RESUME_V3_LOGIN_RATE_LIMIT_EMAIL_LIMIT", "8")
            ),
            login_rate_limit_email_window_seconds=int(
                os.getenv(
                    "RESUME_V3_LOGIN_RATE_LIMIT_EMAIL_WINDOW_SECONDS",
                    str(15 * 60),
                )
            ),
            login_account_backpressure_window_seconds=int(
                os.getenv(
                    "RESUME_V3_LOGIN_ACCOUNT_BACKPRESSURE_WINDOW_SECONDS",
                    str(60 * 60),
                )
            ),
            login_account_backpressure_free_failures=int(
                os.getenv("RESUME_V3_LOGIN_ACCOUNT_BACKPRESSURE_FREE_FAILURES", "3")
            ),
            login_account_backpressure_base_delay_seconds=float(
                os.getenv(
                    "RESUME_V3_LOGIN_ACCOUNT_BACKPRESSURE_BASE_DELAY_SECONDS", "0.25")
            ),
            login_account_backpressure_max_delay_seconds=float(
                os.getenv(
                    "RESUME_V3_LOGIN_ACCOUNT_BACKPRESSURE_MAX_DELAY_SECONDS", "2.0")
            ),
            transactional_email_outbox_max_attempts=int(
                os.getenv("RESUME_V3_TRANSACTIONAL_EMAIL_OUTBOX_MAX_ATTEMPTS", "5")
            ),
            transactional_email_outbox_lease_seconds=int(
                os.getenv("RESUME_V3_TRANSACTIONAL_EMAIL_OUTBOX_LEASE_SECONDS", "90")
            ),
            transactional_email_outbox_retry_base_seconds=int(
                os.getenv("RESUME_V3_TRANSACTIONAL_EMAIL_OUTBOX_RETRY_BASE_SECONDS", "60")
            ),
            registration_rate_limit_global_limit=int(
                os.getenv("RESUME_V3_REGISTRATION_RATE_LIMIT_GLOBAL_LIMIT", "20")
            ),
            registration_rate_limit_global_window_seconds=int(
                os.getenv(
                    "RESUME_V3_REGISTRATION_RATE_LIMIT_GLOBAL_WINDOW_SECONDS",
                    str(60 * 60),
                )
            ),
            registration_rate_limit_client_limit=int(
                os.getenv("RESUME_V3_REGISTRATION_RATE_LIMIT_CLIENT_LIMIT", "3")
            ),
            registration_rate_limit_client_window_seconds=int(
                os.getenv(
                    "RESUME_V3_REGISTRATION_RATE_LIMIT_CLIENT_WINDOW_SECONDS",
                    str(15 * 60),
                )
            ),
            registration_rate_limit_email_limit=int(
                os.getenv("RESUME_V3_REGISTRATION_RATE_LIMIT_EMAIL_LIMIT", "3")
            ),
            registration_rate_limit_email_window_seconds=int(
                os.getenv(
                    "RESUME_V3_REGISTRATION_RATE_LIMIT_EMAIL_WINDOW_SECONDS",
                    str(24 * 60 * 60),
                )
            ),
            trusted_proxy_cidrs=_comma_separated_values(
                os.getenv("RESUME_V3_TRUSTED_PROXY_CIDRS", "")
            ),
            tencent_secret_id=os.getenv("TENCENT_SECRET_ID") or None,
            tencent_secret_key=os.getenv("TENCENT_SECRET_KEY") or None,
            tencent_ocr_region=os.getenv("TENCENT_OCR_REGION", "ap-guangzhou"),
            tencent_ocr_timeout_seconds=int(
                os.getenv("TENCENT_OCR_TIMEOUT_SECONDS", "20")
            ),
            ocr_sparse_text_chars_per_page=int(
                os.getenv("OCR_SPARSE_TEXT_CHARS_PER_PAGE", "500")
            ),
        )

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.upload_dir.mkdir(parents=True, exist_ok=True)

    def validate_runtime(self) -> None:
        if self.database_pool_size < 1:
            raise ValueError("RESUME_V3_DATABASE_POOL_SIZE must be at least 1")
        if self.database_max_overflow < 0:
            raise ValueError("RESUME_V3_DATABASE_MAX_OVERFLOW must not be negative")
        if self.deepseek_timeout_seconds < 1:
            raise ValueError("DEEPSEEK_TIMEOUT_SECONDS must be at least 1")
        if self.legacy_openai_compatible_endpoint and not self.legacy_openai_compatible_endpoint.startswith(
            "https://"
        ):
            raise ValueError("RESUME_V3_LEGACY_OPENAI_COMPATIBLE_ENDPOINT must be an HTTPS URL")
        if any(not reference or not secret for reference, secret in self.ai_provider_credentials.items()):
            raise ValueError("RESUME_V3_AI_PROVIDER_CREDENTIALS_JSON contains an empty value")
        if bool(self.tencent_secret_id) != bool(self.tencent_secret_key):
            raise ValueError(
                "TENCENT_SECRET_ID and TENCENT_SECRET_KEY must be configured together"
            )
        if self.tencent_ocr_timeout_seconds < 1:
            raise ValueError("TENCENT_OCR_TIMEOUT_SECONDS must be at least 1")
        if self.ocr_sparse_text_chars_per_page < self.min_text_chars_per_page:
            raise ValueError(
                "OCR_SPARSE_TEXT_CHARS_PER_PAGE must be at least "
                "MIN_TEXT_CHARS_PER_PAGE"
            )
        if self.ai_extraction_job_max_attempts < 1:
            raise ValueError(
                "RESUME_V3_AI_EXTRACTION_JOB_MAX_ATTEMPTS must be at least 1"
            )
        if self.mailbox_retention_cleanup_interval_seconds < 60:
            raise ValueError(
                "RESUME_V3_MAILBOX_RETENTION_CLEANUP_INTERVAL_SECONDS must be at least 60"
            )
        if not 1 <= self.candidate_data_recovery_days <= 90:
            raise ValueError("RESUME_V3_CANDIDATE_DATA_RECOVERY_DAYS must be between 1 and 90")
        if not 30 <= self.candidate_data_file_access_ttl_seconds <= 3600:
            raise ValueError(
                "RESUME_V3_CANDIDATE_DATA_FILE_ACCESS_TTL_SECONDS must be between 30 and 3600"
            )
        if not 60 <= self.candidate_data_export_ttl_seconds <= 7 * 24 * 60 * 60:
            raise ValueError(
                "RESUME_V3_CANDIDATE_DATA_EXPORT_TTL_SECONDS must be between 60 and 604800"
            )
        if self.candidate_data_lifecycle_cleanup_interval_seconds < 60:
            raise ValueError(
                "RESUME_V3_CANDIDATE_DATA_LIFECYCLE_CLEANUP_INTERVAL_SECONDS must be at least 60"
            )
        if not 30 <= self.candidate_data_purge_lease_seconds <= 3600:
            raise ValueError(
                "RESUME_V3_CANDIDATE_DATA_PURGE_LEASE_SECONDS must be between 30 and 3600"
            )
        if not 30 <= self.candidate_data_export_lease_seconds <= 3600:
            raise ValueError(
                "RESUME_V3_CANDIDATE_DATA_EXPORT_LEASE_SECONDS must be between 30 and 3600"
            )
        if not 1 <= self.candidate_data_export_max_items <= 10000:
            raise ValueError(
                "RESUME_V3_CANDIDATE_DATA_EXPORT_MAX_ITEMS must be between 1 and 10000"
            )
        if self.candidate_data_export_max_original_bytes < self.max_upload_bytes:
            raise ValueError(
                "RESUME_V3_CANDIDATE_DATA_EXPORT_MAX_ORIGINAL_BYTES must cover one upload"
            )
        if self.ai_extraction_job_lease_seconds < self.deepseek_timeout_seconds + 30:
            raise ValueError(
                "RESUME_V3_AI_EXTRACTION_JOB_LEASE_SECONDS must exceed "
                "DEEPSEEK_TIMEOUT_SECONDS by at least 30 seconds"
            )
        if self.ai_extraction_worker_poll_seconds <= 0:
            raise ValueError(
                "RESUME_V3_AI_EXTRACTION_WORKER_POLL_SECONDS must be positive"
            )
        if self.mailbox_sync_interval_seconds <= 0:
            raise ValueError("RESUME_V3_MAILBOX_SYNC_INTERVAL_SECONDS must be positive")
        if self.mailbox_sync_attachment_limit < 1:
            raise ValueError("RESUME_V3_MAILBOX_SYNC_ATTACHMENT_LIMIT must be at least 1")
        if self.mailbox_sync_attachment_limit > 100:
            raise ValueError("RESUME_V3_MAILBOX_SYNC_ATTACHMENT_LIMIT must not exceed 100")
        if not self.mailbox_imap_allowed_hosts:
            raise ValueError("RESUME_V3_MAILBOX_IMAP_ALLOWED_HOSTS must not be empty")
        if any(
            not host
            or "*" in host
            or "://" in host
            or "/" in host
            or "@" in host
            for host in self.mailbox_imap_allowed_hosts
        ):
            raise ValueError(
                "RESUME_V3_MAILBOX_IMAP_ALLOWED_HOSTS must contain exact host names"
            )
        # Reuse the transport's canonical hostname checks at startup. A bad
        # deployment allowlist must fail closed before a recruiter ever saves
        # a mailbox configuration; DNS is intentionally not queried here.
        from app.services.mailbox_imap_transport import (
            MailboxImapTransportError,
            validate_imap_endpoint,
        )

        try:
            for host in self.mailbox_imap_allowed_hosts:
                validate_imap_endpoint(self, host=host, port=993)
        except MailboxImapTransportError as exc:
            raise ValueError(
                "RESUME_V3_MAILBOX_IMAP_ALLOWED_HOSTS must contain valid exact IMAPS host names"
            ) from exc
        if not 1 <= self.mailbox_imap_connect_timeout_seconds <= 60:
            raise ValueError(
                "RESUME_V3_MAILBOX_IMAP_CONNECT_TIMEOUT_SECONDS must be between 1 and 60"
            )
        if not 1 <= self.mailbox_imap_max_resolved_addresses <= 32:
            raise ValueError(
                "RESUME_V3_MAILBOX_IMAP_MAX_RESOLVED_ADDRESSES must be between 1 and 32"
            )
        if self.mailbox_max_raw_message_bytes < self.max_upload_bytes:
            raise ValueError(
                "RESUME_V3_MAILBOX_MAX_RAW_MESSAGE_BYTES must cover one uploaded attachment"
            )
        if not 1 <= self.mailbox_max_header_bytes <= self.mailbox_max_raw_message_bytes:
            raise ValueError(
                "RESUME_V3_MAILBOX_MAX_HEADER_BYTES must be positive and no larger than the raw message limit"
            )
        if not 1 <= self.mailbox_max_mime_parts <= 1024:
            raise ValueError("RESUME_V3_MAILBOX_MAX_MIME_PARTS must be between 1 and 1024")
        if not 1 <= self.mailbox_max_mime_depth <= 64:
            raise ValueError("RESUME_V3_MAILBOX_MAX_MIME_DEPTH must be between 1 and 64")
        if not 1 <= self.mailbox_max_attachments_per_message <= 100:
            raise ValueError(
                "RESUME_V3_MAILBOX_MAX_ATTACHMENTS_PER_MESSAGE must be between 1 and 100"
            )
        if not 1 <= self.mailbox_max_search_response_bytes <= self.mailbox_max_raw_message_bytes:
            raise ValueError(
                "RESUME_V3_MAILBOX_MAX_SEARCH_RESPONSE_BYTES must be positive and bounded"
            )
        if not 1 <= self.mailbox_max_body_cache_bytes <= self.mailbox_max_raw_message_bytes:
            raise ValueError(
                "RESUME_V3_MAILBOX_MAX_BODY_CACHE_BYTES must be positive and bounded"
            )
        if not 1 <= self.mailbox_consecutive_failure_alert_threshold <= 20:
            raise ValueError(
                "RESUME_V3_MAILBOX_CONSECUTIVE_FAILURE_ALERT_THRESHOLD must be between 1 and 20"
            )
        if self.mailbox_consecutive_failure_window_seconds < 60:
            raise ValueError(
                "RESUME_V3_MAILBOX_CONSECUTIVE_FAILURE_WINDOW_SECONDS must be at least 60"
            )
        if self.transactional_email_provider not in {
            "disabled",
            "tencent_ses",
            "feishu_smtp",
            "test",
        }:
            raise ValueError("RESUME_V3_TRANSACTIONAL_EMAIL_PROVIDER is not supported")
        if self.email_verification_ttl_seconds < 5 * 60:
            raise ValueError("RESUME_V3_EMAIL_VERIFICATION_TTL_SECONDS must be at least 300")
        if self.email_verification_resend_cooldown_seconds < 10:
            raise ValueError(
                "RESUME_V3_EMAIL_VERIFICATION_RESEND_COOLDOWN_SECONDS must be at least 10"
            )
        if self.email_verification_daily_limit < 1:
            raise ValueError("RESUME_V3_EMAIL_VERIFICATION_DAILY_LIMIT must be at least 1")
        if self.password_reset_ttl_seconds < 5 * 60:
            raise ValueError("RESUME_V3_PASSWORD_RESET_TTL_SECONDS must be at least 300")
        if not 0.25 <= self.password_reset_min_response_seconds <= 2:
            raise ValueError(
                "RESUME_V3_PASSWORD_RESET_MIN_RESPONSE_SECONDS must be between 0.25 and 2"
            )
        if not 0 <= self.password_reset_response_jitter_seconds <= 1:
            raise ValueError(
                "RESUME_V3_PASSWORD_RESET_RESPONSE_JITTER_SECONDS must be between 0 and 1"
            )
        if self.password_reset_min_response_seconds + self.password_reset_response_jitter_seconds > 2:
            raise ValueError(
                "RESUME_V3_PASSWORD_RESET_MIN_RESPONSE_SECONDS plus "
                "RESUME_V3_PASSWORD_RESET_RESPONSE_JITTER_SECONDS must be at most 2"
            )
        for name, value in (
            (
                "RESUME_V3_PASSWORD_RESET_RATE_LIMIT_CLIENT_LIMIT",
                self.password_reset_rate_limit_client_limit,
            ),
            (
                "RESUME_V3_PASSWORD_RESET_RATE_LIMIT_EMAIL_LIMIT",
                self.password_reset_rate_limit_email_limit,
            ),
        ):
            if value < 1:
                raise ValueError(f"{name} must be at least 1")
        for name, value in (
            (
                "RESUME_V3_PASSWORD_RESET_RATE_LIMIT_CLIENT_WINDOW_SECONDS",
                self.password_reset_rate_limit_client_window_seconds,
            ),
            (
                "RESUME_V3_PASSWORD_RESET_RATE_LIMIT_EMAIL_WINDOW_SECONDS",
                self.password_reset_rate_limit_email_window_seconds,
            ),
        ):
            if value < 60:
                raise ValueError(f"{name} must be at least 60")
        if not 60 <= self.login_account_backpressure_window_seconds <= 24 * 60 * 60:
            raise ValueError(
                "RESUME_V3_LOGIN_ACCOUNT_BACKPRESSURE_WINDOW_SECONDS must be between 60 and 86400"
            )
        if not 0 <= self.login_account_backpressure_free_failures <= 20:
            raise ValueError(
                "RESUME_V3_LOGIN_ACCOUNT_BACKPRESSURE_FREE_FAILURES must be between 0 and 20"
            )
        if not 0.05 <= self.login_account_backpressure_base_delay_seconds <= 2:
            raise ValueError(
                "RESUME_V3_LOGIN_ACCOUNT_BACKPRESSURE_BASE_DELAY_SECONDS must be between 0.05 and 2"
            )
        if not (
            self.login_account_backpressure_base_delay_seconds
            <= self.login_account_backpressure_max_delay_seconds
            <= 5
        ):
            raise ValueError(
                "RESUME_V3_LOGIN_ACCOUNT_BACKPRESSURE_MAX_DELAY_SECONDS must be at least the "
                "base delay and at most 5"
            )
        for name, value in (
            ("RESUME_V3_LOGIN_RATE_LIMIT_CLIENT_LIMIT", self.login_rate_limit_client_limit),
            ("RESUME_V3_LOGIN_RATE_LIMIT_EMAIL_LIMIT", self.login_rate_limit_email_limit),
        ):
            if value < 1:
                raise ValueError(f"{name} must be at least 1")
        for name, value in (
            (
                "RESUME_V3_LOGIN_RATE_LIMIT_CLIENT_WINDOW_SECONDS",
                self.login_rate_limit_client_window_seconds,
            ),
            (
                "RESUME_V3_LOGIN_RATE_LIMIT_EMAIL_WINDOW_SECONDS",
                self.login_rate_limit_email_window_seconds,
            ),
        ):
            if value < 60:
                raise ValueError(f"{name} must be at least 60")
        if self.transactional_email_outbox_max_attempts < 1:
            raise ValueError("RESUME_V3_TRANSACTIONAL_EMAIL_OUTBOX_MAX_ATTEMPTS must be at least 1")
        if not 30 <= self.transactional_email_outbox_lease_seconds <= 3600:
            raise ValueError(
                "RESUME_V3_TRANSACTIONAL_EMAIL_OUTBOX_LEASE_SECONDS must be between 30 and 3600"
            )
        if not 10 <= self.transactional_email_outbox_retry_base_seconds <= 24 * 60 * 60:
            raise ValueError(
                "RESUME_V3_TRANSACTIONAL_EMAIL_OUTBOX_RETRY_BASE_SECONDS must be between 10 and 86400"
            )
        for name, value in (
            ("RESUME_V3_REGISTRATION_RATE_LIMIT_GLOBAL_LIMIT", self.registration_rate_limit_global_limit),
            ("RESUME_V3_REGISTRATION_RATE_LIMIT_CLIENT_LIMIT", self.registration_rate_limit_client_limit),
            ("RESUME_V3_REGISTRATION_RATE_LIMIT_EMAIL_LIMIT", self.registration_rate_limit_email_limit),
        ):
            if value < 1:
                raise ValueError(f"{name} must be at least 1")
        for name, value in (
            (
                "RESUME_V3_REGISTRATION_RATE_LIMIT_GLOBAL_WINDOW_SECONDS",
                self.registration_rate_limit_global_window_seconds,
            ),
            (
                "RESUME_V3_REGISTRATION_RATE_LIMIT_CLIENT_WINDOW_SECONDS",
                self.registration_rate_limit_client_window_seconds,
            ),
            (
                "RESUME_V3_REGISTRATION_RATE_LIMIT_EMAIL_WINDOW_SECONDS",
                self.registration_rate_limit_email_window_seconds,
            ),
        ):
            if value < 60:
                raise ValueError(f"{name} must be at least 60")
        for cidr in self.trusted_proxy_cidrs:
            try:
                ip_network(cidr, strict=False)
            except ValueError as exc:
                raise ValueError("RESUME_V3_TRUSTED_PROXY_CIDRS contains an invalid network") from exc
        if self.transactional_email_provider != "disabled":
            if not self.public_app_url:
                raise ValueError("RESUME_V3_PUBLIC_APP_URL is required for transactional email")
            if not self.public_app_url.startswith(("https://", "http://")):
                raise ValueError("RESUME_V3_PUBLIC_APP_URL must be an absolute HTTP URL")
        if self.transactional_email_provider in {"tencent_ses", "feishu_smtp"}:
            if not self.transactional_email_from:
                raise ValueError("RESUME_V3_TRANSACTIONAL_EMAIL_FROM is required for transactional email")
        if self.transactional_email_provider == "tencent_ses":
            if not self.tencent_secret_id or not self.tencent_secret_key:
                raise ValueError("Tencent SES requires TENCENT_SECRET_ID and TENCENT_SECRET_KEY")
            if not self.tencent_ses_verification_template_id:
                raise ValueError("TENCENT_SES_VERIFICATION_TEMPLATE_ID is required for Tencent SES")
        if self.transactional_email_provider == "feishu_smtp":
            if not self.feishu_smtp_host:
                raise ValueError("RESUME_V3_FEISHU_SMTP_HOST is required for Feishu SMTP")
            if not 1 <= self.feishu_smtp_port <= 65535:
                raise ValueError("RESUME_V3_FEISHU_SMTP_PORT must be between 1 and 65535")
            if self.feishu_smtp_tls_mode not in {"ssl", "starttls"}:
                raise ValueError("RESUME_V3_FEISHU_SMTP_TLS_MODE must be ssl or starttls")
            if self.feishu_smtp_tls_mode == "ssl" and self.feishu_smtp_port != 465:
                raise ValueError("Feishu SMTP SSL must use port 465")
            if self.feishu_smtp_tls_mode == "starttls" and self.feishu_smtp_port != 587:
                raise ValueError("Feishu SMTP STARTTLS must use port 587")
            if self.feishu_smtp_timeout_seconds < 1:
                raise ValueError("RESUME_V3_FEISHU_SMTP_TIMEOUT_SECONDS must be at least 1")
            if not self.feishu_smtp_username or not self.feishu_smtp_password:
                raise ValueError("Feishu SMTP requires username and app password")
            sender_address = parseaddr(self.transactional_email_from or "")[1]
            if not sender_address or sender_address.casefold() != self.feishu_smtp_username.casefold():
                raise ValueError(
                    "RESUME_V3_TRANSACTIONAL_EMAIL_FROM must match RESUME_V3_FEISHU_SMTP_USERNAME"
                )
        if self.environment in {"production", "prod"}:
            if self.allow_unauthenticated:
                raise RuntimeError("production_must_not_allow_unauthenticated")
            if self.database_url.startswith("sqlite"):
                raise RuntimeError("production_requires_postgresql_database_url")
            if self.auto_create_schema:
                raise RuntimeError("production_must_use_alembic_not_auto_create_schema")
            if self.seed_registry_on_startup:
                raise RuntimeError("production_must_seed_registry_explicitly")
            if not self.session_secret:
                raise RuntimeError("production_session_secret_required")
            # A mailbox connection or transactional sender makes the
            # dedicated at-rest key mandatory.  An installation that has not
            # enabled either flow may start without it, but mailbox/email
            # actions still fail closed until an independent key is supplied.
            if (
                self.transactional_email_provider != "disabled"
                and not self.email_credentials_key
            ):
                raise RuntimeError("production_email_credentials_key_required")
            if self.email_credentials_key:
                try:
                    Fernet(self.email_credentials_key.encode("utf-8"))
                except (TypeError, ValueError) as exc:
                    raise RuntimeError("production_email_credentials_key_invalid") from exc
                if self.session_secret == self.email_credentials_key:
                    raise RuntimeError(
                        "production_session_and_email_credentials_keys_must_differ"
                    )
            if self.legacy_admin_token_enabled and not self.admin_token:
                raise RuntimeError("legacy_admin_token_enabled_requires_admin_token")
            if self.admin_token and self.admin_token == self.session_secret:
                raise RuntimeError("production_session_secret_must_differ_from_legacy_admin_token")
            if self.transactional_email_provider == "test":
                raise RuntimeError("production_must_not_use_test_transactional_email_provider")
            if self.public_app_url and not self.public_app_url.startswith("https://"):
                raise RuntimeError("production_public_app_url_must_use_https")

    def session_signing_secret(self) -> str:
        """Return the only key that may sign browser/public-auth state.

        Production never falls back to a legacy admin token or a source-code
        literal. Local unauthenticated/test workspaces retain a deterministic
        development fallback so existing developer ergonomics do not become a
        deployment dependency.
        """

        if self.session_secret:
            return self.session_secret
        if self.environment in {"production", "prod"}:
            raise RuntimeError("production_session_secret_required")
        return "resume-v3-development-session"


def _optional_positive_int(name: str) -> int | None:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return None
    value = int(raw_value)
    if value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _comma_separated_values(raw_value: str) -> tuple[str, ...]:
    return tuple(value.strip() for value in raw_value.split(",") if value.strip())


def _secret_reference_map(name: str) -> dict[str, str]:
    """Parse a server-only credential map without placing secrets in the DB.

    Example: ``{"provider_a_primary": "..."}``.  Model route records hold
    only ``provider_a_primary``; this helper deliberately returns no values to
    API responses or logs.
    """

    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return {}
    try:
        decoded = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must be a JSON object") from exc
    if not isinstance(decoded, dict):
        raise ValueError(f"{name} must be a JSON object")
    normalized: dict[str, str] = {}
    for raw_reference, raw_secret in decoded.items():
        if not isinstance(raw_reference, str) or not isinstance(raw_secret, str):
            raise ValueError(f"{name} must map string references to string secrets")
        reference = raw_reference.strip()
        secret = raw_secret.strip()
        if not reference or not secret:
            raise ValueError(f"{name} contains an empty reference or secret")
        normalized[reference] = secret
    return normalized
