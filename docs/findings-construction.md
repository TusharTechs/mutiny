# Working out how to build the object first

Four of the forty-five pull requests with real behaviour to check produced no
verdict, and the reason was always the same: nothing the generator wrote could
construct the receiver. `InstanceState` wants a session behind it, `Job` wants a
scheduler, `TableDataElement` wants a table. The generator was being asked to
solve two problems inside one expression — *how do I build this* and *what
should I call on it* — and it failed at the first, so the second never happened.

So they are solved separately now. Before any probe is written, `construct.py`
looks for a **recipe**: a snippet that builds the receiver and is proven to run.

It is a loop rather than a prompt because the useful information only arrives by
executing things. Each attempt runs its candidates in the sandbox, reads what
actually failed, and escalates the evidence it asks for — the class definition
first, the repository's own tests only if that was not enough. Context costs
tokens, so it is fetched when the last attempt has proved it was needed.

## On the pull requests that produced nothing

    rich#4079      could not build a TableDataElement
                   built on attempt 1, from the class definition
                   0 -> 12 probes, behaviour preserved

    schedule#604   could not build a Job
                   built on attempt 1, from the class definition
                   0 -> 12 probes, behaviour preserved

    arrow#1264     0 -> 15 probes, behaviour change found
    arrow#1242     0 -> 15 probes, behaviour change found

Verdicts on the corpus go from **36 of 45 to 40 of 45**. The two arrow cases
were fixed by earlier work rather than by this loop; they are counted here
because they were part of the same gap.

## It is a fallback, and that is deliberate

Measured on targets where generation already worked, the recipe makes things
*worse*:

    cachetools Cache.__setitem__     18 probes -> 7 with a recipe

Anchoring every expression to one proven construction narrows what the model
explores. So the loop runs only when generation has produced nothing, which is
the case it was built for. This is the second time a helpful-looking addition
has cost yield where it was not needed — the first was showing mined tests to a
generator that did not need them — and the rule is the same: an escalation that
fires when nothing was wrong is a regression.

## What it still cannot build

    sqlalchemy ClauseAdapter.replace    three attempts, no working recipe
    tenacity BaseRetrying._run_wait     three attempts, no working recipe

Both need collaborators that need collaborators. A recipe for `ClauseAdapter`
means constructing a selectable, which means a table, which means metadata. The
loop escalates its evidence but does not yet decompose the problem — it asks for
the whole construction each time rather than building the pieces in order.

## Two bugs this found, both mine

**A correct recipe produced zero probes.** Generated probes are validated line
by line and imports are rejected outright, so a recipe beginning
`from cachetools import Cache` was discarded before it ran. The import was also
unnecessary: a probe is evaluated in the target module's own globals, where the
class is already a name. The recipe is reduced to one import-free line, and that
line is executed to prove it works rather than assuming it follows from the
multi-line version.

**`generate()` was handed an argument it does not accept** — for the second
time, identically. `awaitable` first, then `recipe`: each added to
`generate_validated` and to the call it makes, but not to `generate`. There was
a test for exactly this, and it listed the arguments by hand, so it missed the
second one. It now reads the call out of the source, and I checked it fails when
the argument is removed rather than trusting that it would.
