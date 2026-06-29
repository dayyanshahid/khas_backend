from __future__ import annotations
import difflib
import json
import logging
import re
import time
import threading
import uuid
from typing import Any
from django.utils import timezone
import openai
from django.conf import settings

from database.models.chatbot import ChatBot
from api.controllers.chatbot.schema_docs import (
    column_description,
    column_name_from_catalog_entry,
    load_schema_docs,
    table_description,
)

logger = logging.getLogger(__name__)

# =============================================================================
# PROMPTS
# =============================================================================

SQL_RULES = """
SQL Server / T-SQL rules:
- Output exactly ONE read-only SELECT statement.
- Use only table and column names from the database catalog.
- Each column in the catalog is listed with its SQL Server type in parentheses, e.g. `column_name (datetime)`.
- Never invent column names that are not in the catalog, even if the name "feels obvious" or "should exist".
- For date filters (today, yesterday, this month, etc.) you MUST pick a column whose type is date, datetime, datetime2, smalldatetime, or datetimeoffset. Never cast int, numeric, decimal, money, varchar, or *_id columns to DATE — that will fail.
- If no date/datetime column exists on a table, do NOT invent one. Either pick a different table or return: SELECT 'no_matching_schema' AS reason
- Use TOP N for simple limits. Do not use LIMIT.
- Do not combine TOP with OFFSET/FETCH.
- SQL Server allows ORDER BY alias only as a standalone item. Do not use SELECT aliases inside ORDER BY calculations like alias1 + alias2. Repeat the aggregate expressions there, or use a subquery.
- Use GETDATE(), DATEADD(), and DATEDIFF() for date logic.
- For calendar-day comparisons on datetime columns, use CAST(column AS DATE).
- No markdown, explanations, UNION, DDL, DML, EXEC, temp tables, variables, or multiple statements.
- If the catalog cannot answer the question, return: SELECT 'no_matching_schema' AS reason
- Use only the EXACT column names from the catalog. Do not rename, pluralize, abbreviate, or substitute "friendlier" alternatives. Copy the spelling and casing exactly as written.
- If the question needs a column that is not in the table you are querying, JOIN to the table that does have it — never invent a column on the wrong table.
""".strip()

ROUTER_PROMPT = """
Classify the user question.
Reply with exactly one lowercase word: general or database.

Use database when the question requires live data from a business database —
counts, totals, lists, rankings, filters by entity or time period, or anything
that needs records currently stored in the system.

Also use database when the current message is a follow-up to an earlier data
question in the conversation (short replies, pronouns, "that", "those", "same",
"and", ordinals, or extra filters on the prior topic) even if the message alone
looks vague.

Use general for greetings, thanks, definitions, explanations, how-to or UI
questions, or any conceptual question that does not need live database rows.

When unsure, choose database.
""".strip()

GENERAL_PROMPT = """
You are a helpful assistant for a business application.
Answer conceptual or how-to questions clearly and briefly.
Do not mention SQL, table names, or internal implementation details.
If live data is needed, tell the user to phrase the question as a data question.
""".strip()

SQL_GENERATION_PROMPT = """
You are an expert Microsoft SQL Server T-SQL writer.
Create one safe SELECT query that answers the user's question using only the catalog.

═══ FILTER DISCIPLINE ═══
Do NOT add WHERE filters that the user did not explicitly request.
Only add a filter when the user's question explicitly names the constraint:
  - A time period (a date, a date range, or a relative period the user named).
  - A specific entity (a named shop, category, region, status, etc.).

If the user did not name a constraint, do NOT add it. Empty or misleading results
caused by invented filters is the single most common failure mode in this system.
When in doubt, leave filters OUT.

═══ RANKING QUESTIONS (top, best, worst, most, least, highest, lowest) ═══
When the user asks for a ranking without specifying a metric, pick the most
business-meaningful measure available in this priority order:

  1. Monetary totals — SUM of money / decimal / numeric columns that represent
     amounts, totals, net or gross values, sales, or revenue.
  2. Quantities — SUM of integer columns that represent quantities or units.
  3. Counts — COUNT of rows, or COUNT(DISTINCT) of a transaction identifier.
  4. Auxiliary measures (points, bonuses, rewards, scores) — only when 1-3
     are unavailable. Such columns are frequently zero-filled in real systems.

Return MORE than one useful column. A good ranking answer includes a
human-readable label, the primary metric, and at least one secondary metric
(such as a count or a recency date) where the catalog supports it. A result
that is only a list of IDs and one number is rarely the right answer.

═══ JOINING FOR NAMES ═══
When the user asks for a "name", "title", or "description", JOIN to the
master/lookup table and select a column whose OWN name reads like a name
(e.g. item_name, product_name, field_name, Description).
Never substitute *_code, *_sku, *_id, *_no columns — those are still
identifiers. If no such text column exists, return the IDs and say so.

═══ FOLLOW-UP ENRICHMENT ═══
If the user asks for ANY extra attribute about entities shown in the prior
turn ("name", "description", "category", "address", "phone", "date", etc.),
JOIN to the table that actually holds that attribute. Never substitute a
different identifier column (a *_code, *_sku, *_no) when the user asked for
a descriptive attribute. If the catalog truly has no such column for that
entity, say so by returning only the original columns.

When the prior turn's "Result of the SQL above" block is included, those are
the exact entities the user means by "these" / "those" / "the items above".
Filter the new query to those exact IDs by writing an IN clause that contains
the literal numeric IDs copied from that preview (for example: WHERE
Product_Item_ID IN (7909, 7903, 7902)). NEVER write angle-bracket
placeholders like (the ids) or (id_col) — copy the real numbers in.

═══ TABLE SELECTION ═══
Prefer the most populated table for the measure being requested. Detail or
line-item tables usually hold the real measure columns; header tables usually
hold identifiers and dates only.
Only tables listed in the catalog exist and may be used. Empty tables are
omitted from the catalog.

═══ BUSINESS DESCRIPTIONS ═══
The catalog includes business descriptions for tables and columns.
Use them to map the user's words (customers, sales, revenue, shops, etc.)
to the correct tables and columns.
Descriptions explain meaning only — always use the exact table and column
names from the catalog when writing SQL.

═══ T-SQL RULES ═══
When ordering by a calculated total, do not add SELECT aliases together in
ORDER BY; repeat the SUM/COUNT expressions or wrap in a subquery.

═══ CONVERSATION CONTEXT (always apply when history is present) ═══
If a "Previous conversation" block is included, read it before writing SQL.
The current message may be incomplete on its own — resolve pronouns, ordinals,
"that/those/the same/also/and", time ranges, shop or product names, metrics,
and filters from the prior turns, prior assistant answers, and prior SQL.

Stay on the same topic, entities, tables, joins, and measures as the last
relevant database turn unless the user clearly starts a new subject.

If the new question names a measure from a different family than the prior
turn (sales, inventory, purchases, customers, discounts), TREAT IT AS A NEW
SUBJECT and discard the prior turn's tables, joins, and WHERE filters.

Do not treat a bare number in a follow-up as a raw *_id unless the prior
context already established that ID for the entity being discussed. Ordinals
(1st, 4th, "the Nth one") mean position in the last result list or ranking,
not row number or Product_Item_ID = N.

Return SQL only. No prose. No markdown.
""".strip()

ANSWER_PROMPT = """
You are a senior data analyst writing the response for a business user.

The user will ALREADY see the full result rendered as a table, bar chart, and
pie chart directly below your text. Do NOT repeat the rows.

You must return BOTH parts in a single JSON object:

  {
    "summary":  "<1–3 sentence executive summary>",
    "insights": ["bullet 1", "bullet 2", "bullet 3"]
  }

──────────────────────────────────────────────────────────────────────
SUMMARY rules (the headline above the chart)
──────────────────────────────────────────────────────────────────────
  • 1–3 sentences, 25–60 words total.
  • State the headline finding in plain language.
  • Call out the single most important number (top item, total, or share).
  • Optionally add one quick observation (concentration / trend / outlier).
  • DO NOT list every row. DO NOT produce a numbered list of items.
  • DO NOT show columns like "Qty: X, Price: Y" — that's the chart's job.
  • At most ONE inline reference to a specific item by name (the leader).
  • Never expose SQL, JSON, or raw column names.
  • Money in Pakistani Rupees (PKR).
  • If there are no rows, summary = "No matching records were found."
    and insights = [].

──────────────────────────────────────────────────────────────────────
INSIGHTS rules ("What This Means" bullets below the chart)
──────────────────────────────────────────────────────────────────────
  • 3 to 4 bullets, each ONE sentence, 12–30 words.
  • No leading "•" or "-" — just the sentence.
  • Each bullet should do ONE of these:
      – Highlight the leader and its share of the total.
      – Quantify the gap between leader and runner-up.
      – Describe concentration (e.g. "top 3 hold X% of total").
      – Flag an outlier, anomaly, gap, or notable pattern.
      – State a concrete business implication or next step.
  • NEVER pick "Others", "Misc", "N/A", "Unknown" or any catch-all
    bucket as the leader or runner-up — those are aggregated groups.
  • Use compact numbers (1.2K, 38M) where natural. Money in PKR.
  • If the result has only one meaningful row, return a single bullet.
  • For empty / all-zero results, return insights = [].

──────────────────────────────────────────────────────────────────────
OUTPUT
──────────────────────────────────────────────────────────────────────
  • Return ONLY the JSON object. No markdown fences. No prose around it.
  • Use double quotes. Escape inner quotes properly.

When conversation history is provided, interpret the current question in
that context (follow-ups, references to earlier entities, dates, metrics).
""".strip()

VISUALIZATION_PROMPT = """
Pick EXACTLY 3 charts for a business dashboard — each from a DIFFERENT family.
Use the question, columns, distinct counts, row count, and sample rows provided.

Families (never pick two from the same one):
  bars: column (3–12 short labels) | bar (8+ or long names)
  trend: line | area — date/month/week/hour axis only
  shares: pie | donut — ≤8 slices, parts-of-whole
  radial: 4–10 ranked items
  treemap: 10–30 categories

Rules (first match wins):
  1. Time axis → one of {line,area} + one of {bar,column} + radial or treemap. No pie/donut.
  2. Share/breakdown/mix → one of {pie,donut} + treemap + one of {bar,column,radial}.
  3. >12 labels → bar + treemap + radial.
  4. Top-N ranking (≤10) → bar or column + radial + donut or treemap.
  5. Else (3–12 categories) → column + donut + radial (not column+pie every time).

Columns: exact names only; label = categorical/ordered, value = numeric.
Prefer 3–20 distinct labels; avoid *_id unless nothing else fits.
Pick the metric the user asked about; different value_columns OK if insightful.
Titles: short business English, not raw SQL names.

Return ONLY JSON, no markdown:
{"charts":[{"type":"...","title":"...","label_column":"...","value_column":"..."}, ...]}
Exactly 3 chart objects, 3 different families.
""".strip()


# =============================================================================
# OPENAI CLIENT
# =============================================================================

_openai_client: openai.OpenAI | None = None

def get_openai_client() -> openai.OpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = openai.OpenAI(api_key=settings.OPENAI_API_KEY)
    return _openai_client


def create_openai_conversation() -> str:
    """Return a new conversation id used to group turns in our own DB.

    Memory is handled locally via ``user_message_with_history``, so we no longer
    create a server-side OpenAI conversation (which accumulated context and
    contributed to 429 "Request too large"). A local UUID is sufficient.
    """
    return f"conv_{uuid.uuid4().hex}"


def call_llm(
    *,
    label: str,
    system: str,
    user: str,
    max_tokens: int = 600,
    conversation_id: str | None = None,
) -> str:
    """All OpenAI API calls go through here.

    Note: we deliberately do NOT pass the OpenAI ``conversation`` here. Memory is
    supplied explicitly via ``user_message_with_history``. Using both at once
    double-fed the context (server-side stored turns + our manual history) and
    blew past the per-minute token limit, causing 429 "Request too large".
    """
    kwargs: dict[str, Any] = {
        "model": settings.OPENAI_MODEL,
        "instructions": system,
        "input": user,
        "max_output_tokens": max(max_tokens, 16),
    }

    started = time.time()
    response = get_openai_client().responses.create(**kwargs)
    text = (response.output_text or "").strip()

    logger.info(
        "[LLM:%s] %dms prompt=%d user=%d reply=%d conv=%s",
        label,
        int((time.time() - started) * 1000),
        len(system),
        len(user),
        len(text),
        "yes" if conversation_id else "no",
    )
    return text


# =============================================================================
# SQL SERVER ACCESS
# =============================================================================

def db_connect(timeout: int = 10):
    """Create a SQL Server connection from settings.RCMS_DB."""
    import pyodbc

    cfg = settings.RCMS_DB
    parts = [
        f"DRIVER={{{cfg['DRIVER']}}}",
        f"SERVER={cfg['SERVER']}",
        f"DATABASE={cfg['DATABASE']}",
        "TrustServerCertificate=yes",
    ]

    if str(cfg.get("TRUSTED", "no")).lower() == "yes":
        parts.append("Trusted_Connection=yes")
    else:
        parts.append(f"UID={cfg['USER']}")
        parts.append(f"PWD={cfg['PASSWORD']}")

    return pyodbc.connect(";".join(parts) + ";", timeout=timeout)


def ensure_db_connected(max_retries: int = 5, wait_seconds: float = 1.0) -> tuple[bool, str | None]:

    last_error: str | None = None
    for attempt in range(1, max_retries + 1):
        try:
            with db_connect(timeout=3) as conn:
                cur = conn.cursor()
                cur.execute("SELECT 1")
                cur.fetchone()
            if attempt > 1:
                logger.info("[DB] connected on attempt %d/%d", attempt, max_retries)
            return True, None
        except Exception as exc:
            last_error = str(exc)
            logger.warning("[DB] connect attempt %d/%d failed: %s", attempt, max_retries, exc)
            if attempt < max_retries:
                time.sleep(wait_seconds)

    logger.error("[DB] could not connect after %d attempts. Last error: %s", max_retries, last_error)
    return False, last_error


def _rows_from_cursor(cur) -> list[dict[str, Any]]:
    columns = [col[0] for col in cur.description]
    return [
        {col: (str(value) if value is not None else None) for col, value in zip(columns, row)}
        for row in cur.fetchall()
    ]


def execute_sql(sql: str) -> list[dict[str, Any]]:
    """Public safe SQL executor for generated user queries."""
    sql = normalize_tsql(sql)
    ok, reason = validate_sql_safety(sql)
    if not ok:
        raise RuntimeError(f"Unsafe SQL blocked: {reason}")

    started = time.time()
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute(sql)
        rows = _rows_from_cursor(cur)

    logger.info("[SQL] %d row(s) in %dms", len(rows), int((time.time() - started) * 1000))
    return rows


# =============================================================================
# SCHEMA CATALOG
# =============================================================================

CATALOG_SQL = """
SELECT
    CASE WHEN o.type = 'V' THEN 'VIEW' ELSE 'TABLE' END AS object_type,
    s.name + '.' + o.name AS object_name,
    STRING_AGG(
        CAST(c.name AS NVARCHAR(MAX)) + ' (' + CAST(t.name AS NVARCHAR(MAX)) + ')',
        ', '
    ) WITHIN GROUP (ORDER BY c.column_id) AS columns
FROM sys.objects o
JOIN sys.schemas s   ON s.schema_id     = o.schema_id
JOIN sys.columns c   ON c.object_id     = o.object_id
JOIN sys.types t     ON t.user_type_id  = c.user_type_id
WHERE o.type IN ('U', 'V')
  AND o.is_ms_shipped = 0
GROUP BY o.type, s.name, o.name
ORDER BY object_type, object_name
"""

ROW_COUNTS_SQL = """
SELECT
    s.name + '.' + t.name AS object_name,
    SUM(p.rows) AS row_count
FROM sys.tables t
JOIN sys.schemas s ON s.schema_id = t.schema_id
JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0, 1)
WHERE t.is_ms_shipped = 0
GROUP BY s.name, t.name
"""

_schema_cache: dict[str, Any] | None = None
_schema_cache_loaded_at: float = 0.0
_schema_cache_lock = threading.Lock()


def load_schema_catalog(*, force_refresh: bool = False) -> dict[str, Any]:
    """Load the database schema. Cached in memory for CHATBOT_SCHEMA_CACHE_TTL seconds."""
    global _schema_cache, _schema_cache_loaded_at

    ttl = getattr(settings, "CHATBOT_SCHEMA_CACHE_TTL", 3600)

    with _schema_cache_lock:
        if (not force_refresh
                and _schema_cache is not None
                and (time.time() - _schema_cache_loaded_at) < ttl):
            return _schema_cache

        try:
            with db_connect() as conn:
                cur = conn.cursor()
                cur.execute(CATALOG_SQL)
                catalog_rows = _rows_from_cursor(cur)
                cur.execute(ROW_COUNTS_SQL)
                row_count_rows = _rows_from_cursor(cur)
        except Exception as exc:
            logger.exception("[Schema] failed to load catalog")
            return {
                "catalog_text": f"DATABASE CATALOG UNAVAILABLE: {exc}",
                "objects": {},
                "row_counts": {},
                "stats": {"schema_ok": False, "error": str(exc)},
            }

        objects: dict[str, list[str]] = {}
        kinds: dict[str, str] = {}
        for row in catalog_rows:
            name = row["object_name"]
            objects[name] = [c.strip() for c in (row["columns"] or "").split(",") if c.strip()]
            kinds[name] = row["object_type"]

        row_counts = {row["object_name"]: int(row["row_count"] or 0) for row in row_count_rows}
        docs = load_schema_docs()
        catalog_text = build_catalog_text(objects, kinds, row_counts, docs)
        table_count = sum(1 for k in kinds.values() if k == "TABLE")
        view_count = sum(1 for k in kinds.values() if k == "VIEW")

        _schema_cache = {
            "catalog_text": catalog_text,
            "objects": objects,
            "row_counts": row_counts,
            "docs": docs,
            "stats": {
                "schema_ok": bool(objects),
                "table_count": table_count,
                "view_count": view_count,
                "catalog_chars": len(catalog_text),
                "docs_ok": docs.get("ok", False),
                "error": None,
            },
        }
        _schema_cache_loaded_at = time.time()
        logger.info("[Schema] loaded and cached: %d tables, %d views", table_count, view_count)
        return _schema_cache

def build_catalog_text(
    objects: dict[str, list[str]],
    kinds: dict[str, str],
    row_counts: dict[str, int],
    docs: dict[str, Any] | None = None,
) -> str:
    docs = docs or load_schema_docs()

    def sort_key(name: str):
        kind = kinds.get(name, "TABLE")
        count = row_counts.get(name, 0)
        # Populated tables first (by row count desc), views last.
        group = 0 if kind == "TABLE" else 1
        return (group, -count, name.lower())

    skipped_empty = 0
    included: list[str] = []

    for name in sorted(objects, key=sort_key):
        kind = kinds.get(name, "TABLE")
        count = row_counts.get(name, 0)

        # Skip empty tables — do not send them to the LLM.
        if kind == "TABLE" and count == 0:
            skipped_empty += 1
            continue

        row_tag = f" [{count:,} rows]" if kind == "TABLE" else ""
        table_desc = table_description(docs, name)
        block = [f"{kind}{row_tag} {name}"]
        if table_desc:
            block.append(f"  About: {table_desc}")

        for col_entry in objects[name]:
            col_name = column_name_from_catalog_entry(col_entry)
            col_desc = column_description(docs, name, col_name)
            if col_desc:
                block.append(f"  {col_entry} — {col_desc}")
            else:
                block.append(f"  {col_entry}")

        included.append("\n".join(block))

    logger.info(
        "[Schema] catalog text built: %d objects shown to LLM, %d empty tables skipped",
        len(included), skipped_empty,
    )

    header = [
        "DATABASE CATALOG - use only these exact table and column names.",
        "Empty tables are not shown. Only tables with actual data are listed.",
    ]
    if docs.get("application"):
        header.append(f"Application: {docs['application']}")
    for conv_name, conv_desc in docs.get("conventions", []):
        header.append(f"Convention {conv_name}: {conv_desc}")
    header.append("")
    return "\n".join(header + included)

def schema_context(catalog: dict[str, Any] | None = None) -> str:
    catalog = catalog or load_schema_catalog()
    return f"{SQL_RULES}\n\n{catalog['catalog_text']}"


# =============================================================================
# SQL GUARDRAIL + NORMALIZATION
# =============================================================================

FORBIDDEN_SQL = (
    "INSERT", "UPDATE", "DELETE", "DROP", "TRUNCATE", "ALTER", "CREATE",
    "EXEC", "EXECUTE", "GRANT", "REVOKE", "MERGE", "BULK", "OPENROWSET",
    "INTO", "DECLARE", "SET", "USE",
)


def normalize_tsql(sql: str) -> str:
    """Clean common LLM SQL formatting mistakes."""
    text = re.sub(r"```(?:sql)?|```", "", sql, flags=re.IGNORECASE).strip()
    text = re.sub(r"--[^\n]*", "", text)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = text.strip()
    text = text.rstrip(";").strip()

    has_top = re.search(r"\bTOP\s+\d+\b", text, re.IGNORECASE)
    has_offset_fetch = re.search(
        r"\bOFFSET\s+\d+\s+ROWS?\s+FETCH\s+(?:NEXT|FIRST)\s+\d+\s+ROWS?\s+ONLY\b",
        text,
        re.IGNORECASE,
    )
    if has_top and has_offset_fetch:
        text = re.sub(
            r"\s*\bOFFSET\s+\d+\s+ROWS?\s+FETCH\s+(?:NEXT|FIRST)\s+\d+\s+ROWS?\s+ONLY\b",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip()

    limit_match = re.search(r"\s+LIMIT\s+(\d+)\s*$", text, flags=re.IGNORECASE)
    if limit_match:
        count = limit_match.group(1)
        base = text[: limit_match.start()].rstrip()
        if not re.search(r"\bTOP\s+\d+\b", base, flags=re.IGNORECASE):
            base = re.sub(
                r"^\s*(SELECT\s+(?:DISTINCT\s+)?)",
                rf"\1TOP {count} ",
                base,
                count=1,
                flags=re.IGNORECASE,
            )
        text = base

    return text


def validate_sql_safety(sql: str) -> tuple[bool, str]:
    cleaned = normalize_tsql(sql)
    upper = cleaned.upper()

    if not upper.lstrip().startswith("SELECT"):
        return False, "Only SELECT statements are allowed."

    if ";" in cleaned:
        return False, "Multiple SQL statements are not allowed."

    for keyword in FORBIDDEN_SQL:
        if re.search(rf"\b{keyword}\b", upper):
            return False, f"Forbidden keyword detected: {keyword}."

    return True, ""


# =============================================================================
# CONVERSATION HISTORY (all routes / all query types)
# =============================================================================
def user_message_with_history(
    conversation_id: str | None,
    message: str,
    *,
    light: bool = False,
) -> str:
    """Build history context and append the current message.

    Prior turns include user text, assistant answers, and (for database turns)
    the SQL that was run. The MOST RECENT database turn also includes a tiny
    preview of its result rows — so a follow-up like "names of these items"
    can pin "these" to the actual IDs instead of re-querying the whole table.

    ``light=True`` (used by the cheap router call) omits SQL and rows.
    """
    if not conversation_id:
        return message

    limit = getattr(settings, "CHATBOT_MAX_HISTORY_TURNS", 8)
    turns = list(
        ChatBot.objects.filter(conv_id=conversation_id)
        .order_by("-created_at")[:limit]
    )

    if not turns:
        return message

    lines = ["Previous conversation (oldest first):"]
    ordered = list(reversed(turns))

    # Index of the last database turn — it's the only one we attach a result
    # preview to (keeps tokens bounded; older turns just keep their SQL).
    last_db_idx = next(
        (i for i in range(len(ordered) - 1, -1, -1) if ordered[i].route == "database"),
        -1,
    )

    for i, turn in enumerate(ordered):
        lines.append(f"User: {turn.message.strip()}")
        if turn.answer:
            lines.append(f"Assistant ({turn.route}): {turn.answer.strip()}")

        if light or turn.route != "database":
            continue

        if turn.generated_sql:
            lines.append(f"SQL used:\n{turn.generated_sql.strip()}")

        # Attach a short result preview ONLY for the most recent DB turn.
        if i == last_db_idx and turn.sql_result_raw:
            preview = _format_result_preview(turn.sql_result_raw)
            if preview:
                lines.append(f"Result of the SQL above (first rows):\n{preview}")

    history = "\n".join(lines)
    return f"{history}\n\nCurrent question:\n{message}"


def _format_result_preview(rows: Any, max_rows: int = 10) -> str:
    """Render the prior SQL result as a compact JSON list for the LLM.

    Keeps it small: at most ``max_rows`` rows, no nested objects.
    """
    try:
        if not isinstance(rows, list) or not rows:
            return ""
        preview = rows[:max_rows]
        return json.dumps(preview, default=str, ensure_ascii=False)
    except Exception:
        return ""


# =============================================================================
# ROUTER
# =============================================================================

def route_question(message: str, conversation_id: str | None = None) -> str:
    """Ask the LLM to classify the question as 'general' or 'database'."""
    reply = call_llm(
        label="router",
        system=ROUTER_PROMPT,
        user=user_message_with_history(conversation_id, message, light=True),
        max_tokens=8,
        conversation_id=conversation_id,
    )
    return "database" if "database" in reply.lower() else "general"


# =============================================================================
# DATABASE QUESTION STEPS
# =============================================================================

def generate_sql(
    message: str,
    *,
    schema_text: str,
    feedback: str = "",
    conversation_id: str | None = None,
) -> str:
    feedback_text = f"\nPrevious attempt feedback:\n{feedback}\n" if feedback else ""
    user = (
        f"{user_message_with_history(conversation_id, message)}"
        f"{feedback_text}\n\nSQL:"
    )

    raw_sql = call_llm(
        label="sql_gen",
        system=f"{SQL_GENERATION_PROMPT}\n\n{schema_text}",
        user=user,
        max_tokens=700,
        conversation_id=conversation_id,
    )
    return normalize_tsql(raw_sql)


def format_answer(
    rows: list[dict[str, Any]],
    message: str,
    conversation_id: str,
) -> dict[str, Any]:
    """
    Single LLM call that returns BOTH the headline summary AND the
    "What This Means" insight bullets. Combining them halves the latency
    and token spend compared to two separate calls.

    Returns:
        {"summary": str, "insights": list[str]}
    """
    sample = json.dumps(rows[: settings.CHATBOT_MAX_ROWS_TO_LLM], indent=2, default=str)

    raw = call_llm(
        label="answer+insights",
        system=ANSWER_PROMPT,
        user=(
            f"{user_message_with_history(conversation_id, message)}\n\n"
            f"Rows returned: {len(rows)}\n"
            f"Rows shown to you:\n{sample}\n\n"
            f"Buckets to exclude when picking a leader: "
            f"{sorted(_BUCKET_LABELS)}\n\n"
            'Return JSON now in the shape {"summary": "...", "insights": [...]}'
        ),
        max_tokens=700,
        conversation_id=conversation_id,
    )

    # Strip stray markdown fences and parse.
    cleaned = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()

    summary = ""
    insights: list[str] = []
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            summary = str(data.get("summary") or "").strip()
            raw_items = data.get("insights")
            if isinstance(raw_items, list):
                for it in raw_items:
                    if isinstance(it, str):
                        s = it.strip().lstrip("•-– ").strip()
                        if s:
                            insights.append(s)
            insights = insights[:4]
    except Exception as exc:
        logger.warning("[answer+insights] JSON parse failed: %s — falling back", exc)
        # Treat the whole reply as the summary text and return no bullets.
        summary = raw.strip()

    return {"summary": summary, "insights": insights}


def is_no_matching_schema(rows: list[dict[str, Any]]) -> bool:
    return len(rows) == 1 and str(rows[0].get("reason", "")).lower() == "no_matching_schema"


# Bucket / catch-all labels the LLM must never pick as the "leader". Used by
# `format_answer` when it asks the model to produce summary + insights together.
_BUCKET_LABELS = {
    "others", "other", "misc", "miscellaneous",
    "n/a", "na", "unknown", "null", "none", "rest",
}


# =============================================================================
# VISUALIZATIONS
# =============================================================================

def generate_visualizations(
    rows: list[dict[str, Any]],
    message: str,
    conversation_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return table + up to 2 charts for the frontend."""
    columns = list(rows[0].keys()) if rows else []
    visualizations: list[dict[str, Any]] = [
        {
            "type": "table",
            "title": "Query Results",
            "columns": columns,
            "rows": rows,
            "meta": {"total_rows": len(rows)},
        },
    ]

    if len(rows) < 2 or is_no_matching_schema(rows):
        return visualizations

    max_points = settings.CHATBOT_CHART_MAX_POINTS
    label_max_len = 45

    def to_float(value: Any) -> float | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return float(str(value).strip().replace(",", ""))
        except ValueError:
            return None

    def unique_labels(col: str) -> int:
        return len({str(row.get(col)).strip() for row in rows if row.get(col) is not None})

    def is_numeric_col(col: str) -> bool:
        values = [row.get(col) for row in rows if row.get(col) is not None]
        return bool(values) and all(to_float(v) is not None for v in values)

    def shorten(text: str) -> str:
        text = text.strip()
        return text if len(text) <= label_max_len else f"{text[: label_max_len - 3]}..."

    def pick_default_columns() -> tuple[str | None, str | None]:
        text_cols = [c for c in columns if not is_numeric_col(c)]
        numeric_cols = [c for c in columns if is_numeric_col(c)]
        if not text_cols or not numeric_cols:
            return None, None
        # Prefer a grouping column with a small number of distinct values.
        label_col = min(
            text_cols,
            key=lambda c: (
                0 if 2 <= unique_labels(c) <= 20 else 1,
                unique_labels(c),
            ),
        )
        return label_col, numeric_cols[0]

    def _is_time_series(chart_type: str, label_col: str) -> bool:
        """Line/area on a date-ish column → treat as time series."""
        if chart_type not in ("line", "area"):
            return False
        return bool(re.search(
            r"(date|time|month|week|year|day|hour|quarter)",
            label_col,
            re.IGNORECASE,
        ))

    def build_chart_data(
        label_col: str,
        value_col: str,
        *,
        chart_type: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        # 1. Aggregate values by label.
        totals: dict[str, float] = {}
        for row in rows:
            label = row.get(label_col)
            num = to_float(row.get(value_col))
            if label is None or num is None:
                continue
            key = str(label).strip()
            totals[key] = totals.get(key, 0.0) + num

        is_time = _is_time_series(chart_type, label_col)

        # 2. Order the points.
        #    - Time series → chronological by label so the line reads left→right.
        #    - Everything else → ranked by value, largest first.
        if is_time:
            ranked = sorted(totals.items(), key=lambda item: item[0])
        else:
            ranked = sorted(totals.items(), key=lambda item: item[1], reverse=True)

        total_groups = len(ranked)

        # 3. Decide how many points to show.
        #    Time-series charts (line / area) must NEVER bucket into "Others" —
        #    that breaks the trend. Show every point.
        if is_time:
            point_limit = total_groups          # show all
        elif chart_type in ("pie", "donut"):
            point_limit = min(max_points, 8)
        elif chart_type == "radial":
            point_limit = min(max_points, 10)
        elif chart_type == "treemap":
            point_limit = min(max_points, 25)
        else:
            point_limit = max_points

        truncated = total_groups > point_limit

        # 4. Aggregate the tail into "Others" — but only for non-time-series.
        if truncated and not is_time:
            visible = ranked[: point_limit - 1]
            others_total = sum(value for _, value in ranked[point_limit - 1:])
            if others_total:
                visible.append(("Others", others_total))
            ranked = visible

        data = [
            {"label": shorten(name), "full_label": name, "value": value}
            for name, value in ranked
        ]
        meta = {
            "total_groups": total_groups,
            "shown_groups": len(data),
            "aggregated": True,
            "truncated": truncated,
            "time_series": is_time,
        }
        return data, meta

    # Tell the LLM how many distinct values each column has.
    column_stats = {col: unique_labels(col) for col in columns if not is_numeric_col(col)}
    sample = json.dumps(rows[: settings.CHATBOT_MAX_ROWS_TO_LLM], indent=2, default=str)
    raw = call_llm(
        label="viz_plan",
        system=VISUALIZATION_PROMPT,
        user=(
            f"User question:\n{message}\n\n"
            f"Columns: {json.dumps(columns)}\n"
            f"Distinct label counts: {json.dumps(column_stats)}\n"
            f"Row count: {len(rows)}\n"
            f"Sample rows:\n{sample}\n\n"
            "JSON:"
        ),
        max_tokens=400,
        conversation_id=conversation_id,
    )

    # Visual families — two charts from the SAME family look redundant
    # (e.g. column+bar are both bars, pie+donut are both donuts).
    CHART_FAMILY = {
        "column":  "bars",
        "bar":     "bars",
        "pie":     "shares",
        "donut":   "shares",
        "line":    "trend",
        "area":    "trend",
        "radial":  "radial",
        "treemap": "treemap",
    }
    ALLOWED = set(CHART_FAMILY.keys())

    def looks_like_time_col(col: str) -> bool:
        return bool(re.search(r"(date|time|month|week|year|day|hour|quarter)", col, re.IGNORECASE))

    chart_specs: list[dict[str, str]] = []
    used_families: set[str] = set()
    try:
        cleaned = re.sub(r"```(?:json)?|```", "", raw, flags=re.IGNORECASE).strip()
        for chart in json.loads(cleaned).get("charts", []):
            chart_type = str(chart.get("type", "")).lower()
            label_col = chart.get("label_column", "")
            value_col = chart.get("value_column", "")
            family = CHART_FAMILY.get(chart_type)
            if (
                chart_type in ALLOWED
                and family
                and family not in used_families
                and label_col in columns
                and value_col in columns
                and label_col != value_col
                and is_numeric_col(value_col)
            ):
                title = chart.get("title") or f"Top {value_col} by {label_col}"
                if unique_labels(label_col) > max_points and not title.lower().startswith("top"):
                    title = f"Top {title}"
                chart_specs.append(
                    {
                        "type": chart_type,
                        "title": title,
                        "label_column": label_col,
                        "value_column": value_col,
                    }
                )
                used_families.add(family)
                if len(chart_specs) >= 3:
                    break
    except (json.JSONDecodeError, AttributeError, TypeError):
        pass

    # ---- Smart fallback when the LLM didn't give us three valid charts ----
    if len(chart_specs) < 3:
        label_col, value_col = pick_default_columns()
        if label_col and value_col:
            is_time = looks_like_time_col(label_col)
            n_labels = unique_labels(label_col)

            # Pick a sensible TRIPLET (always from three different families)
            # based on the shape of the data — instead of always column+pie.
            if is_time:
                triplet = ("line", "bar", "radial")
            elif n_labels > 12:
                triplet = ("bar", "treemap", "radial")
            elif n_labels >= 5:
                triplet = ("bar", "donut", "radial")
            else:
                triplet = ("column", "donut", "radial")

            titles = {
                "column":  f"{value_col} by {label_col}",
                "bar":     f"{value_col} by {label_col}",
                "line":    f"{value_col} over {label_col}",
                "area":    f"Cumulative {value_col}",
                "pie":     f"{value_col} share by {label_col}",
                "donut":   f"{value_col} share by {label_col}",
                "radial":  f"Top {label_col} by {value_col}",
                "treemap": f"{value_col} composition",
            }

            # Top up the existing specs while respecting families already used.
            for t in triplet:
                if len(chart_specs) >= 3:
                    break
                fam = CHART_FAMILY[t]
                if fam in used_families:
                    continue
                chart_specs.append(
                    {
                        "type": t,
                        "title": titles.get(t, f"{value_col} by {label_col}"),
                        "label_column": label_col,
                        "value_column": value_col,
                    }
                )
                used_families.add(fam)

            # Still short? Backfill with any remaining unused family.
            if len(chart_specs) < 3:
                for t, fam in CHART_FAMILY.items():
                    if len(chart_specs) >= 3:
                        break
                    if fam in used_families:
                        continue
                    chart_specs.append(
                        {
                            "type": t,
                            "title": titles.get(t, f"{value_col} by {label_col}"),
                            "label_column": label_col,
                            "value_column": value_col,
                        }
                    )
                    used_families.add(fam)

    for spec in chart_specs[:3]:
        data, meta = build_chart_data(
            spec["label_column"],
            spec["value_column"],
            chart_type=spec["type"],
        )
        if data:
            visualizations.append({**spec, "data": data, "meta": meta})

    return visualizations


# =============================================================================
# ERROR FEEDBACK FOR REGENERATION
# =============================================================================

def build_sql_error_feedback(
    error: str,
    sql: str,
    rejected_columns: set[str],
    catalog: dict[str, Any],
) -> str:
    bad_columns = list(dict.fromkeys(re.findall(r"Invalid column name '([^']+)'", str(error))))
    rejected_columns.update(bad_columns)

    table_refs = extract_table_refs(sql)
    objects: dict[str, list[str]] = catalog["objects"]

    parts = [f"SQL Server error: {error}"]

    if bad_columns:
        parts.append("Invalid columns: " + ", ".join(bad_columns))

    if rejected_columns:
        parts.append("Do not use these rejected column names again: " + ", ".join(sorted(rejected_columns)))

    for ref in table_refs:
        match = lookup_object(objects, ref)
        if not match:
            parts.append(f"Table {ref} is not in the catalog. Use only catalog tables.")
            continue

        object_name, columns = match
        parts.append(f"Valid columns for {object_name}: {', '.join(columns)}")
        for bad in bad_columns:
            suggestions = suggest_columns(bad, columns)
            if suggestions:
                parts.append(f"Instead of {bad}, consider: {', '.join(suggestions)}")

    return "\n".join(parts)


def extract_table_refs(sql: str) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()
    pattern = re.compile(
        r"\b(?:FROM|JOIN)\s+(?:\[?(?P<schema>\w+)\]?\.)?\[?(?P<table>\w+)\]?",
        re.IGNORECASE,
    )
    for match in pattern.finditer(sql):
        schema = match.group("schema") or "dbo"
        table = match.group("table")
        ref = f"{schema}.{table}"
        if ref.lower() not in seen:
            seen.add(ref.lower())
            refs.append(ref)
    return refs


def lookup_object(objects: dict[str, list[str]], ref: str) -> tuple[str, list[str]] | None:
    if ref in objects:
        return ref, objects[ref]

    ref_lower = ref.lower()
    for name, columns in objects.items():
        if name.lower() == ref_lower:
            return name, columns
    return None


def suggest_columns(bad: str, columns: list[str]) -> list[str]:
    bad_lower = bad.lower()
    suggestions: list[str] = []

    for col in columns:
        if bad_lower == col.lower() or bad_lower in col.lower() or col.lower() in bad_lower:
            suggestions.append(col)

    suggestions.extend(difflib.get_close_matches(bad, columns, n=5, cutoff=0.45))
    return list(dict.fromkeys(suggestions))[:5]


# =============================================================================
# MASTER PIPELINE
# =============================================================================

def run_chat_pipeline(conversation_id: str, user_message: str) -> dict[str, Any]:
    started = time.time()

    result: dict[str, Any] = {
        "route": "general",
        "generated_sql": None,
        "sql_result_raw": None,
        "visualizations": None,
        "final_answer": "",
        "guardrail_hit": False,
        "guardrail_reason": None,
        "duration_ms": 0,
    }

    try:
        route = route_question(user_message, conversation_id)
        result["route"] = route

        if route == "general":
            result["final_answer"] = call_llm(
                label="general",
                system=GENERAL_PROMPT,
                user=user_message_with_history(conversation_id, user_message),
                max_tokens=500,
                conversation_id=conversation_id,
            )
        else:
            run_database_pipeline(result, conversation_id, user_message)

    except Exception as exc:
        logger.exception("[Pipeline] failed")
        result["final_answer"] = f"I could not complete the request: {exc}"

    result["duration_ms"] = int((time.time() - started) * 1000)

    logger.info(
        "[Pipeline] done route=%s regen=%s duration_ms=%s",
        result.get("route"),
        result.get("duration_ms"),
    )

    return result


def run_database_pipeline(result: dict[str, Any], conversation_id: str, user_message: str) -> None:
    feedback = ""
    rejected_columns: set[str] = set()
    catalog = load_schema_catalog()
    schema_text = schema_context(catalog)

    for attempt in range(settings.CHATBOT_MAX_RETRIES + 1):

        sql = generate_sql(
            user_message,
            schema_text=schema_text,
            feedback=feedback,
            conversation_id=conversation_id,
        )
        result["generated_sql"] = sql

        ok, reason = validate_sql_safety(sql)
        if not ok:
            result["guardrail_hit"] = True
            result["guardrail_reason"] = reason
            result["final_answer"] = f"Query blocked for security: {reason} Please ask a read-only question."
            return

        try:
            rows = execute_sql(sql)
            result["sql_result_raw"] = rows
            result["visualizations"] = generate_visualizations(
                rows,
                user_message,
                conversation_id,
            )
        except Exception as exc:
            if attempt >= settings.CHATBOT_MAX_RETRIES:
                logger.exception("[Pipeline] SQL failed after retries")
                result["final_answer"] = (
                    "I could not produce a reliable database answer for that question. "
                    "Please rephrase it with a specific table area, date range, or business metric."
                )
                return
            feedback = build_sql_error_feedback(str(exc), sql, rejected_columns, catalog)
            continue

        if is_no_matching_schema(rows):
            result["final_answer"] = "I could not find matching tables or columns in the database catalog for that question."
            return

        # ONE LLM call produces both the headline summary AND the
        # "What This Means" bullets. Insights ride inside the
        # `visualizations` JSON so they persist & flow through history.
        parsed = format_answer(rows, user_message, conversation_id)
        result["final_answer"] = parsed.get("summary") or ""

        insights = parsed.get("insights") or []
        if insights:
            viz_list = result.get("visualizations") or []
            viz_list.append({"type": "insights", "items": insights})
            result["visualizations"] = viz_list

        return


# =============================================================================
# REPLY ORCHESTRATION  (single streaming pipeline)
# =============================================================================
def create_reply(*, conv_id: str, message: str, sender: str = "user"):
    """
    Generator that yields progress dicts as the pipeline runs.
    The view wraps each yielded dict into an SSE `data:` frame.

    Event shapes:
      {"type": "status",   "stage": "<name>"}       — progress label
      {"type": "data",     "visualizations": [...], "conv_id": ..., ...}
                                                    — early payload (charts ready)
      {"type": "complete", "messages": [...], ...}  — full final response
      {"type": "error",    "message": "..."}        — fatal error, stop
    """
    started = time.time()

    # 0. Make sure we have a conv_id
    if not conv_id:
        try:
            conv_id = create_openai_conversation()
        except Exception as exc:
            logger.exception("create_openai_conversation failed: %s", exc)
            yield {"type": "error", "message": "Could not start conversation."}
            return

    yield {"type": "status", "stage": "thinking", "conv_id": conv_id}

    # 1. DB reachability
    db_ok, db_error = ensure_db_connected(max_retries=5)
    if not db_ok:
        yield {"type": "error", "message": "Database is currently unreachable. Please try again."}
        return

    # 2. Classify route
    yield {"type": "status", "stage": "routing"}
    try:
        route = route_question(message, conv_id)
    except Exception as exc:
        logger.exception("route_question failed: %s", exc)
        yield {"type": "error", "message": "Could not classify the question."}
        return

    # ─── GENERAL ROUTE (no SQL) ─────────────────────────────────────────
    if route != "database":
        yield {"type": "status", "stage": "answering"}
        try:
            answer = call_llm(
                label="general",
                system=GENERAL_PROMPT,
                user=user_message_with_history(conv_id, message),
                max_tokens=500,
                conversation_id=conv_id,
            )
        except Exception as exc:
            logger.exception("general answer failed: %s", exc)
            yield {"type": "error", "message": "Could not generate an answer."}
            return

        duration_ms = int((time.time() - started) * 1000)
        record = ChatBot.objects.create(
            conv_id=conv_id, message=message, answer=answer,
            route="general", generated_sql=None, sql_result_raw=None,
            visualizations=None, duration_ms=duration_ms,
        )
        ts = (getattr(record, "created_at", None) or timezone.now()).isoformat()

        yield {
            "type": "complete",
            "conv_id": conv_id,
            "route": "general",
            "messages": [
                {"sender": sender, "message": message, "date_time": ts},
                {"sender": "ai",   "message": answer,  "date_time": ts},
            ],
            "visualizations": [],
            "duration_ms": duration_ms,
        }
        return

    # ─── DATABASE ROUTE ─────────────────────────────────────────────────
    catalog = load_schema_catalog()
    schema_text = schema_context(catalog)
    rejected_columns: set[str] = set()
    feedback = ""
    sql = None
    rows = None

    for attempt in range(settings.CHATBOT_MAX_RETRIES + 1):
        yield {"type": "status", "stage": "generating_sql", "attempt": attempt + 1}
        try:
            sql = generate_sql(
                message,
                schema_text=schema_text,
                feedback=feedback,
                conversation_id=conv_id,
            )
        except Exception as exc:
            logger.exception("generate_sql failed: %s", exc)
            yield {"type": "error", "message": "Could not generate SQL."}
            return

        ok, reason = validate_sql_safety(sql)
        if not ok:
            yield {"type": "error", "message": f"Query blocked for security: {reason}"}
            return

        yield {"type": "status", "stage": "executing"}
        try:
            rows = execute_sql(sql)
        except Exception as exc:
            if attempt >= settings.CHATBOT_MAX_RETRIES:
                logger.exception("SQL execution failed after retries")
                yield {"type": "error", "message": "Could not produce a reliable database answer. Please rephrase the question."}
                return
            feedback = build_sql_error_feedback(str(exc), sql, rejected_columns, catalog)
            continue

        # SQL executed cleanly — but if it returned NO ROWS and we still have
        # retry budget, ask the model to try again with feedback about WHY
        # the prior SQL was empty. Common causes: wrong JOIN keys, wrong
        # table, or an over-strict WHERE filter the user did not request.
        if not rows and not is_no_matching_schema(rows) and attempt < settings.CHATBOT_MAX_RETRIES:
            logger.info("[empty-rows retry] attempt=%d sql=%s", attempt, sql[:120])
            feedback = (
                "The previous SQL ran but returned 0 rows. Look at the SQL "
                "below and reconsider:\n"
                "  • Are the JOIN keys correct? Column names that LOOK similar "
                "(e.g. Product_ID vs Product_Item_ID, product_id vs "
                "product_item_id) are usually NOT the same column. Read the "
                "FK description in the catalog and use the EXACT join key it "
                "names.\n"
                "  • Did you filter on a column that doesn't actually exist "
                "in the joined table, or with a value that doesn't exist in "
                "the data?\n"
                "  • If the user asked for a name/description, do NOT drop the "
                "JOIN to the lookup table — fix the join key instead.\n"
                "  • If the user asked for a measure, try the most populated "
                "base table for that measure instead.\n\n"
                f"Previous SQL that returned 0 rows:\n{sql}"
            )
            continue

        break  # success — non-empty rows

    if is_no_matching_schema(rows):
        yield {"type": "error", "message": "I could not find matching tables or columns in the database for that question."}
        return

    # ─────────────────────────────────────────────────────────────────────
    # Context-bleed safety net.
    #
    # If the contextual SQL returned 0 rows AND this is a follow-up turn,
    # retry ONCE with no history — catches the case where a new subject
    # gets the previous turn's tables/filters glued onto it.
    #
    # BUT skip the retry when the question is clearly REFERENCING the prior
    # turn ("these", "those", "the same"…), because dropping history would
    # also drop the IDs the question depends on.
    # ─────────────────────────────────────────────────────────────────────
    _msg = (message or "").lower()
    references_prior = any(w in _msg for w in (
        "these", "those", "the same", "above", "previous", "earlier", "the ids"
    ))
    had_history = bool(conv_id) and ChatBot.objects.filter(conv_id=conv_id).exists()
    if not rows and had_history and not references_prior:
        yield {"type": "status", "stage": "generating_sql", "attempt": "retry-clean"}
        try:
            clean_sql = generate_sql(
                message,
                schema_text=schema_text,
                feedback="",
                conversation_id=None,   # ← key: drop history
            )
            ok, reason = validate_sql_safety(clean_sql)
            if ok and clean_sql.strip().lower() != (sql or "").strip().lower():
                yield {"type": "status", "stage": "executing"}
                clean_rows = execute_sql(clean_sql)
                # Only adopt the retry if it actually found something.
                if clean_rows and not is_no_matching_schema(clean_rows):
                    sql, rows = clean_sql, clean_rows
        except Exception as exc:
            logger.warning("[no-history retry] failed (keeping empty result): %s", exc)

    # Build charts
    yield {"type": "status", "stage": "visualizing"}
    visualizations = generate_visualizations(rows, message, conv_id) or []

    # ⭐ EARLY EMIT — charts & table reach the UI before the LLM writes the summary.
    yield {
        "type": "data",
        "conv_id": conv_id,
        "route": "database",
        "generated_sql": sql,
        "sql_result_raw": rows,
        "visualizations": visualizations,
    }

    # Final LLM call: summary + insights (one round trip)
    yield {"type": "status", "stage": "analyzing"}
    try:
        parsed = format_answer(rows, message, conv_id)
    except Exception as exc:
        logger.exception("format_answer failed: %s", exc)
        yield {"type": "error", "message": "Could not write the summary."}
        return

    summary = (parsed.get("summary") or "").strip()
    insights = parsed.get("insights") or []
    if insights:
        visualizations.append({"type": "insights", "items": insights})

    duration_ms = int((time.time() - started) * 1000)
    record = ChatBot.objects.create(
        conv_id=conv_id, message=message, answer=summary,
        route="database", generated_sql=sql, sql_result_raw=rows,
        visualizations=visualizations, duration_ms=duration_ms,
    )
    ts = (getattr(record, "created_at", None) or timezone.now()).isoformat()

    yield {
        "type": "complete",
        "conv_id": conv_id,
        "route": "database",
        "generated_sql": sql,
        "sql_result_raw": rows,
        "visualizations": visualizations,
        "messages": [
            {"sender": sender, "message": message, "date_time": ts},
            {"sender": "ai",   "message": summary, "date_time": ts},
        ],
        "duration_ms": duration_ms,
    }



# =============================================================================
# CONVERSATION HISTORY  —  API helpers backing /api/chatbot/conversations/
# =============================================================================
from django.db.models import Max, Min, Count


def _truncate_title(text: str, limit: int = 80) -> str:
    text = (text or "").strip() or "Untitled"
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def list_conversations():
    """
    Build the conversation summary list — newest first.
    Returns: ([{conv_id, title, created_at, updated_at, message_count}, ...], 200)
    """
    try:
        rows = (
            ChatBot.objects
            .values("conv_id")
            .annotate(
                first_at=Min("created_at"),
                last_at=Max("created_at"),
                turn_count=Count("id"),
            )
            .order_by("-last_at")
        )

        out = []
        for r in rows:
            first_message = (
                ChatBot.objects
                .filter(conv_id=r["conv_id"])
                .order_by("created_at")
                .values_list("message", flat=True)
                .first()
            )
            out.append({
                "conv_id":       r["conv_id"],
                "title":         _truncate_title(first_message),
                "created_at":    r["first_at"],
                "updated_at":    r["last_at"],
                "message_count": r["turn_count"],
            })

        return out, 200

    except Exception as e:
        logger.exception("list_conversations failed: %s", e)
        return {"error": "Failed to load conversations", "detail": str(e)}, 500


def get_conversation(conv_id: str):
    """
    Fetch every turn for `conv_id`, expanded into a flat user/ai message timeline.
    """
    if not conv_id:
        return {"error": "Missing conv_id"}, 400

    try:
        qs = ChatBot.objects.filter(conv_id=conv_id).order_by("created_at")
        if not qs.exists():
            return {"error": "Conversation not found"}, 404

        messages = []
        for rec in qs:
            ts = rec.created_at.isoformat() if rec.created_at else None
            messages.append({
                "sender":    "user",
                "message":   rec.message,
                "date_time": ts,
            })
            messages.append({
                "sender":         "ai",
                "message":        rec.answer or "",
                "date_time":      ts,
                "visualizations": rec.visualizations or [],
                "route":          rec.route,
                "generated_sql":  rec.generated_sql,
            })

        return {"conv_id": conv_id, "messages": messages}, 200

    except Exception as e:
        logger.exception("get_conversation failed: %s", e)
        return {"error": "Failed to load conversation", "detail": str(e)}, 500


def delete_conversation(conv_id: str):
    """Permanently delete every turn for this conversation."""
    if not conv_id:
        return {"error": "Missing conv_id"}, 400

    try:
        deleted, _ = ChatBot.objects.filter(conv_id=conv_id).delete()
        if not deleted:
            return {"error": "Conversation not found"}, 404
        return {"deleted": deleted}, 200

    except Exception as e:
        logger.exception("delete_conversation failed: %s", e)
        return {"error": "Failed to delete conversation", "detail": str(e)}, 500
