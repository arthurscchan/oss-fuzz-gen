#!/usr/bin/env python3
# Copyright 2024 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Run an experiment with one function-under-test."""

import argparse
import dataclasses
import logging
import os
import shutil
import threading
from multiprocessing import pool
from typing import List, Optional

import pipeline
from agent.prototyper import Prototyper
from data_prep import project_targets
from data_prep.project_context.context_introspector import ContextRetriever
from experiment import builder_runner as builder_runner_lib
from experiment import evaluator as exp_evaluator
from experiment import oss_fuzz_checkout, textcov
from experiment.benchmark import Benchmark
from experiment.workdir import WorkDirs
from llm_toolkit import models, output_parser, prompt_builder, prompts
from results import BuildResult, ExperimentResult, Result

thread_local = threading.local()

logger = logging.getLogger(__name__)

# WARN: Avoid high value for NUM_EVA for local experiments.
# NUM_EVA controls the number of fuzz targets to evaluate in parallel by each
# experiment, while {run_all_experiments.NUM_EXP, default 2} experiments will
# run in parallel.
NUM_EVA = int(os.getenv('LLM_NUM_EVA', '3'))

# Default LLM hyper-parameters.
# #182 shows Gemini returns NUM_SAMPLES independent responses via repeated
#  queries, which generally performs better than top-k responses from one
#  query [1].
# [1] TODO(@happy-qop): Update the link.
# WARN: Avoid large NUM_SAMPLES in highly parallelized local experiments.
# It controls the number of LLM responses per prompt, which may exceed your
# LLM's limit on query-per-second.
NUM_SAMPLES = 2
MAX_TOKENS: int = 4096
RUN_TIMEOUT: int = 30
TEMPERATURE: float = 0.4

RESULTS_DIR = './results'


# TODO(dongge): Move this to results.py
@dataclasses.dataclass
class AggregatedResult:
  """Aggregated evaluation result."""
  build_success_rate: float = 0.0
  crash_rate: float = 0.0
  found_bug: int = 0
  max_coverage: float = 0.0
  max_line_coverage_diff: float = 0.0
  max_coverage_sample: str = ''
  max_coverage_diff_sample: str = ''
  max_coverage_diff_report: str = ''
  full_textcov_diff: textcov.Textcov = dataclasses.field(
      default_factory=textcov.Textcov)

  def __str__(self):
    return (
        f'build success rate: {self.build_success_rate}, '
        f'crash rate: {self.crash_rate}, '
        f'found bug: {self.found_bug}, '
        f'max coverage: {self.max_coverage}, '
        f'max line coverage diff: {self.max_line_coverage_diff}\n'
        f'max coverage sample: {self.max_coverage_sample}\n'
        f'max coverage diff sample: {self.max_coverage_diff_sample}\n'
        f'max coverage diff report: {self.max_coverage_diff_report or "None"}')

  @classmethod
  def from_experiment_result(
      cls, sample_results: list[ExperimentResult]) -> 'AggregatedResult':
    """Aggereate experiment history results of all samples."""
    if not sample_results:
      return AggregatedResult()
    sample_final_build_results = [[
        result
        for result in sample_result_history.history_results
        if isinstance(result, BuildResult)
    ][-1]
                                  for sample_result_history in sample_results]
    build_success_rate = sum([
        int(sample_final_result.status)
        for sample_final_result in sample_final_build_results
    ]) / len(sample_final_build_results)
    return AggregatedResult(build_success_rate=build_success_rate,)


def generate_targets(benchmark: Benchmark, model: models.LLM,
                     prompt: prompts.Prompt, work_dirs: WorkDirs,
                     builder: prompt_builder.PromptBuilder) -> list[str]:
  """Generates fuzz target with LLM."""
  logger.info('Generating targets for %s %s using %s..', benchmark.project,
              benchmark.function_signature, model.name)
  model.query_llm(prompt, response_dir=work_dirs.raw_targets)

  _, target_ext = os.path.splitext(benchmark.target_path)
  generated_targets = []
  for file in os.listdir(work_dirs.raw_targets):
    if not output_parser.is_raw_output(file):
      continue
    raw_output = os.path.join(work_dirs.raw_targets, file)
    target_code = output_parser.parse_code(raw_output)
    target_code = builder.post_process_generated_code(target_code)
    target_id, _ = os.path.splitext(raw_output)
    target_file = f'{target_id}{target_ext}'
    target_path = os.path.join(work_dirs.raw_targets, target_file)
    output_parser.save_output(target_code, target_path)
    generated_targets.append(target_path)

  if generated_targets:
    targets_relpath = map(os.path.relpath, generated_targets)
    targets_relpath_str = '\n '.join(targets_relpath)
    logger.info('Generated:\n %s', targets_relpath_str)
  else:
    logger.info('Failed to generate targets: %s', generated_targets)
  return generated_targets


def fix_code(work_dirs: WorkDirs, generated_targets: List[str]) -> List[str]:
  """Copies the generated target to the fixed target directory for simple
    code fixes."""
  fixed_targets = []
  # Prepare all LLM-generated targets for code fixes.
  for file in generated_targets:
    fixed_target = os.path.join(work_dirs.fixed_targets, os.path.basename(file))
    shutil.copyfile(file, fixed_target)
    fixed_targets.append(fixed_target)

  return fixed_targets


def aggregate_results(target_stats: list[tuple[int, exp_evaluator.Result]],
                      generated_targets: list[str]) -> AggregatedResult:
  """Aggregates experiment status and results of a targets."""
  build_success_rate = sum([int(stat.compiles) for _, stat in target_stats
                           ]) / len(target_stats)
  crash_rate = sum([int(stat.crashes) for _, stat in target_stats
                   ]) / len(target_stats)
  found_bug = sum([
      int(stat.crashes and not stat.is_semantic_error)
      for _, stat in target_stats
  ])
  max_coverage = max([stat.coverage for _, stat in target_stats])
  max_line_coverage_diff = max(
      [stat.line_coverage_diff for _, stat in target_stats])

  max_coverage_sample = ''
  max_coverage_diff_sample = ''
  max_coverage_diff_report = ''

  all_textcov = textcov.Textcov()
  for i, stat in target_stats:
    if stat.coverage == max_coverage:
      max_coverage_sample = generated_targets[i]

    if stat.line_coverage_diff == max_line_coverage_diff:
      max_coverage_diff_sample = generated_targets[i]
      max_coverage_diff_report = stat.coverage_report_path

    if isinstance(stat.textcov_diff, textcov.Textcov):
      all_textcov.merge(stat.textcov_diff)

  return AggregatedResult(build_success_rate, crash_rate, found_bug,
                          max_coverage, max_line_coverage_diff,
                          max_coverage_sample, max_coverage_diff_sample,
                          max_coverage_diff_report, all_textcov)


def check_targets(
    ai_binary: str,
    benchmark: Benchmark,
    work_dirs: WorkDirs,
    generated_targets: List[str],
    cloud_experiment_name: str = '',
    cloud_experiment_bucket: str = '',
    run_timeout: int = RUN_TIMEOUT,
    fixer_model_name: str = models.DefaultModel.name,
) -> Optional[AggregatedResult]:
  """Builds all targets in the fixed target directory."""
  target_stats = []

  if cloud_experiment_name:
    builder_runner = builder_runner_lib.CloudBuilderRunner(
        benchmark,
        work_dirs,
        run_timeout,
        fixer_model_name,
        experiment_name=cloud_experiment_name,
        experiment_bucket=cloud_experiment_bucket,
    )
  else:
    builder_runner = builder_runner_lib.BuilderRunner(benchmark, work_dirs,
                                                      run_timeout,
                                                      fixer_model_name)

  evaluator = exp_evaluator.Evaluator(builder_runner, benchmark, work_dirs)

  ai_target_pairs = [(ai_binary, target) for target in generated_targets]
  with pool.ThreadPool(NUM_EVA) as p:
    for i, target_stat in enumerate(
        p.starmap(evaluator.check_target, ai_target_pairs)):
      if target_stat is None:
        logger.error('This should never happen: Error evaluating target: %s',
                     generated_targets[i])
        target_stat = exp_evaluator.Result()

      target_stats.append((i, target_stat))

  if len(target_stats) > 0:
    return aggregate_results(target_stats, generated_targets)

  logger.info('No targets to check.')
  return None


def prepare(oss_fuzz_dir: str) -> None:
  """Prepares the experiment environment."""
  oss_fuzz_checkout.clone_oss_fuzz(oss_fuzz_dir)
  oss_fuzz_checkout.postprocess_oss_fuzz()


def generate_targets_for_analysis(
    model: models.LLM,
    benchmark: Benchmark,
    work_dirs: WorkDirs,
    template_dir: str,
    use_context: bool,
    example_pair: list[list[str]],
    prompt_builder_to_use: str = 'DEFAULT',
    cloud_experiment_bucket: str = '') -> List[str]:
  """Generates a set of harnesses and build scripts ready to be evaluated
    by `check_targets`. This is where the core first LLM logic is used to
    generate harnesses.

    Returns a list of folders with the generated artifacts.
    """
  logger.info('Generating targets')
  if benchmark.use_project_examples:
    project_examples = project_targets.generate_data(
        benchmark.project,
        benchmark.language,
        cloud_experiment_bucket=cloud_experiment_bucket)
  else:
    project_examples = []

  if use_context:
    retriever = ContextRetriever(benchmark)
    context_info = retriever.get_context_info()
  else:
    context_info = {}

  # If this is a test benchmark then we will use a test prompt builder.
  if benchmark.test_file_path:
    logger.info('Generating a target for test case: %s',
                benchmark.test_file_path)
    builder = prompt_builder.TestToHarnessConverter(model, benchmark,
                                                    template_dir)
  elif benchmark.language == 'jvm':
    # For Java projects
    builder = prompt_builder.DefaultJvmTemplateBuilder(model, benchmark,
                                                       template_dir)
  elif prompt_builder_to_use == 'CSpecific':
    builder = prompt_builder.CSpecificBuilder(model, benchmark, template_dir)
  else:
    # Use default
    builder = prompt_builder.DefaultTemplateBuilder(model, benchmark,
                                                    template_dir)

  prompt = builder.build(example_pair,
                         project_example_content=project_examples,
                         project_context_content=context_info)
  prompt.save(work_dirs.prompt)

  generated_targets = generate_targets(benchmark, model, prompt, work_dirs,
                                       builder)
  generated_targets = fix_code(work_dirs, generated_targets)
  return generated_targets


def initialize_thread(index):
  """Initialize thread-local storage for each thread."""
  # Thread-local storage object
  thread_local.index = index
  # Initialize more complex objects or variables if needed
  print(f"Initialized thread-local storage for index={index}")


def _fuzzing_pipeline(benchmark: Benchmark, model: models.LLM,
                      args: argparse.Namespace, work_dirs: WorkDirs,
                      trial: int) -> ExperimentResult:
  """Runs the predefined 3-stage pipeline for one trial."""
  logger.info('My sample ID: %s', getattr(thread_local, 'index', 'unknown'))
  p = pipeline.Pipeline(
      args=args, writing_stage_agents=[Prototyper(trial=trial, llm=model)])
  results = p.execute(result_history=[
      Result(benchmark=benchmark, trial=trial, work_dirs=work_dirs)
  ])

  return ExperimentResult(results)


def _fuzzing_pipelines(benchmark: Benchmark, model: models.LLM,
                       args: argparse.Namespace,
                       work_dirs: WorkDirs) -> AggregatedResult:
  """Runs all trial experiments in their pipelines."""
  # Create a pool of worker processes
  with pool.ThreadPool(processes=NUM_EVA) as p:
    # Initialize thread-local storage in each worker before processing
    task_args = [(benchmark, model, args, work_dirs, trial)
                 for trial in range(1, NUM_EVA + 1)]
    results = p.starmap(_fuzzing_pipeline, task_args)
  return AggregatedResult.from_experiment_result(results)


def run(benchmark: Benchmark, model: models.LLM, args: argparse.Namespace,
        work_dirs: WorkDirs) -> Optional[AggregatedResult]:
  """Generates code via LLM, and evaluates them."""
  model.cloud_setup()

  if args.agent:
    # TODO(dongge): Make this default when it is ready.
    return _fuzzing_pipelines(benchmark, model, args, work_dirs)

  generated_targets = generate_targets_for_analysis(
      model=model,
      benchmark=benchmark,
      work_dirs=work_dirs,
      template_dir=args.template_directory,
      use_context=args.context,
      example_pair=prompt_builder.EXAMPLES[benchmark.language],
      prompt_builder_to_use=args.prompt_builder,
      cloud_experiment_bucket=args.cloud_experiment_bucket)

  logger.info('Generated %d targets', len(generated_targets))
  if not generated_targets:
    return None

  return check_targets(model.ai_binary, benchmark, work_dirs, generated_targets,
                       args.cloud_experiment_name, args.cloud_experiment_bucket,
                       args.run_timeout, model.name)
