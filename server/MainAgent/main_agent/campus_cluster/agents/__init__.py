from ..contracts import AgentId
from .base import SpecialistAgent
from .campus_introduction import CampusIntroductionAgent
from .campus_services import CampusServicesAgent
from .information_search import InformationSearchAgent
from .rumor_assessment import RumorAssessmentAgent

AGENT_TYPES = {
    AgentId.CAMPUS_INTRODUCTION: CampusIntroductionAgent,
    AgentId.INFORMATION_SEARCH: InformationSearchAgent,
    AgentId.CAMPUS_SERVICES: CampusServicesAgent,
    AgentId.RUMOR_ASSESSMENT: RumorAssessmentAgent,
}

__all__ = [
    "SpecialistAgent", "CampusIntroductionAgent", "CampusServicesAgent",
    "InformationSearchAgent", "RumorAssessmentAgent",
]
