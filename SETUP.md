# Setting up the Derivative Monthly Pricing tool

This tool reads broker statements and produces the month-end pricing sheet.
It runs on your own computer and opens in your web browser. Nothing you
upload leaves your machine except where noted under **New brokers** below.

You do not need to know anything about programming to use it. You do need
someone to help with the one-time setup in Part 1.

---

## Part 1 — One-time setup (you will need help with this)

Someone technical, or your IT team, needs to do these two things once. After
that you never have to think about them again.

**1. Install `uv`.** This is a small helper program the tool relies on. It
installs into your own user folder and does not need administrator rights.

**2. Install Claude Code and sign in.** This is only needed so the tool can
handle broker formats it has never seen before. Signing in to the Claude Code
extension in VS Code is enough — the tool finds it automatically. Everything
else works without it.

> If your Claude sign-in is a **personal** subscription rather than a company
> one, mention that to whoever set this up before you use the new-broker
> feature. It affects where statement data is allowed to go.

---

## Part 2 — Getting the tool onto your computer

**Recommended: GitHub Desktop.** Ask to be added to the project, then install
GitHub Desktop and use it to download the project folder. The advantage is
that when the tool is improved, you click one **Pull** button to get the
update — no files to re-copy.

**Alternative: a copied folder.** Someone can send you the folder directly.
This works, but you will not get fixes automatically, and over time your
version can produce different numbers from everyone else's. Prefer GitHub
Desktop if you can.

---

## Part 3 — Using it, day to day

Open the project folder and **double-click `start-app.cmd`**.

That's the whole thing. A black window opens, and after a moment your browser
opens with the tool in it.

Two things to know:

- **The first run takes a few minutes.** It is downloading what it needs.
  Later runs start in seconds.
- **Leave the black window open while you work.** It is what keeps the tool
  running. When you are finished, close it — that is how you shut down.

If something goes wrong, the black window will explain the problem in plain
language and give you a message to pass on. You will not get a wall of
technical output.

---

## New brokers

Most months you drop in the statements and the tool recognises every format.

When a genuinely new broker format appears, the tool uses Claude to draft a
way of reading it, and **shows you the result for approval before saving it**.
Nothing is saved without you agreeing to it.

Be aware: to do this, the tool sends a sample of the new statement — including
real cell values such as trade references and mark-to-market figures — to
Claude. This only happens for **unrecognised** formats, and only when you
choose to onboard one. The regular monthly run never sends anything anywhere.

---

## If you get stuck

The black window's message is the useful thing to report — copy it and send it
on. Two common ones:

| Message mentions | What it means |
|---|---|
| `uv is not installed` | Part 1 was not completed on this computer. |
| `failed at the uv sync step` | It could not download what it needs. Connect to the VPN and try again. |
