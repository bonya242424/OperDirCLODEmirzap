from sqlalchemy import inspect, text

from app.db import add_missing_columns, engine


def test_adds_missing_columns_to_old_table(client):
    with engine.begin() as c:
        c.execute(text("ALTER TABLE metrics DROP COLUMN source_prompt"))
        c.execute(text("ALTER TABLE metrics DROP COLUMN calc_status"))
    added = add_missing_columns()
    assert "metrics.source_prompt" in added and "metrics.calc_status" in added
    cols = {c["name"] for c in inspect(engine).get_columns("metrics")}
    assert {"source_prompt", "calc_status"} <= cols
    assert add_missing_columns() == []  # повторный запуск ничего не меняет
