# ===========================================================================
# app/core/templates.py
#
# Shared Jinja2Templates instance used by all routers.
#
# Import this everywhere instead of creating a new Jinja2Templates instance
# per router. This ensures all template globals (user_initials, setup_state,
# app_name) are available in every template regardless of which router
# renders it.
#
# Usage in any router:
#   from app.core.templates import templates
# ===========================================================================

from fastapi.templating import Jinja2Templates
from app.core.template_globals import setup_state, user_initials

templates = Jinja2Templates(directory="app/templates")
templates.env.globals["app_name"]      = "My Financial Life"
templates.env.globals["user_initials"] = user_initials
templates.env.globals["setup_state"]   = setup_state
templates.env.filters["abs"]           = abs
