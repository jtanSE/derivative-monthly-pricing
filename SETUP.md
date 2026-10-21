# Setting up the Derivative Monthly Pricing tool

This tool reads broker statements and produces the month-end pricing sheet.
It runs on your own computer and opens in your web browser. Nothing you
upload leaves your machine except where noted under **New brokers** below.

You do not need to know anything about programming to use it. You do need
someone to help with the one-time setup in Part 1.

---

## Part 1 — One-time setup

**1. Install `uv`.** This is a small helper program the tool relies on — the
only thing you must install. It goes into your own user folder and does not
need administrator rights. It brings its own copy of Python, so there is
nothing else to install.

**2. Restart the computer.** This step is not optional and it is the single
most common reason the tool fails to start. Installing `uv` does not make it
visible to programs that were already running, and Windows Explorer — which
is what launches the tool when you double-click it — is one of them. A
restart fixes this permanently.

**3. (Optional) Install Claude Code and sign in.** This is only needed so the
tool can handle broker formats it has never seen before. Signing in to the
Claude Code extension in VS Code is enough — the tool finds it automatically.
Everything else works without it.

> If your Claude sign-in is a **personal** subscription rather than a company
> one, mention that to whoever set this up before you use the new-broker
> feature. It affects where statement data is allowed to go.

---

## Part 2 — Getting the tool onto your computer

You will be sent a single `.zip` file. Three steps, in this order:

**1. Unblock it.** Right-click the `.zip` → **Properties** → tick **Unblock**
at the bottom → **OK**. Windows marks files that arrived by email or Teams as
untrusted and will silently refuse to run the launcher inside. Do this
*before* extracting — unblocking the zip covers everything inside it.

**2. Extract it somewhere with a short path.** Right-click → **Extract All**.
In the box that appears, **delete the folder name Windows suggests on the end**
so you do not end up with a folder inside a folder of the same name. Good
places:

- `C:\pricing-tool`
- `C:\Users\<your name>\Documents`

Do **not** extract into OneDrive, Teams, or a deeply nested Downloads folder.
Those paths are long on their own, and the tool needs room underneath itself
for the components it installs. The launcher checks this and will tell you if
the folder is too deep.

**3. Open the extracted folder** and continue to Part 3.

> **Updates:** because this arrived as a zip rather than through GitHub, you
> will not get fixes automatically. When a new version is sent to you, delete
> the old folder and repeat Part 2. Running an old copy is how two people end
> up with different numbers for the same month.

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

If the window shows a message and then closes, **do not worry about catching
it** — the same message is saved to a file called `SETUP-PROBLEM.txt` in the
project folder. Send that file on. That is all anyone needs to diagnose it.

| Message mentions | What it means | What to do |
|---|---|---|
| `One-time setup is needed` | Windows cannot see `uv` yet. | Restart the computer and try again. If it persists, `uv` needs reinstalling. |
| `buried too deep on the disk` | The folder path is too long. | Move the whole folder to `C:\pricing-tool` and try again. |
| `failed at the uv sync step` | It could not download what it needs. | Connect to the VPN and try again. |
| `The tool could not start` | Components installed, but the app itself errored. | Send a photo of the window plus `SETUP-PROBLEM.txt`. |
