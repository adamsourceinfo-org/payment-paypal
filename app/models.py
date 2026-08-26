"""對外的 request / response schema。金額一律用字串傳遞 ——
浮點數不該碰錢，而 JSON 的 number 在很多語言裡就是浮點數。"""
from typing import Optional

from pydantic import BaseModel, Field


class OrderCreate(BaseModel):
    reference_id: str = Field(min_length=1, max_length=100,
                              description="caller 提供的冪等鍵，同一個值只會建一筆")
    amount: str = Field(description='字串金額，例如 "10.00"')
    currency: str = Field(description="必填，沒有預設值")
    description: Optional[str] = Field(default=None, max_length=127)
    return_url: Optional[str] = None
    cancel_url: Optional[str] = None


class RefundCreate(BaseModel):
    amount: Optional[str] = Field(default=None, description="省略代表全額退款")
    note: Optional[str] = Field(default=None, max_length=255)


class PlanCreate(BaseModel):
    name: str = Field(min_length=1, max_length=127)
    amount: str
    currency: str
    interval_count: int = Field(default=1, ge=1, le=12,
                                description="每幾個月收一次")
    description: Optional[str] = Field(default=None, max_length=256)


class SubscriptionCreate(BaseModel):
    reference_id: str = Field(min_length=1, max_length=100)
    plan_id: str
    subscriber_email: Optional[str] = None
    return_url: Optional[str] = None
    cancel_url: Optional[str] = None


class WebhookEndpointPut(BaseModel):
    url: str = Field(
        min_length=1, max_length=2000,
        description="推送目標，只收 https://。內網位址與 .internal 一律拒絕")
