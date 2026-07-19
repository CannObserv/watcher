"""Jinja2 environment for the dashboard — single shared ``templates`` instance.

Lives in its own module (not the package ``__init__``) so route modules can
import it at top level without creating a package↔module import cycle.
"""

from pathlib import Path
from urllib.parse import quote as _url_quote

from starlette.templating import Jinja2Templates

from src.core.config import BUILD_ID
from src.core.notifications.default_templates import TEMPLATE_VARIABLES
from src.core.notifications.events import EVENT_TITLES
from src.dashboard.deps import PAGE_SIZES

STATIC_DIR = Path(__file__).parent / "static"
TEMPLATE_DIR = Path(__file__).parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
templates.env.globals["build_id"] = BUILD_ID
templates.env.globals["event_titles"] = EVENT_TITLES
templates.env.globals["template_variables"] = TEMPLATE_VARIABLES
templates.env.globals["page_sizes"] = PAGE_SIZES
templates.env.filters["url_quote"] = lambda s: _url_quote(str(s), safe="")
