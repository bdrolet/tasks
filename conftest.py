import sys
from unittest.mock import MagicMock

# Mock psycopg before any imports try to use it
sys.modules['psycopg'] = MagicMock()
sys.modules['psycopg.rows'] = MagicMock()
sys.modules['psycopg.types'] = MagicMock()
sys.modules['psycopg.types.json'] = MagicMock()
