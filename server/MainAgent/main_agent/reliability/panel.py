"""Optional concurrent reviewers with conservative, per-rule aggregation."""

import asyncio
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import asdict

from .checks import review_findings
from .models import Claim, InvalidReviewError, ReliabilityError, Review, ReviewContext, Reviewer, RuleCheck
from .review import bounded_json, parse_review


class ReviewPanel:
    """Any explicit failure or uncertainty survives aggregation.

    Each member receives a separate context and must cover all rules. Using
    different models is possible but does not imply independent errors.
    """

    def __init__(self, reviewers: Sequence[Reviewer]):
        self.reviewers = tuple(reviewers)
        if not self.reviewers:
            raise ValueError("至少需要一个评审器")

    def bind_runtime(self, runtime):
        from .runtime import bind_runtime
        return ReviewPanel([bind_runtime(reviewer, runtime) for reviewer in self.reviewers])

    async def review(self, context: ReviewContext) -> Review:
        try:
            async with asyncio.TaskGroup() as group:
                tasks = [
                    group.create_task(reviewer.review(deepcopy(context)))
                    for reviewer in self.reviewers
                ]
        except ExceptionGroup as errors:
            pending = list(errors.exceptions)
            while pending:
                error = pending.pop(0)
                if isinstance(error, ReliabilityError):
                    raise error from errors
                if isinstance(error, ExceptionGroup):
                    pending.extend(error.exceptions)
            raise
        reviews = []
        member_findings = []
        for task in tasks:
            review = task.result()
            if not isinstance(review, Review):
                raise InvalidReviewError("评审组成员必须返回 Review")
            review = parse_review(bounded_json(asdict(review), 100_000))
            member_findings.extend(
                f"评审器 {len(reviews) + 1}: {finding.code}: {finding.message}"
                for finding in review_findings(context, review)
            )
            reviews.append(review)
        checks = []
        for rule in context.rules:
            members = [next(check for check in review.rule_checks if check.rule_id == rule.id) for review in reviews]
            status = max((check.status for check in members), key={"pass": 0, "unknown": 1, "fail": 2}.__getitem__)
            checks.append(RuleCheck(rule.id, status, "；".join(dict.fromkeys(check.reason for check in members))))
        claims: dict[str, Claim] = {}
        for review in reviews:
            for claim in review.claims:
                previous = claims.get(claim.text)
                if previous is None:
                    claims[claim.text] = claim
                    continue
                status = max((previous.status, claim.status), key={"supported": 0, "unsupported": 1, "contradicted": 2}.__getitem__)
                kind = "action" if "action" in (previous.kind, claim.kind) else "fact"
                citations = tuple(dict.fromkeys(previous.citations + claim.citations))
                claims[claim.text] = Claim(claim.text, kind, status, citations)
        issues = tuple(dict.fromkeys([
            *(issue for review in reviews for issue in review.issues), *member_findings,
        ]))
        return Review(tuple(checks), tuple(claims.values()), issues)
