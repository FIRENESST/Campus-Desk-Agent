"""Allowlist of the routes shared by HTTP and local callers."""

from dataclasses import dataclass


CAMPUS_PHOTO_GALLERY = "/v1/campus-view/photo-places/{place}/photos"


@dataclass(frozen=True)
class Interface:
    path: str
    methods: tuple[str, ...]
    access: str
    service: str
    target: str
    data_domain: str | None = None

    def describe(self) -> dict:
        return {
            "path": self.path, "methods": self.methods, "access": self.access,
            "service": self.service, "data_domain": self.data_domain,
        }


def main(path: str, *methods: str, public: bool = False, data_domain: str = "postgresql") -> Interface:
    return Interface(path, methods, "public" if public else "user", "main", path, data_domain)


def admin(service: str, path: str, method: str, target: str, *, data_domain: str | None = None) -> Interface:
    domains = {"rag": "milvus", "rag_worker": "milvus", "routing_worker": "routing_sqlite"}
    return Interface(f"/admin/{path}", (method,), "admin", service, target, data_domain or domains.get(service))


USER_INTERFACES = (
    Interface("/health", ("GET",), "public", "manager", "health"),
    Interface("/v1/interfaces", ("GET",), "public", "manager", "user_interfaces"),
    main("/ready", "GET", public=True),
    main("/v1/auth/providers", "GET", public=True),
    main("/v1/auth/register", "POST", public=True),
    main("/v1/auth/login", "POST", public=True),
    main("/v1/auth/refresh", "POST", public=True),
    main("/v1/auth/remember", "POST", public=True),
    main("/v1/auth/verification/request", "POST", public=True),
    main("/v1/auth/verification/confirm", "POST", public=True),
    main("/v1/auth/password/request", "POST", public=True),
    main("/v1/auth/password/reset", "POST", public=True),
    main("/v1/auth/sso/campus/start", "POST", public=True),
    main("/v1/auth/sso/campus/callback", "GET", "POST", public=True),
    main("/v1/auth/sso/campus/finish", "POST", public=True),
    main("/v1/auth/sso/campus/metadata", "GET", public=True),
    main("/v1/auth/identities", "GET"),
    main("/v1/auth/identities/{identity_id}", "DELETE"),
    main("/v1/auth/password/change/request", "POST"),
    main("/v1/auth/password/change", "POST"),
    main("/v1/auth/logout", "POST"),
    main("/v1/auth/logout-all", "POST"),
    main("/v1/auth/sessions", "GET"),
    main("/v1/auth/sessions/{session_id}", "DELETE"),
    main("/v1/me", "GET"),
    main("/v1/profile", "GET", "PUT"),
    main("/v1/calendar/events", "GET", "POST"),
    main("/v1/calendar/events/{event_id}", "PUT", "DELETE"),
    main("/v1/calendar/subscriptions", "GET", "POST"),
    main("/v1/calendar/subscriptions/{subscription_id}", "DELETE"),
    main("/v1/calendar/export.ics", "GET", "HEAD", data_domain="minta_simulation"),
    main("/v1/calendar/feed.ics", "GET", "HEAD", data_domain="minta_simulation"),
    main("/v1/calendar/microsoft-todo", "GET", "DELETE"),
    main("/v1/calendar/microsoft-todo/start", "POST"),
    main("/v1/calendar/microsoft-todo/finish", "POST"),
    main("/v1/calendar/microsoft-todo/sync", "POST"),
    main("/v1/instances", "GET", "POST"),
    main("/v1/instances/{instance_id}", "GET", "PATCH", "DELETE"),
    main("/v1/instances/{instance_id}/conversations", "GET", "POST"),
    main("/v1/conversations/{conversation_id}", "PATCH", "DELETE"),
    main("/v1/conversations/{conversation_id}/messages", "GET"),
    main("/v1/conversations/{conversation_id}/runs", "POST"),
    main("/v1/runs/{run_id}", "GET"),
    main("/v1/runs/{run_id}/cancel", "POST"),
    main("/v1/runs/{run_id}/retry", "POST"),
    main("/v1/runs/{run_id}/withdrawal", "GET", "POST"),
    main("/v1/campus-view", "GET", data_domain="campus_files"),
    main("/v1/campus-view/routes/{route_id}", "GET", data_domain="campus_files"),
    main("/v1/campus-view/points/{point_id}", "GET", data_domain="campus_files"),
    main("/v1/campus-view/resolve", "POST", data_domain="campus_files"),
    main("/v1/campus-view/media/{name}", "GET", data_domain="campus_files"),
    main("/v1/campus-view/photo-places", "GET"),
    main(CAMPUS_PHOTO_GALLERY, "GET", "POST"),
    main("/v1/campus-view/photos/{photo_id}/image", "GET"),
    Interface("/v1/campus-digest/wechat", ("GET",), "user", "digest", "/api/campus-desk/wechat", "campus_digest_sqlite"),
    Interface("/v1/campus-digest/wechat/accounts", ("GET",), "user", "digest", "/api/campus-desk/wechat/accounts", "campus_digest_sqlite"),
    main("/v1/campus-digest/wechat/following", "GET", "PUT"),
    Interface("/v1/campus-digest/wechat/events", ("GET",), "user", "digest", "/api/campus-desk/wechat/events", "campus_digest_sqlite"),
    Interface("/v1/campus-rumor", ("GET",), "user", "rumor", "rumors", "campus_rumor_sqlite"),
    Interface("/v1/campus-rumor/tips", ("POST",), "user", "rumor", "tips", "campus_rumor_sqlite"),
    Interface("/v1/rag/search", ("POST",), "user", "rag", "/search", "milvus"),
    Interface("/v1/minta/schedule", ("GET",), "user", "minta_shortcut", "schedule", "minta_simulation"),
    main("/v1/minta/activities", "GET", data_domain="minta_simulation"),
    main("/v1/minta/semester-report", "GET", data_domain="minta_simulation"),
    main("/v1/minta/classrooms", "GET", data_domain="minta_simulation"),
    main("/v1/minta/operations", "GET", "POST"),
    main("/v1/minta/operations/{operation_id}", "DELETE"),
    main("/v1/minta/catalog/{service}", "GET", data_domain="minta_simulation"),
    main("/v1/minta/timetable", "GET", data_domain="minta_simulation"),
    main("/v1/minta/teacher-credential", "GET", data_domain="teacher_credentials"),
    main("/v1/minta/clubs", "GET", "POST", data_domain="minta_simulation"),
)


ADMIN_INTERFACES = (
    admin("manager", "interfaces", "GET", "admin_interfaces"),
    admin("main", "main/health", "GET", "/health"),
    admin("main", "postgres/ready", "GET", "/ready", data_domain="postgresql"),
    admin("rag", "rag/health", "GET", "/health"),
    admin("rag", "rag/scopes", "GET", "/scopes"),
    admin("rag", "rag/search", "POST", "/search"),
    admin("rag_worker", "rag/documents", "GET", "list"),
    admin("rag_worker", "rag/documents", "POST", "ingest"),
    admin("rag_worker", "rag/documents/{document_id}", "GET", "view"),
    admin("rag_worker", "rag/documents/{document_id}", "DELETE", "delete"),
    admin("routing_worker", "routing/status", "GET", "status"),
    admin("routing_worker", "routing/status", "PUT", "set_enabled"),
    admin("routing_worker", "routing/overview", "GET", "overview"),
    admin("routing_worker", "routing/introductions", "GET", "introductions"),
    admin("routing_worker", "routing/introductions/{role}", "PUT", "set_introduction"),
    admin("routing_worker", "routing/introductions/{role}", "DELETE", "reset_introduction"),
    admin("routing_worker", "routing/preview", "POST", "preview"),
    admin("routing_worker", "routing/samples", "GET", "list"),
    admin("routing_worker", "routing/samples/bulk-delete", "POST", "bulk_delete"),
    admin("routing_worker", "routing/samples/{sample_id}/grade", "PUT", "grade"),
    admin("routing_worker", "routing/samples/{sample_id}/grade", "DELETE", "clear_grade"),
    admin("routing_worker", "routing/samples/{sample_id}", "DELETE", "delete"),
    admin("llm", "llm/health", "GET", "/health"),
    *(
        Interface(f"/admin/llm{path}", (method,), "model", "llm", path)
        for method, path in (
            ("GET", "/v1/models"), ("POST", "/v1/chat/completions"),
            ("POST", "/v1/completions"), ("POST", "/v1/embeddings"), ("POST", "/v1/responses"),
        )
    ),
    Interface("/admin/mcp", ("GET", "POST", "DELETE"), "main_agent", "mcp", ""),
    admin("minta", "minta/healthz", "GET", "/healthz"),
    admin("minta", "minta/readyz", "GET", "/readyz"),
    admin("minta", "minta/v1/call", "POST", "/v1/call"),
)


def web_admin(path, method, service, target, domain=None):
    return Interface(f"/admin/api/{path}", (method,), "web_admin", service, target, domain)


WEB_ADMIN_INTERFACES = (
    Interface("/admin/rumor/{tail:path}", ("GET", "POST", "PUT", "PATCH", "DELETE"), "web_admin",
              "web_rumor", "dashboard", "campus_rumor_sqlite"),
    web_admin("login", "POST", "web_auth", "login"),
    web_admin("logout", "POST", "web_auth", "logout"),
    web_admin("session", "GET", "web_auth", "session"),
    web_admin("audit", "GET", "web_auth", "audit"),
    web_admin("data/options", "GET", "web_data", "options", "postgresql"),
    web_admin("data/{resource}", "GET", "web_data", "list", "postgresql"),
    web_admin("data/{resource}", "POST", "web_data", "create", "postgresql"),
    web_admin("data/{resource}/{record_id}", "PATCH", "web_data", "update", "postgresql"),
    web_admin("data/{resource}/{record_id}", "DELETE", "web_data", "delete", "postgresql"),
    web_admin("data/{resource}/{record_id}/role", "PATCH", "web_data", "role", "postgresql"),
    web_admin("data/{resource}/{record_id}/revoke", "POST", "web_data", "revoke", "postgresql"),
    web_admin("data/minta/{record_id}/close", "POST", "web_data", "minta_close", "postgresql"),
    web_admin("data/minta/{record_id}/reconcile", "POST", "web_data", "minta_reconcile", "postgresql"),
    web_admin("cas/profiles", "GET", "web_campus_profiles", "campus_profiles", "postgresql"),
    web_admin("cas/profiles/bulk-review", "POST", "web_campus_profiles", "campus_profiles_bulk_review", "postgresql"),
    web_admin("cas/profiles/{record_id}", "PATCH", "web_campus_profiles", "campus_profile_review", "postgresql"),
    web_admin("cas/profiles/{record_id}", "DELETE", "web_campus_profiles", "campus_identity_unbind", "postgresql"),
    web_admin("credentials/{action}", "POST", "web_credentials", "manage", "teacher_credentials"),
    web_admin("runtime/processes", "GET", "web_runtime", "processes"),
    web_admin("runtime/processes/{service}", "POST", "web_runtime", "process_action"),
    web_admin("runtime/skills", "GET", "web_inventory", "skills"),
    web_admin("runtime/tools", "GET", "web_inventory", "tools"),
    web_admin("digest/weread", "GET", "web_weread", "status"),
    web_admin("digest/weread/login", "GET", "web_weread", "login"),
    web_admin("digest/weread/login", "POST", "web_weread", "login"),
    web_admin("campus/{service}", "GET", "web_inventory", "campus", "minta_simulation"),
    web_admin("campus/rules/{service}", "GET", "web_campus_controls", "campus_rules", "postgresql"),
    web_admin("campus/rules/{service}", "PUT", "web_campus_controls", "campus_rule_update", "postgresql"),
    web_admin("campus/rules/classroom/{record_id}", "PUT", "web_campus_controls", "campus_rule_update", "postgresql"),
    web_admin("campus/clubs/review", "GET", "web_campus_controls", "club_reviews", "minta_simulation"),
    web_admin("campus/clubs/review/{record_id}", "PATCH", "web_campus_controls", "club_review", "minta_simulation"),
    web_admin("campus/clubs/archives", "GET", "web_campus_controls", "club_archives", "minta_simulation"),
    web_admin("campus/clubs/archives/{record_id}", "PATCH", "web_campus_controls", "club_archive", "minta_simulation"),
    web_admin("campus/photos/places", "GET", "web_campus_photos", "photo_review_places", "postgresql"),
    web_admin("campus/photos/review", "GET", "web_campus_photos", "photo_reviews", "postgresql"),
    web_admin("campus/photos/review/{record_id}", "PATCH", "web_campus_photos", "photo_review", "postgresql"),
    web_admin("campus/photos/review/{record_id}", "DELETE", "web_campus_photos", "photo_delete", "postgresql"),
    web_admin("campus/photos/review/{record_id}/image", "GET", "web_campus_photos", "photo_preview", "postgresql"),
    web_admin("minta/readyz", "GET", "web_inventory", "minta-health", "minta_simulation"),
    *(
        web_admin(entry.path.removeprefix("/admin/"), entry.methods[0], entry.service, entry.target, entry.data_domain)
        for entry in ADMIN_INTERFACES
        if entry.service in {"rag", "rag_worker", "routing_worker"} or entry.path in {
            "/admin/main/health", "/admin/postgres/ready", "/admin/llm/health",
        }
    ),
)
