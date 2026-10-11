# AI use

## Tools

I used Codex, configured as `gpt-6.1-sol` with `xhigh` for building.
The submitted harness uses Anthropic Claude, configured as `claude-haiku-5-5` with
high effort

## How you worked with them

This was a substantially AI-assisted build. I initially asked for small explained
snippets and entered them myself. Later, I asked Codex to implement larger sections
directly. Codex proposed much of the architecture and wrote and repaired a large
part of the code, tests, and documentation.

I chose the extraction model configuration, ran setup and live API checks in my
terminal, supplied failure outputs, asked for explanations and removal of redundant
code, and requested audits restricted to the assignment's business rules. I asked
for cached replay where possible because API credits were limited.

The log records failures as well as successful runs: incomplete invoice schemas,
invalid evidence quotes, structured-output grammar limits, token truncation,
missing conflict findings, a full-mailbox timeout, and percentage-unit and payment
record interpretation errors. Codex supplied much of the diagnosis and fixes; I
ran checks and returned results. The verification distinguishes fresh model calls,
controlled model responses, and API-disabled cache replay. Passing the public
checks does not establish performance on the unseen evaluation.

## Written without AI

I am not claiming any implementation module, test suite, or submission write-up as
independently authored without AI. My own contributions included the
model choice, terminal runs, reported failures, and requests to change or review
the implementation.

## Prompt log

- [Original conversation and tool activity](ai_log/conversation_with_tools.html):
  the supplied export, copied byte for byte. It contains 1,835 records and ends on
  10 October 2026 at 22:44:46 IST.
