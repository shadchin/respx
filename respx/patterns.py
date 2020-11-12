import json as jsonlib
import operator
import re
from enum import Enum
from functools import reduce
from http.cookies import SimpleCookie
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Pattern as RegexPattern,
    Sequence,
    Set,
    Tuple,
    Type,
    Union,
)
from urllib.parse import urljoin

import httpx

from .types import (
    CookieTypes,
    HeaderTypes,
    QueryParamTypes,
    RequestTypes,
    URLPatternTypes,
)


class Lookup(Enum):
    EQUAL = "eq"
    REGEX = "regex"
    STARTS_WITH = "startswith"
    CONTAINS = "contains"
    IN = "in"


class Match:
    def __init__(self, matches: bool, **context: Any) -> None:
        self.matches = matches
        self.context = context

    def __bool__(self):
        return bool(self.matches)

    def __invert__(self):
        self.matches = not self.matches
        return self

    def __repr__(self):  # pragma: nocover
        return f"<Match {self.matches}>"


class Pattern:
    lookups: Tuple[Lookup, ...] = (Lookup.EQUAL,)
    key: str

    lookup: Lookup
    base: Optional["Pattern"]
    value: Any

    def __init__(self, value: Any, lookup: Optional[Lookup] = None) -> None:
        if lookup and lookup not in self.lookups:
            raise NotImplementedError(
                f"{self.key!r} pattern does not support {lookup.value!r} lookup"
            )
        self.lookup = lookup or self.lookups[0]
        self.base = None
        self.value = self.clean(value)

    def __iter__(self):
        yield self

    def __and__(self, other: "Pattern") -> "Pattern":
        return _And((self, other))

    def __or__(self, other: "Pattern") -> "Pattern":
        return _Or((self, other))

    def __invert__(self):
        return _Invert(self)

    def __repr__(self):  # pragma: nocover
        return f"<{self.__class__.__name__} {self.lookup.value} {repr(self.value)}>"

    def __hash__(self):
        return hash((self.__class__, self.lookup, self.value))

    def __eq__(self, other: object) -> bool:
        return hash(self) == hash(other)

    def clean(self, value: Any) -> Any:
        """
        Clean and return pattern value.
        """
        return value

    def parse(self, request: RequestTypes) -> Any:  # pragma: nocover
        """
        Parse and return request value to match with pattern value.
        """
        raise NotImplementedError()

    def strip_base(self, value: Any) -> Any:  # pragma: nocover
        return value

    def match(self, request: RequestTypes) -> Match:
        value = self.parse(request)

        # Match and strip base
        if self.base:
            base_match = self.base._match(value)
            if not base_match:
                return base_match
            value = self.strip_base(value)

        return self._match(value)

    def _match(self, value: Any) -> Match:
        lookup_method = getattr(self, f"_{self.lookup.value}")
        return lookup_method(value)

    def _eq(self, value: Any) -> Match:
        return Match(value == self.value)

    def _regex(self, value: str) -> Match:
        match = self.value.search(value)
        if match is None:
            return Match(False)
        return Match(True, **match.groupdict())

    def _startswith(self, value: str) -> Match:
        return Match(value.startswith(self.value))

    def _contains(self, value: Any) -> Match:  # pragma: nocover
        raise NotImplementedError()

    def _in(self, value: Any) -> Match:
        return Match(value in self.value)


class PathPattern(Pattern):
    path: Optional[str]

    def __init__(
        self, value: Any, lookup: Optional[Lookup] = None, *, path: Optional[str] = None
    ) -> None:
        self.path = path
        super().__init__(value, lookup)


class _And(Pattern):
    value: Tuple[Pattern, Pattern]

    def __repr__(self):  # pragma: nocover
        a, b = self.value
        return f"{repr(a)} AND {repr(b)}"

    def __iter__(self):
        a, b = self.value
        yield from a
        yield from b

    def match(self, request: RequestTypes) -> Match:
        a, b = self.value
        match1 = a.match(request)
        match2 = b.match(request)
        if match1 and match2:
            return Match(True, **{**match1.context, **match2.context})
        return Match(False)


class _Or(Pattern):
    value: Tuple[Pattern, Pattern]

    def __repr__(self):  # pragma: nocover
        a, b = self.value
        return f"{repr(a)} OR {repr(b)}"

    def __iter__(self):
        a, b = self.value
        yield from a
        yield from b

    def match(self, request: RequestTypes) -> Match:
        a, b = self.value
        match = a.match(request)
        if not match:
            match = b.match(request)
        return match


class _Invert(Pattern):
    value: Pattern

    def __repr__(self):  # pragma: nocover
        return f"NOT {repr(self.value)}"

    def __iter__(self):
        yield from self.value

    def match(self, request: RequestTypes) -> Match:
        return ~self.value.match(request)


class Method(Pattern):
    lookups = (Lookup.EQUAL, Lookup.IN)
    key = "method"
    value: Union[str, Sequence[str]]

    def clean(self, value: Union[str, Sequence[str]]) -> Union[str, Sequence[str]]:
        if isinstance(value, str):
            return value.upper()
        return value

    def parse(self, request: RequestTypes) -> str:
        if isinstance(request, httpx.Request):
            method = request.method
        else:
            _method, *_ = request
            method = _method.decode("ascii")
        return method


class MultiItemsMixin:
    lookup: Lookup
    value: Any

    def __hash__(self):
        return hash((self.__class__, self.lookup, str(self.value)))

    def _contains(self, value: Any) -> Match:
        value_list = self.value.multi_items()
        request_list = value.multi_items()

        if len(value_list) > len(request_list):
            return Match(False)

        for item in value_list:
            if item not in request_list:
                return Match(False)

        return Match(True)


class Headers(MultiItemsMixin, Pattern):
    lookups = (Lookup.CONTAINS, Lookup.EQUAL)
    key = "headers"
    value: httpx.Headers

    def clean(self, value: HeaderTypes) -> httpx.Headers:
        return httpx.Headers(value)

    def parse(self, request: RequestTypes) -> httpx.Headers:
        if isinstance(request, httpx.Request):
            headers = request.headers
        else:
            _, _, _headers, *_ = request
            headers = httpx.Headers(_headers)

        return headers


class Cookies(Pattern):
    lookups = (Lookup.CONTAINS, Lookup.EQUAL)
    key = "cookies"
    value: Set[Tuple[str, str]]

    def __hash__(self):
        return hash((self.__class__, self.lookup, tuple(sorted(self.value))))

    def clean(self, value: CookieTypes) -> Set[Tuple[str, str]]:
        if isinstance(value, dict):
            return set(value.items())

        return set(value)

    def parse(self, request: RequestTypes) -> Set[Tuple[str, str]]:
        if isinstance(request, httpx.Request):
            headers = request.headers
        else:
            _, _, _headers, *_ = request
            headers = httpx.Headers(_headers)

        cookie_header = headers.get("cookie")
        if not cookie_header:
            return set()

        cookies: SimpleCookie = SimpleCookie()
        cookies.load(rawdata=cookie_header)

        return {(cookie.key, cookie.value) for cookie in cookies.values()}

    def _contains(self, value: Set[Tuple[str, str]]) -> Match:
        return Match(bool(self.value & value))


class Scheme(Pattern):
    lookups = (Lookup.EQUAL, Lookup.IN)
    key = "scheme"
    value: Union[str, Sequence[str]]

    def clean(self, value: Union[str, Sequence[str]]) -> Union[str, Sequence[str]]:
        if isinstance(value, str):
            return value.lower()
        return value

    def parse(self, request: RequestTypes) -> str:
        if isinstance(request, httpx.Request):
            scheme = request.url.scheme
        else:
            _, (_scheme, *_), *_ = request
            scheme = _scheme.decode("ascii")
        return scheme


class Host(Pattern):
    lookups = (Lookup.EQUAL, Lookup.IN)
    key = "host"
    value: Union[str, Sequence[str]]

    def parse(self, request: RequestTypes) -> str:
        if isinstance(request, httpx.Request):
            host = request.url.host
        else:
            _, (_, _host, *_), *_ = request
            host = _host.decode("ascii")
        return host


class Port(Pattern):
    lookups = (Lookup.EQUAL, Lookup.IN)
    key = "port"
    value: Optional[int]

    def parse(self, request: RequestTypes) -> Optional[int]:
        scheme: Optional[str] = None
        if isinstance(request, httpx.Request):
            scheme = request.url.scheme
            port = request.url.port
        else:
            _, (_scheme, _, port, _), *_ = request
            if _scheme:
                scheme = _scheme.decode("ascii")
        scheme_port = get_scheme_port(scheme)
        return port or scheme_port


class Path(Pattern):
    lookups = (Lookup.EQUAL, Lookup.REGEX, Lookup.STARTS_WITH, Lookup.IN)
    key = "path"
    value: Union[str, Sequence[str], RegexPattern[str]]

    def clean(
        self, value: Union[str, RegexPattern[str]]
    ) -> Union[str, RegexPattern[str]]:
        if self.lookup in (Lookup.EQUAL, Lookup.STARTS_WITH) and isinstance(value, str):
            path = urljoin("/", value)  # Ensure leading slash
            value = httpx.URL(path).path
        elif self.lookup is Lookup.REGEX and isinstance(value, str):
            value = re.compile(value)
        return value

    def parse(self, request: RequestTypes) -> str:
        if isinstance(request, httpx.Request):
            path = request.url.path
        else:
            _, (_, _, _, _path), *_ = request
            _path, _, _ = _path.partition(b"?")
            path = _path.decode("ascii")
        return path

    def strip_base(self, value: str) -> str:
        value = urljoin("/", value[len(self.base.value) :])
        return value


class Params(MultiItemsMixin, Pattern):
    lookups = (Lookup.CONTAINS, Lookup.EQUAL)
    key = "params"
    value: httpx.QueryParams

    def clean(self, value: QueryParamTypes) -> httpx.QueryParams:
        return httpx.QueryParams(value)

    def parse(self, request: RequestTypes) -> httpx.QueryParams:
        if isinstance(request, httpx.Request):
            query = request.url.query
        else:
            _, url, *_ = request
            query = httpx.URL(url).query

        return httpx.QueryParams(query)


class URL(Pattern):
    lookups = (
        Lookup.EQUAL,
        Lookup.REGEX,
        Lookup.STARTS_WITH,
    )
    key = "url"
    value: Union[str, RegexPattern[str]]

    def clean(self, value: URLPatternTypes) -> Union[str, RegexPattern[str]]:
        url: Union[str, RegexPattern[str]]
        if self.lookup is Lookup.EQUAL and isinstance(value, (str, tuple, httpx.URL)):
            _url = httpx.URL(value)
            url = str(_url)
            if not _url._uri_reference.path:  # Ensure path
                url += "/"
        elif self.lookup is Lookup.REGEX and isinstance(value, str):
            url = re.compile(value)
        elif isinstance(value, (str, RegexPattern)):
            url = value
        else:
            raise ValueError(f"Invalid url: {value!r}")
        return url

    def parse(self, request: RequestTypes) -> str:
        if isinstance(request, httpx.Request):
            url = str(request.url)
            if not request.url._uri_reference.path:  # Ensure path
                url += "/"
        else:
            _, _url, *_ = request
            url = str(httpx.URL(_url))
        return url


class ContentMixin:
    def parse(self, request: RequestTypes) -> Any:
        if not isinstance(request, httpx.Request):
            method, url, headers, stream = request
            request = httpx.Request(
                method, httpx.URL(url), headers=headers, stream=stream
            )
        content = request.read()
        return content


class Content(ContentMixin, Pattern):
    lookups = (Lookup.EQUAL,)
    key = "content"
    value: bytes

    def clean(self, value: Union[bytes, str]) -> bytes:
        if isinstance(value, str):
            return value.encode()
        return value


class JSON(ContentMixin, PathPattern):
    lookups = (Lookup.EQUAL,)
    key = "json"
    value: str

    def clean(self, value: Union[str, List, Dict]) -> str:
        return self.hash(value)

    def parse(self, request: RequestTypes) -> str:
        content = super().parse(request)
        json = jsonlib.loads(content.decode("utf-8"))

        if self.path:
            value = json
            for bit in self.path.split("__"):
                key = int(bit) if bit.isdigit() else bit
                try:
                    value = value[key]
                except KeyError as e:
                    raise KeyError(f"{self.path!r} not in {json!r}") from e
                except IndexError as e:
                    raise IndexError(f"{self.path!r} not in {json!r}") from e
        else:
            value = json

        return self.hash(value)

    def hash(self, value: Union[str, List, Dict]) -> str:
        return jsonlib.dumps(value, sort_keys=True)


class Data(ContentMixin, Pattern):
    lookups = (Lookup.EQUAL,)
    key = "data"
    value: bytes

    def clean(self, value: Dict) -> bytes:
        request = httpx.Request("POST", "/", data=value)
        data = request.read()
        return data


# TODO: Refactor to registration when subclassing Pattern
PATTERNS: Dict[str, Type[Union[Pattern, PathPattern]]] = {
    P.key: P  # type: ignore
    for P in (
        Method,
        Headers,
        Cookies,
        Scheme,
        Host,
        Port,
        Path,
        Params,
        URL,
        Content,
        Data,
        JSON,
    )
}


def M(*patterns: Pattern, **lookups: Any) -> Pattern:
    extras = None

    for pattern__lookup, value in lookups.items():
        if not value:
            continue

        # Handle url pattern
        if pattern__lookup == "url":
            extras = parse_url_patterns(value)
            continue

        # Parse pattern key and lookup
        pattern_key, __, rest = pattern__lookup.partition("__")
        path, __, lookup_name = rest.rpartition("__")
        if pattern_key not in PATTERNS:
            raise KeyError(f"{pattern_key!r} is not a valid Pattern")

        # Get pattern class
        P = PATTERNS[pattern_key]
        pattern: Union[Pattern, PathPattern]

        if issubclass(P, PathPattern):
            # Make path supported pattern, i.e. JSON
            try:
                lookup = Lookup(lookup_name) if lookup_name else None
            except ValueError:
                lookup = None
                path = rest
            pattern = P(value, lookup=lookup, path=path)
        else:
            # Make regular pattern
            lookup = Lookup(lookup_name) if lookup_name else None
            pattern = P(value, lookup=lookup)

        patterns += (pattern,)

    # Combine and merge patterns
    combined_pattern = combine(patterns)
    if extras:
        combined_pattern = merge_patterns(combined_pattern, **extras)

    return combined_pattern


def get_scheme_port(scheme: Optional[str]) -> Optional[int]:
    return {"http": 80, "https": 443}.get(scheme)


def combine(
    patterns: Sequence[Pattern], op: Callable = operator.and_
) -> Optional[Pattern]:
    patterns = tuple(filter(None, patterns))
    if not patterns:
        return None
    return reduce(op, patterns)


def parse_url_patterns(
    url: Optional[URLPatternTypes], exact: bool = True
) -> Dict[str, Pattern]:
    bases: Dict[str, Pattern] = {}
    if not url:
        return bases

    if isinstance(url, RegexPattern):
        return {"url": URL(url, lookup=Lookup.REGEX)}

    url = httpx.URL(url)
    scheme_port = get_scheme_port(url.scheme)

    if url.scheme:
        bases[Scheme.key] = Scheme(url.scheme)
    if url.host:
        bases[Host.key] = Host(url.host)
    if url.port and url.port != scheme_port:
        bases[Port.key] = Port(url.port)
    if url._uri_reference.path:  # URL.path always returns "/"
        lookup = Lookup.EQUAL if exact else Lookup.STARTS_WITH
        bases[Path.key] = Path(url.path, lookup=lookup)
    if url.query:
        lookup = Lookup.EQUAL if exact else Lookup.CONTAINS
        bases[Params.key] = Params(url.query, lookup=lookup)

    return bases


def merge_patterns(pattern: Pattern, **bases: Pattern) -> Pattern:
    if not bases:
        return pattern

    if pattern:
        # Flatten pattern
        patterns = list(iter(pattern))

        if "host" in (_pattern.key for _pattern in patterns):
            # Pattern is "absolute", skip merging
            bases = None
        else:
            # Traverse pattern and set releated base
            for _pattern in patterns:
                base = bases.pop(_pattern.key, None)
                # Skip "exact" base + don't overwrite existing base
                if _pattern.base or base and base.lookup is Lookup.EQUAL:
                    continue
                _pattern.base = base

    if bases:
        # Combine left over base patterns with pattern
        base_pattern = combine(list(bases.values()))
        if pattern:
            pattern = base_pattern & pattern
        else:
            pattern = base_pattern

    return pattern
