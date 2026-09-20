#!/usr/bin/env python3

"""Run mini-SWE-agent on SWE-bench instances in batch mode."""
# Read this first: https://mini-swe-agent.com/latest/usage/swebench/  (usage docs)

import json
import random
import re
import threading
import time
import traceback
from pathlib import Path

import typer
import yaml
from datasets import load_dataset
from rich.live import Live
from rich.markup import escape as rich_escape
from jinja2 import StrictUndefined, Template

from minisweagent import Environment
from minisweagent.agents.default import DefaultAgent
from minisweagent.config import builtin_config_dir, get_config_path
from minisweagent.environments import get_environment
from minisweagent.models import get_model
from minisweagent.run.extra.swebench import pull_docker_images
from minisweagent.run.extra.utils.batch_progress import RunBatchProgressManager
from minisweagent.run.utils.save import save_traj
from minisweagent.utils.log import add_file_handler, logger, set_instance_file_handler
from minisweagent.agents.tree_search_agent import TreeSearchAgent
from minisweagent.agents.single_action_agent import SingleActionAgent
from minisweagent.agents.reward_model import RewardModel

_HELP_TEXT = """Run mini-SWE-agent on SWEBench instances.

[not dim]
More information about the usage: [bold green]https://mini-swe-agent.com/latest/usage/swebench/[/bold green]
[/not dim]
"""

app = typer.Typer(rich_markup_mode="rich", add_completion=False)

DATASET_MAPPING = {
    "full": "princeton-nlp/SWE-Bench",
    "verified": "princeton-nlp/SWE-Bench_Verified",
    "lite": "princeton-nlp/SWE-Bench_Lite",
    "multimodal": "princeton-nlp/SWE-Bench_Multimodal",
    "multilingual": "swe-bench/SWE-Bench_Multilingual",
    "smith": "SWE-bench/SWE-smith",
    "_test": "klieret/swe-bench-dummy-test-dataset",
    "verified_mini": "MariusHobbhahn/swe-bench-verified-mini",
    "pro": "ScaleAI/SWE-bench_Pro",
}


def get_instance_exit_status(traj_path: Path) -> str | None:
    """Read info.exit_status from a trajectory file if it exists and is valid JSON."""
    if not traj_path.exists():
        return None
    try:
        data = json.loads(traj_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data.get("info", {}).get("exit_status")

_OUTPUT_FILE_LOCK = threading.Lock()
_REPRODUCTION_LOCK = threading.Lock()


def _reproduction_patch_path(reproductions_dir: Path, instance_id: str) -> Path:
    return reproductions_dir / f"{instance_id}.patch"


def _load_reproduction_results(reproductions_dir: Path) -> list[dict]:
    """Load reproduction results from per-instance patch files."""
    reproduction_results: list[dict] = []

    for reproduction_patch_file in sorted(reproductions_dir.glob("*.patch")):
        reproduction_results.append(
            {
                "instance_id": reproduction_patch_file.stem,
                "reproduction_patch": reproduction_patch_file.read_text(encoding="utf-8"),
            }
        )

    return reproduction_results

class ProgressTrackingAgent(TreeSearchAgent):
    """Simple wrapper around TreeSearchAgent that provides progress updates."""

    def __init__(self, *args, progress_manager: RunBatchProgressManager, instance_id: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self.progress_manager: RunBatchProgressManager = progress_manager
        self.instance_id = instance_id

    def step(self) -> dict:
        """Override step to provide progress updates."""
        self.progress_manager.update_instance_status(
            self.instance_id, f"Step {self.n_expanded + 1:3d} (${self.model.cost+(self.reward_model.model.cost if hasattr(self, 'reward_model') else 0.0):.2f})"
        )
        return super().step()


class ProgressTrackingReproductionAgent(SingleActionAgent):
    """Wrapper around SingleActionAgent that provides progress updates."""

    def __init__(self, *args, progress_manager: RunBatchProgressManager, instance_id: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self.progress_manager: RunBatchProgressManager = progress_manager
        self.instance_id = instance_id

    def step(self) -> dict:
        """Override step to provide progress updates."""
        self.progress_manager.update_instance_status(
            self.instance_id, f"[Repro] Step {self.n_expanded + 1:3d} (${self.model.cost:.2f})"
        )
        return super().step()


def get_swebench_docker_image_name(instance: dict) -> str:
    """Get the image name for a SWEBench instance."""
    image_name = instance.get("image_name", None)
    if image_name is None:
        docker_tag = instance.get("dockerhub_tag", None)
        if docker_tag is not None:
            image_name = f"docker.io/jefzda/sweap-images:{docker_tag}"
        else:
            # Docker doesn't allow double underscore, so we replace them with a magic token
            iid = instance["instance_id"]
            id_docker_compatible = iid.replace("__", "_1776_")
            image_name = f"docker.io/swebench/sweb.eval.x86_64.{id_docker_compatible}:latest".lower()
            

    return image_name


def get_sb_environment(config: dict, instance: dict) -> Environment:
    # Make a copy to avoid mutating the original config across iterations
    import copy
    env_config = copy.deepcopy(config.get("environment", {}))
    env_config["environment_class"] = env_config.get("environment_class", "docker")
    
    # Build checkpoint path fresh for this instance
    if env_config.get("checkpoint") is not None:
        base_checkpoint = env_config["checkpoint"]
        # Ensure base path ends with / for proper concatenation
        if not base_checkpoint.endswith("/"):
            base_checkpoint += "/"
        env_config["checkpoint"] = base_checkpoint + f"{instance['instance_id']}/{instance['instance_id']}.tree.json"
    
    image_name = get_swebench_docker_image_name(instance)
    if env_config["environment_class"] == "docker" or env_config["environment_class"] == "dummy":
        env_config["image"] = image_name
    elif env_config["environment_class"] == "singularity":
        env_config["image"] = "docker://" + image_name
    env = get_environment(env_config)
    if startup_command := config.get("run", {}).get("env_startup_command"):
        startup_command = Template(startup_command, undefined=StrictUndefined).render(**instance)
        out = env.execute(startup_command)
        if out["returncode"] != 0:
            raise RuntimeError(f"Error executing startup command: {out}")
    return env


def update_preds_file(output_path: Path, instance_id: str, model_name: str, result: str):
    """Update the output JSON file with results from a single instance."""
    with _OUTPUT_FILE_LOCK:
        output_data = {}
        if output_path.exists():
            output_data = json.loads(output_path.read_text())
        output_data[instance_id] = {
            "model_name_or_path": model_name,
            "instance_id": instance_id,
            "model_patch": result,
        }
        output_path.write_text(json.dumps(output_data, indent=2))


def should_redo_existing_instance(instance_result: dict) -> bool:
    """Return True when an existing result has a non-empty invalid model_patch."""
    model_patch = instance_result.get("model_patch", "")
    if not model_patch:
        return False
    return not model_patch.lstrip().startswith("diff --git")


def should_redo_existing_reproduction(reproduction_result: dict) -> bool:
    """Return True when an existing reproduction has a non-empty invalid reproduction_patch."""
    reproduction_patch = reproduction_result.get("reproduction_patch", "")
    if not reproduction_patch:
        return False
    return not reproduction_patch.lstrip().startswith("diff --git")


def remove_from_preds_file(output_path: Path, instance_id: str):
    """Remove an instance from the predictions file."""
    if not output_path.exists():
        return

    with _OUTPUT_FILE_LOCK:
        output_data = json.loads(output_path.read_text(encoding="utf-8"))

        if instance_id in output_data:
            del output_data[instance_id]
            output_path.write_text(
                json.dumps(output_data, indent=2, ensure_ascii=False),
                encoding="utf-8"
            )


def update_reproductions_file(
    reproductions_dir: Path,
    instance_id: str,
    model_name: str,
    reproduction_patch: str
):
    """Write the reproduction patch for a single instance to <instance_id>.patch."""
    reproduction_file = _reproduction_patch_path(reproductions_dir, instance_id)
    reproduction_file.parent.mkdir(parents=True, exist_ok=True)

    patch_content = (
        reproduction_patch if reproduction_patch.endswith("\n")
        else reproduction_patch + "\n"
    )

    with _REPRODUCTION_LOCK:
        reproduction_file.write_text(patch_content, encoding="utf-8")


def remove_from_reproductions_file(reproductions_dir: Path, instance_id: str):
    """Remove an instance's patch file."""
    reproduction_file = _reproduction_patch_path(reproductions_dir, instance_id)
    with _REPRODUCTION_LOCK:
        reproduction_file.unlink(missing_ok=True)


def get_reproduction_patch_for_instance(reproductions_dir: Path, instance_id: str) -> str:
    """Return the stored reproduction patch for an instance, or an empty string if not found."""
    reproduction_file = _reproduction_patch_path(reproductions_dir, instance_id)
    if reproduction_file.exists():
        return reproduction_file.read_text(encoding="utf-8")
    return ""


def reproduce_instance(
    instance: dict,
    output_dir: Path,
    reproductions_dir: Path,
    config: dict,
    repro_config: dict,
    progress_manager: RunBatchProgressManager,
) -> str | None:
    """Reproduce a single SWEBench instance using SingleActionAgent."""
    instance_id = instance["instance_id"]
    instance_dir = output_dir / instance_id
    
    # Keep existing reproduction entry until we have a replacement result.
    # (instance_dir / f"{instance_id}.reproduction.traj.json").unlink(missing_ok=True)
    
    model = get_model(config=repro_config.get("model", {}))
    task = instance["problem_statement"]
    
    progress_manager.update_instance_status(instance_id, "[Reproduction] Pulling/starting docker")
    
    agent = None
    extra_info = None
    exit_status = None
    result = ""
    
    try:
        instance_dir.mkdir(parents=True, exist_ok=True)
        # Clear the log file for this instance if it already exists
        repro_log_path = instance_dir / "reproduction.log"
        if repro_log_path.exists():
            repro_log_path.unlink()
        set_instance_file_handler(repro_log_path)
        
        env = get_sb_environment(repro_config, instance)
        agent = ProgressTrackingReproductionAgent(
            model,
            env,
            progress_manager=progress_manager,
            instance_id=instance_id,
            **repro_config.get("agent", {}),
        )
        exit_status, result = agent.run(task)
        if result and not result.lstrip().startswith("diff --git"):
            raise ValueError(f"Invalid reproduction patch format for instance {instance_id}: {result[:200]}")
        update_reproductions_file(reproductions_dir, instance_id, model.config.model_name, result)
    except Exception as e:
        logger.error(f"Error reproducing instance {rich_escape(instance_id)}: {rich_escape(str(e))}", exc_info=True)
        exit_status, result = type(e).__name__, str(e)
        extra_info = {"traceback": traceback.format_exc()}
    finally:
        save_traj(
            agent,
            instance_dir / f"{instance_id}.reproduction.traj.json",
            exit_status=exit_status,
            result=result,
            extra_info=extra_info,
            instance_id=instance_id,
            print_fct=logger.info,
        )
        # Save reproduction tree
        if agent is not None and hasattr(agent, 'tree_root') and agent.tree_root is not None:
            with (instance_dir / f"{instance_id}.reproduction.tree.json").open("w", encoding="utf-8") as f:
                json.dump(agent.tree_root.to_tree(), f, indent=2, ensure_ascii=False)

    return exit_status


def process_instance(
    instance: dict,
    output_dir: Path,
    config: dict,
    progress_manager: RunBatchProgressManager,
    reproduction_patch: str = "",
) -> str | None:
    """Process a single SWEBench instance."""
    instance_id = instance["instance_id"]
    instance_dir = output_dir / instance_id
    # Keep existing prediction entry until we have a replacement result.
    # (instance_dir / f"{instance_id}.traj.json").unlink(missing_ok=True)
    model = get_model(config=config.get("model", {}))
    reward_config = config.get("reward_model", {})
    reward_model = RewardModel(
        get_model(config=reward_config),
        use_combined_scoring=reward_config.get("use_combined_scoring", True),
        max_retries=reward_config.get("max_retries", 3)
    )
    task = instance["problem_statement"]

    progress_manager.update_instance_status(instance_id, "[Fixer] Pulling/starting docker")

    agent = None
    extra_info = None
    exit_status = None
    result = ""

    try:
        instance_dir.mkdir(parents=True, exist_ok=True)
        # clear the log file for this instance if it already exists
        if (instance_dir / "experiment.log").exists():
            (instance_dir / "experiment.log").unlink()
        set_instance_file_handler(instance_dir / "experiment.log")
        env = get_sb_environment(config, instance)
        # if dummy environment is used, we pass the previous .tree.json file to the agent to use it as checkpoint. Agent will just simulate the tree search without actually executing any command or calculating reward.
        agent_config = {**config.get("agent", {}), "reproduction_patch": reproduction_patch}
        agent = ProgressTrackingAgent(
            model,
            env,
            reward_model,
            progress_manager=progress_manager,
            instance_id=instance_id,
            **agent_config,
        )
        exit_status, result = agent.run(task)
        update_preds_file(output_dir / "preds.json", instance_id, model.config.model_name, result)
    except Exception as e:
        logger.error(f"Error processing instance {rich_escape(instance_id)}: {rich_escape(str(e))}", exc_info=True)
        exit_status, result = type(e).__name__, str(e)
        extra_info = {"traceback": traceback.format_exc()}
    finally:
        save_traj(
            agent,
            instance_dir / f"{instance_id}.traj.json",
            exit_status=exit_status,
            result=result,
            extra_info=extra_info,
            instance_id=instance_id,
            print_fct=logger.info,
        )
        # save tree
        if agent is not None:
            with (instance_dir / f"{instance_id}.tree.json").open("w", encoding="utf-8") as f:
                json.dump(agent.tree_root.to_tree(), f, indent=2, ensure_ascii=False)
            with (instance_dir / f"{instance_id}.rtv.json").open("w", encoding="utf-8") as f:
                json.dump(agent.rtv, f, indent=2, ensure_ascii=False)

    return exit_status, result


def filter_instances(
    instances: list[dict], *, filter_spec: str, slice_spec: str = "", shuffle: bool = False
) -> list[dict]:
    """Filter and slice a list of SWEBench instances."""
    if shuffle:
        instances = sorted(instances.copy(), key=lambda x: x["instance_id"])
        random.seed(42)
        random.shuffle(instances)
    before_filter = len(instances)
    instances = [instance for instance in instances if re.match(filter_spec, instance["instance_id"])]
    if (after_filter := len(instances)) != before_filter:
        logger.info(f"Instance filter: {before_filter} -> {after_filter} instances")
    if slice_spec:
        values = [int(x) if x else None for x in slice_spec.split(":")]
        instances = instances[slice(*values)]
        if (after_slice := len(instances)) != before_filter:
            logger.info(f"Instance slice: {before_filter} -> {after_slice} instances")
    return instances


# fmt: off
@app.command(help=_HELP_TEXT)
def main(
    subset: str = typer.Option("lite", "--subset", help="SWEBench subset to use or path to a dataset", rich_help_panel="Data selection"),
    split: str = typer.Option("dev", "--split", help="Dataset split", rich_help_panel="Data selection"),
    slice_spec: str = typer.Option("", "--slice", help="Slice specification (e.g., '0:5' for first 5 instances)", rich_help_panel="Data selection"),
    filter_spec: str = typer.Option("", "--filter", help="Filter instance IDs by regex", rich_help_panel="Data selection"),
    shuffle: bool = typer.Option(False, "--shuffle", help="Shuffle instances", rich_help_panel="Data selection"),
    output: str = typer.Option("output/debug", "-o", "--output", help="Output directory", rich_help_panel="Basic"),
    workers: int = typer.Option(1, "-w", "--workers", help="Number of worker threads for parallel processing", rich_help_panel="Basic"),
    model: str | None = typer.Option(None, "-m", "--model", help="Model to use", rich_help_panel="Basic"),
    model_class: str | None = typer.Option(None, "-c", "--model-class", help="Model class to use (e.g., 'anthropic' or 'minisweagent.models.anthropic.AnthropicModel')", rich_help_panel="Advanced"),
    pre_pull: bool = typer.Option(False, "--pre-pull", help="Pull all required Docker images before starting any tasks", rich_help_panel="Basic"),
    redo_existing: bool = typer.Option(False, "--redo-existing", help="Redo existing instances", rich_help_panel="Data selection"),
    redo_existing_repro: bool = typer.Option(False, "--redo-existing-repro", help="Redo existing reproduction instances", rich_help_panel="Data selection"),
    redo_exit_status: str | None = typer.Option(
        None,
        "--redo-exit-status",
        help="Redo instances whose traj info.exit_status matches this value, plus instances with no existing traj",
        rich_help_panel="Data selection",
    ),
    reproduce_only: bool = typer.Option(False, "--reproduce-only", help="Only run reproduction stage, skip fixing", rich_help_panel="Reproduction"),
    config_spec: Path | None = typer.Option(None, "-c", "--config", help="Path to a config file (defaults to swebench_ts_pro.yaml for --subset pro, otherwise swebench_ts.yaml)", rich_help_panel="Basic"),
    environment_class: str | None = typer.Option( None, "--environment-class", help="Environment type to use. Recommended are docker or singularity", rich_help_panel="Advanced"),
    repro_config_spec: Path | None = typer.Option(None, "--repro-config", help="Path to a reproduction config file (defaults to swebench_pro_repro.yaml for --subset pro, otherwise swebench_repro.yaml)", rich_help_panel="Reproduction"),
) -> None:
    # fmt: on
    output_path = Path(output)
    output_path.mkdir(parents=True, exist_ok=True)
    logger.info(f"Results will be saved to {output_path}")
    add_file_handler(output_path / "minisweagent.log")

    # Load reproduction config
    if repro_config_spec is None:
        repro_config_name = "swebench_pro_repro.yaml" if subset == "pro" else "swebench_repro.yaml"
        repro_config_spec = builtin_config_dir / "extra" / repro_config_name
    repro_config_path = get_config_path(repro_config_spec)
    logger.info(f"Loading reproduction agent config from '{repro_config_path}'")
    repro_config = yaml.safe_load(repro_config_path.read_text())
    if environment_class is not None:
        repro_config.setdefault("environment", {})["environment_class"] = environment_class
        
    dataset_path = DATASET_MAPPING.get(subset, subset)
    dataset_dir_name = dataset_path.replace("/", "__")
    repro_model_config = repro_config.get("model", {})
    # Create dataset-specific reproductions directory
    reproductions_dir = (
        Path("reproductions")
        / (dataset_dir_name if subset != "verified_mini" else "princeton-nlp__SWE-Bench_Verified")
        / repro_model_config.get('model_name').replace('/', '__')
    )
    
    reproductions_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"Loading dataset {dataset_path}, split {split}...")
    instances = list(load_dataset(dataset_path, split=split))

    instances = filter_instances(instances, filter_spec=filter_spec, slice_spec=slice_spec, shuffle=shuffle)
    
    # Determine which instances to process
    instances_to_process = instances.copy()
    instances_to_reproduce = instances.copy()

    if redo_exit_status is not None:
        before_fix_filter = len(instances_to_process)

        fix_missing_traj_count = 0
        fix_matching_status_count = 0
        filtered_fix_instances = []
        for instance in instances_to_process:
            traj_exit_status = get_instance_exit_status(
                output_path / instance["instance_id"] / f"{instance['instance_id']}.traj.json"
            )
            if traj_exit_status is None:
                fix_missing_traj_count += 1
                filtered_fix_instances.append(instance)
            elif traj_exit_status == redo_exit_status:
                fix_matching_status_count += 1
                filtered_fix_instances.append(instance)

        repro_missing_traj_count = 0
        repro_matching_status_count = 0
        filtered_repro_instances = []
        for instance in instances_to_reproduce:
            traj_exit_status = get_instance_exit_status(
                output_path / instance["instance_id"] / f"{instance['instance_id']}.reproduction.traj.json"
            )
            if traj_exit_status is None:
                repro_missing_traj_count += 1
                filtered_repro_instances.append(instance)
            elif traj_exit_status == redo_exit_status:
                repro_matching_status_count += 1
                filtered_repro_instances.append(instance)

        instances_to_process = filtered_fix_instances

        logger.info(
            f"Fix exit status filter '{redo_exit_status}': {before_fix_filter} -> {len(instances_to_process)} instances "
            f"({fix_matching_status_count} matched status, {fix_missing_traj_count} missing traj)"
        )

    # Check for existing fix results
    if redo_exit_status is None and not redo_existing and (output_path / "preds.json").exists():
        existing_results = json.loads((output_path / "preds.json").read_text())
        existing_instance_ids = set(existing_results.keys())
        redo_instance_ids = {
            instance_id
            for instance_id, instance_result in existing_results.items()
            if should_redo_existing_instance(instance_result)
        }
        skip_instance_ids = existing_instance_ids - redo_instance_ids
        if skip_instance_ids:
            logger.info(
                f"Skipping {len(skip_instance_ids)} existing fix instances and redoing {len(redo_instance_ids)} instances with invalid model_patch values"
            )
        instances_to_process = [instance for instance in instances_to_process if instance["instance_id"] not in skip_instance_ids]

    # Check for existing reproduction results; this is controlled separately from redo_existing.
    reproduction_patch_files = sorted(reproductions_dir.glob("*.patch"))
    if not redo_existing_repro and reproduction_patch_files:
        reproductions_data = _load_reproduction_results(reproductions_dir)
        existing_repro_ids = {r.get("instance_id") for r in reproductions_data}
        redo_repro_ids = {
            r.get("instance_id")
            for r in reproductions_data
            if should_redo_existing_reproduction(r)
        }
        skip_repro_ids = existing_repro_ids - redo_repro_ids
        if skip_repro_ids:
            logger.info(f"Skipping {len(skip_repro_ids)} existing reproduction instances")
        instances_to_reproduce = [instance for instance in instances_to_reproduce if instance["instance_id"] not in skip_repro_ids]
    
    # Log processing plan
    if reproduce_only:
        logger.info(f"Running in reproduction-only mode on {len(instances_to_reproduce)} instances...")
        instances_to_process = []
    else:
        logger.info(
            f"Running in per-instance mode: reproduction on {len(instances_to_reproduce)} instances and fix on {len(instances_to_process)} instances..."
        )

    if config_spec is None:
        config_name = "swebench_ts_pro.yaml" if subset == "pro" else "swebench_ts.yaml"
        config_spec = builtin_config_dir / "extra" / config_name
    config_path = get_config_path(config_spec)
    logger.info(f"Loading agent config from '{config_path}'")
    config = yaml.safe_load(config_path.read_text())
    if environment_class is not None:
        config.setdefault("environment", {})["environment_class"] = environment_class
    if model is not None:
        config.setdefault("model", {})["model_name"] = model
    if model_class is not None:
        config.setdefault("model", {})["model_class"] = model_class

    if workers != 1:
        logger.info(
            "Per-instance mode is sequential (reproduce -> fix for each instance); ignoring workers=%d",
            workers,
        )

    repro_ids = {instance["instance_id"] for instance in instances_to_reproduce}
    process_ids = {instance["instance_id"] for instance in instances_to_process}

    if pre_pull:
        repro_instances = [instance for instance in instances if instance["instance_id"] in repro_ids]
        process_instances = [instance for instance in instances if instance["instance_id"] in process_ids]
        repro_environment = repro_config.get("environment", {})
        process_environment = config.get("environment", {})
        pull_docker_images(repro_instances, repro_environment)
        if process_environment.get("environment_class", "docker") == "docker":
            already_pulled_ids = (
                {instance["instance_id"] for instance in repro_instances}
                if repro_environment.get("environment_class", "docker") == "docker"
                and repro_environment.get("executable", "docker")
                == process_environment.get("executable", "docker")
                else set()
            )
            pull_docker_images(
                [instance for instance in process_instances if instance["instance_id"] not in already_pulled_ids],
                process_environment,
            )

    total_progress_tasks = sum(
        1 for instance in instances if instance["instance_id"] in repro_ids or instance["instance_id"] in process_ids
    )
    progress_manager = RunBatchProgressManager(
        total_progress_tasks,
        output_path
        / (
            f"reproduction_statuses_{time.time()}.yaml"
            if reproduce_only
            else f"run_statuses_{time.time()}.yaml"
        ),
    )

    if reproduce_only:
        logger.info("Running per-instance mode in reproduction-only flow...")
    else:
        logger.info("Running per-instance mode: reproduction then fix for each instance...")

    with Live(progress_manager.render_group, refresh_per_second=4):
        for idx, instance in enumerate(instances, start=1):
            instance_id = instance["instance_id"]
            do_repro = instance_id in repro_ids
            do_fix = (not reproduce_only) and (instance_id in process_ids)

            if not do_repro and not do_fix:
                continue

            logger.info(
                f"[{idx}/{len(instances)}] Processing {rich_escape(instance_id)} "
                f"(repro={'yes' if do_repro else 'no'}, fix={'yes' if do_fix else 'no'})"
            )

            progress_manager.on_instance_start(instance_id)
            final_exit_status = None

            if do_repro:
                final_exit_status = reproduce_instance(
                    instance,
                    output_path,
                    reproductions_dir,
                    config,
                    repro_config,
                    progress_manager,
                )

            if do_fix:
                final_exit_status, patch = process_instance(
                    instance,
                    output_path,
                    config,
                    progress_manager,
                    get_reproduction_patch_for_instance(reproductions_dir, instance_id),
                )
            
            # Evaluate the result if fix was run
            resolved = False
            # if do_fix and 'patch' in locals():
            #     from swebench.harness.run_evaluation import run_instance
            #     from swebench.harness.test_spec.test_spec import make_test_spec
            #     from swebench.harness.constants import KEY_MODEL, KEY_PREDICTION, KEY_INSTANCE_ID
            #     import docker

            #     client = docker.from_env()
            #     test_spec = make_test_spec(instance)

            #     prediction = {
            #         KEY_INSTANCE_ID: instance["instance_id"],
            #         KEY_MODEL: model if model is not None else config.get("model", {}).get("model_name", "unknown_model"),
            #         KEY_PREDICTION: patch,
            #     }

            #     result = run_instance(
            #         test_spec=test_spec,
            #         pred=prediction,
            #         rm_image=False,
            #         force_rebuild=False,
            #         client=client,
            #         run_id=output_path.name,
            #         timeout=400,
            #         clean_start=False,
            #     )

            #     if result["completed"]:
            #         resolved = result["resolved"]
            #         logger.info(f"Evaluation completed. Resolved: {resolved}")
            #     else:
            #         logger.error("Evaluation failed")
                
            progress_manager.on_instance_end(instance_id, final_exit_status)

if __name__ == "__main__":
    app()
