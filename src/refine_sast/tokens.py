from __future__ import annotations

import re


TOKEN_ESTIMATOR_VERSION = "c-lexical-v1"

_TOKEN = re.compile(
    r'''(?:u8|u|U|L)?"(?:\\.|[^"\\])*"'''
    r'''|(?:u|U|L)?'(?:\\.|[^'\\])*' '''
    r'''|[A-Za-z_$][A-Za-z0-9_$]*'''
    r'''|0[xX][0-9A-Fa-f]+'''
    r'''|0[bB][01]+'''
    r'''|(?:\d+\.\d*|\.\d+|\d+)(?:[eEpP][+-]?\d+)?[A-Za-z]*'''
    r'''|>>=|<<=|->\*|\.\*|\+\+|--|->|&&|\|\||<=|>=|==|!='''
    r'''|<<|>>|\+=|-=|\*=|/=|%=|&=|\|=|\^='''
    r'''|\S''',
    re.VERBOSE | re.DOTALL,
)


class TokenEstimator:
    """Deterministic C-like lexical token estimator.

    This is a source-budget estimator, not a claim about any provider tokenizer.
    Stage 4 will record provider-reported usage separately.
    """

    version = TOKEN_ESTIMATOR_VERSION

    def estimate_bytes(self, data: bytes) -> int:
        return self.estimate_text(data.decode("utf-8", errors="replace"))

    def estimate_text(self, text: str) -> int:
        return sum(1 for _ in _TOKEN.finditer(text))
