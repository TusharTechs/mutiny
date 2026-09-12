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

Building from a managed laptop behind corporate TLS inspection, every Python,
Node and uv outbound call failed with `CERTIFICATE_VERIFY_FAILED` while `curl`
worked — curl consults the macOS keychain, the others do not. Not something
Nebius can fix, but it cost an hour and will hit anyone building from a managed
device. A one-line note in the quickstart ("behind a TLS-inspecting proxy, point
`SSL_CERT_FILE` at a bundle that includes your organisation's CA") would save
that hour. MUTINY now merges certifi with the system keychain automatically —
see `mutiny/tls.py`.

## A truncated reasoning trace returns a completely empty response

The sharpest issue we hit, and the most expensive.

When a Nemotron 3 response is cut off by `max_tokens` while the model is still
reasoning, the API returns `finish_reason: "length"` with **both** `content` and
`reasoning` empty — zero characters in each — while billing the full completion
allowance. There is no error and no partial output. From the caller's side a
paid-for request is indistinguishable from one that returned nothing at all.

We reproduced it in isolation on `nemotron-3-super-120b-a12b`:

| prompt | max_tokens | temperature | content | reasoning | billed |
|---|---:|---:|---:|---:|---:|
| 706 tokens | 4000 | 0.0 | 0 ch | 0 ch | 4000 |
| 706 tokens | 4000 | 0.3 | 0 ch | 0 ch | 4000 |
| 706 tokens | 4000 | 0.7 | 0 ch | 0 ch | 4000 |
| 706 tokens | 12000 | 0.0 | 0 ch | 0 ch | 12000 |

A 706-token prompt, so this is not a context-length problem. It is also stable
across temperature, which is itself surprising.

The natural remedy — retry with a larger allowance — makes it worse, since each
retry bills in full and returns the same nothing. We now detect the signature
(truncated, `reasoning` empty, `content` empty) and abandon the request rather
than widening, because widening measurably never helps.

Two suggestions:

1. Return the partial reasoning rather than discarding it. Even an unterminated
   trace tells the caller what happened, and would have saved us several hours.
2. Failing that, a distinct `finish_reason` for "the allowance was consumed
   before any output could be emitted" would make this diagnosable instead of
   looking like an empty model response.

We also could not find any way to disable reasoning for high-volume calls, which
compounds this: `chat_template_kwargs={"thinking": False}` is silently ignored
and `/no_think` lengthens the trace.

## Sandboxes: the Token Factory key authenticates but cannot spawn

A Token Factory API key is accepted by the Sandboxes endpoint — `whoami`
succeeds and returns real limits — but every permission on it is false:

```
permissions: import False   spawn False   spawn_disposable False
             list   False   cancel False  set_image_tag  False

limits:      instance_max_timeout          3600
             instance_max_concurrency        50
             instance_max_layer_bytes      12 GiB
             images_import_max_concurrency    8
```

Any actual operation then fails with `ForbiddenError: You do not have permission
to perform this action`, and only when the lazily-built image chain is awaited —
`images.use(...).run(...)` returns a `PREPARED` image with no error at all, so
the failure surfaces some distance from its cause.

The enablement path turns out to be a **Request beta access** button on the
Sandboxes page in the Token Factory console, which resolves it. Two suggestions
remain, both cheap:

1. Since `whoami` already reports the permission set, the SDK could refuse at
   client construction with "this token has no Sandboxes permissions; enable
   Sandboxes for the account" rather than deferring to a generic 403 later.
2. Say in the docs that a Token Factory key does not carry Sandboxes rights
   until Beta access is granted, and link the request button. The SDK quickstart
   reads as though the key you already have will work, and the console page that
   carries the button is a different surface from the documentation.

3. `NEBIUS_PROJECT_ID` is required alongside the key, and appears only in the
   console quickstart — not in the docs we were working from. A missing project
   id produces no distinct error either.

The confirmed `instance_max_concurrency: 50` does answer the concurrency
question we had posted to the forum — it matches the documented Beta limit and
it is per token, not per request.

Separately, `get_token_info` reports our key expiring the same day it was
issued, with the SDK printing "Token expires in 0 hours" on every client
construction. Token Factory inference calls with the same key work normally, so
either the expiry is specific to the Sandboxes view of the token or the warning
is miscalculating. Either way the message appears on stdout rather than through
`logging`, which is awkward for anything running non-interactively.
