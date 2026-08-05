from personaforge_control.adapters.inbound.http_api import create_control_app
from personaforge_control.bootstrap.container import build_control_container
from personaforge_control.bootstrap.settings import ControlSettings

settings = ControlSettings.from_env()
app = create_control_app(build_control_container(settings))
