# Examples

Small, runnable demonstrations of `pytest-pglite` with common Python stacks.
Each directory is a self-contained test project; run one with:

```bash
poetry run pytest examples/<name>
```

| Example | What it shows |
| --- | --- |
| `fastapi-sqlalchemy` | FastAPI app with a synchronous SQLAlchemy engine |
| `fastapi-async-sqlalchemy` | FastAPI + SQLAlchemy asyncio (psycopg async) |
| `django` | Django ORM with pytest-django, no test database switch needed |
| `asyncpg` | asyncpg with pytest-asyncio |
| `alembic` | Running Alembic migrations against PGlite |

All examples rely on the fixtures provided by the installed plugin; there is
no per-project setup or Docker.
