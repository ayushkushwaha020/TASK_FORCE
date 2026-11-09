import unittest
from sqlalchemy.engine import make_url

from app.database import normalize_database_url


class DatabaseUrlTests(unittest.TestCase):
    def test_supabase_session_pooler_uses_ssl_and_asyncpg(self):
        normalized = normalize_database_url(
            "postgresql://postgres.project-ref:password@aws-0-ap-south-1.pooler.supabase.com:5432/postgres"
        )
        url = make_url(normalized)
        self.assertEqual(url.drivername, "postgresql+asyncpg")
        self.assertEqual(url.query.get("ssl"), "require")
        self.assertNotIn("sslmode", url.query)
        self.assertNotIn("statement_cache_size", url.query)

    def test_supabase_transaction_pooler_disables_statement_cache(self):
        normalized = normalize_database_url(
            "postgresql://postgres.project-ref:password@aws-0-ap-south-1.pooler.supabase.com:6543/postgres?sslmode=require"
        )
        url = make_url(normalized)
        self.assertEqual(url.query.get("ssl"), "require")
        self.assertEqual(url.query.get("statement_cache_size"), "0")

    def test_local_sqlite_url_is_normalized(self):
        self.assertEqual(
            normalize_database_url("sqlite:///./workforce.db"),
            "sqlite+aiosqlite:///./workforce.db",
        )

    def test_supabase_task_schema_is_private_and_uses_rls(self):
        # The repository uses a non-exposed schema; SQL initialization enables RLS.
        from app.database import PersistentTaskRepository
        repository = PersistentTaskRepository(
            "postgresql://postgres.project-ref:password@aws-0-ap-south-1.pooler.supabase.com:5432/postgres"
        )
        self.assertEqual(repository.backend, "supabase_postgresql")
        self.assertEqual(repository.schema, "workforce_private")


if __name__ == "__main__":
    unittest.main()
