import os
from typing import List, Optional

import re
import requests
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Minimal, robust backend: avoid heavy optional deps to ensure boot reliability.
from database import create_document, get_documents, db
from schemas import CollectionEntry

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


# ---------- Cardmarket scraping (regex-lite) ----------
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


_id_pat = re.compile(r"OP\d{2}-\d{3}", re.IGNORECASE)
_lang_hint = re.compile(r"(english|japanese)", re.IGNORECASE)


def parse_cardmarket_search_regex(html: str) -> List[SearchResult]:
    results: List[SearchResult] = []
    for m in re.finditer(r"<a[^>]+href=\"(/en/OnePiece/[^"]+)\"[^>]*>(.*?)</a>", html, re.IGNORECASE | re.DOTALL):
        href = m.group(1)
        text = re.sub("<[^>]+>", " ", m.group(2)).strip()
        start = m.end()
        snippet = html[start:start+400]
        img_match = re.search(r"<img[^>]+(?:data-src|src)=\"([^\"]+)\"", snippet, re.IGNORECASE)
        img_url = img_match.group(1) if img_match else None

        id_code = None
        idm = _id_pat.search(text)
        if idm:
            id_code = idm.group(0).upper()

        language = None
        langm = _lang_hint.search(text)
        if langm:
            language = "EN" if langm.group(1).lower() == "english" else ("JP" if langm.group(1).lower()=="japanese" else None)

        full_url = "https://www.cardmarket.com" + href.split("?")[0]
        if not any(r.source_url == full_url for r in results):
            results.append(SearchResult(id_code=id_code, name=text or None, language=language, image_url=img_url, source_url=full_url))
    return results[:48]


@app.get("/api/search/cardmarket", response_model=List[SearchResult])
def search_cardmarket(q: str):
    # Always try remote search first
    try:
        url = f"https://www.cardmarket.com/en/OnePiece/Products/Search?searchString={requests.utils.quote(q)}"
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code == 200:
            parsed = parse_cardmarket_search_regex(r.text)
            if parsed:
                return parsed
        # If blocked/unavailable or parsed empty, fall back gracefully
    except Exception:
        pass

    # Fallback 1: if query looks like a card id, return a synthetic stub pointing to Cardmarket search
    stub: List[SearchResult] = []
    if _id_pat.search(q or ""):
        stub.append(SearchResult(
            id_code=_id_pat.search(q).group(0).upper(),
            name=q.strip(),
            language=None,
            image_url=None,
            source_url=f"https://www.cardmarket.com/en/OnePiece/Products/Search?searchString={requests.utils.quote(q)}",
        ))
    # Fallback 2: empty array (200 OK) so frontend can show "no results" without error
    return stub


# ---------- Image upload & matching ----------
@app.post("/api/upload-image")
def upload_image(file: UploadFile = File(...)):
    try:
        filename = (file.filename or "upload").replace(" ", "_")
        base, ext = os.path.splitext(filename)
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", base) + (ext if ext else "")
        out_path = os.path.join(UPLOAD_DIR, safe)
        with open(out_path, "wb") as f:
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
        url = f"/uploads/{os.path.basename(out_path)}"
        return {"url": url}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid image: {e}")


@app.post("/api/search/by-image", response_model=List[SearchResult])
def search_by_image(file: UploadFile = File(...), q: Optional[str] = Form(None)):
    # Degraded behavior: image-based matching not implemented in this environment
    raise HTTPException(status_code=501, detail="Image search is temporarily unavailable in this environment.")


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
        filename = f"custom_{entry_id}.bin"
        out_path = os.path.join(UPLOAD_DIR, filename)
        with open(out_path, "wb") as f:
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
        url = f"/uploads/{os.path.basename(out_path)}"
        # Update DB reference
        from bson import ObjectId
        db["collectionentry"].update_one({"_id": ObjectId(entry_id)}, {"$set": {"custom_image_url": url}})
        return {"custom_image_url": url}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
