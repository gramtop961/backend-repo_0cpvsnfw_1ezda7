import os
from io import BytesIO
from typing import List, Optional

import requests
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from bs4 import BeautifulSoup
from PIL import Image
import imagehash

from database import create_document, get_documents, db
from schemas import CollectionEntry, Card

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Ensure uploads directory exists
UPLOAD_DIR = os.path.join(os.getcwd(), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")


@app.get("/")
def read_root():
    return {"message": "OPTCG Collector API running"}


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
            response["database"] = "✅ Connected & Working"
            response["database_url"] = "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set"
            response["database_name"] = db.name
            response["connection_status"] = "Connected"
            response["collections"] = db.list_collection_names()[:10]
        else:
            response["database"] = "⚠️ Available but not initialized"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:80]}"
    return response


# ---------- Currency ----------
@app.get("/api/rate")
def get_rate(frm: str = "USD", to: str = "EUR"):
    try:
        r = requests.get(f"https://api.exchangerate.host/convert?from={frm}&to={to}", timeout=10)
        data = r.json()
        if not data.get("success", True):
            raise Exception("Rate API error")
        return {"from": frm.upper(), "to": to.upper(), "rate": float(data.get("result"))}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch rate: {e}")


# ---------- Cardmarket scraping (basic) ----------
class SearchResult(BaseModel):
    id_code: Optional[str] = None
    name: Optional[str] = None
    language: Optional[str] = None
    image_url: Optional[str] = None
    source_url: str
    source: str = "cardmarket"


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}


def parse_cardmarket_search(html: str) -> List[SearchResult]:
    soup = BeautifulSoup(html, "lxml")
    results: List[SearchResult] = []
    product_rows = soup.select(".table-body .row, .product-list .row")
    if not product_rows:
        product_rows = soup.select(".search-results .row")
    for row in product_rows:
        link = row.select_one("a[href*='/en/OnePiece/Products']") or row.select_one("a[href*='/en/OnePiece']")
        if not link:
            continue
        href = "https://www.cardmarket.com" + link.get("href").split("?")[0]
        name = link.get_text(strip=True) or None
        # try find image
        img = row.select_one("img")
        img_url = None
        if img and img.get("data-src"):
            img_url = img.get("data-src")
        elif img and img.get("src"):
            img_url = img.get("src")
        # Try detect id code pattern like OP05-119
        id_code = None
        text = row.get_text(" ", strip=True)
        import re
        m = re.search(r"OP\d{2}-\d{3}", text, re.IGNORECASE)
        if m:
            id_code = m.group(0).upper()
        # language heuristic
        language = None
        lang_el = row.select_one(".product-attributes img[title]")
        if lang_el:
            title = lang_el.get("title", "").lower()
            if "english" in title:
                language = "EN"
            elif "japanese" in title:
                language = "JP"
        results.append(SearchResult(id_code=id_code, name=name, language=language, image_url=img_url, source_url=href))
    return results


@app.get("/api/search/cardmarket", response_model=List[SearchResult])
def search_cardmarket(q: str):
    url = f"https://www.cardmarket.com/en/OnePiece/Products/Search?searchString={requests.utils.quote(q)}"
    r = requests.get(url, headers=HEADERS, timeout=15)
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail="Cardmarket unreachable")
    return parse_cardmarket_search(r.text)


# ---------- Image upload & matching ----------
@app.post("/api/upload-image")
def upload_image(file: UploadFile = File(...)):
    try:
        contents = file.file.read()
        image = Image.open(BytesIO(contents)).convert("RGB")
        # save as webp for size
        base_name = os.path.splitext(file.filename or "upload")[0]
        safe_name = base_name.replace(" ", "_")
        out_path = os.path.join(UPLOAD_DIR, f"{safe_name}.webp")
        image.save(out_path, format="WEBP", quality=85)
        url = f"/uploads/{os.path.basename(out_path)}"
        return {"url": url}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid image: {e}")


@app.post("/api/search/by-image", response_model=List[SearchResult])
def search_by_image(file: UploadFile = File(...), q: Optional[str] = Form(None)):
    try:
        target = Image.open(file.file).convert("RGB")
        target_hash = imagehash.average_hash(target)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid image upload")

    if not q:
        raise HTTPException(status_code=400, detail="Image search requires a query to narrow results.")

    candidates = search_cardmarket(q)

    matched: List[SearchResult] = []
    for c in candidates:
        if not c.image_url:
            continue
        try:
            img_resp = requests.get(c.image_url, headers=HEADERS, timeout=15)
            if img_resp.status_code != 200:
                continue
            cand_img = Image.open(BytesIO(img_resp.content)).convert("RGB")
            cand_hash = imagehash.average_hash(cand_img)
            dist = target_hash - cand_hash
            # Exact or near-exact match threshold
            if dist <= 2:
                if c.language in ("EN", "JP"):
                    matched.append(c)
        except Exception:
            continue
    return matched


# ---------- Collection CRUD ----------
@app.get("/api/collection")
def list_collection():
    try:
        docs = get_documents("collectionentry", {})
        for d in docs:
            d["_id"] = str(d.get("_id"))
        return docs
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class AddToCollectionPayload(BaseModel):
    id_code: Optional[str] = None
    name: Optional[str] = None
    language: Optional[str] = None
    image_url: Optional[str] = None
    source_url: Optional[str] = None
    source: Optional[str] = "cardmarket"
    quantity: int = 1
    purchase_price: float
    purchase_currency: str = "USD"


@app.post("/api/collection")
def add_to_collection(payload: AddToCollectionPayload):
    try:
        entry = CollectionEntry(
            id_code=payload.id_code,
            name=payload.name,
            language=payload.language,
            source=payload.source,
            source_url=payload.source_url,
            image_url=payload.image_url,
            quantity=payload.quantity,
            purchase_price=payload.purchase_price,
            purchase_currency=payload.purchase_currency.upper(),
        )
        new_id = create_document("collectionentry", entry)
        return {"_id": new_id}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.put("/api/collection/{entry_id}/image")
def set_custom_image(entry_id: str, file: UploadFile = File(...)):
    try:
        contents = file.file.read()
        image = Image.open(BytesIO(contents)).convert("RGB")
        out_path = os.path.join(UPLOAD_DIR, f"custom_{entry_id}.webp")
        image.save(out_path, format="WEBP", quality=85)
        url = f"/uploads/{os.path.basename(out_path)}"
        db["collectionentry"].update_one({"_id": __import__("bson").ObjectId(entry_id)}, {"$set": {"custom_image_url": url}})
        return {"custom_image_url": url}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
