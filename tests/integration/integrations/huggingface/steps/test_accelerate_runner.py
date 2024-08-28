#  Copyright (c) ZenML GmbH 2024. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at:
#
#       https://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
#  or implied. See the License for the specific language governing
#  permissions and limitations under the License.

import os
import shutil
from pathlib import Path

import pytest
import transformers
from accelerate import Accelerator
from datasets import load_from_disk

from zenml import pipeline, step
from zenml.integrations.huggingface.steps import run_with_accelerate
from zenml.steps.entrypoint_function_utils import StepArtifact

try:
    from tests.integration.integrations.huggingface.steps import (
        load_base_model,
    )
except ImportError:
    # this happens when this scripts is called from within accelerate
    from helpers import load_base_model


def train_fn() -> str:
    def get_full_path(folder: str):
        return os.path.join(os.path.split(__file__)[0], folder)

    tokenized_train_dataset = load_from_disk(get_full_path("trn_dataset"))
    tokenized_eval_dataset = load_from_disk(get_full_path("eval_dataset"))
    model = load_base_model()

    ft_model_dir = Path("model_dir")

    accelerator = Accelerator()

    trainer = transformers.Trainer(
        model=model,
        args=transformers.TrainingArguments(
            output_dir="test_trainer",
            evaluation_strategy="epoch",
            no_cuda=True,
            max_steps=1,
            per_device_train_batch_size=1,
            report_to="none",
        ),
        train_dataset=tokenized_train_dataset,
        eval_dataset=tokenized_eval_dataset,
    )
    trainer.train()

    ft_model_dir = Path(ft_model_dir)
    if accelerator.is_main_process:
        ft_model_dir.mkdir(parents=True, exist_ok=True)
    unwrapped_model = accelerator.unwrap_model(model)
    unwrapped_model.save_pretrained(
        ft_model_dir,
        is_main_process=accelerator.is_main_process,
        save_function=accelerator.save,
    )

    return str(ft_model_dir)


@step
def train_step() -> str:
    return train_fn()


@run_with_accelerate(num_processes=1, cpu=True, num_cpu_threads_per_process=1)
@step
def train_step_accelerated() -> str:
    return train_fn()


train_step_accelerated_functional = run_with_accelerate(
    train_step, num_processes=1, cpu=True, num_cpu_threads_per_process=1
)


@pipeline(enable_cache=False)
def train_pipe_decorated():
    model_dir = train_step_accelerated()
    # if it is StepArtifact, we are still composing the pipeline
    if not isinstance(model_dir, StepArtifact):
        assert isinstance(model_dir, str)
        assert model_dir == "model_dir"


@pipeline(enable_cache=False)
def train_pipe_functional_in_place():
    model_dir = run_with_accelerate(
        train_step, num_processes=1, cpu=True, num_cpu_threads_per_process=1
    )()
    # if it is StepArtifact, we are still composing the pipeline
    if not isinstance(model_dir, StepArtifact):
        assert isinstance(model_dir, str)
        assert model_dir == "model_dir"


@pipeline(enable_cache=False)
def train_pipe_functional_imported():
    model_dir = train_step_accelerated_functional()
    # if it is StepArtifact, we are still composing the pipeline
    if not isinstance(model_dir, StepArtifact):
        assert isinstance(model_dir, str)
        assert model_dir == "model_dir"


@pytest.mark.parametrize(
    argnames=("pipeline",),
    argvalues=(
        (train_pipe_decorated,),
        (train_pipe_functional_in_place,),
        (train_pipe_functional_imported,),
    ),
)
def test_accelerate_runner_works_on_cpu_with_toy_model(
    pipeline,
    clean_client,
):
    """Tests whether the run_with_accelerate wrapper works as expected."""
    try:
        prev_files = os.listdir()
        response = pipeline()
        assert response.status.lower() == "completed"
    finally:
        cur_files = os.listdir()
        for each in set(cur_files) - set(prev_files):
            shutil.rmtree(each)
