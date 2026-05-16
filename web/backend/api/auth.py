"""
认证模块
JWT 登录闸门 + 用户管理
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
from pydantic import BaseModel
from sqlalchemy.orm import Session

from web.backend.config import settings
from web.backend.database import get_db, User

logger = logging.getLogger(__name__)

router = APIRouter()

security = HTTPBearer(auto_error=False)

ALGORITHM = "HS256"


# ==================== Schemas ====================

class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    token: str
    username: str
    role: str


class UserInfo(BaseModel):
    id: int
    username: str
    role: str
    created_at: datetime


class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str = "guest"


class ChangePasswordRequest(BaseModel):
    password: str


# ==================== 工具函数 ====================

def _hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def _verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def _create_token(username: str, role: str) -> str:
    expire = datetime.utcnow() + timedelta(hours=settings.auth_token_expire_hours)
    payload = {"sub": username, "role": role, "exp": expire}
    return jwt.encode(payload, settings.auth_secret_key, algorithm=ALGORITHM)


def _decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, settings.auth_secret_key, algorithms=[ALGORITHM])
    except JWTError:
        return None


# ==================== 依赖注入 ====================

def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: Session = Depends(get_db),
) -> User:
    """从 JWT token 解析当前用户，失败则 401"""
    if not credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录")
    payload = _decode_token(credentials.credentials)
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已过期")
    user = db.query(User).filter(User.username == payload["sub"]).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在")
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    """要求管理员权限"""
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return user


# ==================== 启动时同步用户 ====================

def sync_users_from_config():
    """将 config.yaml 中的用户同步到数据库（仅新增，不覆盖已有）"""
    from web.backend.database import SessionLocal
    db = SessionLocal()
    try:
        for u in settings.auth_users:
            username = u.get('username', '').strip()
            password = u.get('password', '')
            role = u.get('role', 'guest')
            if not username or not password:
                continue
            existing = db.query(User).filter(User.username == username).first()
            if existing:
                continue
            user = User(
                username=username,
                password_hash=_hash_password(password),
                role=role,
            )
            db.add(user)
            logger.info(f"从配置同步用户: {username} (role={role})")
        db.commit()
    finally:
        db.close()


# ==================== 路由 ====================

@router.post("/login", response_model=LoginResponse)
def login(req: LoginRequest, db: Session = Depends(get_db)):
    """登录"""
    user = db.query(User).filter(User.username == req.username).first()
    if not user or not _verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    token = _create_token(user.username, user.role)
    return LoginResponse(token=token, username=user.username, role=user.role)


@router.get("/me", response_model=UserInfo)
def get_me(user: User = Depends(get_current_user)):
    """获取当前用户信息"""
    return UserInfo(id=user.id, username=user.username, role=user.role, created_at=user.created_at)


@router.get("/users", response_model=list[UserInfo])
def list_users(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """列出所有用户（管理员）"""
    users = db.query(User).order_by(User.id).all()
    return [UserInfo(id=u.id, username=u.username, role=u.role, created_at=u.created_at) for u in users]


@router.post("/users", response_model=UserInfo)
def create_user(req: CreateUserRequest, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """创建用户（管理员）"""
    if req.role not in ("admin", "guest"):
        raise HTTPException(status_code=400, detail="角色只能是 admin 或 guest")
    existing = db.query(User).filter(User.username == req.username).first()
    if existing:
        raise HTTPException(status_code=400, detail="用户名已存在")
    user = User(username=req.username, password_hash=_hash_password(req.password), role=req.role)
    db.add(user)
    db.commit()
    db.refresh(user)
    return UserInfo(id=user.id, username=user.username, role=user.role, created_at=user.created_at)


@router.put("/users/{user_id}/password")
def change_password(user_id: int, req: ChangePasswordRequest, db: Session = Depends(get_db), current: User = Depends(get_current_user)):
    """修改密码（管理员可改任何人，普通用户只能改自己）"""
    if current.role != "admin" and current.id != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权操作")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    user.password_hash = _hash_password(req.password)
    db.commit()
    return {"ok": True}


@router.delete("/users/{user_id}")
def delete_user(user_id: int, db: Session = Depends(get_db), current: User = Depends(require_admin)):
    """删除用户（管理员，不能删自己）"""
    if current.id == user_id:
        raise HTTPException(status_code=400, detail="不能删除自己")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    db.delete(user)
    db.commit()
    return {"ok": True}


@router.get("/status")
def auth_status(db: Session = Depends(get_db)):
    """公开端点：返回是否已有用户（前端判断是否需要引导）"""
    count = db.query(User).count()
    return {"has_users": count > 0}
