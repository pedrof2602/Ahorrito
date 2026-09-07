from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ItemBase(BaseModel):
    title: str = Field(..., examples=["Manzanas rojas"])
    description: str | None = Field(None, examples=["1 kg de manzanas de estación"])
    quantity: int = Field(1, ge=1, examples=[2])
    price: float | None = Field(None, ge=0, examples=[150.50])


class ItemCreate(ItemBase):
    pass


class ItemResponse(ItemBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    status: str = "pending"
