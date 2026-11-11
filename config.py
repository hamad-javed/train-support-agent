# Settings shared by agent.py, seed_db.py and policy_search.py
import os

# Paths are relative to this folder, so the scripts work from any directory
HERE = os.path.dirname(os.path.abspath(__file__))

# Models (both run locally through Ollama).
# The chat model must support tool calling: `ollama show <model>` lists "tools"
# under Capabilities. llama3 does NOT; llama3.1 and qwen3 do.
LLM_MODEL = "llama3.1"
EMBEDDING_MODEL = "nomic-embed-text"

# Sample SQLite database created by seed_db.py
DB_PATH = os.path.join(HERE, "train.db")

# Policy documents searched by the search_policies tool (RAG)
POLICY_DIR = os.path.join(HERE, "policies")
CHUNK_SIZE = 500
TOP_K = 3

# Most rows the model gets back from one SQL query
MAX_SQL_ROWS = 20

# Stop the agent loop if the model keeps calling tools without answering
MAX_TOOL_ROUNDS = 8

# Customer who is "logged in" when no --email is given
DEFAULT_CUSTOMER_EMAIL = "alice@example.com"
