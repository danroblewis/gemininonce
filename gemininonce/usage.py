"""Token usage and what the same conversation would cost on the Gemini API.

Gemini web on a Workspace/personal plan is flat-rate (or free); this is only an estimate of the API
equivalent. USD per 1M tokens: (input, output, input >200k, output >200k).
Source: https://ai.google.dev/gemini-api/docs/pricing (page updated 2026-09-24). Override: --price.
"""
from __future__ import annotations

PRICES = {
    "3.8 flash": (0.75, 3.75, 0.75, 3.75),   # $1.50 / $7.50 from 2027-01-01
    "3.7 flash": (0.75, 3.75, 0.75, 3.75),   # $1.50 / $7.50 from 2027-01-01
    "3.6 flash": (0.75, 3.75, 0.75, 3.75),   # $1.50 / $7.50 from 2027-01-01
    "3.5 flash": (1.50, 9.00, 1.50, 9.00),
    "3.5 flash-lite": (0.30, 2.50, 0.30, 2.50),
    "3.1 flash-lite": (0.25, 1.50, 0.25, 1.50),
    "3.1 pro": (2.00, 12.00, 4.00, 18.00),
    "2.5 pro": (1.25, 10.00, 2.50, 15.00),
    "2.5 flash": (0.30, 2.50, 0.30, 2.50),
    "2.5 flash-lite": (0.10, 0.40, 0.10, 0.40),
}
CHARS_PER_TOKEN = 4  # Google's rule of thumb; real tokenization varies (code is often denser)


def _k(n: float) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else f"{n:.0f}"


class Usage:
    """Characters sent and received per message. price: "IN,OUT" USD per 1M tokens, overriding PRICES."""

    def __init__(self, price: str | None = None):
        self.turns: list[tuple[int, int]] = []
        self.price = price

    def record(self, sent: int, received: int) -> None:
        self.turns.append((sent, received))

    def _rates(self, model: str | None, price: str | None):
        price = price or self.price
        if price:
            rates = tuple(float(x) for x in price.split(","))
            return rates[0], rates[1], rates[0], rates[1]
        return PRICES.get((model or "").lower())

    def totals(self, model: str | None, price: str | None = None, upto: int | None = None
               ) -> tuple[float, float, float | None]:
        """(input tokens, output tokens, API-equivalent USD or None if no price is known) for the first
        `upto` messages (all by default). The API is stateless, so each message re-sends the whole
        conversation so far as input; that's counted here (without context caching discounts)."""
        rates = self._rates(model, price)
        history, tok_in, tok_out, cost = 0, 0.0, 0.0, 0.0
        for sent, received in self.turns[:upto]:
            i, o = (history + sent) / CHARS_PER_TOKEN, received / CHARS_PER_TOKEN
            tok_in, tok_out = tok_in + i, tok_out + o
            if rates:
                long = i > 200_000
                cost += (i * rates[2 if long else 0] + o * rates[3 if long else 1]) / 1e6
            history += sent + received
        return tok_in, tok_out, cost if rates else None

    def step(self, model: str | None, since: int, label: str) -> str:
        """Cost of the messages after the first `since`, and the running total, on one line, e.g.
        "this reply: ~6.7k in, ~293 out, $0.0035  |  total: ~21.0k in, ~1.8k out, $0.0109"."""
        all_in, all_out, all_cost = self.totals(model)
        before_in, before_out, before_cost = self.totals(model, upto=since)

        def part(name, tin, tout, cost):
            return f"{name}: ~{_k(tin)} in, ~{_k(tout)} out" + (f", ${cost:.4f}" if cost is not None else "")

        line = (part(label, all_in - before_in, all_out - before_out,
                     None if all_cost is None else all_cost - before_cost)
                + "  |  " + part("total", all_in, all_out, all_cost))
        if all_cost is None:
            line += f"  (no API price known for {model or 'this model'!r}; see --price)"
        return line

    def report(self, model: str | None, price: str | None = None) -> str:
        tok_in, tok_out, cost = self.totals(model, price)
        price = price or self.price
        lines = [f"Gemini usage: {len(self.turns)} message(s), ~{_k(tok_in)} tokens in (incl. re-sent history), "
                 f"~{_k(tok_out)} tokens out"]
        if cost is not None:
            rates = self._rates(model, price)
            lines.append(f"API-equivalent cost ({price and 'custom price' or model} at ${rates[0]:g}/${rates[1]:g} "
                         f"per 1M in/out): ~${cost:.4f}  (excludes hidden thinking tokens; "
                         f"Gemini web itself is free or flat-rate)")
        else:
            lines.append(f"No API price known for model {model or 'unknown'!r}; "
                         "pass --price IN,OUT (USD per 1M tokens)")
        return "\n".join(lines)
