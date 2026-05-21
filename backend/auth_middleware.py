"""
全局认证中间件
所有 /api/* 请求（除白名单外）必须携带有效 JWT token
guest 角色仅允许 GET 请求（只读浏览）
"""
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from backend.api.auth import _decode_token

# 不需要鉴权的路径前缀
_PUBLIC_PREFIXES = (
    "/api/auth/login",
    "/api/auth/status",
    "/api/img-proxy",
)

# guest 允许写操作的路径（改自己密码）
_GUEST_WRITE_WHITELIST = (
    "/api/auth/me",
)


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        method = request.method

        # 非 API 路径（前端静态文件）放行
        if not path.startswith("/api/"):
            return await call_next(request)

        # 公开端点放行
        if any(path.startswith(p) for p in _PUBLIC_PREFIXES):
            return await call_next(request)

        # 检查 Authorization header；SSE（EventSource）不支持自定义 header，
        # 允许通过 ?token=xxx 查询参数传递 JWT
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
        else:
            token = request.query_params.get("token", "")
        if not token:
            return JSONResponse(status_code=401, content={"detail": "未登录"})
        payload = _decode_token(token)
        if not payload:
            return JSONResponse(status_code=401, content={"detail": "登录已过期"})

        # guest 角色只读限制
        if payload.get("role") == "guest" and method not in ("GET", "HEAD", "OPTIONS"):
            if not any(path.startswith(p) for p in _GUEST_WRITE_WHITELIST):
                return JSONResponse(status_code=403, content={"detail": "访客账号只有浏览权限"})

        return await call_next(request)
