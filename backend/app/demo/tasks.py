"""Built-in demo repositories. Each contains a real bug with failing tests plus a
scripted solution, so the full workflow can be tried without an API key.

Runs on these repositories are *simulated*: no model is called and token / cost
figures are estimates. The UI labels them as such."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..gitops import IDENT, prepare_workspace, run_git
from ..llm import ScriptStep


@dataclass
class Demo:
    id: str
    title: str
    description: str
    task: str
    files: dict[str, str]
    script: list[ScriptStep] = field(default_factory=list)


_CONFTEST = "# Makes the repository root importable when running pytest.\n"

# --------------------------------------------------------------------------- pagination
_PAGINATION_SRC = '''def total_pages(total_items, per_page):
    """Number of pages needed to show `total_items`."""
    return (total_items + per_page - 1) // per_page


def paginate(items, page, per_page):
    """Return the items on the given page. Pages are numbered from 1."""
    if page < 1 or per_page < 1:
        raise ValueError("page and per_page must be >= 1")
    start = page * per_page
    end = start + per_page
    return items[start:end]
'''
_PAGINATION_TEST = '''import pytest

from shop.pagination import paginate, total_pages

ITEMS = list(range(10))


def test_first_page_starts_at_the_beginning():
    assert paginate(ITEMS, 1, 3) == [0, 1, 2]


def test_middle_page():
    assert paginate(ITEMS, 2, 3) == [3, 4, 5]


def test_last_page_may_be_short():
    assert paginate(ITEMS, 4, 3) == [9]


def test_page_past_the_end_is_empty():
    assert paginate(ITEMS, 5, 3) == []


def test_total_pages_rounds_up():
    assert total_pages(10, 3) == 4


def test_invalid_page_raises():
    with pytest.raises(ValueError):
        paginate(ITEMS, 0, 3)
'''

# --------------------------------------------------------------------------- slugify
_SLUG_SRC = '''def slugify(text):
    """Turn a title into a lowercase, hyphen-separated URL slug."""
    return text.lower().replace(" ", "-")
'''
_SLUG_TEST = '''from blog.slugs import slugify


def test_simple_title():
    assert slugify("Hello World") == "hello-world"


def test_punctuation_is_dropped():
    assert slugify("Hello, World!") == "hello-world"


def test_repeated_spaces_collapse():
    assert slugify("  Multiple   spaces  ") == "multiple-spaces"


def test_symbols_between_words():
    assert slugify("C++ & Python") == "c-python"


def test_already_a_slug():
    assert slugify("already-a-slug") == "already-a-slug"
'''

# --------------------------------------------------------------------------- discounts
_DISCOUNT_SRC = '''def apply_discount(price, percent):
    """Return `price` after taking `percent` percent off, rounded to cents."""
    if not 0 <= percent <= 100:
        raise ValueError("percent must be between 0 and 100")
    return round(price - percent, 2)


def cart_total(prices, percent=0):
    """Total of all prices with the discount applied to the whole cart."""
    return apply_discount(sum(prices), percent)
'''
_DISCOUNT_TEST = '''import pytest

from store.pricing import apply_discount, cart_total


def test_quarter_off():
    assert apply_discount(200, 25) == 150.0


def test_rounds_to_cents():
    assert apply_discount(19.99, 10) == 17.99


def test_zero_percent_changes_nothing():
    assert apply_discount(50, 0) == 50


def test_cart_total_with_discount():
    assert cart_total([10, 20, 30], 50) == 30.0


def test_out_of_range_percent():
    with pytest.raises(ValueError):
        apply_discount(10, 120)
'''

_PLAN = lambda *steps: ("I'll plan first, then look at the code.", [("update_plan", {"steps": list(steps)})])

DEMOS: dict[str, Demo] = {
    "pagination": Demo(
        id="pagination", title="Pagination returns the wrong page",
        description="paginate() skips the first page of results.",
        task="Users report that page 1 of the results shows the second page's items. Fix the pagination bug and make the tests pass.",
        files={"shop/__init__.py": "", "shop/pagination.py": _PAGINATION_SRC, "tests/test_pagination.py": _PAGINATION_TEST,
               "conftest.py": _CONFTEST},
        script=[
            _PLAN("Search for the pagination code", "Run the tests to see the failure", "Fix the page offset", "Re-run the tests"),
            ("Let me find where pagination is implemented.", [("search_code", {"query": "paginate page items per_page"})]),
            ("This looks like the right file. I'll read it.", [("read_file", {"path": "shop/pagination.py"})]),
            ("Before changing anything, I'll run the tests to see them fail.", [("run_tests", {"command": "pytest -q"})]),
            ("Pages are numbered from 1 but the start offset uses `page` directly, so page 1 skips a full page. It should be (page - 1) * per_page.",
             [("replace_in_file", {"path": "shop/pagination.py", "old_string": "    start = page * per_page",
                                    "new_string": "    start = (page - 1) * per_page", "confidence": 0.96,
                                    "reason": "Pages are 1-indexed, so the offset must use page - 1."})]),
            ("Now I'll verify the fix.", [("run_tests", {"command": "pytest -q"})]),
            ("", [("finish", {"summary": "Fixed an off-by-one in shop/pagination.py: the slice start used `page * per_page` for 1-indexed pages, so page 1 returned page 2's items. It is now `(page - 1) * per_page`. All tests pass.", "confidence": 0.96})]),
        ]),
    "slugify": Demo(
        id="slugify", title="Slugs contain stray hyphens",
        description="slugify() mishandles punctuation and repeated spaces.",
        task="Slugs for titles like 'Hello, World!' come out wrong. Fix slugify so punctuation and repeated spaces produce clean, single-hyphen slugs.",
        files={"blog/__init__.py": "", "blog/slugs.py": _SLUG_SRC, "tests/test_slugs.py": _SLUG_TEST, "conftest.py": _CONFTEST},
        script=[
            _PLAN("Find slugify", "Run tests", "Replace the naive space replacement with a regex", "Re-run tests"),
            ("Searching for the slug code.", [("search_code", {"query": "slugify slug title hyphen"})]),
            ("Reading the implementation.", [("read_file", {"path": "blog/slugs.py"})]),
            ("Running the tests first.", [("run_tests", {"command": "pytest -q"})]),
            ("It only replaces spaces, so punctuation stays and repeated spaces make repeated hyphens. I'll collapse every run of non-alphanumerics into one hyphen and trim the ends. That needs `re`.",
             [("replace_in_file", {"path": "blog/slugs.py", "old_string": '    return text.lower().replace(" ", "-")',
                                    "new_string": '    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")', "confidence": 0.93,
                                    "reason": "A single regex collapses punctuation and whitespace into one hyphen."})]),
            ("Adding the import the new line needs.",
             [("replace_in_file", {"path": "blog/slugs.py", "old_string": "def slugify(text):",
                                    "new_string": "import re\n\n\ndef slugify(text):", "confidence": 0.9,
                                    "reason": "The regex call requires importing re."})]),
            ("Verifying.", [("run_tests", {"command": "pytest -q"})]),
            ("", [("finish", {"summary": "slugify now lowercases, replaces every run of non-alphanumeric characters with a single hyphen and trims leading/trailing hyphens. Added `import re`. All tests pass.", "confidence": 0.93})]),
        ]),
    "discounts": Demo(
        id="discounts", title="Discounts subtract instead of multiply",
        description="apply_discount() takes the percent off as a flat amount.",
        task="A 25% discount on a 200 item gives 175, not 150. Fix the discount calculation and make the tests pass.",
        files={"store/__init__.py": "", "store/pricing.py": _DISCOUNT_SRC, "tests/test_pricing.py": _DISCOUNT_TEST,
               "conftest.py": _CONFTEST},
        script=[
            _PLAN("Find the discount code", "Run tests", "Fix the calculation", "Re-run tests"),
            ("Looking for the discount logic.", [("search_code", {"query": "apply_discount percent price"})]),
            ("Reading pricing.py.", [("read_file", {"path": "store/pricing.py"})]),
            ("Running tests to see the failures.", [("run_tests", {"command": "pytest -q"})]),
            ("`price - percent` subtracts the percentage as if it were a money amount. It should scale the price by (1 - percent/100).",
             [("replace_in_file", {"path": "store/pricing.py", "old_string": "    return round(price - percent, 2)",
                                    "new_string": "    return round(price * (1 - percent / 100), 2)", "confidence": 0.97,
                                    "reason": "A percentage discount multiplies the price by (1 - percent/100)."})]),
            ("Verifying.", [("run_tests", {"command": "pytest -q"})]),
            ("", [("finish", {"summary": "apply_discount subtracted the percent as a flat amount. It now returns price * (1 - percent/100), rounded to cents. cart_total inherits the fix. All tests pass.", "confidence": 0.97})]),
        ]),
}


def materialize(demo_id: str, dest: Path) -> str:
    """Write the demo repository to `dest` as a git repo. Returns the commit sha."""
    demo = DEMOS[demo_id]
    dest.mkdir(parents=True, exist_ok=True)
    for rel, content in demo.files.items():
        f = dest / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content, encoding="utf-8")
    run_git(["init", "-q"], cwd=dest)
    run_git(["symbolic-ref", "HEAD", "refs/heads/main"], cwd=dest)
    prepare_workspace(dest)
    run_git(["add", "-A"], cwd=dest)
    run_git([*IDENT, "commit", "-q", "-m", "Initial commit"], cwd=dest)
    return run_git(["rev-parse", "HEAD"], cwd=dest).strip()


def public_list() -> list[dict]:
    return [{"id": d.id, "title": d.title, "description": d.description, "task": d.task} for d in DEMOS.values()]
