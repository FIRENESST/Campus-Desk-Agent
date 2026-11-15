"""Project the delivered monitoring records into the student-facing feed."""

import json
import re
from datetime import date, timedelta
from urllib.parse import urlsplit


_LIMIT = 5
_PUBLISHED_STATUSES = {"verified_true": "supported", "verified_false": "refuted"}
_PRIVATE_TEXT = re.compile(r"https?://\S+|[0-9]{7,}|[\w.+-]+@[\w.-]+|(?:微信号?|QQ号?|电话|手机号)\s*[：:]?\s*[\w+-]{5,}")
_PUBLIC_CHANGES = frozenset({"class_suspend", "class_switch", "class_start", "closure", "exam", "gate_control",
                            "restriction", "outage", "power", "water"})
_PERSONAL_CLAIM = re.compile(r"(?:[\u4e00-\u9fff]{2,3})(?:老师|同学|教授|主任|辅导员)(?:被|已|将|要|说|涉嫌|因此)")


def _public_title(claim, published, *, pending=False):
    from app.analysis.assessment import claim_scope_label, unclassified_event

    if (claim.get("external_school") or claim.get("manual") or _PERSONAL_CLAIM.search(claim["excerpt"])
            or unclassified_event(claim["event"], claim["subject"])):
        return ""
    if pending and claim["event"] not in _PUBLIC_CHANGES:
        return ""
    label = claim_scope_label(claim)
    if not label:
        # Ordinary claims may have no formatted label. Show a recognizable, bounded excerpt.
        label = claim["excerpt"].strip().splitlines()[0] if claim["excerpt"].strip() else ""
    timing = tuple(day for day in claim.get("days", ()) if day)
    if timing and not all(day in label for day in timing):
        label += " · 涉及日期 " + "、".join(timing)
    if not timing and published:
        label += " · 原消息日期 " + published[:10]
    return _PRIVATE_TEXT.sub("〔已隐藏〕", label)[:180]


def _public_wording(claim):
    return _PRIVATE_TEXT.sub("〔已隐藏〕", claim["excerpt"].strip())[:500]


def _public_sources(record, claim_key, status):
    from app.core.official_urls import reference_url

    if not isinstance(record, dict):
        return []
    result = next((row for row in (record.get("results") or ()) if row.get("key") == claim_key), None)
    if not result or result.get("status") != status or result.get("pending_revisions"):
        return []
    sources = []
    for proof in result.get("evidence", ()):
        if (proof["relation"] != status or proof["provenance"] not in {"official_article", "operator_excerpt"}
                or not (url := reference_url(proof["url"], operator_excerpt=True)) or urlsplit(url).path in {"", "/"}):
            continue
        quote = proof["quote"].strip()
        try:
            published = date.fromisoformat(proof["published"]).isoformat()
        except (ValueError, TypeError):
            continue
        if quote:
            source = {"url": url, "published": published, "quote": _PRIVATE_TEXT.sub("〔已隐藏〕", quote)[:700],
                      "provenance": proof["provenance"], "relation": status}
            if source not in sources:
                sources.append(source)
    return sources[:3]


def _verified_reviews(connection, filters, *, claim_key=None):
    from app.analysis import verification
    from app.core import db

    reviewed = db.rows(f"""
            SELECT k.claim_key,k.claim_json,p.verification_json,p.detail,p.publish_time,p.analysis_version,
                   p.review_generation,p.claims_json,
                   CASE WHEN {db.SCOPED_REVIEW} THEN r.status ELSE p.trace_status END status,
                   CASE WHEN {db.SCOPED_REVIEW} THEN r.reviewed_at ELSE p.reviewed_at END reviewed_at,
                   r.verification_id
            FROM post_claims k JOIN posts p ON p.id=k.post_id
            LEFT JOIN claim_reviews r ON r.post_id=p.id AND r.review_generation=p.review_generation
                    AND r.claim_key=k.claim_key
            WHERE p.analysis_version=:version AND p.verification_json IS NOT NULL
              AND (:key IS NULL OR k.claim_key=:key)
              AND (({db.SCOPED_REVIEW} AND r.verification_id IS NOT NULL) OR (NOT {db.SCOPED_REVIEW} AND NOT p.review_stale))
              AND CASE WHEN {db.SCOPED_REVIEW} THEN r.status ELSE p.trace_status END IN ('verified_true','verified_false')
              AND CASE WHEN {db.SCOPED_REVIEW} THEN r.reviewed_at ELSE p.reviewed_at END >= :since
              AND CASE WHEN {db.SCOPED_REVIEW} THEN r.reviewed_at ELSE p.reviewed_at END < :until
            ORDER BY reviewed_at DESC,k.claim_key
        """, {**filters, "key": claim_key}, connection=connection)

    confirmed, conflicts = {}, set()
    for row in reviewed:
        key = row["claim_key"]
        verdict = _PUBLISHED_STATUSES[row["status"]]
        record = json.loads(row["verification_json"])
        if not record or record.get("revision") != verification.revision(row):
            continue
        if row["verification_id"] is not None and record.get("id") != row["verification_id"]:
            continue
        if (record.get("reviewed_at") or "") > row["reviewed_at"]:
            continue
        if source := _public_sources(record, key, verdict):
            claim = json.loads(row["claim_json"])
            if title := _public_title(claim, row["publish_time"]):
                previous = confirmed.setdefault(key, {
                    "id": key, "title": title, "verdict": verdict,
                    "reviewed_on": row["reviewed_at"][:10], "sources": source,
                    "claim": _public_wording(claim), "published_on": (row["publish_time"] or "")[:10],
                })
                if previous["verdict"] != verdict:
                    conflicts.add(key)
    return {key: row for key, row in confirmed.items() if key not in conflicts}


def verified_claim(key):
    from app.analysis import assessment
    from app.core import db
    from app.core.config import now

    filters = {"version": assessment.VERSION, "since": "", "until": (now().date() + timedelta(days=1)).isoformat()}
    with db.conn() as connection:
        return _verified_reviews(connection, filters, claim_key=key).get(key)


def student_feed():
    from app.analysis import assessment
    from app.core import dashboard, db
    from app.core.config import now

    current = now()
    filters = {"version": assessment.VERSION, "since": (current.date() - timedelta(days=dashboard.WINDOW - 1)).isoformat(),
               "until": (current.date() + timedelta(days=1)).isoformat()}
    with db.conn() as connection:
        connection.execute("BEGIN")
        pending_rows = db.rows(f"""
            SELECT k.claim_key,k.claim_json,k.copy_hash,k.published,p.publish_time,p.views,p.count_comment
            FROM post_claims k JOIN posts p ON p.id=k.post_id {dashboard.REVIEW_JOIN}
            WHERE k.is_candidate=1 AND p.analysis_version=:version AND ({dashboard.PENDING})
              AND k.published>=:since AND k.published<:until
            ORDER BY k.published DESC,k.preference DESC
        """, filters, connection=connection)
        confirmed = _verified_reviews(connection, filters)

    items, heat_by_copy = {}, {}
    for row in pending_rows:
        key = row["claim_key"]
        if key in confirmed:
            continue
        if key not in items:
            claim = json.loads(row["claim_json"])
            if not (title := _public_title(claim, row["published"], pending=True)):
                continue
            items[key] = {"id": key, "title": title, "last_seen": row["published"][:10],
                          "claim": _public_wording(claim), "published_on": row["published"][:10]}
            heat_by_copy[key] = {}
        copies = heat_by_copy[key]
        digest = row["copy_hash"]
        copies[digest] = max(copies.get(digest, 0), dashboard._heat(row, current))
    for key, item in items.items():
        item["heat"] = round(sum(heat_by_copy[key].values()), 1)
    pending = sorted(items.values(), key=lambda item: (item["heat"], item["last_seen"]), reverse=True)
    return {"pending": pending[:_LIMIT], "confirmed": list(confirmed.values())[:_LIMIT]}
