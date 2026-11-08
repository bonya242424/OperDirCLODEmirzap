from datetime import date

import pytest

from app.assembly_run import _ask_owner, copy_confirmation, resolve_copy_confirmations
from app.db import SessionLocal
from app.models import AssemblyQuestion, AssemblyRun


READY = "На экране выделена целиком печатная форма. Таблица товаров видна. Неуверенности в следующем действии нет: осталось Control+C, проверить rdp_clipboard и немедленно save_document. Остановился перед копированием. Подтвердите продолжение."


@pytest.mark.parametrize("text", [READY, "Можно скопировать печатную форму?", "Разрешите save_document", "Подтвердите продолжение копирования"])
def test_routine_copy_permission_is_recognized(text):
    assert copy_confirmation(text)


@pytest.mark.parametrize("text", ["Где кнопка печатной формы?", "Control+C не работает, буфер пустой, как выделить таблицу?", "Ошибка подключения, что делать?"])
def test_real_obstacles_are_not_suppressed(text):
    assert not copy_confirmation(text)


def test_copy_question_is_not_created_or_waited_for(client, owner):
    with SessionLocal() as db:
        run = AssemblyRun(connection_id=1, date_from=date(2026, 11, 6), date_to=date(2026, 11, 6))
        db.add(run)
        db.commit()
        result = _ask_owner(db, run, READY, lambda _: None)
        assert "уже разрешены" in result
        assert db.query(AssemblyQuestion).count() == 0


def test_update_closes_only_pending_copy_confirmation(client, owner):
    with SessionLocal() as db:
        run = AssemblyRun(connection_id=1, date_from=date(2026, 11, 6), date_to=date(2026, 11, 6))
        db.add(run)
        db.flush()
        copy = AssemblyQuestion(run_id=run.id, question=READY)
        problem = AssemblyQuestion(run_id=run.id, question="Не знаю, где печатная форма")
        db.add_all([copy, problem])
        db.commit()
        ids = copy.id, problem.id
    resolve_copy_confirmations()
    with SessionLocal() as db:
        assert db.get(AssemblyQuestion, ids[0]).answer
        assert db.get(AssemblyQuestion, ids[1]).answer is None
