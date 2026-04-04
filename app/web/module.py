from __future__ import annotations

from flask import Blueprint

from app.web.admin_routes import register_admin_routes
from app.web.agent_routes import register_agent_routes
from app.web.mini_app_routes import register_mini_app_routes


class WebModule:
    def __init__(self, onboarding_callback):
        self.onboarding_callback = onboarding_callback
        self.blueprint = Blueprint("web_module", __name__)
        self._register_routes()

    def _register_routes(self) -> None:
        register_admin_routes(self.blueprint, self.onboarding_callback)
        register_mini_app_routes(self.blueprint)
        register_agent_routes(self.blueprint)
