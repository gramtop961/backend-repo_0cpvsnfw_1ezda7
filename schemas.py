"""
Database Schemas for OPTCG App

Each Pydantic model represents a collection in MongoDB. The collection name is the lowercase of the class name.
"""
from pydantic import BaseModel, Field
from typing import Optional, List, Literal
from datetime import datetime

Currency = Literal["USD", "EUR"]

class Card(BaseModel):
    """
    Cards reference as discovered from external sources
    Collection name: "card"
    """
    id_code: Optional[str] = Field(None, description="Card code like OP05-119")
    name: Optional[str] = Field(None, description="Card name")
    language: Optional[Literal["EN", "JP", "Other"]] = Field(None, description="Language of the card")
    source: Literal["cardmarket", "pricecharting", "cardtrader", "collectr"] = Field(...)
    source_url: str = Field(..., description="Deep link to the source product page")
    image_url: Optional[str] = Field(None, description="Image URL scraped from the source")

class CollectionEntry(BaseModel):
    """
    User collection entries
    Collection name: "collectionentry"
    """
    card_id: Optional[str] = Field(None, description="Reference to card _id (stringified)")
    id_code: Optional[str] = Field(None, description="Card code like OP05-119")
    name: Optional[str] = Field(None)
    language: Optional[str] = Field(None)
    source: Optional[str] = Field(None)
    source_url: Optional[str] = Field(None)
    image_url: Optional[str] = Field(None, description="Source image URL")
    custom_image_url: Optional[str] = Field(None, description="User uploaded custom image URL")

    quantity: int = Field(1, ge=1)
    purchase_price: float = Field(..., ge=0)
    purchase_currency: Currency = Field("USD")

    # Aggregates updated over time
    last_known_price: Optional[float] = Field(None, ge=0)
    last_known_currency: Optional[Currency] = Field(None)

class Sale(BaseModel):
    """
    Sale transactions to compute realized P&L
    Collection name: "sale"
    """
    collection_entry_id: str = Field(...)
    sale_price: float = Field(..., ge=0)
    sale_currency: Currency = Field("USD")
    quantity: int = Field(1, ge=1)
    sold_at: datetime = Field(default_factory=datetime.utcnow)
