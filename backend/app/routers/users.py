from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import current_user
from ..models import User
from ..schemas import UserCreate, UserOut, UserUpdate
from ..security import hash_password

router = APIRouter(prefix="/api/users", tags=["users"])

_PRIVILEGED_FIELDS = {"role", "can_apply_changes", "is_active"}


def _active_owners(db: Session) -> int:
    return db.scalar(
        select(func.count()).select_from(User).where(User.role == "owner", User.is_active.is_(True))
    ) or 0


@router.get("", response_model=list[UserOut])
def list_users(db: Session = Depends(get_db), _: User = Depends(current_user)):
    return db.scalars(select(User).order_by(User.id)).all()


@router.post("", response_model=UserOut, status_code=201)
def create_user(body: UserCreate, db: Session = Depends(get_db), me: User = Depends(current_user)):
    # Добавлять сотрудников может любой, но роли и права выдаёт только владелец.
    if (body.role != "employee" or body.can_apply_changes) and me.role != "owner":
        raise HTTPException(status_code=403, detail="Назначать роли и права может только владелец")
    if db.scalar(select(User).where(User.email == body.email)) is not None:
        raise HTTPException(status_code=409, detail="Сотрудник с таким email уже есть")
    user = User(
        email=body.email,
        password_hash=hash_password(body.password),
        full_name=body.full_name,
        position=body.position,
        department=body.department,
        role=body.role,
        can_apply_changes=body.can_apply_changes or body.role == "owner",
        bitrix_user_id=body.bitrix_user_id,
    )
    db.add(user)
    db.commit()
    return user


@router.patch("/{user_id}", response_model=UserOut)
def update_user(
    user_id: int,
    body: UserUpdate,
    db: Session = Depends(get_db),
    me: User = Depends(current_user),
):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Сотрудник не найден")
    data = body.model_dump(exclude_unset=True)

    if _PRIVILEGED_FIELDS & data.keys() and me.role != "owner":
        raise HTTPException(status_code=403, detail="Менять роли, права и доступ может только владелец")
    if "password" in data and data["password"] is None:
        del data["password"]
    if "password" in data and me.id != user.id and me.role != "owner":
        raise HTTPException(status_code=403, detail="Пароль другого сотрудника может сменить только владелец")

    new_role = data.get("role", user.role)
    new_active = data.get("is_active", user.is_active)
    losing_owner = user.role == "owner" and user.is_active and (new_role != "owner" or not new_active)
    if losing_owner and _active_owners(db) <= 1:
        raise HTTPException(status_code=400, detail="Нельзя убрать последнего владельца")

    if "password" in data:
        user.password_hash = hash_password(data.pop("password"))
    for field, value in data.items():
        setattr(user, field, value)
    if user.role == "owner":
        user.can_apply_changes = True
    db.commit()
    return user
