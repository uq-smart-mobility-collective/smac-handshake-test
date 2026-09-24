# REIT7820 Handshake Test

The handshake test connects to your agent the way the Showcase orchestrator will, over Streamable HTTP. It then runs the six checks from the Agent Interface Specification v1.0 (section 9). You need to pass all six to take part in the Week 12 Interoperability Showcase.

**Deadline: pass by the end of Week 10 (Friday 9 October)** and submit your report as described below. You can run the test as many times as you like before then.

## Set up (once)

Use a separate virtual environment from your agent:

```bash
python -m venv .venv-handshake
# Windows: .venv-handshake\Scripts\activate    macOS/Linux: source .venv-handshake/bin/activate
pip install -r requirements.txt
```

## Run it

1. Start your agent with the Streamable HTTP transport, not stdio. With the Python SDK:

   ```python
   mcp.run(transport="streamable-http")   # serves http://localhost:8000/mcp by default
   ```

2. In a second terminal:

   ```bash
   python handshake.py http://localhost:8000/mcp
   ```

The output shows PASS or FAIL for each check. Every FAIL line tells you what to fix. The run ends with `HANDSHAKE PASSED` or `HANDSHAKE NOT PASSED`.

Useful options:

| Option | What it does |
|---|---|
| `-v` | Shows every call as it happens. Useful if the test seems stuck. |
| `--examples examples.json` | Gives one valid set of arguments per tool (see below). |
| `--json report.json` | Saves the full report. Attach it if you post about a failure on Ed. |

## Submitting your result

You don't need anyone to sign you off. Your report is your proof:

```bash
python handshake.py http://localhost:8000/mcp --json handshake_report.json
```

Copy `handshake_report.json` into the **root of your own agent repo**, commit it and push it by Friday 9 October. You can re-run the test and push a newer report as often as you like. We read the latest report from every repo after the deadline.

The Showcase runs the same checks live against your agent, so the report only counts if your agent actually behaves that way on the day.

## The six checks

| # | Check | What passes |
|---|---|---|
| 1 | Initialise | `initialize` succeeds on protocol **2025-11-25** and the `tools` capability is advertised. `serverInfo.name` is `smac-{domain}-{slot}`, where domain is one of `charging`, `pt`, `policy`, `equity`, `network`, `custom`. `serverInfo.version` is a semantic version such as `1.0.0`. |
| 2 | `health` tool | It exists and takes no arguments. It returns `status: "ok"`, an `agent` field matching your server name, `version`, a non-empty `data_sources` list and `llm_backend`. |
| 3 | Tool schemas | You have 1 to 5 domain tools, not counting `health`. Tool names are verb-first snake_case, every tool has a description, and every parameter has a type and a description. |
| 4 | Data conventions | Every response includes a `sources` array. Coordinates use `{"lat": ..., "lon": ...}`. Timestamps are ISO 8601 with a timezone. Distances are integer metres and durations are integer seconds. Money is an AUD decimal string such as `"0.50"`. |
| 5 | Malformed input | Missing arguments, wrong types and unknown tool names all return an MCP error. Your agent never crashes or hangs, and `health` still responds afterwards. |
| 6 | Response time | Every call returns, or errors, within **45 seconds**. |

WARN lines don't stop you passing. They point to things that could hurt you at the Showcase, such as a very short tool description.

## If check 4 says it couldn't make a successful call

Check 4 needs one real answer from each of your tools. The test builds arguments from your `inputSchema`, using defaults, examples, enums and sensible SEQ values such as UQ St Lucia for coordinates. If that doesn't work for your tool, you have two options:

- Add `default` or `examples` to your parameters. This also helps the orchestrator.
- Or pass an examples file with one valid call per tool:

  ```json
  {
    "find_chargers_within_radius": {"lat": -27.4698, "lon": 153.0251, "radius_m": 3000}
  }
  ```

## Common fixes

- **"Could not connect"**: your agent isn't running, or it's still on stdio. Check the port.
- **"404 Not Found"**: the path is wrong. The Python SDK uses `/mcp`.
- **Version is not semantic**: FastMCP has no version argument. Set it after creating the server:
  `mcp._mcp_server.version = "1.0.0"`
- **Parameter has no description**: use `Annotated[float, Field(description="...")]` from pydantic.
- **Timeouts**: long LLM calls need a timeout and an error path. Remember the 429 handling from Week 7.

## Reference agent

`reference_agent.py` is a minimal agent that passes all six checks. Use it to see the shape a conformant agent needs. It isn't a starting point for your thesis tool.

```bash
python reference_agent.py            # terminal 1
python handshake.py http://localhost:8000/mcp   # terminal 2
```

Stuck for more than 15 minutes? Post on Ed with your `--json` report attached. Anonymous posts are fine. Ed is monitored over the break.
