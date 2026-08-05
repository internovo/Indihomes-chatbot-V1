"""
New request models for the Phase 1 "Multiple Factors Matter" feature.

NOTE ON SCOPE: app.py already defines its existing request models
(SearchRequest, PropertyDetailRequest, LeadRequest, etc.) inline, in place.
This file does NOT relocate those - moving working, already-imported models
out of app.py is a bigger refactor than this feature needs and adds risk for
no behavioural benefit. This file holds only what's new for the multi-factor
priority flow, imported into app.py alongside the existing inline models.
"""

from typing import Optional
from pydantic import BaseModel


class PriorityParseRequest(BaseModel):
    """Body for POST /parse-priorities.

    WATI collects the user's free-text reply to "Which factors matter?
    (1,2,3)" into @priority_selection and posts it here. The backend turns
    that into flat yes/no strings so WATI's Condition nodes (which only
    reliably compare with Equal - see claude.md) can branch to the relevant
    follow-up questions without WATI needing to understand text parsing.
    """
    phone: Optional[str] = ""
    priority_selection: Optional[str] = ""
