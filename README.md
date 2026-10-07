# SWE-Xplorer

**Iterative Tree Search with Cross-Path Reconciliation for Autonomous Software Engineering**

SWE-Xplorer is a research framework for exploring test-time search and scaling
strategies for software-engineering agents. It replaces the usual single linear
agent trajectory with structured exploration over a tree of coding trajectories.

The system uses mini-SWE-agent as its lightweight backbone and adds:

- path-level best-first search over complete investigation trajectories;
- iteration-based pruning to balance exploration and exploitation;
- cross-path reconciliation to combine complementary evidence before pruning;
- action-specific rewards for reads, searches, edits, tests, and submissions;
- lightweight Git-based code-state restoration;
- deferred termination and recursive tournament voting for final patch selection.

This repository is the working research artifact for the **SWE-Xplorer** paper.
It also provides reproducible experiment setups for multiple agent scaffolds,
including the SWE-Xplorer tree-search agent, base mini-SWE-agent, OpenHands,
Claude Code, and OpenCode.

## Repository Map

| Path | Purpose |
| --- | --- |
| `src/minisweagent/agents/` | SWE-Xplorer search, reward-guided, and base agent implementations |
| `src/minisweagent/run/extra/` | SWE-bench batch and single-instance runners |
| `src/minisweagent/config/extra/` | SWE-Xplorer and SWE-bench experiment configurations |
| `deep-swe/` | Harbor/Pier workflows for base and tree-search agents on DeepSWE tasks |
| `experiments/openhands-swebench/` | OpenHands baseline on SWE-bench Verified |
| `experiments/claude-code-swebench/` | Claude Code baseline using GPT-5 mini on SWE-bench Verified |
| `experiments/opencode-swebench/` | OpenCode baseline using GPT-5 Mini on SWE-bench Verified |
| `swesearch/` | Official Moatless Tree Search implementation used for SWE-Search experiments |
| `pier/` | Pier evaluation framework, pinned as a submodule |
| `openhands/` | OpenHands release used by the baseline, pinned as a submodule |
| `swebench/` | SWE-bench evaluation tooling |
| `output/`, `evaluation/` | Local predictions, trajectories, and evaluation artifacts |

## Installation

Linux is recommended. On Windows, use WSL 2 with Docker Desktop configured for
Linux containers.

Requirements:

- Git
- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/)
- Docker for sandboxed benchmark execution
- provider credentials such as `OPENROUTER_API_KEY`

Clone with the pinned experiment dependencies:

```bash
git clone --recurse-submodules https://github.com/mahirlabibdihan/SWE-Xplorer.git
cd SWE-Xplorer
```

For an existing clone:

```bash
git pull
git submodule sync --recursive
git submodule update --init --recursive
```

Install the main project:

```bash
uv sync --extra dev
```

The `dev` extra includes the Hugging Face `datasets` package required by the
SWE-bench runners. Conda is not required; `uv` manages the project environment
directly.
Run project commands through `uv` without activating the environment:

```bash
uv run mini-extra --help
uv run mini --help
```

Alternatively, activate the environment once per shell and then use the
commands directly:

```bash
source .venv/bin/activate
mini-extra --help
```

On Windows PowerShell, activation is `.venv\Scripts\Activate.ps1`.

## SWE-bench Experiments

Put the required API credentials in your environment or a local `.env` file.
Tree search and the linear backbone use different inference commands and
different evaluation entry points.

### SWE-Xplorer tree search

The checked-in tree-search configuration is
`src/minisweagent/config/extra/swebench_ts.yaml`. Run tree-search inference on
SWE-bench Verified with:

```bash
mini-extra swebench-ts \
  --subset verified \
  --split test \
  --output output/verified.test.60/Qwen__Qwen2.5-7B-Instruct/
```

For an interactive single-instance run, use `mini-extra swebench-ts-single`.
Tree-search inference writes per-instance tree artifacts beneath the output
directory. Evaluate the directory with the tree-search harness:

```bash
python -m swebench.harness.run_evaluation_ts \
  --dataset_name princeton-nlp/SWE-bench_Verified \
  --predictions_dir output/verified.test.60/Qwen__Qwen2.5-7B-Instruct/ \
  --max_workers 5 \
  --run_id verified.test.60 \
  --report_dir evaluation \
  --cache_level instance
```

`run_evaluation_ts` evaluates candidate patches stored in the per-instance
`*.tree.json` artifacts and records pass/fail information back into the trees.

### Linear mini-SWE-agent backbone

Run the base agent on SWE-bench Verified with:

```bash
mini-extra swebench \
  --subset verified \
  --split test \
  --output output/verified.test.64/openai__gpt-5-mini/
```

The base runner produces a standard `preds.json`. Evaluate it with the official
SWE-bench harness:

```bash
python -m swebench.harness.run_evaluation \
  --dataset_name princeton-nlp/SWE-bench_Verified \
  --predictions_path output/verified.test.35/openai__gpt-5-mini/preds.json \
  --max_workers 5 \
  --run_id verified.test.35 \
  --report_dir evaluation \
  --cache_level instance
```

The run IDs in these examples are experiment labels. Keep each inference output,
evaluation `run_id`, model name, configuration, and step budget aligned in real
runs.

### SWE-Search baseline

The official Moatless Tree Search repository is pinned as the `swesearch/`
submodule. The following configuration runs SWE-Search on SWE-bench Verified
with GPT-5 Mini, a maximum branching factor of two, and a global budget of at
most 50 newly created child nodes:

```bash
cd swesearch
export OPENROUTER_API_KEY="<your-key>"

python -m moatless.benchmark.run_evaluation \
  --config gpt4o_mini \
  --split verified \
  --slice 0:10 \
  --no-index \
  --no-testbed \
  --model openrouter/openai/gpt-5-mini \
  --max-iterations 51 \
  --max-expansions 2 \
  --num-workers 1 \
  --evaluation-name swe-search-gpt-5-mini-verified-b2-e50
```

In this implementation, `max_iterations` limits the total number of nodes and
counts the root. Consequently, `--max-iterations 51` permits the root plus at
most 50 successful child-node expansions. `--max-expansions 2` limits each
node to two children; it does not create two children per search iteration.
Runs can finish before reaching the node budget because of another termination
condition, such as the cost limit, a completion threshold, or no remaining
expandable nodes. LLM calls are not equivalent to node expansions because
selection, value evaluation, feedback, and discrimination may make additional
model calls.

Use `--instance-ids <instance_id>` during initial smoke tests before launching
the complete Verified split. Use `--slice START:STOP[:STEP]` to select a stable
range from the ordered instance list; for example, `--slice 0:10` runs the
first ten Verified instances. The slice is also applied when explicit
`--instance-ids` are supplied.

The example uses `--no-index` because the historical prebuilt Moatless index
store is no longer available. Indexed search remains the default when this
flag is omitted. In no-index mode, the agent uses directory listing, exact
snippet search, and direct code viewing instead of semantic, class, and
function index searches.

The example also uses `--no-testbed`, which avoids the hosted Moatless testbed
service and removes in-search test execution from the agent. Omit the flag to
retain the original testbed-backed `RunTests` action when testbed access is
configured. With the flag enabled, evaluate the generated patch afterward
using the standard SWE-bench Docker harness.

For model-provider troubleshooting, add `--litellm-debug`. This enables
LiteLLM's in-process verbose debugging; redact credentials and sensitive
request content before sharing the resulting logs.

If the legacy LiteLLM version fails in its OpenRouter adapter, add
`--openrouter-direct`. The agent then calls OpenRouter through its
OpenAI-compatible endpoint using `OPENROUTER_API_KEY`; tree search and agent
settings remain unchanged.

The runner enables SWE-Search's LLM value function by default, using the same
model at temperature 0.0 as the reproducibility code. Use
`--no-value-function` only for an explicit reward-free ablation.

Export generated SWE-Search patches and evaluate them with the official
SWE-bench Docker harness:

```bash
cd swesearch
poetry run python -m moatless.benchmark.export_predictions \
  ./evals/<evaluation-name>

cd ../swebench
python -m swebench.harness.run_evaluation \
  --dataset_name princeton-nlp/SWE-bench_Verified \
  --split test \
  --predictions_path ../swesearch/evals/<evaluation-name>/predictions.json \
  --max_workers 5 \
  --run_id <evaluation-name> \
  --cache_level instance
```

Use `--redo-existing` to rerun the selected instances from scratch under an
existing evaluation name. Their previous artifacts are preserved under the
evaluation's `.redo_backups/` directory.

## DeepSWE Experiments

DeepSWE uses Harbor task directories and program-based verifiers rather than
Hugging Face SWE-bench rows. Pier prepares each repository, invokes the agent,
extracts its patch, and executes the task verifier.

From `deep-swe/`, run the SWE-Xplorer tree-search agent:

```bash
uv run --project ../pier pier run -p tasks \
  --agent tree-search-mini-swe-agent \
  --model openrouter/openai/gpt-5-mini \
  --env-file .env
```

Run the linear mini-SWE-agent backbone under the same evaluator:

```bash
uv run --project ../pier pier run -p tasks \
  --agent local-mini-swe-agent \
  --model openrouter/openai/gpt-5-mini \
  --env-file .env
```

### Network filesystems, jobs, and retries

On NFS and other network filesystems, Docker may reject Pier's bind mounts.
Use mountless logging so Pier copies logs and artifacts from each container
after it finishes:

```bash
PIER_MOUNT_LOGS=0 uv run --project ../pier pier run -p tasks \
  --agent tree-search-mini-swe-agent \
  --model openrouter/openai/gpt-5-mini \
  --env-file .env \
  --job-name full-gpt-5-mini \
  --n-concurrent 5
```

Reusing the same job name continues incomplete work and preserves completed
trials. `PIER_MOUNT_LOGS` is runtime state and is not saved in the job
configuration, so it must also prefix resume commands:

```bash
PIER_MOUNT_LOGS=0 uv run --project ../pier pier job resume \
  --job-path jobs/full-gpt-5-mini \
  --max-workers 2 \
  --filter-error-type EnvironmentStartTimeoutError
```

Use `--max-retries 2` on `pier run` to retry eligible exceptions during a new
run. A completed trial that produced no patch is not automatically considered
retryable.

Use `--force-build` after changing the installed agent or adapter. Omit it when
continuing a large job unless a rebuild is necessary: forcing a build disables
Docker's build cache and also rebuilds Pier's egress-proxy image, making runs
more vulnerable to transient package-mirror failures.

See [`deep-swe/README.md`](deep-swe/README.md) for single-task runs, sampling,
network-filesystem notes, artifacts, and verifier behavior.

## OpenHands Baseline

The OpenHands experiment uses `CodeActAgent`, GPT-5 mini through OpenRouter, a
50-iteration limit, and SWE-bench Verified:

```bash
bash experiments/openhands-swebench/setup.sh
cp experiments/openhands-swebench/.env.example \
  experiments/openhands-swebench/.env
# Add OPENROUTER_API_KEY to the new .env file.

EVAL_LIMIT=10 NUM_WORKERS=2 \
  bash experiments/openhands-swebench/run.sh
```

Evaluate its patches with:

```bash
bash experiments/openhands-swebench/evaluate.sh
```

Full instructions are in
[`experiments/openhands-swebench/README.md`](experiments/openhands-swebench/README.md).

## Claude Code Baseline

This experiment runs the official Claude Code CLI through Pier while routing
all model roles to `openai/gpt-5-mini` through OpenRouter. Each task has a
50-turn limit.

```bash
bash experiments/claude-code-swebench/setup.sh
cp experiments/claude-code-swebench/.env.example \
  experiments/claude-code-swebench/.env
# Add OPENROUTER_API_KEY to the new .env file.

JOB_NAME=claude-code-first10 N_TASKS=10 N_CONCURRENT=2 \
  bash experiments/claude-code-swebench/run.sh
```

Omit `SAMPLE_SEED` to take tasks in alphabetical instance-ID order. Set it
explicitly only for a reproducible shuffled sample. To run a fixed range:

```bash
JOB_NAME=claude-code-10-20 N_CONCURRENT=2 \
  bash experiments/claude-code-swebench/run.sh --slice 10:20
```

Claude Code jobs are immutable. If `JOB_NAME` already exists, the wrapper asks
whether to delete it and start again; use a different name to preserve the
existing run. See
[`experiments/claude-code-swebench/README.md`](experiments/claude-code-swebench/README.md)
for prediction export and official SWE-bench evaluation.

## OpenCode Baseline

This experiment runs OpenCode through Pier with `openai/gpt-5-mini` on
OpenRouter. Its primary agent is capped at 50 agentic steps and cannot spawn
subagents with independent budgets:

```bash
bash experiments/opencode-swebench/setup.sh
cp experiments/opencode-swebench/.env.example \
  experiments/opencode-swebench/.env
# Add OPENROUTER_API_KEY to the new .env file.

JOB_NAME=opencode-first10 N_TASKS=10 N_CONCURRENT=2 \
  bash experiments/opencode-swebench/run.sh
```

Run a fixed alphabetical range with:

```bash
JOB_NAME=opencode-10-20 N_CONCURRENT=2 \
  bash experiments/opencode-swebench/run.sh --slice 10:20
```

Full setup, budget, export, and evaluation instructions are in
[`experiments/opencode-swebench/README.md`](experiments/opencode-swebench/README.md).

## Interactive mini-SWE-agent

The upstream-compatible linear agent remains available for interactive use and
controlled backbone comparisons:

```bash
uv run mini --model openrouter/openai/gpt-5-mini
```

The original mini-SWE-agent documentation is available at
[mini-swe-agent.com](https://mini-swe-agent.com/latest/). This fork retains the
small bash-only agent design while extending it with the SWE-Xplorer search
stack and experiment infrastructure.

## Reproducibility Notes

- Record the parent repository commit and both submodule commits for every run.
- Keep model names, search budgets, step limits, and sampling seeds with results.
- Do not commit `.env` files or API keys.
- Full benchmark runs are costly and can consume substantial Docker storage.
- Use the same task ordering and evaluator version for comparisons.
- Pier writes per-trial trajectories, patches, metrics, and verifier artifacts.


## Acknowledgments

SWE-Xplorer builds on [mini-SWE-agent](https://github.com/SWE-agent/mini-swe-agent), [SWE-bench](https://github.com/SWE-bench/SWE-bench) and [WebOperator](https://github.com/kagnlp/WebOperator).

## License

This repository retains the licensing terms of the upstream mini-SWE-agent project. Submodules and third-party components are governed by their respective licenses.
