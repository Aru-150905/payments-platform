# Project notes

One set of notes per session, covering only what was actually built or learned
in that session. Nothing aspirational, nothing copied from documentation.

## Files

| Session | Date | Covers |
|---|---|---|
| [session-01](session-01.pdf) | 14 Sep 2026 | Terminal and shell basics, PATH, Homebrew, containers and Docker Compose, ports and volumes, the Python 3.14 wheel failure, double-entry ledger, Kafka vs queues, partitions and keys, the transactional outbox, row locking, every command run and why |
| [session-02](session-02.pdf) | 15 Sep 2026 | Exit codes, HTTP status codes as a contract, heredocs, `&&` chaining, `git commit --amend`, the Kafka `advertised.listeners` crash, the TOML append trap, recreating one container without wiping the database, the two real bugs (500-instead-of-409 and the non-repeatable demo), consumer group rebalancing, reviewing a diff |

## How to regenerate a PDF after editing the markdown

```bash
brew install pandoc
brew install --cask wkhtmltopdf
cd docs/notes
pandoc session-01.md -o session-01.pdf --pdf-engine=wkhtmltopdf \
  --css=style.css --toc --toc-depth=2
```

## Convention for future sessions

Each file has the same seven parts, in this order:

1. What we are building, in plain English
2. Vocabulary introduced this session
3. The concepts, with a "why not the alternative" note on every decision
4. Every problem hit, the diagnostic reasoning, and the fix
5. The design ideas in the code
6. Every command run, what it does, and why that one and not another
7. Where you actually are, and what is next

Plus a glossary. The glossary is cumulative -- carry forward the previous
session's entries and add to them.

The "why not the alternative" notes are the point. Knowing what you built is
table stakes; knowing what you rejected and why is what separates having built
something from having followed a tutorial.
