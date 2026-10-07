# SWE-Xplorer for VS Code

**SWE-X**plorer in your sidebar: describe a bug or a change, watch the tree search work, and review the winning patch in VS Code's diff editor.

- **Chat in the sidebar.** The search streams in as reasoning (steps, backtracks, reconcile, prune); the winning solution's explanation is the reply. Follow-ups continue the same search tree.
- **Review like a pull request.** Changed files open in VS Code's multi-file diff. Keep or reject the result; rejecting reverts exactly the agent's patch.
- **Ask about code.** Select code and press `Ctrl+Alt+X` (or right-click → *Ask SWE-Xplorer About This Code*). The selection and its problems are attached to your request.
- **See the search.** *Show Search Tree* opens the live tree and git graph in an editor tab.

## Requirements

- A clone of the SWE-Xplorer repository, installed into a Python 3.10+:
  ```bash
  git clone --recurse-submodules https://github.com/mahirlabibdihan/SWE-Xplorer.git
  cd SWE-Xplorer
  pip install -e .
  ```
  The extension finds that Python automatically (`python`, `python3`, or `py` on Windows). If you installed it into a
  virtualenv (e.g. with `uv sync`), set `sweXplorer.pythonPath` to that environment's Python
  (`<clone>/.venv/Scripts/python.exe` on Windows, `<clone>/.venv/bin/python` elsewhere).
- A git repository open as the workspace. The agent edits it in place; your branch and history are left as they were.
- An API key (OpenRouter, OpenAI, Anthropic, Gemini or DeepSeek): *SWE-Xplorer: Set API Key*.
- Windows: Git for Windows (the agent's commands run in Git Bash; WSL is not needed).

## Settings

| Setting | Default | |
|---|---|---|
| `sweXplorer.config` | `swe_xplorer.yaml` | Agent config: built-in name or YAML path |
| `sweXplorer.pythonPath` | auto | Python that runs the backend |
| `sweXplorer.serverUrl` | | Attach to a running GUI server instead of starting one |
| `sweXplorer.rewardSameAsPolicy` | `true` | Use the chosen model as reward model |

## Build

```bash
cd vscode-extension
npm run package        # creates swe-xplorer-<version>.vsix
code --install-extension swe-xplorer-0.1.0.vsix
```
