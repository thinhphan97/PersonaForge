from personaforge_chat.adapters.inbound.fastapi_app import create_app
from personaforge_observability import configure_logging

configure_logging("personaforge-chat")
app = create_app()
