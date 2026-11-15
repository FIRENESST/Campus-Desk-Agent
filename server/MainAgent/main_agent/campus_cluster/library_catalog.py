"""Validate and search the anonymous library catalog for both the website and tools."""

import unicodedata
from collections import Counter
from datetime import UTC, timedelta
from typing import Annotated

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from .skills import SkillInput


MIN_LOAN_DAYS, MAX_LOAN_DAYS, DEFAULT_LOAN_DAYS = 1, 14, 7
LIBRARY_PAGE_SIZE, LIBRARY_TOOL_PAGE_SIZE = 8, 6
LoanDays = Annotated[int, Field(ge=MIN_LOAN_DAYS, le=MAX_LOAN_DAYS)]
ResourceId = Annotated[str, Field(min_length=1, max_length=96, pattern=r"^[a-zA-Z0-9_-]+$")]


class LibraryCatalogQuery(SkillInput):
    q: str = Field(default="", max_length=120, description="按书名、作者或馆藏编号搜索，可用空格组合关键词")
    category: str = Field(default="", max_length=48, description="分类名称或编号，例如文学与小说或 literature")
    page: int = Field(default=1, ge=1, le=10_000, description="页码，从 1 开始")


class LibraryLoanWindow(BaseModel):
    model_config = ConfigDict(strict=True)
    borrowed_at: AwareDatetime = Field(strict=False)
    due_at: AwareDatetime = Field(strict=False)
    duration_days: LoanDays

    @model_validator(mode="after")
    def check_period(self):
        elapsed = self.due_at.astimezone(UTC) - self.borrowed_at.astimezone(UTC)
        if elapsed != timedelta(days=self.duration_days):
            raise ValueError("invalid library loan period")
        return self


class LibraryBook(BaseModel):
    model_config = ConfigDict(strict=True)
    id: ResourceId
    title: str
    author: str
    category_id: ResourceId = "uncategorized"
    category: str = "未分类"
    total_copies: int = Field(ge=0)
    available_copies: int = Field(ge=0)
    loan_progress: list[LibraryLoanWindow] = Field(default_factory=list)


def _search_key(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return "".join(char for char in normalized if char.isalnum())


def search_terms(query: str) -> tuple[str, ...]:
    return tuple(term for part in query.split() if (term := _search_key(part)))


def matches_search(terms: tuple[str, ...], *fields: str) -> bool:
    if not terms:
        return True
    keys = tuple(_search_key(field) for field in fields)
    return all(any(term in key for key in keys) for term in terms)


def page_slice(items: list, page: int, page_size: int) -> tuple[list, int]:
    last_page = max(1, (len(items) + page_size - 1) // page_size)
    page = min(page, last_page)
    offset = (page - 1) * page_size
    return items[offset:offset + page_size], page


def library_catalog(books: list, query: LibraryCatalogQuery | None = None, *, page_size=LIBRARY_PAGE_SIZE) -> dict:
    items = []
    category_labels = {}
    counts = Counter()
    for book in books:
        item = LibraryBook.model_validate(book).model_dump(mode="json")
        item["borrowed_copies"] = item["total_copies"] - item["available_copies"]
        if item["borrowed_copies"] < len(item["loan_progress"]):
            raise ValueError("inconsistent library copies")
        category_labels[item["category_id"]] = item["category"]
        counts[item["category_id"]] += 1
        items.append(item)

    summary = {
        "titles": len(items),
        "available_copies": sum(item["available_copies"] for item in items),
        "borrowed_copies": sum(item["borrowed_copies"] for item in items),
    }
    categories = [{"id": key, "label": label, "titles": counts[key]} for key, label in category_labels.items()]
    if query is None:
        return {"items": items, "categories": categories, "summary": summary,
                "total": len(items), "page": 1, "page_size": max(1, len(items))}
    terms, category = search_terms(query.q), _search_key(query.category)
    matching = [
        book for book in items
        if (not category or category in {_search_key(book["category_id"]), _search_key(book["category"])})
        and matches_search(terms, book["title"], book["author"], book["id"])
    ]
    page_items, page = page_slice(matching, query.page, page_size)
    return {"items": page_items, "categories": categories, "summary": summary,
            "total": len(matching), "page": page, "page_size": page_size}
