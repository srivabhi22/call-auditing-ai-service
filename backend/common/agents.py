"""Extension and DID to agent name.

Cloud Connect tell us an extension number and, on the call log row, a DID. They
do not tell us who that is. The name the dashboard groups and filters by
therefore comes from here, not from the telephony data.

Two keys, because there are two ways a call can identify its agent and they do
not always both arrive:

    extension   the agent's handset. Present on the webhook and on the call
                log row, and the primary key of the directory.
    did         the number the agent's calls run through. On an inbound call
                it is what the customer dialled; on an outbound one it is the
                number shown to them. It is the fallback when the extension is
                missing or unmapped.

`resolve(extension, did)` tries them in that order. An extension is exact and
per-handset; a DID can in principle be shared by a hunt group, so it is the
second choice rather than the first.

DIDs are normalised to their last ten digits on both sides of the comparison.
The roster has them written inconsistently — `07971509216` and `7971509217` sit
next to each other in the source — and Cloud Connect may send either form, or a
91-prefixed one. Ten digits is the part that is always the same.

Backed by agents.json at the repo root, reloaded when the file changes so the
directory can be corrected without a restart. In production this is a table of
its own, or a pull from the PBX's own account list; the lookup signature stays
the same.

An unknown extension is not an error. It resolves to a placeholder so the call
is still ingested and audited, and shows up in the dashboard under an obviously
wrong agent — which is the behaviour that gets the directory fixed. Dropping the
call instead would hide the problem.
"""

import json
import os
import re
import threading
from dataclasses import dataclass

from .config import REPO_ROOT
from .trace import trace

DIRECTORY_PATH = os.environ.get(
    "AGENTS_PATH", os.path.join(REPO_ROOT, "agents.json")
)

# The significant part of an Indian landline DID. Everything the roster and the
# PBX disagree about — a leading 0, a 91 or +91 country code, spaces — is
# outside it.
DID_SIGNIFICANT_DIGITS = 10


def normalise_did(raw):
    digits = re.sub(r"\D", "", str(raw or ""))
    return digits[-DID_SIGNIFICANT_DIGITS:] if digits else ""


@dataclass(frozen=True)
class Agent:
    extension: str
    name: str
    did: str = ""
    accountId: str = ""
    known: bool = True
    # The reporting line, from the roster sheet rather than from Cloud Connect —
    # the PBX has no concept of a team. An agent with a `tl` is on the audited
    # floor: the dashboard groups them under that leader and the day roll-ups
    # file their calls under that team. An agent without one is a live
    # extension that still ingests and still resolves to a name, but is not
    # reported on.
    tl: str = ""
    state: str = ""

    @property
    def did_key(self):
        return normalise_did(self.did)

    @property
    def audited(self):
        return bool(self.tl)


def _placeholder(extension, did=""):
    """What an unmapped call is filed under.

    The extension is in the name on purpose: it is the one piece of information
    that makes the row actionable, and putting it where the dashboard displays
    it means the fix is obvious without opening the record.
    """
    label = extension or (f"DID {did}" if did else "?")
    return Agent(
        extension=extension,
        name=f"Unmapped extension {label}",
        did=did,
        known=False,
    )


class AgentDirectory:
    def __init__(self, path=DIRECTORY_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._by_extension = {}
        self._by_did = {}
        self._by_account = {}
        self._mtime = None
        self._warned = set()

    def _load_if_stale(self):
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            if self._mtime is not None or not self._by_extension:
                self._by_extension = {}
                self._by_did = {}
                self._by_account = {}
                self._mtime = None
            return

        if mtime == self._mtime:
            return

        try:
            with open(self.path) as fh:
                raw = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            trace("agents", f"could not read {self.path}: {exc}")
            return

        entries = raw.get("agents", raw) if isinstance(raw, dict) else raw
        by_extension, by_did, by_account = {}, {}, {}
        for entry in entries:
            extension = str(entry.get("extension", "")).strip()
            if not extension:
                continue
            agent = Agent(
                extension=extension,
                name=str(entry.get("name", "")).strip(),
                did=str(entry.get("did", "")).strip(),
                accountId=str(entry.get("accountId", "")).strip(),
                tl=str(entry.get("tl", "")).strip(),
                state=str(entry.get("state", "")).strip(),
            )
            by_extension[extension] = agent
            # The webhook sends accountId in its `extension_number`
            # field while the Call Log API sends the extension, so both
            # are indexed and one lookup serves either source.
            if agent.accountId:
                by_account[agent.accountId] = agent

            key = agent.did_key
            if not key:
                continue
            if key in by_did:
                # A DID mapped to two extensions cannot be a reliable fallback,
                # so the whole key is dropped rather than resolving to whichever
                # row happened to be last. The extension lookup still works for
                # both; only the fallback is lost, and only for this number.
                trace("agents", f"DID {agent.did} maps to both extension "
                                f"{by_did[key].extension} and {extension} — "
                                f"dropping it as a fallback key")
                by_did[key] = None
            else:
                by_did[key] = agent

        self._by_extension = by_extension
        self._by_account = by_account
        self._by_did = {k: v for k, v in by_did.items() if v is not None}
        self._mtime = mtime
        self._warned.clear()
        trace("agents", f"loaded {len(by_extension)} agents "
                        f"({len(self._by_did)} DIDs) from {self.path}")

    def lookup(self, extension):
        """By extension alone. Kept because most callers have only that."""
        return self.resolve(extension)

    def resolve(self, extension, did=""):
        """Extension, then accountId, then DID, then a placeholder.

        `extension` may be either identifier: the Call Log API calls an agent
        "706" and the webhook calls the same agent "102863706". Trying both
        costs one dict lookup and removes a whole class of unmapped call.
        """
        extension = str(extension or "").strip()
        did_key = normalise_did(did)

        with self._lock:
            self._load_if_stale()

            agent = self._by_extension.get(extension)
            if agent is not None:
                return agent

            agent = self._by_account.get(extension)
            if agent is not None:
                return agent

            agent = self._by_did.get(did_key) if did_key else None
            if agent is not None:
                trace("agents", f"extension {extension or '-'} unmapped, "
                                f"matched DID {did} -> {agent.name}")
                return agent

            warn_key = (extension, did_key)
            if warn_key not in self._warned:
                self._warned.add(warn_key)
                trace("agents", f"extension {extension or '-'!r} / DID "
                                f"{did or '-'} is not in the directory — the "
                                f"call will be filed under a placeholder")

        return _placeholder(extension, did)

    def by_did(self, did):
        with self._lock:
            self._load_if_stale()
            return self._by_did.get(normalise_did(did))

    def all_agents(self):
        with self._lock:
            self._load_if_stale()
            return list(self._by_extension.values())

    def audited_agents(self):
        """The floor that is reported on: everyone with a team leader.

        Sorted by extension because that is the order the roster is
        administered in, and the order somebody scanning for a gap reads it.
        """
        return sorted(
            (a for a in self.all_agents() if a.audited),
            key=lambda a: a.extension,
        )

    def teams(self):
        """Team leader -> their agents, for the audited floor only."""
        grouped = {}
        for agent in self.audited_agents():
            grouped.setdefault(agent.tl, []).append(agent)
        return grouped


directory = AgentDirectory()
