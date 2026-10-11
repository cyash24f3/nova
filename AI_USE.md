# AI use

## Tools

I used Codex, configured as `gpt-6.1-sol` with `xhigh` for building.
The submitted harness uses Anthropic Claude, configured as `claude-haiku-5-5` with
high effort.

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

To read the log:

1. Sign in to GitHub using an account with access to this private repository.
2. Follow the original conversation link above to open the HTML file's GitHub page.
3. Use **Download raw file** to save `conversation_with_tools.html`. Keep the `.html` extension.
4. Open the saved file in Chrome, Firefox, Safari, or another browser. GitHub's file page does not run the viewer.

The viewer contains its own data and needs no API key, installation, or network connection. It offers search,
message/tool filters, expandable entries, and access to each original exported record. The file is about 26 MB;
GitHub may omit its source preview, but the file remains downloadable. Earlier machine-specific paths inside
conversation records are historical text, rather than links needed to use the viewer.
