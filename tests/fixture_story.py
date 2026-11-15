"""The synthetic "shopfront" story behind ``tests/fixtures/transcripts``.

Five Claude Code sessions (one with a subagent) and one session from another
agent in the generic format, all working on the small fake repository in
``tests/fixtures/shopfront``. The recurring problems are deliberate:

* agents run ``pytest`` directly, which cannot work; the repo needs ``make test``;
* agents hand-edit the large generated ``schema_gen.py`` and their edits miss;
* a human keeps repeating that ``legacy_charge`` is deprecated;
* agents search for a ``RefundPolicy`` that does not exist under that name.

Regenerate the fixture files with::

    python -m tests.fixture_story
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from tests.builders import EDIT_AMBIGUOUS, EDIT_NOT_FOUND, STALE, Session, session_uuid

FIXTURES = Path(__file__).parent / "fixtures"
REPO = FIXTURES / "shopfront"
TRANSCRIPTS = FIXTURES / "transcripts"
#: Where the fake repo "lived" when the sessions were recorded.
RECORDED_AT = "/home/dev/shopfront"
PROJECT_DIR = "-home-dev-shopfront"

SCHEMA = f"{RECORDED_AT}/src/shopfront/api/schema_gen.py"
SERVICE = f"{RECORDED_AT}/src/shopfront/checkout/service.py"
CLIENT = f"{RECORDED_AT}/src/shopfront/payments/client.py"

NO_MODULE = (
    "ImportError while loading conftest '/home/dev/shopfront/tests/conftest.py'.\n"
    "tests/conftest.py:3: in <module>\n"
    "    from shopfront.db import make_engine\n"
    "E   ModuleNotFoundError: No module named 'shopfront'"
)
NO_DATABASE = (
    "tests/conftest.py:11: in engine\n"
    "    return make_engine(os.environ['DATABASE_URL'])\n"
    "E   KeyError: 'DATABASE_URL'\n"
    "ERROR tests/test_checkout.py - KeyError: 'DATABASE_URL'"
)
PASSED = "PYTHONPATH=src DATABASE_URL=sqlite:///:memory: pytest -q\n42 passed in 3.1s"


def _day(day: int, month: int = 11, hour: int = 10) -> datetime:
    return datetime(2026, month, day, hour, 0, 0, tzinfo=timezone.utc)


def _session(name: str, start: datetime) -> Session:
    session = Session(session_uuid(name), RECORDED_AT, start)
    # Lines the parser must ignore, as found at the top of real transcripts.
    session.raw(
        {"type": "queue-operation", "operation": "enqueue", "sessionId": session.session_id}
    )
    session.raw({"type": "summary", "summary": name, "leafUuid": session_uuid(name + "/leaf")})
    return session


def gift_message() -> Session:
    s = _session("gift-message", _day(26, month=10))
    s.user("Add an optional gift_message field to orders and expose it in the checkout API.")
    s.say("I'll add the field to the API schema first.", thinking="Find the Order schema.")
    s.read(SCHEMA)
    s.tick(20).edit(
        SCHEMA,
        "    note: str | None = None",
        "    gift_message: str | None = None",
        error=EDIT_AMBIGUOUS,
        n=4,
    )
    s.tick(25).edit(
        SCHEMA,
        "class Order:\n    note: str | None = None",
        "class Order:\n    gift_message: str",
        error=EDIT_NOT_FOUND,
    )
    s.tick(15).read(SCHEMA, offset=400, limit=120)
    s.tick(30).edit(
        SCHEMA, "class OrderOut:\n    id: int", "class OrderOut:\n    gift_message: str"
    )
    s.tick(20).read(SERVICE)
    s.tick(40).edit(
        SERVICE, "    total = cart.total()", "    total = cart.total()\n    legacy_charge(total)"
    )
    s.tick(10).bash("pytest tests/test_checkout.py -q", NO_MODULE, exit_code=4)
    s.tick(15).bash("PYTHONPATH=src pytest tests/test_checkout.py -q", NO_DATABASE, exit_code=1)
    s.tick(20).bash(
        "cat Makefile", "test:\n\tPYTHONPATH=src DATABASE_URL=sqlite:///:memory: pytest -q"
    )
    s.tick(5).bash("make test", PASSED, took=9)
    s.say("Done: orders accept gift_message and checkout passes it through.")
    s.tick(240).user(
        "Don't call legacy_charge() here, it's deprecated. Use PaymentClient.charge() instead."
    )
    s.tick(10).read(CLIENT)
    s.tick(25).edit(SERVICE, "    legacy_charge(total)", "    payments.charge(total)")
    s.tick(5).bash("make test", PASSED, took=9)
    return s


def partial_refunds() -> Session:
    s = _session("partial-refunds", _day(29, month=10, hour=14))
    s.user("Implement partial refunds for orders. Follow the existing refund rules.")
    s.grep("RefundPolicy", f"{RECORDED_AT}/src/shopfront")
    s.tick(10).glob("src/shopfront/**/refund*.py")
    s.tick(10).read(f"{RECORDED_AT}/src/shopfront/payments/refunds.py", missing=True)
    s.tick(15).read(CLIENT)
    s.tick(60).edit(
        CLIENT,
        "    def charge(self",
        "    def refund(self, amount):\n        ...\n\n    def charge(self",
    )
    s.tick(45).write(
        f"{RECORDED_AT}/tests/test_refunds.py", "def test_partial_refund():\n    ...\n"
    )
    s.tick(10).bash("pytest tests/test_refunds.py", NO_MODULE, exit_code=4)
    s.tick(20).bash("pytest tests/test_refunds.py -x", NO_MODULE, exit_code=4)
    s.tick(30).bash("pip install -e .", "Successfully installed shopfront-0.4.0", took=14)
    s.tick(10).bash("pytest tests/test_refunds.py -x", NO_DATABASE, exit_code=1)
    s.tick(25).bash("make test", PASSED.replace("42", "43"), took=9)
    s.tick(30).edit(
        SERVICE, "def refund(order):", "def refund(order):\n    legacy_charge(-order.total)"
    )
    s.say("Partial refunds are implemented and covered by tests/test_refunds.py.")
    s.tick(300).user(
        "No, I said use PaymentClient, not the legacy module. "
        "src/shopfront/payments/legacy.py is deprecated and is being deleted next sprint."
    )
    s.tick(20).edit(SERVICE, "    legacy_charge(-order.total)", "    payments.refund(order.total)")
    s.tick(5).bash("make test", PASSED.replace("42", "43"), took=9)
    return s


def price_currency() -> Session:
    s = _session("price-currency", _day(3, hour=9))
    s.user("Add a currency field (ISO 4217, default EUR) to the Price schema.")
    s.read(SCHEMA)
    s.tick(20).edit(
        SCHEMA,
        "class Price:\n    amount: int",
        "class Price:\n    currency: str",
        error=EDIT_NOT_FOUND,
    )
    s.tick(20).edit(
        SCHEMA,
        "    amount: int",
        "    amount: int\n    currency: str = 'EUR'",
        error=EDIT_AMBIGUOUS,
        n=3,
    )
    s.tick(15).bash("make codegen", "wrote src/shopfront/api/schema_gen.py (1031 lines)", took=6)
    s.tick(10).edit(
        SCHEMA,
        "class PriceOut:\n    amount: int",
        "class PriceOut:\n    currency: str",
        error=STALE,
    )
    s.tick(10).read(SCHEMA)
    s.tick(30).edit(
        SCHEMA,
        "class PriceOut:\n    amount: int",
        "class PriceOut:\n    amount: int\n    currency: str = 'EUR'",
    )
    s.tick(20).reject(
        "Bash",
        {"command": "rm -rf migrations/versions && alembic revision --autogenerate -m currency"},
        "never delete existing migrations, add a new revision on top",
    )
    s.tick(15).bash(
        "alembic revision --autogenerate -m currency",
        "Generating migrations/versions/a1b2_currency.py ... done",
    )
    s.tick(30).edit(
        SCHEMA,
        "class PriceIn:\n    amount: int",
        "class PriceIn:\n    amount: int\n    currency: str = 'EUR'",
    )
    s.interrupt()
    s.tick(40).user(
        "No, stop hand-editing src/shopfront/api/schema_gen.py. It is generated: change "
        "api/openapi.yaml and run make codegen."
    )
    s.tick(15).read(f"{RECORDED_AT}/api/openapi.yaml")
    s.tick(40).edit(
        f"{RECORDED_AT}/api/openapi.yaml",
        "        amount:",
        "        currency:\n          type: string\n        amount:",
    )
    s.tick(5).bash("make codegen", "wrote src/shopfront/api/schema_gen.py (1037 lines)", took=6)
    s.tick(5).bash("make test", PASSED.replace("42", "43"), took=9)
    return s


def discount_codes() -> tuple[Session, Session]:
    s = _session("discount-codes", _day(9, hour=15))
    s.user("Add percentage discount codes to checkout.")
    s.read(SERVICE)
    s.tick(30).edit(SERVICE, "def checkout(cart):", "def checkout(cart, code=None):")
    s.tick(20).edit(
        SERVICE, "    total = cart.total()", "    total = apply_discount(cart.total(), code)"
    )
    s.tick(20).edit(SERVICE, "    return Decimal(total)", "    return round(total, 2)")
    s.tick(10).bash("pytest tests/test_checkout.py", NO_MODULE, exit_code=4)
    s.tick(10).bash(
        "make test",
        "FAILED tests/test_checkout.py::test_rounding - assert Decimal('9.99') == 9.99\n"
        "1 failed, 42 passed",
        exit_code=2,
        took=9,
    )
    s.tick(20).edit(SERVICE, "    return round(total, 2)", "    return Decimal(total)")
    s.tick(25).edit(
        SERVICE, "def apply_discount(total, code):", "def apply_discount(total: Decimal, code):"
    )
    s.tick(20).edit(SERVICE, "    return total * rate", "    return (total * rate).quantize(CENT)")
    s.tick(20).edit(SERVICE, "    charge = payments.charge", "    charge = legacy_charge")
    s.tick(5).bash("make test", PASSED.replace("42", "44"), took=9)
    s.say("Discount codes work and all tests pass.")
    s.tick(600).user(
        "It's still failing for me on staging, and you used legacy_charge again in "
        "src/shopfront/checkout/service.py. I told you: PaymentClient only."
    )
    s.tick(10).grep("legacy_charge", f"{RECORDED_AT}/src/shopfront/checkout", found=True)
    s.tick(20).edit(SERVICE, "    charge = legacy_charge", "    charge = payments.charge")
    s.tick(10).bash(
        "curl -s -H 'Authorization: Bearer sf_stg_9f8e7d6c5b4a39281706f5e4d3c2b1a0' "
        "https://staging.shopfront.example/api/health",
        "curl: (7) Failed to connect to staging.shopfront.example port 443: Connection refused",
        exit_code=7,
    )
    s.tick(5).bash("make test", PASSED.replace("42", "44"), took=9)

    sub = Session(s.session_id, RECORDED_AT, _day(9, hour=15), sidechain=True)
    sub.user("Find where discount and refund policies are defined in this repo.")
    sub.grep("DiscountPolicy", f"{RECORDED_AT}/src/shopfront")
    sub.tick(8).grep("RefundPolicy", f"{RECORDED_AT}/src/shopfront")
    sub.tick(8).glob("**/*policy*.py", f"{RECORDED_AT}/src/shopfront")
    sub.tick(8).grep("refund", f"{RECORDED_AT}/src/shopfront", found=True)
    sub.say("Refund rules live in src/shopfront/payments/rules.py as REFUND_RULES.")
    return s, sub


def api_version() -> Session:
    s = _session("api-version", _day(12, hour=11))
    s.user("Bump the API version to 2.4 in the schema and make sure tests still pass.")
    s.read(SCHEMA)
    s.tick(15).read(SCHEMA)
    s.tick(15).edit(SCHEMA, 'API_VERSION = "2.3"', 'API_VERSION = "2.4"', error=EDIT_NOT_FOUND)
    s.tick(15).read(SCHEMA)
    s.tick(20).grep("API_VERSION", f"{RECORDED_AT}/api/openapi.yaml")
    s.tick(20).edit(f"{RECORDED_AT}/api/openapi.yaml", "  version: 2.3.0", "  version: 2.4.0")
    s.tick(5).bash("make codegen", "wrote src/shopfront/api/schema_gen.py (1037 lines)", took=6)
    s.tick(10).bash("python -m pytest -q", NO_MODULE, exit_code=4)
    s.tick(10).bash("make test", PASSED.replace("42", "44"), took=9)
    return s


def other_agent() -> list[dict[str, object]]:
    """A session from another agent, converted to the generic format."""
    sid = "aider-2026-11-05"
    base = {"session": sid, "cwd": RECORDED_AT}

    def at(second: int) -> str:
        return f"2026-11-05T16:{second // 60:02d}:{second % 60:02d}Z"

    return [
        {**base, "ts": at(0), "kind": "user_message", "text": "Add a sku field to LineItem."},
        {
            **base,
            "ts": at(10),
            "kind": "tool_call",
            "id": 1,
            "op": "edit",
            "path": "src/shopfront/api/schema_gen.py",
            "old": "class LineItem:",
            "new": "class LineItem:\n    sku: str",
        },
        {
            **base,
            "ts": at(12),
            "kind": "tool_result",
            "id": 1,
            "ok": False,
            "error": "edit_not_found",
            "output": "SEARCH block failed to exactly match lines in schema_gen.py",
        },
        {
            **base,
            "ts": at(40),
            "kind": "tool_call",
            "id": 2,
            "op": "edit",
            "path": "src/shopfront/api/schema_gen.py",
            "old": "class LineItemOut:",
            "new": "class LineItemOut:\n    sku: str",
        },
        {**base, "ts": at(42), "kind": "tool_result", "id": 2, "ok": True},
        {**base, "ts": at(60), "kind": "tool_call", "id": 3, "op": "shell", "command": "pytest -q"},
        {
            **base,
            "ts": at(64),
            "kind": "tool_result",
            "id": 3,
            "ok": False,
            "exit_code": 4,
            "output": NO_MODULE,
        },
        {**base, "ts": at(90), "kind": "tool_call", "id": 4, "op": "shell", "command": "make test"},
        {**base, "ts": at(99), "kind": "tool_result", "id": 4, "ok": True, "output": PASSED},
        {
            **base,
            "ts": at(400),
            "kind": "user_message",
            "text": "That's wrong, schema_gen.py is generated. Revert that, edit api/openapi.yaml.",
        },
    ]


def write_story(dest: Path = TRANSCRIPTS) -> list[Path]:
    """Write every fixture transcript under ``dest`` and return the paths."""
    project = dest / PROJECT_DIR
    paths = [
        gift_message().save(project),
        partial_refunds().save(project),
        price_currency().save(project),
    ]
    main, sub = discount_codes()
    paths.append(main.save(project))
    paths.append(sub.save(project / main.session_id / "subagents", "agent-a1f3c9"))
    paths.append(api_version().save(project))
    generic = dest / "other-agents" / "aider-2026-11-05.jsonl"
    generic.parent.mkdir(parents=True, exist_ok=True)
    generic.write_text(
        "".join(json.dumps(e) + "\n" for e in other_agent()), encoding="utf-8", newline="\n"
    )
    paths.append(generic)
    return paths


if __name__ == "__main__":
    for written in write_story():
        print(written.relative_to(FIXTURES.parent.parent))
