from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import search as search_index
from ..db import get_db
from ..deps import current_user
from ..models import User

router = APIRouter(prefix="/api/search", tags=["search"])


@router.get("")
def find(q: str, limit: int = 10, db: Session = Depends(get_db), _: User = Depends(current_user)):
    q = q.strip()
    if len(q) < 2:
        raise HTTPException(status_code=422, detail="Запрос короче двух символов")
    return search_index.search(db, q[:200], limit)
