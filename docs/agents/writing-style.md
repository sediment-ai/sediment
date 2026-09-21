# Writing style — Google developer documentation style

The [Google developer documentation style
guide](https://developers.google.com/style) governs every word an agent
writes here: chat replies to the builder, PR descriptions, issue comments,
commit message bodies, and every page under `docs/`. Treat it as the
authority and follow it wherever this page is silent. This page doesn't
restate the guide. It pins the rules that drafts break by default, the swaps
worth memorizing, and the places this repo narrows or overrides Google's
guidance.

Which *mode* a published page is — tutorial, how-to, reference, explanation
— is a separate question: see [the four modes](doc-style.md#the-four-modes).
Agent docs, this one included, sit outside that scheme. Contributor doc,
not published to the docs site.

## The defaults every draft breaks

Check these first. A draft that clears all five is most of the way there.

1. **Timeless.** Cut *currently*, *now*, *new*, *recently*, *soon*, *at this
   time*, *as of this writing*, *does not yet*. Write "the exporter doesn't
   support X", never "doesn't currently support X". `CHANGELOG.md` and
   release notes are the exceptions — they carry a date, so *new* means
   something there.
2. **The reader is *you*; the software is never *we*.** Name the actor: "the
   deriver scores survival", "you set `SEDIMENT_ORG_ID`" — never "we then
   score the Session". Use *we* for the project's own choices and measurements
   ("we rejected X") in `README.md`, `CONTRIBUTING.md`, or an explanation page.
   It never stands in for what the code does.
3. **Conditions before instructions.** "If you run your own gateway, set
   `SEDIMENT_GATEWAY_KEY`" — not "Set `SEDIMENT_GATEWAY_KEY` if you run your
   own gateway". A reader who acts on the first clause has already acted.
4. **No *simply*, *just*, *easy*, *quickly*.** What's easy for you may not be
   easy for the reader. The word costs their trust the moment the step
   fails. Delete it, or swap it for the concrete thing: "about five
   minutes".
5. **No *please note*, *note that*, *it is worth noting*.** State the fact;
   a `Note:` block holds skippable information, never a prerequisite —
   that belongs in the step that needs it.

## Sentences

- **Active voice, named actor.** "The test caught the error", not "the error
  was caught by the test". Scan a draft for was/were/is/are + participle +
  *by*. Passive stays only where the actor is unknown ("the ref was
  force-pushed") or irrelevant ("errors are logged automatically").
- **Present tense.** "The server sends an acknowledgment", not "will send".
  Future tense is for events that genuinely come later: "the file is
  archived the next time the backup runs".
- **One idea per sentence.** Split a sentence that carries a second idea
  across *and*, *but*, or *because*. Past three parallel items, switch to a
  list.
- **Serial commas.** "Facts, Derivations, and Rollouts" — never "Facts,
  Derivations and Rollouts". The comma before the final *and*/*or* keeps a
  three-item list from reading as two; it doesn't apply inside one compound
  item ("prompt and bucket checks").
- **Contract a negation** — *isn't*, *don't*, *can't*, *doesn't*; a skimming
  reader misses a bare *not*. Other two-word contractions are optional;
  never use three-word or invented ones.
- **Keep the helper words.** *That*, *then*, and *of* get dropped in
  conversational English, and their absence is what makes a sentence read
  twice: "the rules that drafts break", not "the rules drafts break".
- **Say the concrete thing.** "About five minutes", not "quickly". "Three
  retries", not "a few".
- **American spelling** — *labeled*, *normalize*, *behavior*. A literal name
  keeps its own spelling: the file `labelled_cases.json` and the CI result
  value `cancelled` stay as they are.
- **No idioms, humor, or culture-bound references.** They fail the reader
  whose first language isn't English, and they fail in translation.

## Words to swap

| Instead of | Write |
| --- | --- |
| utilize, leverage | use |
| in order to | to |
| allows you to, enables you to | lets you |
| execute (a command) | run |
| facilitate | help, let |
| desired | the one you want |
| e.g., i.e. | for example, that is |
| etc., and so on | finish the list, or name what bounds it |
| above, below (within a page) | earlier, preceding, later, following |
| make a decision, perform an analysis | decide, analyze |
| end result, past history, advance planning | result, history, planning |
| very, really, actually, basically | delete the word |
| sanity check, dummy value | completeness check, placeholder |
| kill, abort (a process) | stop, cancel, end |
| blacklist, whitelist | blocklist, allowlist |

A literal name stays literal — `git rebase --abort`, `SIGKILL`, and a
vendor's `whitelist` field — the table governs prose, never a quoted
identifier. Extend it by shape, not by lookup: a verb buried in a noun (*do
a rewrite* → *rewrite*), a doubled word, an intensifier propping up a weak
claim, a metaphor of violence or disability standing in for a precise term.

## Terms

`CONTEXT.md` is the ubiquitous language. It outranks Google's word list on
any term it defines. Use its terms exactly, with its capitalization, and
never rotate synonyms — a second name for one concept reads as a second
concept.

Schema terms take a capital wherever they name the Sediment concept: Fact,
Session, Derivation, Attribution, Attributed completion, Rollout, Push,
Reward, Confidence, Provenance, Evidence recipe, Recovery, Developer
decision, Edit observation, Retry linkage. The ordinary-English senses stay
lowercase — a git push, a deployment rollout, high-confidence keys, a
statistical confidence interval — as do code identifiers, command display
names, and verbatim CLI output.

A `CONTEXT.md` term stays bare on first use; spell out anything else once —
RBAC (role-based access control) — or define or drop it.

## Pages in the docs tree

- **Sentence case headings.** A task heading takes the bare infinitive:
  "Create an instance", not "Creating an instance". A concept heading takes
  a noun phrase: "Attribution semantics". Keep code font out of a heading
  unless the heading names a literal — an event, a file, a command, a flag —
  where the backticks are what mark it as one: "DPO — `dpo.jsonl`".
- **Descriptive link text** — the destination's title, capitalized as part
  of the sentence. Never *here*, *this page*, *click here*, or a bare URL.
  Punctuation goes outside the link.
- **Numbered lists for a sequence, bulleted lists for everything else**; a
  table pairs a term with its value, never a bulleted list of pairs. One
  action per numbered step, imperative verb first, result in the same
  step: "Click **Run**. The query results appear." Prefix an optional step
  with `Optional:`. A procedure with one step is a bullet, not a numbered
  list of one.
- **Bold for UI element names**, code font for code, paths, flags, and
  literal values. Don't name the element type: "Click **Save**", not "click
  the Save button".
- **No directional language** — *above*, *below*, *the left-hand side*. It
  breaks for screen readers and for any layout that reflows. Name or link
  the section instead.
- **Unambiguous dates** — `2026-09-12`, ISO order, never `9/12/26`, which a
  reader outside the U.S. reads day-first.
- **Alt text on every image**, and a warning at the point of danger rather
  than in a preamble.
- **Draw the flow** when prose needs three tries. One diagram beats a
  paragraph describing a pipeline.

## Prose to the builder

Chat replies, PR descriptions, issue comments, commit message bodies, and
decision asks obey everything on this page, plus the always-on house rules
in [Communication](../../AGENTS.md#communication): answer first, mark
choices with `Decision:`, and state uncertainty plainly.
