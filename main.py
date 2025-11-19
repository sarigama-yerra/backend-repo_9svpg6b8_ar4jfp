import os
from datetime import datetime, timedelta, time, date, timezone
from typing import List, Optional, Dict, Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from database import db, create_document, get_documents
from bson import ObjectId

app = FastAPI(title="Micro Delivery API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----- Config -----
CUTOFF_HOUR = int(os.getenv("CUTOFF_HOUR", 23))  # 11 PM default


# ----- Models -----
class TopUpRequest(BaseModel):
    user_id: str
    amount: float = Field(..., gt=0)
    note: Optional[str] = None


class PlaceOrderItem(BaseModel):
    product_id: str
    qty: int = Field(..., gt=0)


class PlaceOrderRequest(BaseModel):
    user_id: str
    items: List[PlaceOrderItem]


class ProductIn(BaseModel):
    name: str
    price: float = Field(..., ge=0)
    category: str
    image_url: Optional[str] = None
    available: bool = True


class ProductUpdate(BaseModel):
    name: Optional[str] = None
    price: Optional[float] = Field(None, ge=0)
    category: Optional[str] = None
    image_url: Optional[str] = None
    available: Optional[bool] = None


# ----- Helpers -----

def _oid(id_str: str) -> ObjectId:
    try:
        return ObjectId(id_str)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid id format")


def compute_wallet_balance(user_id: str) -> float:
    txns = get_documents("transaction", {"user_id": user_id})
    balance = 0.0
    for t in txns:
        amt = float(t.get("amount", 0))
        if t.get("type") == "credit":
            balance += amt
        elif t.get("type") == "debit":
            balance -= amt
    return round(balance, 2)


def get_delivery_date(now: datetime) -> date:
    cutoff = time(hour=CUTOFF_HOUR, minute=0)
    if now.time() < cutoff:
        return (now + timedelta(days=1)).date()
    else:
        return (now + timedelta(days=2)).date()


# ----- Routes -----
@app.get("/")
def read_root():
    return {"message": "Micro Delivery Backend Running"}


@app.get("/api/config")
def get_config():
    now = datetime.now()
    delivery = get_delivery_date(now)
    return {
        "server_time": now.isoformat(),
        "cutoff_hour": CUTOFF_HOUR,
        "expected_delivery_date": delivery.isoformat(),
    }


# Wallet
@app.get("/api/wallet/balance")
def wallet_balance(user_id: str = Query(...)):
    return {"user_id": user_id, "balance": compute_wallet_balance(user_id)}


@app.post("/api/wallet/topup")
def wallet_topup(req: TopUpRequest):
    txn = {
        "user_id": req.user_id,
        "type": "credit",
        "amount": float(req.amount),
        "note": req.note or "Top-up",
    }
    txn_id = create_document("transaction", txn)
    return {"transaction_id": txn_id, "new_balance": compute_wallet_balance(req.user_id)}


# Products (Admin)
@app.get("/api/products")
def list_products():
    products = get_documents("product", {})
    for p in products:
        p["id"] = str(p.pop("_id"))
    return products


@app.post("/api/products")
def create_product(p: ProductIn):
    pid = create_document("product", p.model_dump())
    return {"id": pid}


@app.put("/api/products/{product_id}")
def update_product(product_id: str, upd: ProductUpdate):
    data = {k: v for k, v in upd.model_dump().items() if v is not None}
    if not data:
        return {"updated": False}
    res = db["product"].update_one({"_id": _oid(product_id)}, {"$set": data})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Product not found")
    return {"updated": True}


@app.delete("/api/products/{product_id}")
def delete_product(product_id: str):
    res = db["product"].delete_one({"_id": _oid(product_id)})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Product not found")
    return {"deleted": True}


# Orders
@app.post("/api/orders/place")
def place_order(req: PlaceOrderRequest):
    if not req.items:
        raise HTTPException(status_code=400, detail="No items in order")

    # Build items with verified product data
    product_ids = [i.product_id for i in req.items]
    lookup = {}
    for pid in product_ids:
        prod = db["product"].find_one({"_id": _oid(pid)})
        if not prod:
            raise HTTPException(status_code=404, detail=f"Product {pid} not found")
        if not prod.get("available", True):
            raise HTTPException(status_code=400, detail=f"Product {prod.get('name','')} unavailable")
        lookup[pid] = prod

    order_items: List[Dict[str, Any]] = []
    subtotal = 0.0
    for it in req.items:
        prod = lookup[it.product_id]
        price = float(prod.get("price", 0))
        line_total = price * it.qty
        subtotal += line_total
        order_items.append({
            "product_id": it.product_id,
            "name": prod.get("name"),
            "price": price,
            "qty": it.qty,
        })
    subtotal = round(subtotal, 2)

    # Check wallet
    balance = compute_wallet_balance(req.user_id)
    if balance < subtotal:
        short = round(subtotal - balance, 2)
        raise HTTPException(status_code=402, detail={
            "message": "Insufficient wallet balance",
            "required_topup": short,
            "balance": balance,
            "subtotal": subtotal,
        })

    # Delivery date
    now = datetime.now()
    delivery = get_delivery_date(now)

    # Create order
    order_doc = {
        "user_id": req.user_id,
        "items": order_items,
        "subtotal": subtotal,
        "delivery_date": delivery.isoformat(),
        "status": "placed",
    }
    order_id = create_document("order", order_doc)

    # Create debit transaction
    debit = {
        "user_id": req.user_id,
        "type": "debit",
        "amount": subtotal,
        "order_id": order_id,
        "note": f"Order {order_id} payment",
    }
    create_document("transaction", debit)

    new_balance = compute_wallet_balance(req.user_id)
    return {
        "order_id": order_id,
        "delivery_date": delivery.isoformat(),
        "subtotal": subtotal,
        "balance": new_balance,
        "status": "confirmed",
    }


@app.get("/api/orders/summary-next-morning")
def summary_next_morning():
    # Consolidate items for orders scheduled for "tomorrow" (from server time)
    tomorrow = (datetime.now().date() + timedelta(days=1)).isoformat()
    orders = get_documents("order", {"delivery_date": tomorrow, "status": {"$in": ["placed", "packed"]}})

    consolidated: Dict[str, Dict[str, Any]] = {}
    for o in orders:
        for item in o.get("items", []):
            pid = item.get("product_id")
            key = pid
            if key not in consolidated:
                consolidated[key] = {
                    "product_id": pid,
                    "name": item.get("name"),
                    "total_qty": 0,
                }
            consolidated[key]["total_qty"] += int(item.get("qty", 0))

    return {
        "date": tomorrow,
        "items": list(consolidated.values()),
        "order_count": len(orders),
    }


@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": []
    }
    try:
        if db is not None:
            response["database"] = "✅ Available"
            response["database_url"] = "✅ Configured"
            response["database_name"] = db.name if hasattr(db, 'name') else "✅ Connected"
            response["connection_status"] = "Connected"
            try:
                collections = db.list_collection_names()
                response["collections"] = collections[:10]
                response["database"] = "✅ Connected & Working"
            except Exception as e:
                response["database"] = f"⚠️  Connected but Error: {str(e)[:50]}"
        else:
            response["database"] = "⚠️  Available but not initialized"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:50]}"

    response["database_url"] = "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set"
    response["database_name"] = "✅ Set" if os.getenv("DATABASE_NAME") else "❌ Not Set"
    return response


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
