# Train support agent (local, Ollama)

A customer support AI agent for UK train travel, built to learn how agents work. It runs fully locally on a small model (llama3.1, 8B) through Ollama, with about 600 lines of Python and no agent framework.

## Inspiration

After building a [RAG](https://en.wikipedia.org/wiki/Retrieval-augmented_generation) pipeline, I wanted to understand what makes an AI *agent* different. I needed a realistic example, so I picked the app I use most often: **Trainline**. Its customer support handles the kinds of requests an agent is good at: checking whether my train is on time, claiming money back when it's late (Delay Repay), and finding trains for a trip.

This project recreates that kind of support experience with dummy data: 26 real UK station names, about 15,000 generated train journeys, and two sample customers with bookings. None of it comes from Trainline or any real booking system.

## What it does

The agent can:

1. **Check train status** for the customer's bookings (on time, delayed or cancelled)
2. **Handle refunds**, including Delay Repay for late trains, with the rules explained using RAG over policy documents
3. **Find trains in the timetable** by **writing its own SQL query** from a plain-English request

Anything else (for example changing a booking) is handed to a human with `escalate_to_human`.

## RAG vs agent

- **RAG:** the code always runs the same steps: retrieve relevant text, then generate an answer.
- **Agent:** the model decides the steps. It picks a tool, reads the result, and repeats until it can answer. Here, RAG is just one of the agent's tools.

## Setup

Install [Ollama](https://ollama.com), then:

```bash
ollama pull llama3.1            # chat model, must support tool calling
ollama pull nomic-embed-text    # embeddings for the policy search
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python seed_db.py               # creates train.db with dates relative to today (re-run any time to reset)
```

## Run

```bash
python agent.py                                   # chat as Alice
python agent.py -v                                # also show tool calls, the SQL the model writes, and tool results
python agent.py -e ben@example.com                # log in as Ben
python agent.py "Is my train tomorrow on time?"   # ask one question and exit
python agent.py -m qwen3:8b                       # use a different Ollama model (must support tools)
python policy_search.py "late train refund"       # test the RAG part on its own
sqlite3 train.db "select * from refunds"          # see what the agent changed
```

On a laptop CPU each reply takes up to a minute.

## How it works

```
You ──► agent loop (agent.py) ──► llama3.1 decides: call a tool, or answer
              ▲                              │
              └──── tool result ◄── tools.py ┘──► train.db (SQLite) / policies/ (RAG)
```

| File | What it is |
|---|---|
| `agent.py` | The system prompt and **the agent loop**: send the conversation and tool schemas to the model, run the tools it asks for, add the results to the conversation, and repeat until it answers |
| `tools.py` | The 5 tools and the JSON schemas the model sees |
| `policy_search.py` | RAG over `policies/`: chunk, embed with `nomic-embed-text`, cosine search |
| `seed_db.py` | Builds `train.db`: 26 UK stations, about 15,000 journeys over 11 days, 2 customers and 6 bookings |
| `config.py` | Model names and settings |

### The tools

| Tool | Type | What it does |
|---|---|---|
| `get_my_bookings` | Fixed query | The logged-in customer's bookings with live train status |
| `search_policies` | RAG | Finds the relevant refund or railcard policy text |
| `search_trains_sql` | **Model writes the SQL** | Searches the timetable for any route, date or time |
| `request_refund` | Fixed code, write | Works out eligibility and amount in Python, then asks the human **y/N** before saving |
| `escalate_to_human` | Write | Creates a ticket for a human agent |

### Guardrails (enforced in code, not the prompt)

- **Customers only see their own data.** The model never passes a customer id, so "Refund BK2001" (someone else's booking) fails.
- **Business rules in Python.** Refund amounts and eligibility are calculated by code, never by the model.
- **Human in the loop.** `request_refund` runs as a dry run first, then waits for a human to type `y`.
- **Model-written SQL is sandboxed.** It runs on a read-only connection, and an SQLite authorizer only allows reading the `journeys` and `stations` tables. `SELECT * FROM bookings` and `DELETE ...` are blocked.
- **Loop limits.** `MAX_TOOL_ROUNDS` and a repeat-call check stop the model from looping forever.

## Things to try

| Type this (as Alice) | What it shows |
|---|---|
| "I'm travelling on <date> from Sheffield to London, give me the available options around 12PM" | **The model writes SQL** (use `-v` to see the query) |
| "Is my train to Edinburgh tomorrow on time?" | Fixed tool call, answer from real data |
| "My train to York yesterday was late, can I get money back?" | Refund with **human confirmation** (£17.50 = 50% of £35) |
| "Refund booking BK2001" | Guardrail: another customer's booking is refused |
| "Can you move my Birmingham train to a later one?" | Out of scope, so it hands over to a human |

Use any date from today to 7 days ahead for train searches.

## Sample data

Dates are relative to the day you run `seed_db.py`.

- **Alice** (alice@example.com, 16-25 Railcard): BK1001 to York yesterday (45 min late), BK1002 to Edinburgh tomorrow, BK1003 to Edinburgh in 2 days (cancelled), BK1004 to Birmingham in 3 days
- **Ben** (ben@example.com): BK2001 to Manchester 2 days ago (130 min late), BK2002 from Manchester in 2 days
- **Timetable:** 26 stations and 31 routes in both directions, with about 5% of trains delayed and 1.5% cancelled at random (fixed seed)

## What testing with llama3.1 (8B) taught me

1. **Small models claim to use tools they never called.** It said "I've checked the status" without checking. Fix: fewer, simpler tools and a prompt rule to report only what tools returned.
2. **Write tool output for the model to read.** Given ISO dates, it matched "Wednesday" to the wrong booking. Readable text such as `"Friday 13 Nov, 09:00 (in 2 days)"` and a calendar in the prompt fixed it.
3. **Tool calls sometimes come back as text.** `text_tool_calls()` in `agent.py` recovers them.
4. **Clear errors let the model fix itself.** "Missing: origin. Unknown arguments you used: from" and the next call was right.
5. **Know your model's limits.** Changing a booking needed 3 tool calls in a row and the 8B model lost track, so that feature was removed. Larger tool-calling models handle longer chains much better.

## Ideas to extend it

- Evals: scripted conversations with checks such as "did it ask before refunding?", run against several models
- Add booking changes back with a stronger model
- A web UI (FastAPI + a simple chat page)

This is a personal learning project. It is not affiliated with or endorsed by Trainline or any train operator, and all bookings, customers and timetable data are generated dummy data.
