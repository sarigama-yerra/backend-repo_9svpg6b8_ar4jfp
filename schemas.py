"""
Database Schemas for Micro-delivery app

Each Pydantic model corresponds to a MongoDB collection with the lowercase
class name used as the collection name.
"""
from typing import Optional, List, Literal
from pydantic import BaseModel, Field
from datetime import datetime, date

# Core domain schemas

class User(BaseModel):
    name: str = Field(..., description="Full name")
    email: str = Field(..., description="Email address")
    address: Optional[str] = Field(None, description="Delivery address")
    phone: Optional[str] = Field(None, description="Phone number")
    is_active: bool = Field(True, description="Active user")

class Product(BaseModel):
    name: str = Field(..., description="Product name")
    price: float = Field(..., ge=0, description="Price per unit in currency")
    category: str = Field(..., description="Product category")
    image_url: Optional[str] = Field(None, description="Product image URL")
    available: bool = Field(True, description="Available for purchase")

class Transaction(BaseModel):
    user_id: str = Field(..., description="User identifier")
    type: Literal['credit', 'debit'] = Field(..., description="credit=top-up, debit=order")
    amount: float = Field(..., gt=0, description="Transaction amount")
    order_id: Optional[str] = Field(None, description="Linked order id for debits, if any")
    note: Optional[str] = Field(None, description="Optional description")

class OrderItem(BaseModel):
    product_id: str
    name: str
    price: float
    qty: int = Field(..., gt=0)

class Order(BaseModel):
    user_id: str
    items: List[OrderItem]
    subtotal: float
    delivery_date: date
    status: Literal['placed','packed','delivered','cancelled'] = 'placed'

class Subscription(BaseModel):
    user_id: str
    product_id: str
    qty: int = Field(..., gt=0)
    schedule: Literal['daily','weekday','weekend'] = 'daily'
    active: bool = True
