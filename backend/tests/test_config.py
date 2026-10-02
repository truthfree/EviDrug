from evidrug_api.config import Settings


def test_render_postgres_url_uses_async_psycopg_driver() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        database_url="postgresql://user:password@host.internal/evidrug?sslmode=require",
    )

    assert settings.database_url == (
        "postgresql+psycopg://user:password@host.internal/evidrug?sslmode=require"
    )


def test_legacy_postgres_url_uses_async_psycopg_driver() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        database_url="postgres://user:password@host.internal/evidrug",
    )

    assert settings.database_url == "postgresql+psycopg://user:password@host.internal/evidrug"


def test_explicit_database_driver_is_preserved() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        database_url="sqlite+aiosqlite:///test.db",
    )

    assert settings.database_url == "sqlite+aiosqlite:///test.db"
