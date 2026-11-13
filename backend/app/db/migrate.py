"""openGauss schema management: bootstrap (user / PG-compatible database / schema) and
versioned migrations from app/db/migrations/*.sql."""

from datetime import UTC, datetime
from importlib import resources

from psycopg import AsyncConnection, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo


class CompatibilityError(RuntimeError):
    pass


async def bootstrap(admin_dsn: str, app_dsn: str) -> list[str]:
    """Create the app user, database (DBCOMPATIBILITY 'PG' on openGauss) and a schema named
    after the user, using an administrator login. Idempotent; returns actions taken."""
    app = conninfo_to_dict(app_dsn)
    user, password, dbname = app.get("user"), app.get("password"), app.get("dbname")
    if not (user and password and dbname):
        raise ValueError("OPENGAUSS_DSN must contain user, password and dbname")
    done: list[str] = []
    async with await AsyncConnection.connect(admin_dsn, autocommit=True) as conn:
        opengauss = "openGauss" in (await (await conn.execute("SELECT version()")).fetchone())[0]
        if not await (await conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s",
                                         (user,))).fetchone():
            await conn.execute(sql.SQL("CREATE USER {} PASSWORD {}").format(
                sql.Identifier(user), sql.Literal(password)))
            done.append(f"created user {user}")
        if not await (await conn.execute("SELECT 1 FROM pg_database WHERE datname = %s",
                                         (dbname,))).fetchone():
            template = ("CREATE DATABASE {} DBCOMPATIBILITY 'PG' ENCODING 'UTF8' "
                        "TEMPLATE template0 OWNER {}" if opengauss
                        else "CREATE DATABASE {} ENCODING 'UTF8' TEMPLATE template0 OWNER {}")
            await conn.execute(sql.SQL(template).format(sql.Identifier(dbname),
                                                        sql.Identifier(user)))
            done.append(f"created database {dbname}")
    async with await AsyncConnection.connect(make_conninfo(admin_dsn, dbname=dbname),
                                             autocommit=True) as conn:
        if not await (await conn.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s",
                                         (user,))).fetchone():
            await conn.execute(sql.SQL("CREATE SCHEMA {} AUTHORIZATION {}").format(
                sql.Identifier(user), sql.Identifier(user)))
            done.append(f"created schema {user}")
    return done


async def migrate(dsn: str) -> list[str]:
    """Apply pending migrations in order (each in its own transaction)."""
    applied: list[str] = []
    async with await AsyncConnection.connect(dsn) as conn:
        try:
            async with conn.transaction():
                row = await (await conn.execute(
                    "SELECT datcompatibility FROM pg_database WHERE datname = current_database()"
                )).fetchone()
            if row and row[0] != "PG":
                raise CompatibilityError(
                    f"database compatibility is {row[0]!r}; it must be created with "
                    "DBCOMPATIBILITY 'PG' (in 'A' mode empty strings become NULL)")
        except CompatibilityError:
            raise
        except Exception:
            pass  # plain PostgreSQL has no datcompatibility column
        async with conn.transaction():
            await conn.execute("CREATE TABLE IF NOT EXISTS app_migrations ("
                               "version VARCHAR(64) PRIMARY KEY, "
                               "applied_at TIMESTAMP WITH TIME ZONE NOT NULL)")
        done = {r[0] for r in await (await conn.execute(
            "SELECT version FROM app_migrations")).fetchall()}
        for f in sorted(p for p in resources.files("app.db.migrations").iterdir()
                        if p.name.endswith(".sql")):
            version = f.name.removesuffix(".sql")
            if version in done:
                continue
            async with conn.transaction():
                await conn.execute(f.read_text(encoding="utf-8"))  # type: ignore[arg-type]
                await conn.execute("INSERT INTO app_migrations (version, applied_at) "
                                   "VALUES (%s, %s)", (version, datetime.now(UTC)))
            applied.append(version)
    return applied
