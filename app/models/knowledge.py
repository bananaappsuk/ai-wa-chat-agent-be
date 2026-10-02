from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from app.config import settings


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: Optional[str] = Field(default=None, max_length=500)
    # How many knowledge chunks to give the AI per reply (Retell: 1–10, default 3).
    chunks_to_retrieve: int = Field(default=settings.KB_DEFAULT_CHUNKS_TO_RETRIEVE, ge=1, le=10)
    # Minimum relevance (0–1) for a chunk to be used. Higher = stricter.
    similarity_threshold: float = Field(default=settings.KB_DEFAULT_SIMILARITY_THRESHOLD, ge=0, le=1)
    # Re-fetch website sources every KB_REFRESH_HOURS.
    auto_refresh: bool = True


class KnowledgeBaseUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    description: Optional[str] = Field(default=None, max_length=500)
    chunks_to_retrieve: Optional[int] = Field(default=None, ge=1, le=10)
    similarity_threshold: Optional[float] = Field(default=None, ge=0, le=1)
    auto_refresh: Optional[bool] = None


def _clean_paths(v) -> list[str]:
    if not v:
        return []
    if isinstance(v, str):
        v = v.replace("\n", ",").split(",")
    out: list[str] = []
    for p in v:
        s = str(p).strip()[:200]
        if s:
            out.append(s if s.startswith("/") else "/" + s)
    return out[:20]


class CrawlOptions(BaseModel):
    include_paths: list[str] = Field(default_factory=list)
    exclude_paths: list[str] = Field(default_factory=list)
    max_pages: int = Field(default=settings.KB_CRAWL_DEFAULT_MAX_PAGES, ge=1, le=settings.KB_CRAWL_MAX_PAGES)
    max_depth: int = Field(default=3, ge=0, le=settings.KB_CRAWL_MAX_DEPTH)

    @field_validator("include_paths", "exclude_paths", mode="before")
    @classmethod
    def _paths(cls, v):
        return _clean_paths(v)


class SourceCreate(BaseModel):
    type: Literal["url", "crawl", "text"]
    url: Optional[str] = Field(default=None, max_length=2000)
    title: Optional[str] = Field(default=None, max_length=200)
    content: Optional[str] = Field(default=None, max_length=100_000)
    crawl: Optional[CrawlOptions] = None

    @model_validator(mode="after")
    def _check(self):
        if self.type in ("url", "crawl"):
            u = (self.url or "").strip()
            if not u.lower().startswith(("http://", "https://")):
                raise ValueError("Enter a full website address starting with http:// or https://")
            self.url = u
        if self.type == "text":
            if not (self.content or "").strip():
                raise ValueError("Text content is required")
            if not (self.title or "").strip():
                self.title = (self.content or "").strip().split("\n", 1)[0][:80]
        return self


class KbTestRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    with_answer: bool = True
