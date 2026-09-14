"use strict";

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};

let stream = null;
let started = 0;
let ticker = null;

// A run is thirty to ninety seconds of work that used to show nothing at all:
// a fixed row of stage pills, most of which never lit up for a pull request.
// What it shows now is what it is actually doing, and how long it has been
// doing it, so a wait reads as progress rather than as a hang.
function clock() {
  if (!started) return;
  $("elapsed").textContent = `${((Date.now() - started) / 1000).toFixed(0)}s`;
}

function activity(text) {
  if (!started) {
    started = Date.now();
    ticker = setInterval(clock, 200);
  }
  $("activity-text").textContent = text;
  $("activity").hidden = false;
  clock();
}

function milestone(text, note) {
  const li = el("li");
  li.appendChild(el("span", "what", text));
  if (note) li.appendChild(el("span", "note", note));
  li.appendChild(el("span", "at", started
    ? `${((Date.now() - started) / 1000).toFixed(1)}s` : ""));
  $("timeline").appendChild(li);
}

function stopClock() {
  if (ticker) clearInterval(ticker);
  ticker = null;
  $("activity").hidden = true;
}

function runHeader(parts) {
  const node = $("run-header");
  node.replaceChildren();
  parts.forEach((part, i) => {
    if (i) node.appendChild(el("span", "sep", "·"));
    node.appendChild(part.strong ? el("b", null, part.text) : el("span", null, part.text));
  });
}

function renderDiff(lines) {
  const pre = $("diff");
  pre.replaceChildren();
  for (const line of lines) {
    const cls = line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : "ctx";
    const span = el("span", cls, line + "\n");
    pre.appendChild(span);
  }
  $("diff-panel").hidden = false;
}

function fork(side, index, total, state) {
  const grid = $(side === "before" ? "forks-before" : "forks-after");
  while (grid.children.length < total) {
    grid.appendChild(el("div", "fork", String(grid.children.length + 1)));
  }
  const cell = grid.children[index];
  if (cell) cell.className = "fork " + state;
  $("forks-panel").hidden = false;
}

function resetForks() {
  $("forks-before").replaceChildren();
  $("forks-after").replaceChildren();
  $("forks-panel").hidden = true;
}

// Two reprs that differ in one field are a spot-the-difference puzzle:
//   before Version(major=1, minor=0, patch=0, prerelease=None, build='alpha')
//   after  Version(major=1, minor=0, patch=0, prerelease=None, build='alpha.0')
// Trimming the shared head and tail leaves the part that actually changed, which
// is the whole point of showing the pair at all.
function split(before, after) {
  let head = 0;
  const max = Math.min(before.length, after.length);
  while (head < max && before[head] === after[head]) head += 1;

  let tail = 0;
  while (tail < max - head
         && before[before.length - 1 - tail] === after[after.length - 1 - tail]) {
    tail += 1;
  }

  // Nothing shared, or shared so little that highlighting adds noise rather
  // than removing it — show the values plainly.
  if (head + tail < 4) return [[ "", before, "" ], [ "", after, "" ]];

  return [
    [before.slice(0, head), before.slice(head, before.length - tail),
     before.slice(before.length - tail)],
    [after.slice(0, head), after.slice(head, after.length - tail),
     after.slice(after.length - tail)],
  ];
}

function renderResult(e) {
  const box = el("div", "finding"
    + (e.divergences.length ? "" : " clean")
    + (e.described === false ? " alarming" : ""));
  box.appendChild(el("h3", null, e.function));

  if (!e.divergences.length) {
    box.appendChild(el("p", "agreed",
      `Behaviour preserved — ${e.agreed} of ${e.probes} probes ran on both versions and agreed on every one.`));
    $("results").appendChild(box);
    return;
  }

  // A behaviour change is only news when the change did not predict it. A pull
  // request titled "fix X returning the wrong value" changing what X returns is
  // the fix working, and saying "behaviour changed" about it tells a reviewer
  // nothing they did not already know.
  if (e.described === false) {
    box.appendChild(el("p", "flag alarm",
      "This is not described by the change."));
  } else if (e.described === true) {
    box.appendChild(el("p", "flag expected",
      "The change describes this. It is doing what it says."));
  }
  box.appendChild(el("div", "where",
    `${e.divergences.length} of ${e.probes} probes disagree`));
  if (e.summary) box.appendChild(el("p", "summary", e.summary));

  for (const d of e.divergences.slice(0, 4)) {
    const w = el("div", "witness");
    w.appendChild(el("code", "probe", d.input));
    const [beforeParts, afterParts] = split(String(d.before), String(d.after));
    for (const [side, parts] of [["before", beforeParts], ["after", afterParts]]) {
      const row = el("div", "row " + side);
      row.appendChild(el("b", null, side));
      const value = el("span", "value");
      value.appendChild(el("span", "same", parts[0]));
      value.appendChild(el("mark", null, parts[1]));
      value.appendChild(el("span", "same", parts[2]));
      row.appendChild(value);
      w.appendChild(row);
    }
    box.appendChild(w);
  }
  if (e.divergences.length > 4) {
    box.appendChild(el("div", "where", `…and ${e.divergences.length - 4} more`));
  }
  $("results").appendChild(box);
}

function renderVerdict(e) {
  const box = $("verdict");
  box.className = "verdict " + (e.changed ? "changed" : "clean");
  box.replaceChildren();
  const text = e.functions === 0
    ? "Nothing to verify in this change."
    : e.changed
      ? `${e.findings} of ${e.functions} changed function${e.functions > 1 ? "s" : ""} behave differently.`
      : `No behaviour change detected across ${e.functions} function${e.functions > 1 ? "s" : ""}.`;
  box.appendChild(el("strong", null, text));
  box.appendChild(el("span", "meta", `${e.seconds}s · $${Number(e.cost).toFixed(4)}`));
  box.hidden = false;
}

function begin(url, button) {
  if (stream) stream.close();
  for (const b of document.querySelectorAll(".card")) {
    b.setAttribute("aria-pressed", String(b === button));
    b.disabled = true;
  }
  $("url-go").disabled = true;
  $("url-error").hidden = true;
  $("run").hidden = false;
  $("run-header").replaceChildren();
  $("results").replaceChildren();
  $("verdict").hidden = true;
  $("diff-panel").hidden = true;
  resetForks();
  return new EventSource(url);
}

function startUrl(url) {
  const source = begin(`/api/run-url?url=${encodeURIComponent(url)}`, null);
  wire(source);
}

function start(id, button) {
  wire(begin(`/api/run/${encodeURIComponent(id)}`, button));
}

function wire(source) {
  stream = source;
  started = 0;
  if (ticker) clearInterval(ticker);
  $("timeline").replaceChildren();
  activity("starting");
  let lastSide = null;
  let currentFunction = null;
  let forksSeen = 0;

  stream.onmessage = (message) => {
    const e = JSON.parse(message.data);
    switch (e.type) {
      case "status":
        activity(e.text || e.stage);
        break;
      case "target":
        runHeader([{ text: e.repo }, { text: e.path },
                   { text: e.function, strong: true }]);
        break;
      case "flaky":
        milestone("discarded as flaky", `${e.count} probe${e.count === 1 ? "" : "s"}`);
        break;
      case "review": {
        const skipped = e.skipped.length ? `${e.skipped.length} skipped` : null;
        const parts = [{ text: e.repo }, { text: `${e.base}..${e.head}` },
          { text: `${e.targets.length} changed function${e.targets.length === 1 ? "" : "s"}`,
            strong: true }];
        if (skipped) parts.push({ text: skipped });
        runHeader(parts);
        milestone("change resolved",
          `${e.targets.length} function${e.targets.length === 1 ? "" : "s"} to check`);
        break;
      }
      case "diff":
        renderDiff(e.lines);
        milestone("rewrite applied", `${e.lines.length} diff lines`);
        break;
      case "checkpoint":
        milestone("sandbox ready", `${e.kib} KiB · ${e.mode}`);
        activity("generating probes");
        break;
      case "probes":
        milestone("probes generated", `${e.count} inputs`);
        activity(`running ${e.count} probes against both versions`);
        break;
      case "fork":
        if (e.side !== lastSide) {
          lastSide = e.side;
          forksSeen = 0;
          activity(`running the ${e.side} version across ${e.total} sandbox forks`);
        }
        fork(e.side, e.index, e.total, e.state);
        if (e.state === "done") forksSeen += 1;
        break;
      case "function":
        resetForks();
        currentFunction = e.name;
        milestone("checking", e.name);
        break;
      case "result":
        renderResult(e);
        milestone(e.divergences.length ? "behaviour changed" : "behaviour preserved",
                  `${e.function} · ${e.probes} probes`);
        break;
      case "verdict":
        renderVerdict(e);
        milestone("done", `$${(e.cost || 0).toFixed(4)}`);
        stopClock();
        break;
      case "no_probes":
        $("results").appendChild(el("p", "agreed",
          `No usable probes were generated for ${e.function}.`));
        break;
      case "fetched":
        runHeader([{ text: e.pull_request ? "pull request" : "repository" },
                   { text: e.slug, strong: true }]);
        milestone("fetched from GitHub", e.slug);
        activity(e.pull_request ? "working out what the change touched"
                                : "choosing a function to rewrite");
        break;
      case "error":
        stopClock();
        $("url-error").textContent = e.message;
        $("url-error").hidden = false;
        $("results").appendChild(el("p", "agreed", e.message));
        break;
    }
  };

  const finish = () => {
    stopClock();
    if (stream) stream.close();
    stream = null;
    for (const b of document.querySelectorAll(".card")) b.disabled = false;
    $("url-go").disabled = false;
  };
  stream.addEventListener("end", finish);
  stream.onerror = finish;
}

// Examples are ordinary GitHub URLs, the same ones a visitor can paste in.
// Showing the slug makes that obvious rather than implying a private fixture.
function slugOf(url) {
  const m = /github\.com\/([\w.-]+)\/([\w.-]+?)(?:\/pull\/(\d+))?\/?$/.exec(url || "");
  if (!m) return "";
  return `${m[1]}/${m[2]}` + (m[3] ? `#${m[3]}` : "");
}

async function boot() {
  const res = await fetch("/api/examples");
  const data = await res.json();

  const badge = $("sandbox-badge");
  badge.textContent = data.sandboxes.available
    ? `sandboxes ready · ${data.sandboxes.detail}`
    : "sandboxes unavailable";
  badge.className = "badge " + (data.sandboxes.available ? "ok" : "bad");

  if (data.budget && data.budget.exhausted) {
    const note = $("budget-note");
    if (note) {
      note.textContent =
        "This demo has reached its spending limit. Clone the repo and run it " +
        "with your own Nebius key.";
      note.hidden = false;
    }
  }

  $("url-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const url = $("url-input").value.trim();
    if (url) startUrl(url);
  });

  const cards = $("cards");
  cards.replaceChildren();
  for (const example of data.examples) {
    const button = el("button", "card");
    button.type = "button";
    button.setAttribute("aria-pressed", "false");
    button.appendChild(el("span", "repo", slugOf(example.url)));
    button.appendChild(el("h3", null, example.title));
    button.appendChild(el("p", null, example.blurb));
    button.addEventListener("click", () => start(example.id, button));
    cards.appendChild(button);
  }
}

boot();
