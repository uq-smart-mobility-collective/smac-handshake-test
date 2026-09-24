#!/usr/bin/env python3
"""
REIT7820 Smart Mobility Agent Collective: Handshake Test
========================================================

Connects to your MCP agent over Streamable HTTP, the same way the Showcase
orchestrator will, and runs the six conformance checks from the Agent
Interface Specification v1.0 (section 9):

  1. Initialise        MCP initialize on protocol 2025-11-25, tools capability,
                       serverInfo name smac-{domain}-{slot}, semantic version
  2. health tool       present, no arguments, conformant payload
  3. Tool schemas      1 to 5 domain tools, snake_case names, descriptions,
                       complete inputSchema with every parameter described
  4. Data conventions  sources array, WGS84 lat/lon, ISO 8601 with timezone,
                       metres, seconds, AUD decimal strings
  5. Malformed input   bad calls return an MCP error and never crash the agent
  6. Response time     every call returns (or errors) within 45 seconds

Usage
-----
  python handshake.py http://localhost:8000/mcp
  python handshake.py http://localhost:8000/mcp --examples examples.json
  python handshake.py http://localhost:8000/mcp --json report.json

`--examples` is optional: a JSON file mapping each of your tool names to one
valid set of arguments. Without it, the test generates arguments from your
inputSchema (defaults, examples, enums, and sensible SEQ values), which works
for most tools. If check 4 says it couldn't make a successful call, add an
examples file, or add "default"/"examples" to your schema parameters.

Exit code is 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

warnings.filterwarnings("ignore", category=DeprecationWarning)

try:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    from mcp.shared.exceptions import McpError
except ImportError:  # pragma: no cover
    sys.exit("The MCP Python SDK is missing. Run:  pip install -r requirements.txt")

HARNESS_VERSION = "1.0.0"

try:
    BaseExceptionGroup
except NameError:  # Python 3.10
    from exceptiongroup import BaseExceptionGroup  # installed with anyio

# ----------------------------------------------------------------------------
# Spec constants (Agent Interface Specification v1.0, locked)
# ----------------------------------------------------------------------------
PROTOCOL_VERSION = "2025-11-25"
TIMEOUT_S = 45
MIN_DOMAIN_TOOLS, MAX_DOMAIN_TOOLS = 1, 5
DOMAINS = ("charging", "pt", "policy", "equity", "network", "custom")
NAME_RE = re.compile(r"^smac-(%s)-[a-z0-9]+(-[a-z0-9]+)*$" % "|".join(DOMAINS))
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+([-+][0-9A-Za-z.-]+)?$")
TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)+$")
HEALTH_FIELDS = ("status", "agent", "version", "data_sources", "llm_backend")

# Values used when generating a call from a schema (UQ St Lucia, SEQ)
SAMPLE_LAT, SAMPLE_LON = -27.4975, 153.0137

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"


# ----------------------------------------------------------------------------
# Result bookkeeping
# ----------------------------------------------------------------------------
@dataclass
class Check:
    number: int
    name: str
    status: str = PASS
    findings: list[tuple[str, str]] = field(default_factory=list)  # (level, message)

    def fail(self, msg: str) -> None:
        self.findings.append((FAIL, msg))
        self.status = FAIL

    def warn(self, msg: str) -> None:
        self.findings.append((WARN, msg))

    def note(self, msg: str) -> None:
        self.findings.append(("INFO", msg))

    def skip(self, msg: str) -> None:
        self.status = SKIP
        self.findings.append((SKIP, msg))


@dataclass
class CallRecord:
    tool: str
    purpose: str
    seconds: float
    outcome: str  # ok | tool_error | protocol_error | timeout | transport_error


class Report:
    def __init__(self, url: str):
        self.url = url
        self.started = datetime.now(timezone.utc)
        self.checks = [
            Check(1, "Initialise"),
            Check(2, "health tool"),
            Check(3, "Tool schemas"),
            Check(4, "Data conventions"),
            Check(5, "Malformed input"),
            Check(6, "Response time"),
        ]
        self.calls: list[CallRecord] = []
        self.server_name: str | None = None

    def __getitem__(self, n: int) -> Check:
        return self.checks[n - 1]

    @property
    def passed(self) -> bool:
        return all(c.status == PASS for c in self.checks)

    def to_dict(self) -> dict:
        return {
            "harness_version": HARNESS_VERSION,
            "url": self.url,
            "agent": self.server_name,
            "tested_at": self.started.isoformat(),
            "passed": self.passed,
            "checks": [
                {
                    "number": c.number,
                    "name": c.name,
                    "status": c.status,
                    "findings": [{"level": l, "message": m} for l, m in c.findings],
                }
                for c in self.checks
            ],
            "calls": [vars(c) for c in self.calls],
        }


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def result_payload(result) -> Any:
    """Best-effort extraction of a tool result as JSON-like data."""
    sc = getattr(result, "structuredContent", None)
    if sc:
        # FastMCP wraps non-object returns as {"result": ...}
        if isinstance(sc, dict) and set(sc.keys()) == {"result"}:
            return sc["result"]
        return sc
    texts = [c.text for c in (result.content or []) if getattr(c, "type", None) == "text"]
    if len(texts) == 1:
        try:
            return json.loads(texts[0])
        except (json.JSONDecodeError, TypeError):
            return texts[0]
    if texts:
        parsed = []
        for t in texts:
            try:
                parsed.append(json.loads(t))
            except (json.JSONDecodeError, TypeError):
                parsed.append(t)
        return parsed
    return None


def error_text(result) -> str:
    texts = [c.text for c in (result.content or []) if getattr(c, "type", None) == "text"]
    return " ".join(texts).strip()


def sample_value(name: str, schema: dict) -> Any:
    """Generate a plausible valid value for one parameter."""
    if not isinstance(schema, dict):
        return "test"
    if "default" in schema:
        return schema["default"]
    if schema.get("examples"):
        return schema["examples"][0]
    if "example" in schema:
        return schema["example"]
    if schema.get("enum"):
        return schema["enum"][0]
    if "const" in schema:
        return schema["const"]
    for combo in ("anyOf", "oneOf"):
        if combo in schema:
            options = [s for s in schema[combo] if s.get("type") != "null"] or schema[combo]
            return sample_value(name, options[0])

    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), t[0])
    lname = name.lower()

    if t == "object":
        props = schema.get("properties", {})
        keys = set(props) | set(schema.get("required", []))
        if {"lat", "lon"} <= set(props):
            return {"lat": SAMPLE_LAT, "lon": SAMPLE_LON, **{
                k: sample_value(k, props[k]) for k in schema.get("required", []) if k not in ("lat", "lon")}}
        return {k: sample_value(k, props.get(k, {})) for k in keys}
    if t == "array":
        return [sample_value(name, schema.get("items", {}))]
    if t in ("number", "integer"):
        if lname in ("lat", "latitude") or lname.endswith("_lat"):
            return SAMPLE_LAT
        if lname in ("lon", "lng", "longitude") or lname.endswith("_lon"):
            return SAMPLE_LON
        lo = schema.get("minimum", schema.get("exclusiveMinimum"))
        hi = schema.get("maximum")
        guess = 1000 if any(k in lname for k in ("radius", "distance", "metre", "meter")) else \
            3600 if any(k in lname for k in ("duration", "seconds", "time")) else 5
        if lo is not None and guess < lo:
            guess = lo + (1 if "exclusiveMinimum" in schema else 0)
        if hi is not None and guess > hi:
            guess = hi
        return int(guess) if t == "integer" else float(guess)
    if t == "boolean":
        return False
    # string
    fmt = schema.get("format", "")
    if fmt == "date-time" or any(k in lname for k in ("timestamp", "datetime", "depart", "arrive")):
        return datetime.now(timezone(timedelta(hours=10))).replace(microsecond=0).isoformat()
    if fmt == "date" or lname.endswith("date"):
        return datetime.now(timezone(timedelta(hours=10))).date().isoformat()
    if any(k in lname for k in ("suburb", "location", "place", "origin", "destination", "address", "query")):
        return "St Lucia QLD"
    if "sa2" in lname:
        return "305031128"
    return "test"


def sample_args(schema: dict) -> dict:
    schema = schema or {}
    props = schema.get("properties", {}) or {}
    required = schema.get("required", []) or []
    return {k: sample_value(k, props.get(k, {})) for k in required}


def wrong_type_value(schema: dict) -> Any:
    t = (schema or {}).get("type")
    if isinstance(t, list):
        t = t[0]
    return {"string": 12345, "number": "not-a-number", "integer": "not-an-integer",
            "boolean": "not-a-bool", "array": "not-an-array", "object": "not-an-object"}.get(t, {"unexpected": True})


def walk(obj: Any, path: str = ""):
    """Yield (path, key, value) for every key in nested dicts/lists."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else k
            yield p, k, v
            yield from walk(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:50]):
            yield from walk(v, f"{path}[{i}]")


ISO_NO_TZ = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?$")
ISO_WITH_TZ = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})$")


def convention_issues(payload: Any) -> tuple[list[str], list[str]]:
    """Return (failures, warnings) for data-convention spot checks."""
    fails, warns = [], []
    for path, key, val in walk(payload):
        k = key.lower()
        if k in ("latitude", "longitude", "lng", "long"):
            fails.append(f"`{path}`: coordinates must use {{\"lat\": ..., \"lon\": ...}}, not `{key}`")
        if k == "lat" and isinstance(val, (int, float)) and not -90 <= val <= 90:
            fails.append(f"`{path}` = {val} is not a valid WGS84 latitude (lat/lon swapped?)")
        if k == "lat" and isinstance(val, (int, float)) and val > 0:
            warns.append(f"`{path}` = {val} is north of the equator; SEQ latitudes are negative")
        if k == "lon" and isinstance(val, (int, float)) and not -180 <= val <= 180:
            fails.append(f"`{path}` = {val} is not a valid WGS84 longitude")
        if isinstance(val, str) and ISO_NO_TZ.match(val):
            fails.append(f"`{path}` = \"{val}\" is a timestamp without a timezone (use e.g. +10:00)")
        if re.search(r"(_km|_kms|_kilometres|_kilometers|_miles|_mi)$", k):
            fails.append(f"`{path}`: distances must be in metres (integer), not `{key}`")
        if re.search(r"(_min|_mins|_minutes|_hr|_hrs|_hours)$", k):
            fails.append(f"`{path}`: durations must be in seconds (integer), not `{key}`")
        if re.search(r"(_m|_metres|_meters|distance)$", k) and isinstance(val, float) and not val.is_integer():
            warns.append(f"`{path}` = {val}: distances should be integer metres")
        if re.search(r"(_s|_seconds|duration)$", k) and isinstance(val, float) and not val.is_integer():
            warns.append(f"`{path}` = {val}: durations should be integer seconds")
        if re.search(r"(price|cost|fare|aud|amount|tariff)", k) and isinstance(val, (int, float)) \
                and not isinstance(val, bool):
            warns.append(f"`{path}` = {val}: money should be an AUD decimal string, e.g. \"0.50\"")
    return fails, warns


def find_sources(payload: Any) -> Any:
    if isinstance(payload, dict):
        if "sources" in payload:
            return payload["sources"]
        for v in payload.values():
            s = find_sources(v)
            if s is not None:
                return s
    return None


# ----------------------------------------------------------------------------
# The test
# ----------------------------------------------------------------------------
class Handshake:
    def __init__(self, url: str, examples: dict | None, verbose: bool):
        self.url = url
        self.examples = examples or {}
        self.verbose = verbose
        self.r = Report(url)
        self.session: ClientSession | None = None
        self.transport_dead = False
        self.stage = 1

    async def call(self, tool: str, args: dict | None, purpose: str):
        """Call a tool with the spec timeout. Returns (outcome, result_or_exc)."""
        t0 = time.perf_counter()
        outcome, res = "ok", None
        try:
            res = await asyncio.wait_for(
                self.session.call_tool(tool, args or {}, read_timeout_seconds=timedelta(seconds=TIMEOUT_S + 2)),
                timeout=TIMEOUT_S + 2,
            )
            outcome = "tool_error" if res.isError else "ok"
        except asyncio.TimeoutError:
            outcome = "timeout"
        except McpError as e:
            # 408 is the SDK's own read timeout: the agent never answered (hung or crashed)
            outcome, res = ("timeout" if getattr(e.error, "code", None) == 408 else "protocol_error"), e
        except Exception as e:  # connection dropped, server crashed, etc.
            outcome, res = "transport_error", e
            self.transport_dead = True
        dt = time.perf_counter() - t0
        if dt > TIMEOUT_S and outcome != "timeout":
            outcome = outcome + "_slow"
        self.r.calls.append(CallRecord(tool, purpose, round(dt, 3), outcome))
        if self.verbose:
            print(f"    · {tool} [{purpose}] -> {outcome} in {dt:.2f}s")
        return outcome, res

    # -- check 1 ------------------------------------------------------------
    def check_initialise(self, init) -> None:
        c = self.r[1]
        c.note(f"Connected over Streamable HTTP to {self.url}")
        if init.protocolVersion != PROTOCOL_VERSION:
            c.fail(f"Negotiated protocol {init.protocolVersion}; the spec pins {PROTOCOL_VERSION}. "
                   "Update your MCP SDK to a version that supports it.")
        else:
            c.note(f"Protocol {init.protocolVersion}")
        if not (init.capabilities and init.capabilities.tools is not None):
            c.fail("Server does not advertise the `tools` capability")
        name = init.serverInfo.name if init.serverInfo else ""
        ver = init.serverInfo.version if init.serverInfo else ""
        self.r.server_name = name
        if not NAME_RE.match(name or ""):
            c.fail(f"serverInfo.name is \"{name}\"; it must be smac-{{domain}}-{{slot}}, lowercase and hyphenated, "
                   f"with domain one of: {', '.join(DOMAINS)}")
        else:
            c.note(f"Agent name {name}")
        if not SEMVER_RE.match(ver or ""):
            c.fail(f"serverInfo.version is \"{ver}\"; it must be a semantic version such as 1.0.0")

    # -- check 2 ------------------------------------------------------------
    async def check_health(self, tools_by_name: dict, init) -> None:
        c = self.r[2]
        tool = tools_by_name.get("health")
        if tool is None:
            c.fail("No tool named `health`")
            return
        props = (tool.inputSchema or {}).get("properties") or {}
        req = (tool.inputSchema or {}).get("required") or []
        if props or req:
            c.fail(f"`health` must take no arguments; it declares {sorted(set(props) | set(req))}")
        outcome, res = await self.call("health", {}, "health")
        if not outcome.startswith("ok"):
            detail = error_text(res) if hasattr(res, "content") else str(res)
            c.fail(f"`health` call did not succeed ({outcome}) {detail}".strip())
            return
        p = result_payload(res)
        if not isinstance(p, dict):
            c.fail(f"`health` must return a JSON object; got {type(p).__name__}: {str(p)[:120]}")
            return
        missing = [f for f in HEALTH_FIELDS if f not in p]
        if missing:
            c.fail(f"`health` payload is missing: {', '.join(missing)}")
        if "status" in p and p["status"] != "ok":
            c.fail(f"`status` is \"{p['status']}\"; a healthy agent returns \"ok\"")
        if "agent" in p and p["agent"] != init.serverInfo.name:
            c.fail(f"`agent` is \"{p['agent']}\" but serverInfo.name is \"{init.serverInfo.name}\"; they must match")
        if "version" in p and p["version"] != init.serverInfo.version:
            c.warn(f"`version` \"{p['version']}\" differs from serverInfo.version \"{init.serverInfo.version}\"")
        ds = p.get("data_sources")
        if "data_sources" in p and (not isinstance(ds, list) or not ds or not all(isinstance(x, str) and x for x in ds)):
            c.fail("`data_sources` must be a non-empty list of strings naming your datasets/APIs")
        lb = p.get("llm_backend")
        if "llm_backend" in p and (not isinstance(lb, str) or not lb.strip()):
            c.fail("`llm_backend` must name your primary model, e.g. \"claude-haiku-4-5\"")
        if c.status == PASS:
            c.note(f"health OK: {json.dumps(p)[:160]}")

    # -- check 3 ------------------------------------------------------------
    def check_schemas(self, domain_tools: list) -> None:
        c = self.r[3]
        n = len(domain_tools)
        if not MIN_DOMAIN_TOOLS <= n <= MAX_DOMAIN_TOOLS:
            c.fail(f"{n} domain tools found (excluding `health`); the spec allows {MIN_DOMAIN_TOOLS} to {MAX_DOMAIN_TOOLS}")
        else:
            c.note(f"{n} domain tool(s): {', '.join(t.name for t in domain_tools)}")
        for t in domain_tools:
            if not TOOL_NAME_RE.match(t.name):
                c.fail(f"`{t.name}`: tool names must be verb-first snake_case, e.g. find_nearest_chargers")
            desc = (t.description or "").strip()
            if not desc:
                c.fail(f"`{t.name}` has no description. The orchestrator picks tools from descriptions alone.")
            elif len(desc) < 40:
                c.warn(f"`{t.name}` description is only {len(desc)} characters; it may not give the orchestrator "
                       "a reason to choose it")
            s = t.inputSchema or {}
            if s.get("type") != "object":
                c.fail(f"`{t.name}` inputSchema must have \"type\": \"object\"")
            props = s.get("properties") or {}
            if not props:
                c.warn(f"`{t.name}` takes no parameters; check that is intended")
            for pname, pschema in props.items():
                if not isinstance(pschema, dict) or not str(pschema.get("description", "")).strip():
                    c.fail(f"`{t.name}.{pname}` has no description")
                if isinstance(pschema, dict) and not any(k in pschema for k in ("type", "anyOf", "oneOf", "enum", "$ref", "const")):
                    c.fail(f"`{t.name}.{pname}` has no type")
            for r in s.get("required") or []:
                if r not in props:
                    c.fail(f"`{t.name}` lists `{r}` as required but does not define it")

    # -- check 4 ------------------------------------------------------------
    async def check_conventions(self, domain_tools: list) -> None:
        c = self.r[4]
        successes = 0
        for t in domain_tools:
            if self.transport_dead:
                break
            if t.name in self.examples:
                args, how = self.examples[t.name], "examples file"
            else:
                args, how = sample_args(t.inputSchema), "generated from schema"
            outcome, res = await self.call(t.name, args, "valid call")
            if not outcome.startswith("ok"):
                detail = error_text(res) if hasattr(res, "content") else str(res)
                c.warn(f"`{t.name}` did not return a result for a valid-looking call ({how}: "
                       f"{json.dumps(args)[:120]}): {outcome} {detail[:160]}".strip())
                continue
            successes += 1
            p = result_payload(res)
            src = find_sources(p)
            if src is None:
                c.fail(f"`{t.name}` response has no `sources` array (list the datasets/APIs behind this answer)")
            elif not isinstance(src, list) or not src:
                c.fail(f"`{t.name}` `sources` must be a non-empty list")
            fails, warns = convention_issues(p)
            for f in dict.fromkeys(fails):
                c.fail(f"`{t.name}` {f}")
            for w in list(dict.fromkeys(warns))[:5]:
                c.warn(f"`{t.name}` {w}")
            if not fails and src:
                c.note(f"`{t.name}` OK ({how})")
        if successes == 0 and domain_tools:
            c.fail("Could not get a successful response from any domain tool, so conventions could not be checked. "
                   "Pass --examples with one valid call per tool, or add defaults/examples to your schemas.")

    # -- check 5 ------------------------------------------------------------
    async def check_malformed(self, domain_tools: list) -> None:
        c = self.r[5]
        probes = []
        for t in domain_tools:
            s = t.inputSchema or {}
            props = s.get("properties") or {}
            req = s.get("required") or []
            if req:
                probes.append((t.name, {}, "missing required arguments"))
            if props:
                base = self.examples.get(t.name) or sample_args(s)
                pname = req[0] if req else next(iter(props))
                bad = dict(base)
                bad[pname] = wrong_type_value(props.get(pname, {}))
                probes.append((t.name, bad, f"wrong type for `{pname}`"))
        probes.append(("smac_tool_that_does_not_exist", {}, "unknown tool name"))

        for name, args, purpose in probes:
            if self.transport_dead:
                break
            outcome, res = await self.call(name, args, f"malformed: {purpose}")
            if outcome.split("_slow")[0] in ("tool_error", "protocol_error"):
                continue
            if outcome.startswith("ok"):
                c.warn(f"`{name}` accepted a call with {purpose} and returned a normal result; "
                       "it should return an error instead of guessing")
            elif outcome.startswith("timeout"):
                c.fail(f"`{name}` gave no response within {TIMEOUT_S}s to a call with {purpose}. "
                       "It either hung or crashed; check your agent's console for a traceback.")
            else:
                c.fail(f"`{name}` crashed or dropped the connection on a call with {purpose}: {res}")

        # Is the agent still alive?
        if self.transport_dead:
            c.fail("The connection died during malformed-input testing; the agent must never crash on bad input")
            return
        outcome, _ = await self.call("health", {}, "liveness after malformed calls")
        if not outcome.startswith("ok"):
            c.fail("`health` stopped responding after malformed calls")
        elif c.status == PASS:
            c.note(f"{len(probes)} malformed calls handled; agent still healthy afterwards")

    # -- check 6 ------------------------------------------------------------
    def check_timing(self) -> None:
        c = self.r[6]
        if not self.r.calls:
            c.skip("No calls were made")
            return
        slow = [x for x in self.r.calls if x.seconds > TIMEOUT_S or x.outcome.startswith("timeout")]
        for x in slow:
            c.fail(f"`{x.tool}` ({x.purpose}) took {x.seconds:.1f}s; the limit is {TIMEOUT_S}s")
        worst = max(self.r.calls, key=lambda x: x.seconds)
        c.note(f"{len(self.r.calls)} calls, slowest `{worst.tool}` at {worst.seconds:.2f}s (limit {TIMEOUT_S}s)")
        if not slow and worst.seconds > TIMEOUT_S * 0.5:
            c.warn("Slowest call used over half the budget. Showcase queries chain several agents, so aim lower.")

    # -- run ----------------------------------------------------------------
    async def run(self) -> Report:
        try:
            async with streamablehttp_client(self.url, timeout=TIMEOUT_S, sse_read_timeout=TIMEOUT_S * 2) as (rd, wr, _):
                async with ClientSession(rd, wr, read_timeout_seconds=timedelta(seconds=TIMEOUT_S + 2)) as session:
                    self.session = session
                    t0 = time.perf_counter()
                    init = await asyncio.wait_for(session.initialize(), timeout=TIMEOUT_S)
                    self.r.calls.append(CallRecord("(initialize)", "initialize",
                                                   round(time.perf_counter() - t0, 3), "ok"))
                    self.check_initialise(init)

                    tools = (await session.list_tools()).tools
                    by_name = {t.name: t for t in tools}
                    domain = [t for t in tools if t.name != "health"]

                    self.stage = 2
                    await self.check_health(by_name, init)
                    self.stage = 3
                    self.check_schemas(domain)
                    self.stage = 4
                    await self.check_conventions(domain)
                    self.stage = 5
                    await self.check_malformed(domain)
                    self.stage = 6
                    self.check_timing()
        except (Exception, asyncio.CancelledError, BaseExceptionGroup) as e:
            if self.stage == 1:
                c = self.r[1]
                if not any(l == FAIL for l, _ in c.findings):
                    c.fail(explain_connect_error(e, self.url))
                reason = "Not run: could not complete the connection"
            else:
                self.r[self.stage].fail(
                    "The connection to your agent was lost during this check. It most likely crashed; "
                    "check its console for a traceback, fix it, and re-run.")
                reason = "Not run: the connection was lost earlier"
            for ch in self.r.checks[self.stage:5]:
                if ch.status == PASS and not ch.findings:
                    ch.skip(reason)
            if self.r.calls and not self.r[6].findings:
                self.check_timing()
            elif not self.r[6].findings:
                self.r[6].skip(reason)
        return self.r


def unwrap(e: BaseException) -> BaseException:
    while isinstance(e, BaseExceptionGroup) and e.exceptions:  # anyio task groups
        e = e.exceptions[0]
    return e


def probe_status(url: str) -> int | None:
    try:
        import httpx
        return httpx.post(url, json={}, headers={"Accept": "application/json, text/event-stream"}, timeout=5).status_code
    except Exception:
        return None


def explain_connect_error(e: BaseException, url: str) -> str:
    e = unwrap(e)
    s = f"{type(e).__name__}: {e}"
    status = probe_status(url)
    if status == 404:
        return (f"Got 404 Not Found from {url}. Something is listening, but not at that path. "
                "The Python SDK serves Streamable HTTP at /mcp by default.")
    if "ConnectError" in s or "Connection refused" in s or "ConnectError" in type(e).__name__:
        return (f"Could not connect to {url}. Is your agent running with the Streamable HTTP transport "
                "(not stdio), and is the URL, port and path (usually /mcp) correct?")
    if "404" in s:
        return f"Got 404 from {url}. Check the path; the Python SDK serves Streamable HTTP at /mcp by default."
    if "405" in s or "406" in s:
        return f"The server at {url} did not accept a Streamable HTTP request ({s}). Is it running an older SSE-only transport?"
    if isinstance(e, asyncio.TimeoutError):
        return f"Timed out after {TIMEOUT_S}s waiting for initialize to complete"
    return f"Connection failed: {s}"


# ----------------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------------
COLOURS = {PASS: "\033[32m", FAIL: "\033[31m", WARN: "\033[33m", SKIP: "\033[90m", "INFO": "\033[90m"}
RESET = "\033[0m"


def print_report(r: Report, colour: bool) -> None:
    def col(level: str, text: str) -> str:
        return f"{COLOURS.get(level, '')}{text}{RESET}" if colour else text

    print()
    print(f"REIT7820 Handshake Test v{HARNESS_VERSION}")
    print(f"Agent: {r.server_name or '(unknown)'}   URL: {r.url}")
    print("-" * 72)
    for c in r.checks:
        print(f"{col(c.status, f'[{c.status}]'):<{16 if colour else 7}} {c.number}. {c.name}")
        for level, msg in c.findings:
            if level == "INFO":
                print(f"         {col('INFO', msg)}")
            else:
                print(f"         {col(level, level + ':')} {msg}")
    print("-" * 72)
    if r.passed:
        print(col(PASS, "HANDSHAKE PASSED. Your agent meets the spec for Showcase participation."))
    else:
        n = sum(c.status != PASS for c in r.checks)
        print(col(FAIL, f"HANDSHAKE NOT PASSED: {n} check(s) need attention. Fix the FAIL lines above and re-run."))
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description="REIT7820 handshake test for Smart Mobility Collective MCP agents")
    ap.add_argument("url", help="Your agent's Streamable HTTP endpoint, e.g. http://localhost:8000/mcp")
    ap.add_argument("--examples", help="JSON file: {tool_name: {valid arguments}} for check 4")
    ap.add_argument("--json", dest="json_out", help="Also write the full report as JSON to this file")
    ap.add_argument("-v", "--verbose", action="store_true", help="Print every call as it happens")
    ap.add_argument("--no-colour", action="store_true", help="Plain output")
    a = ap.parse_args()

    examples = None
    if a.examples:
        with open(a.examples, encoding="utf-8") as f:
            examples = json.load(f)

    report = asyncio.run(Handshake(a.url, examples, a.verbose).run())
    print_report(report, colour=not a.no_colour and sys.stdout.isatty())
    if a.json_out:
        with open(a.json_out, "w", encoding="utf-8") as f:
            json.dump(report.to_dict(), f, indent=2)
        print(f"Report written to {a.json_out}")
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
