from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import current_user
from ..llm import LLMError, get_provider
from ..models import ChatMessage, User
from ..schemas import ChatIn, ChatMessageOut, ChatOut

router = APIRouter(prefix="/api/chat", tags=["chat"])

HISTORY_LIMIT = 20

SYSTEM_PROMPT = (
    "Ты ИИ-ассистент руководителей компании внутри их рабочей системы. Отвечай по-русски, "
    "коротко и по делу. Сейчас у тебя нет доступа к показателям, задачам и файлам компании: "
    "эти источники ещё не подключены к чату. Если спрашивают конкретные цифры или факты "
    "компании, прямо скажи, что данных у тебя пока нет, и не выдумывай их."
)


@router.get("/history", response_model=list[ChatMessageOut])
def history(db: Session = Depends(get_db), me: User = Depends(current_user)):
    rows = db.scalars(
        select(ChatMessage)
        .where(ChatMessage.user_id == me.id)
        .order_by(ChatMessage.id.desc())
        .limit(50)
    ).all()
    return list(reversed(rows))


@router.post("", response_model=ChatOut)
def send(body: ChatIn, db: Session = Depends(get_db), me: User = Depends(current_user)):
    provider = get_provider()
    if provider is None:
        raise HTTPException(status_code=503, detail="ИИ не подключён: не задан ключ API модели (ANTHROPIC_API_KEY)")

    past = db.scalars(
        select(ChatMessage)
        .where(ChatMessage.user_id == me.id)
        .order_by(ChatMessage.id.desc())
        .limit(HISTORY_LIMIT)
    ).all()
    messages = [{"role": m.role, "content": m.content} for m in reversed(past)]
    # Модель требует, чтобы диалог начинался с реплики пользователя.
    while messages and messages[0]["role"] != "user":
        messages.pop(0)
    messages.append({"role": "user", "content": body.message})

    try:
        answer = provider.complete(SYSTEM_PROMPT, messages)
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    # Обе реплики сохраняем только после успешного ответа, чтобы чередование не ломалось.
    db.add(ChatMessage(user_id=me.id, role="user", content=body.message))
    reply = ChatMessage(user_id=me.id, role="assistant", content=answer)
    db.add(reply)
    db.commit()
    return ChatOut(reply=ChatMessageOut.model_validate(reply))
