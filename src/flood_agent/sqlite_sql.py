"""A query rewritten so that a parser of another dialect reads it as SQLite does (ADR 0008).

Column lineage parses with sqlglot's postgres dialect, which differs from SQLite in comments, quoting, typed literals,
keywords and function syntax. `canonical` therefore tokenises by SQLite's own lexing rules and writes the query again
with nothing left to read two ways: no comments, every name double-quoted, every literal and function reduced to what
lineage needs of it (which columns it takes values from, not what it computes). Anything it is not sure of is
`Unreadable`.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass

FUNCTION = "canopy_fn"  # what every function but COUNT is called, so that none has syntax of its own

# Every keyword SQLite knows (https://sqlite.org/lang_keywords.html).
KEYWORDS = frozenset("""
abort action add after all alter always analyze and as asc attach autoincrement before begin between by cascade case
cast check collate column commit conflict constraint create cross current current_date current_time current_timestamp
database default deferrable deferred delete desc detach distinct do drop each else end escape except exclude exclusive
exists explain fail filter first following for foreign from full generated glob group groups having if ignore immediate
in index indexed initially inner insert instead intersect into is isnull join key last left like limit match
materialized natural no not nothing notnull null nulls of offset on or order others outer over partition plan pragma
preceding primary query raise range recursive references regexp reindex release rename replace restrict returning right
rollback row rows savepoint select set table temp temporary then ties to transaction trigger unbounded union unique
update using vacuum values view virtual when where window with without
""".split())
# Keywords SQLite never reads as a name when written bare, so that they are always keywords.
RESERVED = frozenset("""
select distinct all from where group having order limit as on using join union intersect except and or not in is null
between escape exists case when then else collate
""".split())
_CLOCK = frozenset({"current_date", "current_time", "current_timestamp"})  # constants, even beside such a column
_BOOLEANS = frozenset({"true", "false"})  # constants, unless there is something of that name to read
_COMPARISONS = frozenset({"=", "==", "!=", "<>", "<", "<=", ">", ">="})
_JOINS = frozenset({"left", "right", "full", "inner", "cross", "outer"})
# A name the parser makes up for a column or a subquery that has none: `_col_0`, `_0`, `_u_0`.
_MADE_UP = re.compile(r"_(\d|col|[a-z]_)")
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")

# SQLite's tokens. Its white space is these five characters only, a line comment ends only at "\n", and block comments
# do not nest. Whatever matches none of these ([name], `name`, parameters, a non-ASCII letter) is not accepted.
_TOKEN = re.compile(r"""
    (?P<space>[ \t\n\f\r]+ | --[^\n]* | /\*(?s:.*?)\*/)
  | (?P<blob>[xX]'[0-9A-Fa-f]*')
  | (?P<word>[A-Za-z_][A-Za-z0-9_$]*)
  | (?P<string>'(?:[^']|'')*')
  | (?P<quoted>"(?:[^"]|"")*")
  | (?P<number>(?:0[xX][0-9A-Fa-f]+|(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)(?![A-Za-z0-9_$.]|[^\x00-\x7f]))
  | (?P<op>\|\||<<|>>|<=|>=|==|!=|<>|->>|->|/(?!\*)|[-+*%&|<>=~(),.;])
""", re.VERBOSE)


class Unreadable(Exception):
    """SQL this cannot rewrite with certainty. The message says why, without quoting any of it."""


def fold(name: str) -> str:
    """`name` as SQLite compares names: only the ASCII letters have a case."""
    return name.translate(_ASCII_LOWER)


@dataclass(frozen=True)
class _Token:
    kind: str  # "word", "quoted", "string", "blob", "number" or "op"
    text: str  # as written

    @property
    def word(self) -> str:
        """The folded word, or "" for any other token: what to compare with keywords."""
        return fold(self.text) if self.kind == "word" else ""

    @property
    def name(self) -> str:
        """The name a word, quoted identifier or string spells."""
        if self.kind == "word":
            return self.text
        return self.text[1:-1].replace(self.text[0] * 2, self.text[0])

    @property
    def is_name(self) -> bool:
        """Whether it can only be, or is most simply, a name: not a keyword."""
        return self.kind == "quoted" or self.kind == "word" and self.word not in KEYWORDS


_END = _Token("end", "")


def _tokens(sql: str) -> list[_Token]:
    if "\0" in sql:
        raise Unreadable("a NUL character")
    tokens, at = [], 0
    while at < len(sql):
        match = _TOKEN.match(sql, at)
        if match is None:
            raise Unreadable("a character or an unfinished token that is not accepted")
        if match.lastgroup != "space":
            tokens.append(_Token(str(match.lastgroup), match.group()))
        at = match.end()
    if tokens and tokens[-1].text == ";":
        tokens.pop()
    if any(t.text == ";" for t in tokens):
        raise Unreadable("more than one statement")
    return tokens


def check_name(name: str) -> str:
    """`name`, if both read it as the same name: printable ASCII, and not one the parser makes up itself."""
    if not name or not all(" " <= c <= "~" for c in name):
        raise Unreadable("a name that is empty or not printable ASCII")
    if _MADE_UP.match(fold(name)):
        raise Unreadable("a name of the kind the parser makes up")
    return name


def canonical(sql: str, columns: Iterable[str] = ()) -> str:
    """`sql`, a query SQLite accepts, rewritten for the parser. `columns` names every column of the tables it reads:
    SQLite may read a bare word as one of those, even a word that is a keyword elsewhere."""
    return _Rewrite(_tokens(sql), columns).text()


class _Rewrite:
    def __init__(self, tokens: list[_Token], columns: Iterable[str]):
        self._tokens = tokens
        self._names = {fold(c) for c in columns} | self._names_given(tokens)
        self._out: list[str] = []
        self._previous = _END  # the last token taken
        self._operand = False  # whether it ended a value or a name, so that no operand can follow directly
        self._cases = 0  # CASE expressions open
        self._brackets: list[bool] = []  # one per open bracket: whether it is a CAST's

    @staticmethod
    def _names_given(tokens: list[_Token]) -> set[str]:
        """Everything the query itself may define as a name."""
        names = set()
        for previous, token in zip([_END, *tokens], tokens):
            aliasing = previous.word == "as" or previous.kind in ("word", "quoted", "number") or previous.text == ")"
            if token.kind == "quoted" or token.kind == "string" and aliasing:
                names.add(fold(token.name))
            elif token.kind == "word" and (previous.word == "as" or previous.text == "."):
                names.add(token.word)
        return names

    def _at(self, i: int) -> _Token:
        return self._tokens[i] if i < len(self._tokens) else _END

    def text(self) -> str:
        i = 0
        while i < len(self._tokens):
            i = self._take(i)
        return " ".join(self._out)

    def _emit(self, text: str, token: _Token, operand: bool) -> None:
        self._out.append(text)
        self._previous, self._operand = token, operand

    def _name(self, token: _Token) -> None:
        self._emit('"' + check_name(token.name).replace('"', '""') + '"', token, True)

    def _take(self, i: int) -> int:
        """Rewrite the token at `i`, with any that go with it; returns where the next one is."""
        token = self._tokens[i]
        match token.kind:
            case "number":
                self._emit(token.text if token.text.isdigit() else "1", token, True)
            case "blob":
                self._emit("'s'", token, True)
            case "string":
                self._string(token)
            case "quoted":
                self._function_or_name(i)
            case "word":
                return self._word(i)
            case _:
                self._operator(token)
        return i + 1

    def _string(self, token: _Token) -> None:
        if self._previous.word == "as":
            self._name(token)  # SQLite takes a string for a name here
        elif self._operand:  # and here: `x 'y'` is x under the alias y, never a literal with a type
            self._out.append("AS")
            self._name(token)
        elif self._previous.text == "." or self._previous.word in ("from", "join", "in"):
            raise Unreadable("a string where a table or column is named")
        else:
            self._emit("'s'", token, True)

    def _operator(self, token: _Token) -> None:
        text = token.text
        match text:
            case "(":
                self._brackets.append(self._previous.word == "cast")
                self._emit(text, token, False)
            case ")":
                if not self._brackets:
                    raise Unreadable("unbalanced brackets")
                self._brackets.pop()
                self._emit(text, token, True)
            case "," | ".":
                self._emit(text, token, False)
            case "*" if self._previous is _END or self._previous.text in ("(", ",", ".") or (
                    self._previous.word in ("select", "distinct", "all")):
                self._emit("*", token, True)
            case _ if text in _COMPARISONS:
                self._emit("=", token, False)
            case "-" | "~":
                self._emit("-", token, False)
            case "+" if not self._operand:  # the parser drops a unary +, and with it how SQLite names the column
                self._emit("-", token, False)
            case _:  # every other operator takes values from both sides, as + does
                self._emit("+", token, False)

    def _function_or_name(self, i: int) -> None:
        token = self._tokens[i]
        if self._at(i + 1).text == "(" and not self._names_columns(i + 1):
            self._emit("COUNT" if fold(token.name) == "count" else FUNCTION, token, False)
        else:
            self._name(token)

    def _names_columns(self, opening: int) -> bool:
        """Whether the bracket at `opening` holds a CTE's column list: `name(a, b) AS (`."""
        depth = 0
        for j in range(opening, len(self._tokens)):
            depth += {"(": 1, ")": -1}.get(self._tokens[j].text, 0)
            if depth == 0:
                return self._at(j + 1).word == "as" and (
                    self._at(j + 2).text == "(" or self._at(j + 2).word in ("not", "materialized"))
        return False

    def _word(self, i: int) -> int:
        token, following = self._tokens[i], self._at(i + 1)
        word, previous = token.word, self._previous.word
        # `AS [NOT] MATERIALIZED (` is a hint to SQLite alone.
        if previous == "as" and (word == "materialized" and following.text == "(" or
                                 word == "not" and following.word == "materialized"):
            return i + (2 if word == "not" else 1)
        if word == "collate":  # how values compare, never which values there are
            if not (following.is_name or following.kind == "word"):
                raise Unreadable("a collation that is not named")
            return i + 2
        if word == "as" and self._brackets and self._brackets[-1]:
            return self._cast_type(i)
        if word in RESERVED:
            self._cases += word == "case"
            self._emit(word.upper(), token, word == "null")
        elif previous == "as" or self._previous.text == ".":
            self._name(token)
        elif word in _CLOCK:
            if self._operand:
                raise Unreadable("a clock keyword used as a name")
            self._emit("1", token, True)
        elif word in _BOOLEANS:
            if self._operand or word in self._names:
                raise Unreadable("TRUE or FALSE that may be a name")
            self._emit(word.upper(), token, True)
        elif word not in KEYWORDS:
            self._function_or_name(i)
        elif not self._operand and word in self._names:
            self._name(token)  # where a value is expected, SQLite reads the name
        elif self._is_keyword(i):
            self._cases -= word == "end"
            self._emit(word.upper(), token, word in ("end", "asc", "desc"))
        elif following.text == "(":
            self._emit(FUNCTION, token, False)  # replace(), like(), glob(), if()
        elif word in self._names:
            self._name(token)
        else:
            raise Unreadable("a keyword in a place where it may be a name")
        return i + 1

    def _is_keyword(self, i: int) -> bool:
        """Whether SQLite can only be reading the unreserved keyword at `i` as a keyword, from what is around it."""
        word, previous, following = self._tokens[i].word, self._previous.word, self._at(i + 1)
        after_value, next_word = self._operand, following.word
        match word:
            case "end":
                return after_value and self._cases > 0
            case "asc" | "desc":
                return after_value
            case "by":
                return previous in ("group", "order", "partition")
            case "nulls":
                return after_value and next_word in ("first", "last")
            case "first" | "last":
                return previous == "nulls"
            case "offset":
                return after_value and following.kind == "number"
            case "over":
                return self._previous.text == ")" and (following.text == "(" or following.is_name)
            case "filter":
                return self._previous.text == ")" and following.text == "("
            case "partition":
                return next_word == "by"
            case "rows" | "range" | "groups":
                return next_word in ("between", "unbounded", "current") or following.kind == "number"
            case "unbounded":
                return next_word in ("preceding", "following")
            case "preceding" | "following":
                return previous == "unbounded" or self._previous.kind == "number"
            case "current":
                return next_word == "row"
            case "row":
                return previous == "current"
            case "like" | "glob":
                return after_value or previous == "not"
            case "cast":
                return following.text == "("
            case "with":
                return (self._previous is _END or self._previous.text == "(") and (
                    following.is_name or following.kind == "word")
            case "recursive":
                return previous == "with" and (following.is_name or following.kind == "word")
            case "window":
                return after_value and following.is_name and self._at(i + 2).word == "as"
            case _ if word in _JOINS:
                return next_word == "join" or word != "outer" and next_word == "outer"
        return False

    def _cast_type(self, i: int) -> int:
        """Replace the type of `CAST(x AS type)`, whose `AS` is at `i`: it holds no value."""
        depth, j = 0, i + 1
        while j < len(self._tokens) and not (depth == 0 and self._tokens[j].text == ")"):
            token = self._tokens[j]
            if not (token.kind == "number" or token.kind == "word" and token.word not in RESERVED
                    or token.text in ("(", ")", ",", "+", "-")):
                raise Unreadable("an unusual type in CAST")
            depth += {"(": 1, ")": -1}.get(token.text, 0)
            j += 1
        self._out.append("AS TEXT")
        self._previous, self._operand = _Token("word", "text"), True
        return j
