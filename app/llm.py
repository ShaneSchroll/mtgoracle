"""The shared Anthropic client.

Imports config first so that load_dotenv() has run before the client reads
ANTHROPIC_API_KEY from the environment.
"""

from anthropic import Anthropic

from . import config  # noqa: F401  - imported for its load_dotenv() side effect

client = Anthropic()  # reads ANTHROPIC_API_KEY
