import argparse
import inspect
import json
from datetime import datetime, timedelta

import ollama

import config
from tools import TOOL_SCHEMAS, WRITE_TOOLS, ToolError, Tools

TOOL_NAMES = {t["function"]["name"] for t in TOOL_SCHEMAS}

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"

parser = argparse.ArgumentParser(description="Train ticket support agent running locally on Ollama.")
parser.add_argument("question", nargs="*", help="ask one question and exit (omit for interactive chat)")
parser.add_argument("-e", "--email", default=config.DEFAULT_CUSTOMER_EMAIL, help="customer to log in as")
parser.add_argument("-m", "--model", default=config.LLM_MODEL, help="Ollama chat model (must support tools)")
parser.add_argument("-v", "--verbose", action="store_true", help="show tool calls, the SQL the model writes, and tool results")
args = parser.parse_args()

try:
    tools = Tools(args.email)
except ConnectionError:
    raise SystemExit("Could not reach Ollama. Start the Ollama app and try again.")
customer = tools.customer
today = datetime.now()
# Small models get weekday maths wrong ("Wednesday" -> Tuesday's date), so spell it out
labels = {-1: " (yesterday, past)", 0: " (today)", 1: " (tomorrow)"}
calendar = "\n".join(f"- {d:%A %d %b} = {d:%Y-%m-%d}" + labels.get(i, " (past)" if i < 0 else "")
                     for i, d in ((i, today + timedelta(days=i)) for i in range(-3, 7)))

SYSTEM_PROMPT = f"""You are a customer support agent for a UK train ticket company.
You are talking to {customer['name']} (railcard: {customer['railcard'] or 'none'}), who is logged in.
The time now is {today:%H:%M}. Use this calendar for dates. When the customer names a weekday for a trip they are planning, they mean the next one that is not in the past:
{calendar}

Rules:
- You can call several tools one after another before replying. Finish the task with tools before writing your answer; never ask the customer to wait or log in.
- You can help with three things: checking the status of the customer's trains, refunds (including Delay Repay for late trains), and finding trains in the timetable.
- To find trains, write a SQL query for search_trains_sql. Use the calendar above for dates. For a time like 'around 12pm', search about an hour either side. If you get an SQL error, fix the query and try again.
- When listing trains, show departure and arrival times, the operator, and say clearly if a train is cancelled or delayed.
- Never guess booking details, times or prices.
- Never invent a booking id. Unless the customer gave one, your FIRST tool call must be get_my_bookings, then pick the booking that matches the route and date they describe.
- Only say a train is on time, delayed or cancelled if a tool result told you so.
- Before explaining any rule, fee or entitlement, call search_policies and base your answer on what it returns.
- Refund amounts and fees are calculated by request_refund. Do not calculate them yourself.
- When the customer wants a refund or compensation for a late train, find the booking and call request_refund. Do not ask the customer for times or amounts; the tool checks eligibility and the system asks them to confirm.
- If a tool returns an error, explain it to the customer in plain words.
- If the customer is very upset, asks for a person, or wants anything else (such as changing a booking), call escalate_to_human.
- Keep replies short and friendly. Do not mention tool names or JSON to the customer."""


def confirm(name, preview):
    """Human-in-the-loop: show what a write tool is about to do and ask y/n."""
    print(f"\n{BOLD}  The agent wants to run {name}:{RESET}")
    for key, value in preview.items():
        print(f"    {key}: {value}")
    return input(f"{BOLD}  Confirm? [y/N] {RESET}").strip().lower() in ("y", "yes")


def run_tool(name, arguments):
    """Run one tool call from the model and return the result as a JSON string."""
    fn = getattr(tools, name, None)
    if fn is None or name.startswith("_"):
        return json.dumps({"error": f"There is no tool called {name}."})
    # Small models sometimes invent extra arguments, so keep only real ones
    params = [p for p in inspect.signature(fn).parameters if p != "dry_run"]
    unknown = [k for k in (arguments or {}) if k not in params]
    arguments = {k: v for k, v in (arguments or {}).items() if k in params}
    missing = [p for p in params if p not in arguments
               and inspect.signature(fn).parameters[p].default is inspect.Parameter.empty]
    if missing:
        # A precise error lets the model fix its own call on the next round
        return json.dumps({"error": f"{name} needs arguments {params}. Missing: {missing}. "
                                    f"Unknown arguments you used: {unknown}."})
    try:
        if name in WRITE_TOOLS:
            preview = fn(**arguments, dry_run=True)
            if not confirm(name, preview):
                return json.dumps({"result": "The customer declined. Nothing was changed."})
            result = fn(**arguments, dry_run=False)
        else:
            result = fn(**arguments)
    except ToolError as e:
        result = {"error": str(e)}
    except (TypeError, ValueError) as e:
        result = {"error": f"Bad arguments for {name}: {e}"}
    return json.dumps(result, default=str, ensure_ascii=False)


def text_tool_calls(content):
    """Fallback: small models sometimes write a tool call as JSON text instead of a real
    tool call, e.g. {"name": "get_my_bookings", "parameters": {}}. Recover those."""
    calls, decoder, i = [], json.JSONDecoder(), 0
    while (i := content.find("{", i)) != -1:
        try:
            obj, end = decoder.raw_decode(content, i)
        except json.JSONDecodeError:
            i += 1
            continue
        if isinstance(obj, dict) and obj.get("name") in TOOL_NAMES:
            calls.append((obj["name"], obj.get("parameters") or obj.get("arguments") or {}))
        i = end
    return calls


def agent_turn(messages):
    """THE AGENT LOOP: keep calling the model until it answers instead of calling tools."""
    seen = set()
    for _ in range(config.MAX_TOOL_ROUNDS):
        # 1. ASK THE MODEL: it sees the whole conversation plus the tool schemas
        response = ollama.chat(model=args.model, messages=messages, tools=TOOL_SCHEMAS,
                               options={"temperature": 0})
        message = response.message
        messages.append(message)

        if args.verbose and getattr(message, "thinking", None):
            print(f"{DIM}  [thinking] {message.thinking.strip()}{RESET}")

        calls = [(c.function.name, c.function.arguments) for c in message.tool_calls or []]
        if not calls:
            calls = text_tool_calls(message.content or "")
        # Small models occasionally send arguments that are not a JSON object; treat those as none
        calls = [(name, a if isinstance(a, dict) else {}) for name, a in calls]

        # 2. NO TOOL CALLS: the model has answered the customer, so this turn is done
        if not calls:
            return message.content

        # 3. RUN EACH TOOL and give the result back to the model as a "tool" message
        for name, arguments in calls:
            if args.verbose:  # show which tool the model picked (and the SQL it wrote) only with -v
                if name == "search_trains_sql":
                    print(f"{DIM}  [tool] {name}:\n    " + str((arguments or {}).get("sql", "")).replace("\n", "\n    ") + RESET)
                else:
                    print(f"{DIM}  [tool] {name}({', '.join(f'{k}={v!r}' for k, v in (arguments or {}).items())}){RESET}")
            key = (name, json.dumps(arguments, sort_keys=True, default=str))
            if key in seen:  # stop the model retrying the exact same call forever
                result = json.dumps({"error": "You already made this exact call. Change the arguments, "
                                              "or explain the problem to the customer."})
            else:
                seen.add(key)
                result = run_tool(name, arguments)
            if args.verbose:
                print(f"{DIM}         -> {result}{RESET}")
            messages.append({"role": "tool", "tool_name": name, "content": result})
        # ...then loop: the model reads the results and decides what to do next

    return "Sorry, I'm having trouble with that request. Let me pass you to a colleague."


messages = [{"role": "system", "content": SYSTEM_PROMPT}]

if args.question:
    messages.append({"role": "user", "content": " ".join(args.question)})
    print(f"\n{BOLD}Agent:{RESET} {agent_turn(messages)}")
else:
    print(f"Train support agent ({args.model}). Logged in as {customer['name']}. Type 'quit' to exit.")
    while True:
        try:
            text = input(f"\n{BOLD}You:{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text.lower() in ("quit", "exit", "q"):
            break
        if text:
            messages.append({"role": "user", "content": text})
            print(f"\n{BOLD}Agent:{RESET} {agent_turn(messages)}")
