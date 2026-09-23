#!/usr/bin/env python3
"""Token accounting and price conversion for the pipeline.

Every stage that spends money reports it in the same shape, so a run can be costed
without anyone re-deriving rates from a spreadsheet. Two things live here: a price
table, and a ledger that adds usage up without losing the parts that matter.

The parts that matter are the nested ones. OpenAI returns cached and reasoning
counts inside `prompt_tokens_details` and `completion_tokens_details`, and a summer
that keeps only the top-level integers silently discards both — which is how a
scan that was billing most of its input at the cached rate looked, for months, like
it was paying full price for all of it. `UsageLedger.record` reads the nested
blocks deliberately.

Cached input is roughly a tenth of the uncached rate, so the split is not a detail:
on a long system prompt sent many times it is most of the bill.
"""

# --------------------------------------------------------------------------- #
# Prices. USD per 1M tokens, from the published rate card. Update here, nowhere
# else — every stage reads this table.
# --------------------------------------------------------------------------- #
# `cache_write` is the rate for the first request that puts a prompt into the cache,
# and it is optional: where a model does not charge separately for the write, leave
# it out and those tokens price at the ordinary input rate, which is what they cost.
# gpt-5.6-luna does charge for it, and charges *more* than plain input — so a prompt
# cached and never reused is more expensive than one never cached at all. That only
# pays off across many requests sharing the prompt, which is exactly the shape of the
# conduct scan: one write, then two dozen reads at a tenth the price.
PRICES_USD_PER_MTOK = {
    "gpt-5.6-luna": {"input": 0.20, "cached_input": 0.02, "output": 1.20,
                     "cache_write": 0.25},
    "gpt-5.4-mini": {"input": 0.75, "cached_input": 0.075, "output": 4.50},
    "gpt-5.4":      {"input": 2.50, "cached_input": 0.25,  "output": 15.00},
    "gpt-5-mini":   {"input": 0.25, "cached_input": 0.025, "output": 2.00},
}

# Soniox bills by audio hour, and bundles diarization and language ID into the rate.
SONIOX_USD_PER_HOUR = {
    "stt-async-v5": 0.10,   # file upload
    "stt-rt-v3":    0.12,   # streaming
}
SONIOX_DEFAULT_USD_PER_HOUR = 0.10

PRICE_TABLE_VERSION = "2026-09-07"


def price_for(model):
    """The rate card for `model`, or None when the model is not in the table.

    An unknown model returns None rather than guessing a price: a wrong number
    presented confidently is worse than a missing one, because nobody checks it.
    Matching falls back to the longest known prefix so that a dated snapshot
    (`gpt-5.4-mini-2026-04-01`) prices as the family it belongs to.
    """
    if not model:
        return None
    if model in PRICES_USD_PER_MTOK:
        return PRICES_USD_PER_MTOK[model]
    candidates = [name for name in PRICES_USD_PER_MTOK if model.startswith(name)]
    if not candidates:
        return None
    return PRICES_USD_PER_MTOK[max(candidates, key=len)]


def _detail(usage, block, key):
    """Read `usage[block][key]`, tolerating a missing or non-dict block."""
    details = usage.get(block)
    if not isinstance(details, dict):
        return 0
    value = details.get(key)
    return value if isinstance(value, int) else 0


class UsageLedger:
    """Running token totals for one model, across any number of requests.

    Ledgers are merged rather than shared between threads: `record` is called on a
    thread-local ledger and the results are merged by whoever collects the futures,
    which keeps the concurrent scan free of a lock it would otherwise need.
    """

    def __init__(self, model=None, stage=None):
        self.model = model
        self.stage = stage
        self.calls = 0
        self.prompt_tokens = 0          # includes the cached portion, as OpenAI reports it
        self.cached_prompt_tokens = 0
        self.completion_tokens = 0
        self.reasoning_tokens = 0       # a subset of completion_tokens, billed as output
        self.cache_write_tokens = 0     # prompt tokens written to the cache, when reported

    def record(self, usage):
        """Add one response's `usage` block. A missing or empty block is a no-op."""
        if not isinstance(usage, dict) or not usage:
            return self
        self.calls += 1
        for name in ("prompt_tokens", "completion_tokens"):
            value = usage.get(name)
            if isinstance(value, int):
                setattr(self, name, getattr(self, name) + value)
        self.cached_prompt_tokens += _detail(usage, "prompt_tokens_details",
                                             "cached_tokens")
        self.reasoning_tokens += _detail(usage, "completion_tokens_details",
                                         "reasoning_tokens")
        # Providers name the cache-write count differently, and most do not report one
        # at all. Whichever is present is read; where none is, the write is billed as
        # ordinary input, which is what happens on a model with no separate write rate.
        for block, key in (("prompt_tokens_details", "cache_creation_tokens"),
                           ("prompt_tokens_details", "cache_write_tokens"),
                           ("prompt_tokens_details", "cache_creation_input_tokens")):
            written = _detail(usage, block, key)
            if written:
                self.cache_write_tokens += written
                break
        else:
            written = usage.get("cache_creation_input_tokens")
            if isinstance(written, int):
                self.cache_write_tokens += written
        return self

    def merge(self, other):
        """Fold another ledger of the same stage into this one."""
        if other is None:
            return self
        self.calls += other.calls
        self.prompt_tokens += other.prompt_tokens
        self.cached_prompt_tokens += other.cached_prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.reasoning_tokens += other.reasoning_tokens
        self.cache_write_tokens += other.cache_write_tokens
        if self.model is None:
            self.model = other.model
        return self

    @property
    def uncached_prompt_tokens(self):
        # Never negative, even if a provider ever reports the two inconsistently.
        return max(0, self.prompt_tokens - self.cached_prompt_tokens)

    @property
    def fresh_prompt_tokens(self):
        """Uncached input that was not also written to the cache.

        A written token is an uncached token that additionally got stored, so it is
        already inside `uncached_prompt_tokens` and must not be counted twice.
        """
        return max(0, self.uncached_prompt_tokens - self.cache_write_tokens)

    def cost_usd(self):
        """Dollars for everything recorded, or None when the model has no rate."""
        rates = price_for(self.model)
        if rates is None:
            return None
        # A model with no separate write rate bills a write as ordinary input, which
        # is the truth for every model in the table that does not name one.
        write_rate = rates.get("cache_write", rates["input"])
        billable_writes = min(self.cache_write_tokens, self.uncached_prompt_tokens)
        return (
            self.fresh_prompt_tokens * rates["input"]
            + billable_writes * write_rate
            + self.cached_prompt_tokens * rates["cached_input"]
            + self.completion_tokens * rates["output"]
        ) / 1_000_000

    def as_dict(self):
        """The block written into the output JSON.

        Both the raw counts and the derived numbers are stored. The counts are the
        evidence; the dollars are what gets quoted, and re-deriving them later from
        a rate card that has since moved would give a different answer.
        """
        total = self.cost_usd()
        block = {
            "model": self.model,
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "cached_prompt_tokens": self.cached_prompt_tokens,
            "uncached_prompt_tokens": self.uncached_prompt_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cache_hit_rate": (round(self.cached_prompt_tokens / self.prompt_tokens, 4)
                               if self.prompt_tokens else 0.0),
            "cost_usd": round(total, 6) if total is not None else None,
            "price_table_version": PRICE_TABLE_VERSION,
        }
        if self.stage:
            block["stage"] = self.stage
        if total is None:
            block["cost_note"] = ("No published rate for %r in cost.PRICES_USD_PER_MTOK; "
                                  "tokens counted, dollars not computed." % self.model)
        return block

    def summary_line(self):
        """One line for stderr, so a run says what it cost while you watch it."""
        total = self.cost_usd()
        money = "$%.4f" % total if total is not None else "cost unknown"
        return ("%s: %d call(s), %s in (%d cached, %.0f%%), %s out%s → %s"
                % (self.stage or self.model or "usage",
                   self.calls,
                   f"{self.prompt_tokens:,}",
                   self.cached_prompt_tokens,
                   100 * (self.cached_prompt_tokens / self.prompt_tokens
                          if self.prompt_tokens else 0),
                   f"{self.completion_tokens:,}",
                   (" (%s reasoning)" % f"{self.reasoning_tokens:,}"
                    if self.reasoning_tokens else ""),
                   money))


def soniox_cost_usd(duration_ms, model=None):
    """What Soniox charges for `duration_ms` of audio, billed by the hour."""
    if not duration_ms:
        return 0.0
    rate = SONIOX_USD_PER_HOUR.get(model, SONIOX_DEFAULT_USD_PER_HOUR)
    return (duration_ms / 3_600_000.0) * rate


def soniox_block(duration_ms, model=None):
    """The cost block written into a transcript file."""
    rate = SONIOX_USD_PER_HOUR.get(model, SONIOX_DEFAULT_USD_PER_HOUR)
    return {
        "vendor": "soniox",
        "model": model,
        "audio_seconds": round((duration_ms or 0) / 1000.0, 2),
        "usd_per_hour": rate,
        "cost_usd": round(soniox_cost_usd(duration_ms, model), 6),
        "price_table_version": PRICE_TABLE_VERSION,
    }


def combine(*ledgers):
    """Total dollars across stages. Unknown-price stages are excluded, not zeroed."""
    total = 0.0
    known = False
    for ledger in ledgers:
        if ledger is None:
            continue
        value = ledger.cost_usd() if isinstance(ledger, UsageLedger) else ledger
        if value is not None:
            total += value
            known = True
    return total if known else None
