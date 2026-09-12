# Feedback log — Nebius Token Factory / NVIDIA Nemotron

Running notes from building MUTINY. Accumulated from day one rather than
written at submission time.

## Nemotron 3 on Token Factory

**Model IDs are inconsistently cased**, which makes them easy to get wrong and
impossible to normalise confidently:

```
nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B     upper, with a redundant NVIDIA- prefix
nvidia/nemotron-3-super-120b-a12b         all lower
nvidia/Nemotron-3-Ultra-550b-a55b         mixed
nvidia/Nemotron-3_5-Lightning             mixed, underscore as version separator
```

Four models, four conventions. Suggest normalising, or documenting the exact
strings prominently — `/v1/models` is currently the only reliable source.

**Reasoning cannot be disabled, and this is not documented.** Nemotron 3 always
produces a reasoning trace. It arrives in a non-standard `reasoning` field on
the message (not in the OpenAI schema, so SDK users need `getattr`), and it is
billed as completion tokens. Answering "what is 2+2" costs 44 completion tokens,
of which 2 are the answer.

Things that do not switch it off:
- `extra_body={"chat_template_kwargs": {"thinking": False}}` — silently ignored,
  byte-identical output and token count to the baseline
- `/no_think` in the user message — makes the trace *longer* (66 tokens vs 43)
- `/no_think` as a system message — no effect

A documented way to disable reasoning for high-volume classification calls would
materially change cost planning. If there is one, it is not discoverable.

**Silent truncation is the sharp edge.** Because reasoning is billed against
`max_tokens`, a request with a modest allowance spends the entire budget
thinking and returns `content == ""` with `finish_reason == "length"`. There is
no error and no warning. A first-time user sees an empty string and has no
indication why. Worth calling out in the docs, or emitting a distinct
finish reason when the trace alone exhausted the budget.

## Sandboxes

_(pending — concurrency and billing questions posted to the Devpost forum, not
yet answered)_

## Docs

The Sandboxes branching documentation is genuinely good and was the reason this
project is architected the way it is. The billing model for sandbox compute —
whether it draws on Token Factory credits or a separate meter — is the one thing
not answered anywhere I could find, and it is load-bearing for anyone deciding
how much to fan out.

## Environment (not a Nebius issue, recorded for completeness)

Building from a corporate laptop behind corporate TLS inspection, every Python,
Node and uv outbound call failed with `CERTIFICATE_VERIFY_FAILED` while `curl`
worked — curl consults the macOS keychain, the others do not. Not something
Nebius can fix, but it cost an hour and will hit anyone building from a managed
device. A one-line note in the quickstart ("behind a TLS-inspecting proxy, point
`SSL_CERT_FILE` at a bundle that includes your organisation's CA") would save
that hour. MUTINY now merges certifi with the system keychain automatically —
see `mutiny/tls.py`.
