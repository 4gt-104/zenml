#  Copyright (c) ZenML GmbH 2022. All Rights Reserved.
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
from datetime import datetime
from typing import Optional, Set, Tuple
from uuid import UUID

from zenml import Model
from zenml.client import Client
from zenml.config.step_configurations import Step
from zenml.constants import TEXT_FIELD_MAX_LENGTH
from zenml.enums import ExecutionStatus
from zenml.logger import get_logger
from zenml.models import (
    ModelVersionResponse,
    PipelineDeploymentResponse,
    PipelineRunResponse,
    PipelineRunUpdate,
    StepRunRequest,
    StepRunResponse,
    StepRunUpdate,
)
from zenml.orchestrators import cache_utils, input_utils, utils
from zenml.stack import Stack
from zenml.utils import string_utils

logger = get_logger(__name__)


class StepRunRequestFactory:
    """Helper class to create step run requests."""

    def __init__(
        self,
        deployment: "PipelineDeploymentResponse",
        pipeline_run: "PipelineRunResponse",
        stack: "Stack",
    ) -> None:
        """Initialize the object.

        Args:
            deployment: The deployment for which to create step run requests.
            pipeline_run: The pipeline run for which to create step run
                requests.
            stack: The stack on which the pipeline run is happening.
        """
        self.deployment = deployment
        self.pipeline_run = pipeline_run
        self.stack = stack

    def create_request(self, invocation_id: str) -> StepRunRequest:
        """Create a step run request.

        This will only create a request with basic information and will not yet
        compute information like the cache key and inputs. This is separated
        into a different method `populate_request(...)` that might raise
        exceptions while trying to compute this information.

        Args:
            invocation_id: The invocation ID for which to create the request.

        Returns:
            The step run request.
        """
        return StepRunRequest(
            name=invocation_id,
            pipeline_run_id=self.pipeline_run.id,
            deployment=self.deployment.id,
            status=ExecutionStatus.RUNNING,
            start_time=datetime.utcnow(),
            user=Client().active_user.id,
            workspace=Client().active_workspace.id,
        )

    def populate_request(self, request: StepRunRequest) -> None:
        """Populate a step run request with additional information.

        Args:
            request: The request to populate.
        """
        step = self.deployment.step_configurations[request.name]

        input_artifacts, parent_step_ids = input_utils.resolve_step_inputs(
            step=step,
            pipeline_run=self.pipeline_run,
        )
        input_artifact_ids = {
            input_name: artifact.id
            for input_name, artifact in input_artifacts.items()
        }
        request.inputs = input_artifact_ids
        request.parent_step_ids = parent_step_ids

        cache_key = cache_utils.generate_cache_key(
            step=step,
            input_artifact_ids=input_artifact_ids,
            artifact_store=self.stack.artifact_store,
            workspace_id=Client().active_workspace.id,
        )
        request.cache_key = cache_key

        # TODO: Doing this here means this will always fail when running a
        # template, as the step dependencies are not installed in that case.
        # In this case, should we just not store the docstring/source code?
        # Or should we somehow already include that in the deployment? That
        # would make the deployment object even bigger than it already is
        # though, as it would contain the code for all steps of the pipeline.
        (
            docstring,
            source_code,
        ) = self._get_docstring_and_source_code(step=step)

        request.docstring = docstring
        request.source_code = source_code

        cache_enabled = utils.is_setting_enabled(
            is_enabled_on_step=step.config.enable_cache,
            is_enabled_on_pipeline=self.deployment.pipeline_configuration.enable_cache,
        )

        if cache_enabled:
            if cached_step_run := cache_utils.get_cached_step_run(
                cache_key=cache_key
            ):
                # TODO: If the step is cached, do we also want to include
                # all the inputs of the original step? This would only make
                # a difference if the original step did some dynamic loading
                # of artifacts using `load_artifact`, which would then not be
                # included for the new one

                # request.inputs = {
                #     input_name: artifact.id
                #     for input_name, artifact in cached_step_run.inputs.items()
                # }

                request.original_step_run_id = cached_step_run.id
                request.outputs = {
                    output_name: artifact.id
                    for output_name, artifact in cached_step_run.outputs.items()
                }

                request.status = ExecutionStatus.CACHED
                request.end_time = request.start_time

    @staticmethod
    def _get_docstring_and_source_code(
        step: "Step",
    ) -> Tuple[Optional[str], str]:
        """Gets the docstring and source code of a step.

        Returns:
            The docstring and source code of a step.
        """
        from zenml.steps.base_step import BaseStep

        step_instance = BaseStep.load_from_source(step.spec.source)

        docstring = step_instance.docstring
        if docstring and len(docstring) > TEXT_FIELD_MAX_LENGTH:
            docstring = docstring[: (TEXT_FIELD_MAX_LENGTH - 3)] + "..."

        source_code = step_instance.source_code
        if source_code and len(source_code) > TEXT_FIELD_MAX_LENGTH:
            source_code = source_code[: (TEXT_FIELD_MAX_LENGTH - 3)] + "..."

        return docstring, source_code


def find_cacheable_invocation_candidates(
    deployment: "PipelineDeploymentResponse",
    finished_invocations: Set[str],
) -> Set[str]:
    """Find invocations that can potentially be cached.

    Args:
        deployment: The pipeline deployment containing the invocations.
        finished_invocations: A set of invocations that are already finished.

    Returns:
        The set of invocations that can potentially be cached.
    """
    invocations = set()
    for invocation_id, step in deployment.step_configurations.items():
        if invocation_id in finished_invocations:
            continue

        cache_enabled = utils.is_setting_enabled(
            is_enabled_on_step=step.config.enable_cache,
            is_enabled_on_pipeline=deployment.pipeline_configuration.enable_cache,
        )

        if not cache_enabled:
            continue

        if set(step.spec.upstream_steps) - finished_invocations:
            continue

        invocations.add(invocation_id)

    return invocations


def create_cached_step_runs(
    deployment: "PipelineDeploymentResponse",
    pipeline_run: PipelineRunResponse,
    stack: "Stack",
) -> Set[str]:
    """Create all cached step runs for a pipeline run.

    Args:
        deployment: The deployment of the pipeline run.
        pipeline_run: The pipeline run for which to create the step runs.
        stack: The stack on which the pipeline run is happening.

    Returns:
        The invocation IDs of the created step runs.
    """
    cached_invocations: Set[str] = set()
    visited_invocations: Set[str] = set()
    request_factory = StepRunRequestFactory(
        deployment=deployment, pipeline_run=pipeline_run, stack=stack
    )

    pipeline_model = prepare_pipeline_run_model(pipeline_run=pipeline_run)

    while (
        cache_candidates := find_cacheable_invocation_candidates(
            deployment=deployment,
            finished_invocations=cached_invocations,
        )
        - visited_invocations
    ):
        for invocation_id in cache_candidates:
            visited_invocations.add(invocation_id)

            try:
                step_run_request = request_factory.create_request(
                    invocation_id
                )
                request_factory.populate_request(step_run_request)
            except Exception as e:
                # We failed to create/populate the step run. This might be due
                # to some input resolution error, or an error importing the step
                # source (there might be some missing dependencies). We just
                # treat the step as not cached and let the orchestrator try
                # again in a potentially different environment.
                logger.debug(
                    "Failed preparing step run `%s`: %s", invocation_id, str(e)
                )
                continue

            if step_run_request.status != ExecutionStatus.CACHED:
                # If we're not able to cache the step run, the orchestrator
                # will run the step later which will create the step run
                # -> We don't need to do anything here
                continue

            step_run = Client().zen_store.create_run_step(step_run_request)
            step_model = prepare_step_run_model(
                step_run=step_run, pipeline_run=pipeline_run
            )

            model = step_model or pipeline_model

            # TODO: This function also imports the step code, which means it
            # will fail when running a template. The only reason it imports
            # the step is to load the artifact configs to check whether a
            # different model is specified for a single output. We can go two
            # ways here I think:
            # - Store the artifact config in the step configuration of the
            #   deployment. This makes sense in any case I think, also for the
            #   comment below about linking models to the pipeline run.
            # - Get rid of having a separate model for an output. Not sure what
            #   the use case of that actually is and whether it warrants all
            #   this complexity.
            utils._link_cached_artifacts_to_model(
                model_from_context=model,
                step_run=step_run_request,
                step_source=step_run.spec.source,
            )
            if model:
                # TODO: When a step is not cached, we parse the output artifact
                # configs and also link the pipeline run to these model
                # versions. Why is that not happening here?
                utils._link_pipeline_run_to_model_from_context(
                    pipeline_run_id=pipeline_run.id, model=model
                )

            logger.info("Using cached version of step `%s`.", invocation_id)
            cached_invocations.add(invocation_id)

    return cached_invocations


def get_or_create_model_version_for_pipeline_run(
    model: Model, pipeline_run: PipelineRunResponse
) -> Tuple[ModelVersionResponse, bool]:
    """Get or create a model version as part of a pipeline run.

    Args:
        model: The model to get or create.
        pipeline_run: The pipeline run for which the model should be created.

    Returns:
        The model version and a boolean indicating whether it was newly created
        or not.
    """
    if model.model_version_id:
        return model._get_model_version(), False
    elif model.version:
        if isinstance(model.version, str):
            start_time = pipeline_run.start_time or datetime.utcnow()
            model.version = string_utils.format_name_template(
                model.version,
                date=start_time.strftime("%Y_%m_%d"),
                time=start_time.strftime("%H_%M_%S_%f"),
            )

        # TODO: Check whether the version actually got created or not instead
        # of always returning True here
        return model._get_or_create_model_version(), True

    # The model version should be created as part of this run
    # -> We first check if it was already created as part of this run, and if
    # not we do create it. If this is running in two parallel steps, we might
    # run into issues that this will create two versions
    if model_version := get_model_version_created_by_pipeline_run(
        model_name=model.name, pipeline_run_id=pipeline_run.id
    ):
        return model_version, False
    else:
        return model._get_or_create_model_version(), True


def get_model_version_created_by_pipeline_run(
    model_name: str, pipeline_run_id: UUID
) -> Optional[ModelVersionResponse]:
    """Get a model version that was created by a specific pipeline run.

    Args:
        model_name: The model name for which to get the version.
        pipeline_run_id: The ID of the pipeline run for which to get the
            version.

    Returns:
        A model version with the given name created by the run, or None if such
        a model version does not exist.
    """
    # We always fetch the run here to make sure we get the latest state
    # including all the step runs
    pipeline_run = Client().get_pipeline_run(pipeline_run_id)

    if pipeline_run.config.model and pipeline_run.model_version:
        if (
            pipeline_run.config.model.name == model_name
            and pipeline_run.config.model.version is None
        ):
            return pipeline_run.model_version

    for step_run in pipeline_run.steps.values():
        if step_run.config.model and step_run.model_version:
            if (
                step_run.config.model.name == model_name
                and step_run.config.model.version is None
            ):
                return step_run.model_version

    return None


def prepare_pipeline_run_model(
    pipeline_run: PipelineRunResponse,
) -> Optional[Model]:
    """Prepare the model for a pipeline run.

    Args:
        pipeline_run: The pipeline run for which to prepare the model.

    Returns:
        The prepared model.
    """
    if pipeline_run.model_version:
        return pipeline_run.model_version.to_model_class()
    elif model := pipeline_run.config.model:
        model_version, _ = get_or_create_model_version_for_pipeline_run(
            model=model, pipeline_run=pipeline_run
        )
        pipeline_run = Client().zen_store.update_run(
            run_id=pipeline_run.id,
            run_update=PipelineRunUpdate(model_version_id=model_version.id),
        )
        log_model_version_dashboard_url(model_version)
        return model_version.to_model_class()
    else:
        return None


def prepare_step_run_model(
    step_run: StepRunResponse, pipeline_run: PipelineRunResponse
) -> Optional[Model]:
    """Prepare the model for a step run.

    Args:
        step_run: The step run for which to prepare the model.
        pipeline_run: The pipeline run of the step.

    Returns:
        The prepared model.
    """
    if step_run.model_version:
        return step_run.model_version.to_model_class()
    elif model := step_run.config.model:
        model_version, created = get_or_create_model_version_for_pipeline_run(
            model=model, pipeline_run=pipeline_run
        )
        step_run = Client().zen_store.update_run_step(
            step_run_id=step_run.id,
            step_run_update=StepRunUpdate(model_version_id=model_version.id),
        )
        if created:
            log_model_version_dashboard_url(model_version)

        return model_version.to_model_class()
    else:
        return None


def log_model_version_dashboard_url(
    model_version: ModelVersionResponse,
) -> None:
    """Log the dashboard URL for a model version.

    If the current server is not a ZenML Pro tenant, a fallback message is
    logged instead.

    Args:
        model_version: The model version for which to log the dashboard URL.
    """
    from zenml.utils.cloud_utils import try_get_model_version_url

    if model_version_url_logs := try_get_model_version_url(model_version):
        logger.info(model_version_url_logs)
    else:
        logger.info(
            "Models can be viewed in the dashboard using ZenML Pro. Sign up "
            "for a free trial at https://www.zenml.io/pro/"
        )
