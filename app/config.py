from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from email.utils import parseaddr
from ipaddress import ip_network
from pathlib import Path


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
            if self.transactional_email_provider == "test":
                raise RuntimeError("production_must_not_use_test_transactional_email_provider")
            if self.public_app_url and not self.public_app_url.startswith("https://"):
                raise RuntimeError("production_public_app_url_must_use_https")


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
