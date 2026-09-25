"""Live evals reuse agent-svc's test fixtures (real graph, real commerce-svc on Postgres) with REAL Groq.

Skipped unless GROQ_API_KEY is available (environment or the git-ignored .env).
"""

import pytest
from agent_testkit import agent_settings, commerce_app, commerce_db_url, make_agent  # noqa: F401
from evals_kit import env_value

if not env_value("GROQ_API_KEY"):
    pytest.skip("GROQ_API_KEY not set: live evals skipped", allow_module_level=True)
