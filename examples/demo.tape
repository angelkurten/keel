# Terminal demo for the README and the docs. Re-record with: vhs examples/demo.tape
# The comment lines typed into the shell are the narration.
Output docs/demo.gif

Set Shell zsh
Set FontSize 15
Set Width 1280
Set Height 900
Set Padding 20
Set Theme "Catppuccin Mocha"
Set TypingSpeed 30ms

Hide
Type "setopt interactive_comments && clear"
Enter
Show

Type "# Keel: domain events from your SaaS in, one context graph per end-customer out, five tools for every agent."
Enter
Sleep 2.5s
Type "# 1/4  A cartridge declares the domain. The core never reads it, it only checks that the stream fits the catalog."
Enter
Sleep 1s
Type "uv run keel validate cartridges/fleet --events examples/events/fleet.jsonl"
Enter
Sleep 3.5s
Enter
Type "# 2/4  Ingest: deterministic, no LLM in the path, replayable from the log."
Enter
Sleep 1s
Type "uv run keel ingest cartridges/fleet examples/events/fleet.jsonl"
Enter
Sleep 3s
Type "clear"
Enter
Type "# 3/4  What is true now about one tire. The rule lives in the cartridge and context returns its verdict."
Enter
Sleep 1s
Type "uv run keel context cartridges/fleet examples/events/fleet.jsonl tire:T-9001 | grep -v recorded_at"
Enter
Sleep 7s
Type "clear"
Enter
Type "# 4/4  An agent with no LLM: reads the verdict, writes task memory with evidence. Facts close. They are not overwritten."
Enter
Sleep 1s
Type "uv run python examples/agent_minimal.py"
Enter
Sleep 6s
Enter
Type "# Same five tools over MCP:  uv run keel serve cartridges/fleet --tenant acme-logistics --events examples/events/fleet.jsonl"
Enter
Sleep 5s
