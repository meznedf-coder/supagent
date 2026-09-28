"""Text that the metadata database can store.

Superset's metadata database is sometimes not UTF-8 (PostgreSQL created with LATIN1, MySQL
without ?charset=utf8mb4). Its driver then encodes every text with that encoding, and one
character outside it fails the whole write: "'latin-1' codec can't encode character '\\u2013'".
LLMs write such characters all the time (– — ‑ ’ “ ” … •), and data can hold them too.

`db_codec()` finds the encoding of the connection once (None for UTF-8: nothing to do), and
`fold()` makes a text storable: typographic characters become their plain equivalents
(– → -, ’ → ', … → ...), accented letters outside the encoding lose their accent (ł → l
when possible), the rest becomes "?". Accents that the encoding has (é, à, ç in Latin-1)
are kept. The text columns of supagent's tables fold on write (SafeText, SafeString).
"""

from __future__ import annotations

import logging
import threading
import unicodedata
from typing import Any

import sqlalchemy as sa
from sqlalchemy.types import TypeDecorator

log = logging.getLogger(__name__)
_LOCK = threading.Lock()
_STATE: dict[str, Any] = {"known": False, "codec": None}

PLAIN = {
    **{c: "-" for c in "‐‑‒–—―−⁃⸺⸻"},
    **{c: "'" for c in "‘’‚‛′‵"},
    **{c: '"' for c in "“”„‟″‶"},
    **{c: " " for c in "             　"},
    **{c: "" for c in "​‌‍⁠﻿"},
    "…": "...", "•": "*", "‣": ">", "‧": "-", "●": "*", "◦": "o", "∙": "*",
    "→": "->", "←": "<-", "↔": "<->", "⇒": "=>", "⇐": "<=", "↑": "^", "↓": "v",
    "≤": "<=", "≥": ">=", "≠": "!=", "≈": "~", "∞": "inf", "∑": "sum",
    "✓": "v", "✔": "v", "✗": "x", "✘": "x", "✅": "[ok]", "❌": "[x]",
    "⚠": "(!)", "ℹ": "(i)", "€": "EUR", "™": "(TM)",
    **{c: "#" for c in "█▉▊▋▌▍▎▏▐▀▄■▪"},
    **{c: "|" for c in "│┃║"}, **{c: "-" for c in "─━═"},
    **{c: "+" for c in "┌┐└┘├┤┬┴┼"},
}
_TABLE = str.maketrans(PLAIN)
PG_CODECS = {"UTF8": None, "UNICODE": None, "LATIN1": "latin-1", "LATIN9": "iso8859-15", "WIN1252": "cp1252",
             "SQL_ASCII": "ascii", "LATIN2": "iso8859-2", "WIN1250": "cp1250", "WIN1251": "cp1251"}
MYSQL_CODECS = {"utf8mb4": None, "latin1": "cp1252", "ascii": "ascii", "utf8": "utf8mb3", "utf8mb3": "utf8mb3"}


def fold(text: Any, codec: str | None) -> Any:
    """`text` storable with `codec` (unchanged when it already is, or when codec is None)."""
    if not isinstance(text, str) or codec is None:
        return text
    if codec == "utf8mb3":                              # MySQL "utf8": no character beyond U+FFFF
        return "".join(ch if ord(ch) <= 0xFFFF else "?" for ch in text)
    try:
        text.encode(codec)
        return text
    except UnicodeEncodeError:
        pass
    text = text.translate(_TABLE)
    out = []
    for ch in text:
        try:
            ch.encode(codec)
            out.append(ch)
            continue
        except UnicodeEncodeError:
            pass
        base = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
        try:
            base.encode(codec)
            out.append(base or "?")
        except UnicodeEncodeError:
            out.append("?")
    return "".join(out)


def fold_all(value: Any, codec: str | None) -> Any:
    """fold() on every text of a JSON-like value (tool arguments)."""
    if codec is None:
        return value
    if isinstance(value, str):
        return fold(value, codec)
    if isinstance(value, list):
        return [fold_all(v, codec) for v in value]
    if isinstance(value, dict):
        return {k: fold_all(v, codec) for k, v in value.items()}
    return value


def detect_codec(engine: Any) -> str | None:
    """The Python codec of the connection's client encoding; None for UTF-8 (or unknown)."""
    name = engine.dialect.name
    with engine.connect() as conn:
        if name == "postgresql":
            enc = str(conn.execute(sa.text("SHOW client_encoding")).scalar() or "").upper()
            if enc in PG_CODECS:
                return PG_CODECS[enc]
        elif name in ("mysql", "mariadb"):
            enc = str(conn.execute(sa.text("SELECT @@character_set_client")).scalar() or "").lower()
            if enc in MYSQL_CODECS:
                return MYSQL_CODECS[enc]
        else:
            return None
    try:
        import codecs

        return None if codecs.lookup(enc).name == "utf-8" else codecs.lookup(enc).name
    except LookupError:
        log.warning("supagent: unknown database encoding %r: text is written as it is", enc)
        return None


def db_codec() -> str | None:
    """The metadata database's encoding, found once per process."""
    if _STATE["known"]:
        return _STATE["codec"]
    with _LOCK:
        if not _STATE["known"]:
            try:
                from superset import db

                _STATE["codec"] = detect_codec(db.engine)
                if _STATE["codec"]:
                    log.warning("supagent: the metadata database is not UTF-8 (%s): characters it cannot "
                                "store are replaced (e.g. – by -)", _STATE["codec"])
            except Exception as ex:  # pylint: disable=broad-except   (tried again at the next write)
                log.warning("supagent: database encoding not known yet: %s", ex)
                return None
            _STATE["known"] = True
    return _STATE["codec"]


def set_codec(codec: str | None) -> None:
    """For tests: pretend the metadata database has this encoding."""
    _STATE.update(known=True, codec=codec)


class SafeText(TypeDecorator):  # pylint: disable=abstract-method
    """Text that is folded to what the metadata database can store."""

    impl = sa.Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        return fold(value, db_codec()) if isinstance(value, str) else value


class SafeString(TypeDecorator):  # pylint: disable=abstract-method
    impl = sa.String
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        return fold(value, db_codec()) if isinstance(value, str) else value


def content_disposition(kind: str, name: str) -> str:
    """A download header that any file name survives (HTTP headers are Latin-1)."""
    from urllib.parse import quote

    ascii_name = fold(name, "ascii").replace('"', "'").replace("\\", "_") or "file"
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"
