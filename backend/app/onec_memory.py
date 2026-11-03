"""Общая память по 1С: что где находится и как делается. Одна на всех агентов, которые работают в 1С.

Агент сам записывает сюда проверенные на экране находки (инструмент remember_1c), владелец правит их в «Инструкции 1С».
Любой агент по 1С (сборка сейчас, другие действия потом) получает её первой в поручении через knowledge_text().
Это данные, а не команды: модели передаются внутри тегов и могут быть неточными.
"""

import re
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .models import InstrSection, InstrVideo, utcnow

AREA = "1c"
MEMORY_TITLE = "Память агентов 1С: где что находится и как делается"
QA_TITLE = "Уточнения агента (вопросы и ответы владельца)"
MEMORY_LIMIT = 15000

REMEMBER_TOOL = {
    "name": "remember_1c",
    "description": (
        "Запомнить проверенное на экране знание о 1С для всех будущих агентов: где находится раздел, журнал, кнопка, отбор, "
        "как сделать действие, что делать с окном или ошибкой. Пиши только то, что сработало. Одна тема — одна запись; "
        "та же тема перезаписывается. Пример: topic «Журнал Документы Сборка», how «Меню Склад → Сборки; период: кнопка «Установить период» над списком»."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"topic": {"type": "string", "description": "что (раздел, действие, окно)"}, "how": {"type": "string", "description": "где это и как сделать, по шагам"}},
        "required": ["topic", "how"],
    },
}


def _key(topic: str) -> str:
    return re.sub(r"[^\wа-яё]+", " ", topic.lower()).strip()


def memory_section(db) -> InstrSection:
    sec = db.scalar(select(InstrSection).where(InstrSection.area == AREA, InstrSection.title == MEMORY_TITLE))
    if sec is None:
        sec = InstrSection(area=AREA, title=MEMORY_TITLE, notes="", created_by=None)
        db.add(sec)
        db.flush()
    return sec


def remember(db, topic: str, how: str, who: str = "агент") -> str:
    """Добавляет или обновляет запись «• тема: как (дата, кто)». Возвращает текст для модели."""
    topic, how = " ".join(topic.split())[:150], " ".join(how.split())[:800]
    if len(topic) < 3 or len(how) < 5:
        return "Ошибка: нужны тема и описание, как это сделать"
    sec = memory_section(db)
    day = utcnow().astimezone(ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y")
    entry = f"• {topic}: {how} ({day}, {who})"
    lines = [ln for ln in (sec.notes or "").split("\n") if ln.strip()]
    key = _key(topic)
    replaced = False
    for i, ln in enumerate(lines):
        if ln.startswith("• ") and _key(ln[2:].split(":", 1)[0]) == key:
            lines[i], replaced = entry, True
            break
    if not replaced:
        lines.append(entry)
    text = "\n".join(lines)
    while len(text) > MEMORY_LIMIT and len(lines) > 1:  # самые старые записи уходят первыми
        lines.pop(0)
        text = "\n".join(lines)
    sec.notes = text
    db.commit()
    return "Запомнил: обновил запись" if replaced else "Запомнил"


def knowledge_text(db, limit: int = 15000) -> str:
    """Всё, что известно про 1С: сначала память агентов, потом ответы владельца, потом инструкции и видео."""
    memory, qa, rest = "", "", []
    for sec in db.scalars(select(InstrSection).where(InstrSection.area == AREA).order_by(InstrSection.id)):
        if sec.title == MEMORY_TITLE:
            memory = f"### {sec.title}\n{sec.notes or 'пока пусто'}"
            continue
        if sec.title == QA_TITLE:
            qa = f"### {sec.title}\n{sec.notes or ''}"
            continue
        vids = [v.text for v in db.scalars(select(InstrVideo).where(InstrVideo.section_id == sec.id, InstrVideo.status == "ok")) if v.text]
        rest.append(f"### {sec.title}\nОписание владельца: {sec.notes or 'нет'}\n" + ("\n".join(vids) if vids else ""))
    head = "\n\n".join(p for p in (memory[:6000], qa[-4000:]) if p)
    return (head + "\n\n" + "\n\n".join(rest))[:limit].strip()
