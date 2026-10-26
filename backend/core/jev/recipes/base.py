"""Base definitions for domain workflow recipes and milestones."""

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class Milestone:
    """A distinct milestone in a multi-step workflow."""

    id: str
    name: str
    description: str
    instruction_hint: Any = ""
    is_completed: Optional[Callable[[Dict[str, Any], List[Dict[str, Any]], Dict[str, Any]], bool]] = None
    suggested_action: Optional[Callable[[Dict[str, Any], List[Dict[str, Any]], Dict[str, Any]], Optional[Dict[str, Any]]]] = None


@dataclass
class WorkflowRecipe:
    """A declarative specification for navigating complex, domain-specific multi-step websites."""

    name: str
    domains: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    default_start_url: Optional[str] = None
    field_hints: Dict[str, Any] = field(default_factory=dict)
    
    def matches(self, goal: str, url: str = "") -> bool:
        """True if this recipe applies to the given goal or current URL."""
        url_low = (url or "").lower()
        goal_low = (goal or "").lower()
        if any(d.lower() in url_low for d in self.domains):
            return True
        if any(k.lower() in goal_low for k in self.keywords):
            return True
        return False

    def build_milestones(self, goal: str, context: Dict[str, Any]) -> List[Milestone]:
        """Build the sequential list of milestones for this goal."""
        return []

    def handle_recovery(self, browser, page: Dict[str, Any], history: List[Dict[str, Any]], context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Hook to handle domain-specific popups, session conflicts, or reset dialogs."""
        return None

    def should_terminate(self, goal: str, history: List[Dict[str, Any]], context: Dict[str, Any]) -> bool:
        """Check if goal constraints (e.g., stop before final submit) are satisfied."""
        return False

    def get_budget(self, goal: str, default_budget: int = 35) -> int:
        """Compute recommended step budget for this goal."""
        return default_budget

