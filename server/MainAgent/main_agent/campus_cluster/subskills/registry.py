"""The central registration of built-in campus skills and their assigned roles."""

from ..contracts import AgentId
from ..skills import SkillRegistry
from .campus_digest import CAMPUS_DIGEST
from .campus_street_view import CAMPUS_STREET_VIEW
from .campus_timetable import CAMPUS_TIMETABLE
from .domain_page import READ_DOMAIN_PAGE
from .minta_campus import CAMPUS_ACTION, CAMPUS_STATUS
from .minta_gateway import MINTA_GATEWAY
from .rumor_assessment import CAMPUS_RUMOR
from .search_rag import SEARCH_RAG
from .wechat_articles import WECHAT_ARTICLES


SUBSKILL_ASSIGNMENTS = (
    (READ_DOMAIN_PAGE, tuple(AgentId)),
    (SEARCH_RAG, (AgentId.CAMPUS_INTRODUCTION,)),
    (CAMPUS_DIGEST, (AgentId.INFORMATION_SEARCH, AgentId.RUMOR_ASSESSMENT)),
    (WECHAT_ARTICLES, (AgentId.INFORMATION_SEARCH, AgentId.RUMOR_ASSESSMENT)),
    (CAMPUS_RUMOR, (AgentId.RUMOR_ASSESSMENT,)),
    (CAMPUS_STREET_VIEW, (AgentId.CAMPUS_INTRODUCTION,)),
    (MINTA_GATEWAY, tuple(AgentId)),
    (CAMPUS_STATUS, (AgentId.CAMPUS_SERVICES,)),
    (CAMPUS_TIMETABLE, (AgentId.CAMPUS_SERVICES,)),
    (CAMPUS_ACTION, (AgentId.CAMPUS_SERVICES,)),
)
SUBSKILL_REGISTRY = SkillRegistry.from_assignments(SUBSKILL_ASSIGNMENTS)
