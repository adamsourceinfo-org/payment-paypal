"""服務自己的錯誤型別與對外語意。

錯誤語意的兩個刻意選擇：
- 無效的 API key 一律 401 且不區分原因（不幫攻擊者縮小範圍）
- 查詢別人的資源回 404 而不是 403（403 會洩漏「該資源存在」）
"""
from fastapi import HTTPException


class FieldError(ValueError):
    """帶欄位名的驗證錯誤 —— caller 要知道該改哪一個欄位，
    只回一句訊息的話他得自己猜。"""

    def __init__(self, message: str, field: str, code: str):
        self.field = field
        self.code = code
        super().__init__(message)

    def as_detail(self) -> dict:
        return {"error": self.code, "field": self.field, "message": str(self)}


class UnsupportedCurrency(FieldError):
    def __init__(self, message: str, field: str = "currency"):
        super().__init__(message, field, "unsupported_currency")


class InvalidAmount(FieldError):
    def __init__(self, message: str, field: str = "amount"):
        super().__init__(message, field, "invalid_amount")


class PayPalError(Exception):
    """PayPal 回非 2xx。debug_id 是向 PayPal 客服查詢的唯一憑據，一定要留。"""

    def __init__(self, status: int, name: str = "", debug_id: str = "",
                 details=None, message: str = ""):
        self.status = status
        self.name = name
        self.debug_id = debug_id
        self.details = details or []
        self.message = message
        super().__init__(f"PayPal {status} {name} debug_id={debug_id}")

    @property
    def issues(self) -> list:
        return [d.get("issue") for d in self.details if isinstance(d, dict)]


def not_found(what: str = "resource") -> HTTPException:
    # 別人的資源也走這裡 —— 對呼叫者來說「不存在」與「不屬於你」不該有分別
    return HTTPException(status_code=404, detail=f"{what} not found")


def bad_request(detail) -> HTTPException:
    """detail 可以是字串，也可以是 FieldError —— 後者會展開成
    {"error", "field", "message"}，讓 caller 知道要改哪個欄位。"""
    if isinstance(detail, FieldError):
        return HTTPException(status_code=400, detail=detail.as_detail())
    return HTTPException(status_code=400, detail=detail)


def upstream_error(exc: PayPalError) -> HTTPException:
    return HTTPException(
        status_code=502,
        detail={"error": "paypal_upstream", "paypal_name": exc.name,
                "paypal_debug_id": exc.debug_id, "issues": exc.issues},
    )
