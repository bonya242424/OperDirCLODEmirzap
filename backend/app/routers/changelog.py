from datetime import date

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..changelog import KINDS
from ..db import get_db
from ..deps import current_user
from ..models import ChangeLogEntry, User

router = APIRouter(prefix="/api/changelog", tags=["changelog"])


class EntryOut(BaseModel):
    id: int
    kind: str
    kind_label: str
    title: str
    text: str
    released_on: date


@router.get("", response_model=list[EntryOut])
def list_entries(limit: int = 200, db: Session = Depends(get_db), _: User = Depends(current_user)):
    rows = db.scalars(select(ChangeLogEntry).order_by(ChangeLogEntry.released_on.desc(), ChangeLogEntry.id.desc()).limit(min(max(limit, 1), 500)))
    return [EntryOut(id=r.id, kind=r.kind, kind_label=KINDS.get(r.kind, r.kind), title=r.title, text=r.text, released_on=r.released_on) for r in rows]
