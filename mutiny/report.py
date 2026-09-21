"""Turn a verification into something a reviewer reads inside their pull request.

The command line renders the event stream as text and the browser renders it as
a page. This renders it as one Markdown comment, which is where review actually
happens -- nobody's working day includes pasting pull request URLs into a
website.

The governing rule is silence. A reviewer who is interrupted by a bot on every
pull request stops reading the bot, and most changes have nothing worth saying
about them: of 49 merged pull requests measured in `findings-pull-requests.md`,
twelve diverged and ten of those were changes the author had already announced
in the title. Reporting those is noise wearing the costume of diligence.

So the comment is posted when a behaviour difference is one the change does not
describe, and withheld otherwise.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Lets a later run find the comment it left last time instead of posting again.
MARKER = "<!-- mutiny-report -->"

DOCS = "https://github.com/TusharTechs/mutiny#how-it-works"


@dataclass
class Finding:
    function: str
    path: str = ""
    probes: int = 0
    described: bool | None = None
    summary: str = ""
    witnesses: list = field(default_factory=list)

    @property
    def undescribed(self) -> bool:
        return self.described is False


@dataclass
class Report:
    slug: str = ""
    base: str = ""
    head: str = ""
    functions: int = 0
    findings: list = field(default_factory=list)
    unprobeable: list = field(default_factory=list)
    seconds: float = 0.0
    cost: float = 0.0
    error: str = ""

    @property
    def undescribed(self) -> list:
        return [f for f in self.findings if f.undescribed]

    def worth_saying(self, when: str = "undescribed") -> bool:
        """Should this be posted at all?"""
        if self.error:
            return when == "any"
        if when == "never":
            return False
        if when == "any":
            return True
        return bool(self.undescribed)


def collect(events) -> Report:
    """Fold the event stream into a report."""
    report = Report()
    for event in events:
        kind = event.get("type")
        if kind == "fetched":
            report.slug = event.get("slug", "")
        elif kind == "review":
            report.base, report.head = event.get("base", ""), event.get("head", "")
        elif kind == "no_probes":
            report.unprobeable.append(event.get("function", ""))
        elif kind == "result":
            divergences = event.get("divergences") or []
            if divergences:
                report.findings.append(Finding(
                    function=event.get("function", ""),
                    probes=event.get("probes", 0),
                    described=event.get("described"),
                    summary=event.get("summary", ""),
                    witnesses=divergences,
                ))
        elif kind == "verdict":
            report.functions = event.get("functions", 0)
            report.seconds = event.get("seconds", 0.0)
            report.cost = event.get("cost", 0.0)
        elif kind == "error":
            report.error = event.get("message", "")
    return report


def _witness(divergence: dict) -> str:
    return (f"{divergence['input']}\n"
            f"# before:  {divergence['before']}\n"
            f"# after:   {divergence['after']}")


def render(report: Report, limit: int = 3) -> str:
    """The comment body."""
    if report.error:
        return (f"{MARKER}\n### MUTINY could not check this change\n\n"
                f"`{report.error}`\n\n"
                f"<sub>This is a problem with MUTINY, not with the change.</sub>")

    undescribed = report.undescribed
    lines = [MARKER]

    if not undescribed:
        lines.append("### MUTINY found no undescribed behaviour change")
        lines.append("")
        lines.append(
            f"Checked {report.functions} changed function"
            f"{'' if report.functions == 1 else 's'} by running both versions of "
            f"this code on generated inputs.")
        return "\n".join(lines)

    heading = ("### This change alters behaviour it does not mention"
               if len(undescribed) == 1 else
               f"### {len(undescribed)} functions behave differently in ways this "
               "change does not mention")
    lines += [heading, ""]

    for finding in undescribed:
        lines.append(f"#### `{finding.function}`")
        lines.append("")
        count = len(finding.witnesses)
        lines.append(
            f"{count} of {finding.probes} generated input"
            f"{'' if count == 1 else 's'} produce"
            f"{'s' if count == 1 else ''} a different result before and after "
            f"this change.")
        lines.append("")
        if finding.summary:
            lines.append(f"> {finding.summary}")
            lines.append("")
        lines.append("```python")
        for divergence in finding.witnesses[:limit]:
            lines.append(_witness(divergence))
        lines.append("```")
        extra = len(finding.witnesses) - limit
        if extra > 0:
            lines.append(f"<sub>…and {extra} more "
                         f"input{'' if extra == 1 else 's'} that disagree"
                         f"{'s' if extra == 1 else ''}.</sub>")
        lines.append("")

    described = [f for f in report.findings if f.described is True]
    if described:
        names = ", ".join(f"`{f.function}`" for f in described)
        lines.append(f"<details><summary>{len(described)} further behaviour "
                     f"change{'' if len(described) == 1 else 's'} that this change "
                     f"does describe</summary>\n\n{names}\n\n</details>")
        lines.append("")

    if report.unprobeable:
        names = ", ".join(f"`{n}`" for n in report.unprobeable[:6])
        lines.append(f"<sub>No input could be constructed for {names}, so those "
                     f"were not checked.</sub>")
        lines.append("")

    lines.append(
        f"<sub>Both versions were installed and executed in a Nebius sandbox, "
        f"never on a runner. {report.seconds:.0f}s, ${report.cost:.4f}. "
        f"Every line above is a result that was observed, not predicted — "
        f"paste one into a REPL to check it. [How this works]({DOCS})</sub>")
    return "\n".join(lines)
