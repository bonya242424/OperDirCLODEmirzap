from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

_connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=_connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def add_missing_columns() -> list[str]:
    """Лёгкая замена миграциям: добавляет в существующие таблицы колонки, которых там ещё нет.

    Только добавление (ничего не удаляет и не меняет типы). Для NOT NULL колонок берётся значение по умолчанию из модели.
    """
    from sqlalchemy import inspect, text

    added: list[str] = []
    with engine.begin() as conn:
        insp = inspect(conn)
        existing = set(insp.get_table_names())
        for table in Base.metadata.sorted_tables:
            if table.name not in existing:
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col.type.compile(engine.dialect)}'
                default = getattr(col.default, "arg", None) if col.default is not None else None
                if default is not None and not callable(default):
                    if isinstance(default, bool):
                        ddl += f" DEFAULT {'TRUE' if default else 'FALSE'}" if engine.dialect.name == "postgresql" else f" DEFAULT {int(default)}"
                    elif isinstance(default, (int, float)):
                        ddl += f" DEFAULT {default}"
                    else:
                        ddl += " DEFAULT '" + str(default).replace("'", "''") + "'"
                elif not col.nullable:
                    kind = col.type.python_type
                    ddl += " DEFAULT " + ("0" if kind in (int, float) else "FALSE" if kind is bool and engine.dialect.name == "postgresql" else "0" if kind is bool else "''")
                conn.execute(text(ddl))
                added.append(f"{table.name}.{col.name}")
    return added
