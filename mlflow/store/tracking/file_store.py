import hashlib
import json
import logging
import os
import shutil
import sys
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, NamedTuple, Optional, TypedDict

from mlflow.entities import (
    Assessment,
    Dataset,
    DatasetInput,
    Expectation,
    Experiment,
    ExperimentTag,
    Feedback,
    InputTag,
    LoggedModel,
    LoggedModelInput,
    LoggedModelOutput,
    LoggedModelParameter,
    LoggedModelStatus,
    LoggedModelTag,
    Metric,
    Param,
    Run,
    RunData,
    RunInfo,
    RunInputs,
    RunOutputs,
    RunStatus,
    RunTag,
    SourceType,
    TraceInfo,
    ViewType,
    _DatasetSummary,
)
from mlflow.entities.lifecycle_stage import LifecycleStage
from mlflow.entities.run_info import check_run_is_active
from mlflow.entities.trace_info_v2 import TraceInfoV2
from mlflow.entities.trace_status import TraceStatus
from mlflow.environment_variables import MLFLOW_TRACKING_DIR
from mlflow.exceptions import MissingConfigException, MlflowException
from mlflow.protos import databricks_pb2
from mlflow.protos.databricks_pb2 import (
    INTERNAL_ERROR,
    INVALID_PARAMETER_VALUE,
    RESOURCE_DOES_NOT_EXIST,
)
from mlflow.protos.internal_pb2 import InputVertexType, OutputVertexType
from mlflow.store.entities.paged_list import PagedList
from mlflow.store.model_registry.file_store import FileStore as ModelRegistryFileStore
from mlflow.store.tracking import (
    DEFAULT_LOCAL_FILE_AND_ARTIFACT_PATH,
    SEARCH_LOGGED_MODEL_MAX_RESULTS_DEFAULT,
    SEARCH_MAX_RESULTS_DEFAULT,
    SEARCH_MAX_RESULTS_THRESHOLD,
    SEARCH_TRACES_DEFAULT_MAX_RESULTS,
)
from mlflow.store.tracking.abstract_store import AbstractStore
from mlflow.tracing.utils import (
    generate_assessment_id,
    generate_request_id_v2,
)
from mlflow.utils import get_results_from_paginated_fn
from mlflow.utils.file_utils import (
    append_to,
    exists,
    find,
    get_parent_dir,
    is_directory,
    list_all,
    list_subdirs,
    local_file_uri_to_path,
    make_containing_dirs,
    mkdir,
    mv,
    path_to_local_file_uri,
    read_file,
    read_file_lines,
    write_to,
)
from mlflow.utils.mlflow_tags import (
    MLFLOW_ARTIFACT_LOCATION,
    MLFLOW_DATASET_CONTEXT,
    MLFLOW_LOGGED_MODELS,
    MLFLOW_RUN_NAME,
    _get_run_name_from_tags,
)
from mlflow.utils.name_utils import _generate_random_name, _generate_unique_integer_id
from mlflow.utils.search_utils import (
    SearchExperimentsUtils,
    SearchLoggedModelsUtils,
    SearchTraceUtils,
    SearchUtils,
)
from mlflow.utils.string_utils import is_string_type
from mlflow.utils.time import get_current_time_millis
from mlflow.utils.uri import (
    append_to_uri_path,
    resolve_uri_if_local,
)
from mlflow.utils.validation import (
    _validate_batch_log_data,
    _validate_batch_log_limits,
    _validate_experiment_artifact_location_length,
    _validate_experiment_id,
    _validate_experiment_name,
    _validate_logged_model_name,
    _validate_metric,
    _validate_metric_name,
    _validate_param,
    _validate_param_keys_unique,
    _validate_param_name,
    _validate_run_id,
    _validate_tag_name,
)
from mlflow.utils.yaml_utils import overwrite_yaml, read_yaml, write_yaml

_logger = logging.getLogger(__name__)


def _default_root_dir():
    return MLFLOW_TRACKING_DIR.get() or os.path.abspath(DEFAULT_LOCAL_FILE_AND_ARTIFACT_PATH)


def _read_persisted_experiment_dict(experiment_dict):
    dict_copy = experiment_dict.copy()

    # 'experiment_id' was changed from int to string, so we must cast to string
    # when reading legacy experiments
    if isinstance(dict_copy["experiment_id"], int):
        dict_copy["experiment_id"] = str(dict_copy["experiment_id"])
    return Experiment.from_dictionary(dict_copy)


def _make_persisted_run_info_dict(run_info):
    # 'tags' was moved from RunInfo to RunData, so we must keep storing it in the meta.yaml for
    # old mlflow versions to read
    run_info_dict = dict(run_info)
    run_info_dict["tags"] = []
    if "status" in run_info_dict:
        # 'status' is stored as an integer enum in meta file, but RunInfo.status field is a string.
        # Convert from string to enum/int before storing.
        run_info_dict["status"] = RunStatus.from_string(run_info.status)
    else:
        run_info_dict["status"] = RunStatus.RUNNING
    run_info_dict["source_type"] = SourceType.LOCAL
    run_info_dict["source_name"] = ""
    run_info_dict["entry_point_name"] = ""
    run_info_dict["source_version"] = ""
    return run_info_dict


def _read_persisted_run_info_dict(run_info_dict):
    dict_copy = run_info_dict.copy()
    if "lifecycle_stage" not in dict_copy:
        dict_copy["lifecycle_stage"] = LifecycleStage.ACTIVE
    # 'status' is stored as an integer enum in meta file, but RunInfo.status field is a string.
    # converting to string before hydrating RunInfo.
    # If 'status' value not recorded in files, mark it as 'RUNNING' (default)
    dict_copy["status"] = RunStatus.to_string(run_info_dict.get("status", RunStatus.RUNNING))

    # 'experiment_id' was changed from int to string, so we must cast to string
    # when reading legacy run_infos
    if isinstance(dict_copy["experiment_id"], int):
        dict_copy["experiment_id"] = str(dict_copy["experiment_id"])
    return RunInfo.from_dictionary(dict_copy)


class DatasetFilter(TypedDict, total=False):
    """
    Dataset filter used for search_logged_models.
    """

    dataset_name: str
    dataset_digest: str


class FileStore(AbstractStore):
    TRASH_FOLDER_NAME = ".trash"
    ARTIFACTS_FOLDER_NAME = "artifacts"
    METRICS_FOLDER_NAME = "metrics"
    PARAMS_FOLDER_NAME = "params"
    TAGS_FOLDER_NAME = "tags"
    EXPERIMENT_TAGS_FOLDER_NAME = "tags"
    DATASETS_FOLDER_NAME = "datasets"
    INPUTS_FOLDER_NAME = "inputs"
    OUTPUTS_FOLDER_NAME = "outputs"
    META_DATA_FILE_NAME = "meta.yaml"
    DEFAULT_EXPERIMENT_ID = "0"
    TRACE_INFO_FILE_NAME = "trace_info.yaml"
    TRACES_FOLDER_NAME = "traces"
    TRACE_TAGS_FOLDER_NAME = "tags"
    ASSESSMENTS_FOLDER_NAME = "assessments"
    # "request_metadata" field is renamed to "trace_metadata" in V3,
    # but we keep the old name for backward compatibility
    TRACE_TRACE_METADATA_FOLDER_NAME = "request_metadata"
    MODELS_FOLDER_NAME = "models"
    RESERVED_EXPERIMENT_FOLDERS = [
        EXPERIMENT_TAGS_FOLDER_NAME,
        DATASETS_FOLDER_NAME,
        TRACES_FOLDER_NAME,
        MODELS_FOLDER_NAME,
    ]

    def __init__(self, root_directory=None, artifact_root_uri=None):
        """
        Create a new FileStore with the given root directory and a given default artifact root URI.
        """
        super().__init__()
        self.root_directory = local_file_uri_to_path(root_directory or _default_root_dir())
        if not artifact_root_uri:
            self.artifact_root_uri = path_to_local_file_uri(self.root_directory)
        else:
            self.artifact_root_uri = resolve_uri_if_local(artifact_root_uri)
        self.trash_folder = os.path.join(self.root_directory, FileStore.TRASH_FOLDER_NAME)
        # Create root directory if needed
        if not exists(self.root_directory):
            self._create_default_experiment()
        # Create trash folder if needed
        if not exists(self.trash_folder):
            mkdir(self.trash_folder)

    def _create_default_experiment(self):
        mkdir(self.root_directory)
        self._create_experiment_with_id(
            name=Experiment.DEFAULT_EXPERIMENT_NAME,
            experiment_id=FileStore.DEFAULT_EXPERIMENT_ID,
            artifact_uri=None,
            tags=None,
        )

    def _check_root_dir(self):
        """
        Run checks before running directory operations.
        """
        if not exists(self.root_directory):
            raise Exception(f"'{self.root_directory}' does not exist.")
        if not is_directory(self.root_directory):
            raise Exception(f"'{self.root_directory}' is not a directory.")

    def _get_experiment_path(self, experiment_id, view_type=ViewType.ALL, assert_exists=False):
        parents = []
        if view_type == ViewType.ACTIVE_ONLY or view_type == ViewType.ALL:
            parents.append(self.root_directory)
        if view_type == ViewType.DELETED_ONLY or view_type == ViewType.ALL:
            parents.append(self.trash_folder)
        for parent in parents:
            exp_list = find(parent, experiment_id, full_path=True)
            if len(exp_list) > 0:
                return exp_list[0]
        if assert_exists:
            raise MlflowException(
                f"Experiment {experiment_id} does not exist.",
                databricks_pb2.RESOURCE_DOES_NOT_EXIST,
            )
        return None

    def _get_run_dir(self, experiment_id, run_uuid):
        _validate_run_id(run_uuid)
        if not self._has_experiment(experiment_id):
            return None
        return os.path.join(self._get_experiment_path(experiment_id, assert_exists=True), run_uuid)

    def _get_metric_path(self, experiment_id, run_uuid, metric_key):
        _validate_run_id(run_uuid)
        _validate_metric_name(metric_key, "name")
        return os.path.join(
            self._get_run_dir(experiment_id, run_uuid),
            FileStore.METRICS_FOLDER_NAME,
            metric_key,
        )

    def _get_model_metric_path(self, experiment_id: str, model_id: str, metric_key: str) -> str:
        _validate_metric_name(metric_key)
        return os.path.join(
            self._get_model_dir(experiment_id, model_id), FileStore.METRICS_FOLDER_NAME, metric_key
        )

    def _get_param_path(self, experiment_id, run_uuid, param_name):
        _validate_run_id(run_uuid)
        _validate_param_name(param_name)
        return os.path.join(
            self._get_run_dir(experiment_id, run_uuid),
            FileStore.PARAMS_FOLDER_NAME,
            param_name,
        )

    def _get_experiment_tag_path(self, experiment_id, tag_name):
        _validate_experiment_id(experiment_id)
        _validate_tag_name(tag_name)
        if not self._has_experiment(experiment_id):
            return None
        return os.path.join(
            self._get_experiment_path(experiment_id, assert_exists=True),
            FileStore.TAGS_FOLDER_NAME,
            tag_name,
        )

    def _get_tag_path(self, experiment_id, run_uuid, tag_name):
        _validate_run_id(run_uuid)
        _validate_tag_name(tag_name)
        return os.path.join(
            self._get_run_dir(experiment_id, run_uuid),
            FileStore.TAGS_FOLDER_NAME,
            tag_name,
        )

    def _get_artifact_dir(self, experiment_id, run_uuid):
        _validate_run_id(run_uuid)
        return append_to_uri_path(
            self.get_experiment(experiment_id).artifact_location,
            run_uuid,
            FileStore.ARTIFACTS_FOLDER_NAME,
        )

    def _get_active_experiments(self, full_path=False):
        exp_list = list_subdirs(self.root_directory, full_path)
        return [
            exp
            for exp in exp_list
            if not exp.endswith(FileStore.TRASH_FOLDER_NAME)
            and exp != ModelRegistryFileStore.MODELS_FOLDER_NAME
        ]

    def _get_deleted_experiments(self, full_path=False):
        return list_subdirs(self.trash_folder, full_path)

    def search_experiments(
        self,
        view_type=ViewType.ACTIVE_ONLY,
        max_results=SEARCH_MAX_RESULTS_DEFAULT,
        filter_string=None,
        order_by=None,
        page_token=None,
    ):
        if not isinstance(max_results, int) or max_results < 1:
            raise MlflowException(
                f"Invalid value {max_results} for parameter 'max_results' supplied. It must be "
                f"a positive integer",
                INVALID_PARAMETER_VALUE,
            )
        if max_results > SEARCH_MAX_RESULTS_THRESHOLD:
            raise MlflowException(
                f"Invalid value {max_results} for parameter 'max_results' supplied. It must be at "
                f"most {SEARCH_MAX_RESULTS_THRESHOLD}",
                INVALID_PARAMETER_VALUE,
            )

        self._check_root_dir()
        experiment_ids = []
        if view_type == ViewType.ACTIVE_ONLY or view_type == ViewType.ALL:
            experiment_ids += self._get_active_experiments(full_path=False)
        if view_type == ViewType.DELETED_ONLY or view_type == ViewType.ALL:
            experiment_ids += self._get_deleted_experiments(full_path=False)

        experiments = []
        for exp_id in experiment_ids:
            try:
                # trap and warn known issues, will raise unexpected exceptions to caller
                exp = self._get_experiment(exp_id, view_type)
                if exp is not None:
                    experiments.append(exp)
            except MissingConfigException as e:
                logging.warning(
                    f"Malformed experiment '{exp_id}'. Detailed error {e}",
                    exc_info=True,
                )
        filtered = SearchExperimentsUtils.filter(experiments, filter_string)
        sorted_experiments = SearchExperimentsUtils.sort(
            filtered, order_by or ["creation_time DESC", "experiment_id ASC"]
        )
        experiments, next_page_token = SearchUtils.paginate(
            sorted_experiments, page_token, max_results
        )
        return PagedList(experiments, next_page_token)

    def get_experiment_by_name(self, experiment_name):
        def pagination_wrapper_func(number_to_get, next_page_token):
            return self.search_experiments(
                view_type=ViewType.ALL,
                max_results=number_to_get,
                filter_string=f"name = '{experiment_name}'",
                page_token=next_page_token,
            )

        experiments = get_results_from_paginated_fn(
            paginated_fn=pagination_wrapper_func,
            max_results_per_page=SEARCH_MAX_RESULTS_THRESHOLD,
            max_results=None,
        )
        return experiments[0] if len(experiments) > 0 else None

    def _create_experiment_with_id(self, name, experiment_id, artifact_uri, tags):
        if not artifact_uri:
            resolved_artifact_uri = append_to_uri_path(self.artifact_root_uri, str(experiment_id))
        else:
            resolved_artifact_uri = resolve_uri_if_local(artifact_uri)
        meta_dir = mkdir(self.root_directory, str(experiment_id))
        creation_time = get_current_time_millis()
        experiment = Experiment(
            experiment_id,
            name,
            resolved_artifact_uri,
            LifecycleStage.ACTIVE,
            creation_time=creation_time,
            last_update_time=creation_time,
        )
        experiment_dict = dict(experiment)
        # tags are added to the file system and are not written to this dict on write
        # As such, we should not include them in the meta file.
        del experiment_dict["tags"]
        write_yaml(meta_dir, FileStore.META_DATA_FILE_NAME, experiment_dict)
        if tags is not None:
            for tag in tags:
                self.set_experiment_tag(experiment_id, tag)
        return experiment_id

    def _validate_experiment_does_not_exist(self, name):
        experiment = self.get_experiment_by_name(name)
        if experiment is not None:
            if experiment.lifecycle_stage == LifecycleStage.DELETED:
                raise MlflowException(
                    f"Experiment {experiment.name!r} already exists in deleted state. "
                    "You can restore the experiment, or permanently delete the experiment "
                    "from the .trash folder (under tracking server's root folder) in order to "
                    "use this experiment name again.",
                    databricks_pb2.RESOURCE_ALREADY_EXISTS,
                )
            else:
                raise MlflowException(
                    f"Experiment '{experiment.name}' already exists.",
                    databricks_pb2.RESOURCE_ALREADY_EXISTS,
                )

    def create_experiment(self, name, artifact_location=None, tags=None):
        self._check_root_dir()
        _validate_experiment_name(name)

        if artifact_location:
            _validate_experiment_artifact_location_length(artifact_location)

        self._validate_experiment_does_not_exist(name)
        experiment_id = _generate_unique_integer_id()
        return self._create_experiment_with_id(name, str(experiment_id), artifact_location, tags)

    def _has_experiment(self, experiment_id):
        return self._get_experiment_path(experiment_id) is not None

    def _get_experiment(self, experiment_id, view_type=ViewType.ALL):
        self._check_root_dir()
        _validate_experiment_id(experiment_id)
        experiment_dir = self._get_experiment_path(experiment_id, view_type)
        if experiment_dir is None:
            raise MlflowException(
                f"Could not find experiment with ID {experiment_id}",
                databricks_pb2.RESOURCE_DOES_NOT_EXIST,
            )
        meta = FileStore._read_yaml(experiment_dir, FileStore.META_DATA_FILE_NAME)
        if meta is None:
            raise MissingConfigException(
                f"Experiment {experiment_id} is invalid with empty "
                f"{FileStore.META_DATA_FILE_NAME} in directory '{experiment_dir}'."
            )

        meta["tags"] = self.get_all_experiment_tags(experiment_id)
        experiment = _read_persisted_experiment_dict(meta)
        if experiment_id != experiment.experiment_id:
            logging.warning(
                "Experiment ID mismatch for exp %s. ID recorded as '%s' in meta data. "
                "Experiment will be ignored.",
                experiment_id,
                experiment.experiment_id,
                exc_info=True,
            )
            return None
        return experiment

    def get_experiment(self, experiment_id):
        """
        Fetch the experiment.
        Note: This API will search for active as well as deleted experiments.

        Args:
            experiment_id: Integer id for the experiment

        Returns:
            A single Experiment object if it exists, otherwise raises an Exception.
        """
        experiment_id = FileStore.DEFAULT_EXPERIMENT_ID if experiment_id is None else experiment_id
        experiment = self._get_experiment(experiment_id)
        if experiment is None:
            raise MlflowException(
                f"Experiment '{experiment_id}' does not exist.",
                databricks_pb2.RESOURCE_DOES_NOT_EXIST,
            )
        return experiment

    def delete_experiment(self, experiment_id):
        if str(experiment_id) == str(FileStore.DEFAULT_EXPERIMENT_ID):
            raise MlflowException(
                "Cannot delete the default experiment "
                f"'{FileStore.DEFAULT_EXPERIMENT_ID}'. This is an internally "
                f"reserved experiment."
            )
        experiment_dir = self._get_experiment_path(experiment_id, ViewType.ACTIVE_ONLY)
        if experiment_dir is None:
            raise MlflowException(
                f"Could not find experiment with ID {experiment_id}",
                databricks_pb2.RESOURCE_DOES_NOT_EXIST,
            )
        experiment = self._get_experiment(experiment_id)
        experiment._lifecycle_stage = LifecycleStage.DELETED
        deletion_time = get_current_time_millis()
        experiment._set_last_update_time(deletion_time)
        runs = self._list_run_infos(experiment_id, view_type=ViewType.ACTIVE_ONLY)
        for run_info in runs:
            if run_info is not None:
                new_info = run_info._copy_with_overrides(lifecycle_stage=LifecycleStage.DELETED)
                self._overwrite_run_info(new_info, deleted_time=deletion_time)
            else:
                logging.warning("Run metadata is in invalid state.")
        meta_dir = os.path.join(self.root_directory, experiment_id)
        overwrite_yaml(
            root=meta_dir,
            file_name=FileStore.META_DATA_FILE_NAME,
            data=dict(experiment),
        )
        mv(experiment_dir, self.trash_folder)

    def _hard_delete_experiment(self, experiment_id):
        """
        Permanently delete an experiment.
        This is used by the ``mlflow gc`` command line and is not intended to be used elsewhere.
        """
        experiment_dir = self._get_experiment_path(experiment_id, ViewType.DELETED_ONLY)
        shutil.rmtree(experiment_dir)

    def restore_experiment(self, experiment_id):
        experiment_dir = self._get_experiment_path(experiment_id, ViewType.DELETED_ONLY)
        if experiment_dir is None:
            raise MlflowException(
                f"Could not find deleted experiment with ID {experiment_id}",
                databricks_pb2.RESOURCE_DOES_NOT_EXIST,
            )
        conflict_experiment = self._get_experiment_path(experiment_id, ViewType.ACTIVE_ONLY)
        if conflict_experiment is not None:
            raise MlflowException(
                f"Cannot restore experiment with ID {experiment_id}. "
                "An experiment with same ID already exists.",
                databricks_pb2.RESOURCE_ALREADY_EXISTS,
            )
        mv(experiment_dir, self.root_directory)
        experiment = self._get_experiment(experiment_id)
        meta_dir = os.path.join(self.root_directory, experiment_id)
        experiment._lifecycle_stage = LifecycleStage.ACTIVE
        experiment._set_last_update_time(get_current_time_millis())
        runs = self._list_run_infos(experiment_id, view_type=ViewType.DELETED_ONLY)
        for run_info in runs:
            if run_info is not None:
                new_info = run_info._copy_with_overrides(lifecycle_stage=LifecycleStage.ACTIVE)
                self._overwrite_run_info(new_info, deleted_time=None)
            else:
                logging.warning("Run metadata is in invalid state.")
        overwrite_yaml(
            root=meta_dir,
            file_name=FileStore.META_DATA_FILE_NAME,
            data=dict(experiment),
        )

    def rename_experiment(self, experiment_id, new_name):
        _validate_experiment_name(new_name)
        meta_dir = os.path.join(self.root_directory, experiment_id)
        # if experiment is malformed, will raise error
        experiment = self._get_experiment(experiment_id)
        if experiment is None:
            raise MlflowException(
                f"Experiment '{experiment_id}' does not exist.",
                databricks_pb2.RESOURCE_DOES_NOT_EXIST,
            )
        self._validate_experiment_does_not_exist(new_name)
        experiment._set_name(new_name)
        experiment._set_last_update_time(get_current_time_millis())
        if experiment.lifecycle_stage != LifecycleStage.ACTIVE:
            raise Exception(
                "Cannot rename experiment in non-active lifecycle stage."
                f" Current stage: {experiment.lifecycle_stage}"
            )
        overwrite_yaml(
            root=meta_dir,
            file_name=FileStore.META_DATA_FILE_NAME,
            data=dict(experiment),
        )

    def delete_run(self, run_id):
        run_info = self._get_run_info(run_id)
        if run_info is None:
            raise MlflowException(
                f"Run '{run_id}' metadata is in invalid state.",
                databricks_pb2.INVALID_STATE,
            )
        new_info = run_info._copy_with_overrides(lifecycle_stage=LifecycleStage.DELETED)
        self._overwrite_run_info(new_info, deleted_time=get_current_time_millis())

    def _hard_delete_run(self, run_id):
        """
        Permanently delete a run (metadata and metrics, tags, parameters).
        This is used by the ``mlflow gc`` command line and is not intended to be used elsewhere.
        """
        _, run_dir = self._find_run_root(run_id)
        shutil.rmtree(run_dir)

    def _get_deleted_runs(self, older_than=0):
        """
        Get all deleted run ids.

        Args:
            older_than: get runs that is older than this variable in number of milliseconds.
                        defaults to 0 ms to get all deleted runs.
        """
        current_time = get_current_time_millis()
        experiment_ids = self._get_active_experiments() + self._get_deleted_experiments()
        deleted_runs = self.search_runs(
            experiment_ids=experiment_ids,
            filter_string="",
            run_view_type=ViewType.DELETED_ONLY,
        )
        deleted_run_ids = []
        for deleted_run in deleted_runs:
            _, run_dir = self._find_run_root(deleted_run.info.run_id)
            meta = read_yaml(run_dir, FileStore.META_DATA_FILE_NAME)
            if "deleted_time" not in meta or current_time - int(meta["deleted_time"]) >= older_than:
                deleted_run_ids.append(deleted_run.info.run_id)

        return deleted_run_ids

    def restore_run(self, run_id):
        run_info = self._get_run_info(run_id)
        if run_info is None:
            raise MlflowException(
                f"Run '{run_id}' metadata is in invalid state.",
                databricks_pb2.INVALID_STATE,
            )
        new_info = run_info._copy_with_overrides(lifecycle_stage=LifecycleStage.ACTIVE)
        self._overwrite_run_info(new_info, deleted_time=None)

    def _find_experiment_folder(self, run_path):
        """
        Given a run path, return the parent directory for its experiment.
        """
        parent = get_parent_dir(run_path)
        if os.path.basename(parent) == FileStore.TRASH_FOLDER_NAME:
            return get_parent_dir(parent)
        return parent

    def _find_run_root(self, run_uuid):
        _validate_run_id(run_uuid)
        self._check_root_dir()
        all_experiments = self._get_active_experiments(True) + self._get_deleted_experiments(True)
        for experiment_dir in all_experiments:
            runs = find(experiment_dir, run_uuid, full_path=True)
            if len(runs) == 0:
                continue
            return os.path.basename(os.path.abspath(experiment_dir)), runs[0]
        return None, None

    def update_run_info(self, run_id, run_status, end_time, run_name):
        _validate_run_id(run_id)
        run_info = self._get_run_info(run_id)
        check_run_is_active(run_info)
        new_info = run_info._copy_with_overrides(run_status, end_time, run_name=run_name)
        if run_name:
            self._set_run_tag(run_info, RunTag(MLFLOW_RUN_NAME, run_name))
        self._overwrite_run_info(new_info)
        return new_info

    def create_run(self, experiment_id, user_id, start_time, tags, run_name):
        """
        Creates a run with the specified attributes.
        """
        experiment_id = FileStore.DEFAULT_EXPERIMENT_ID if experiment_id is None else experiment_id
        experiment = self.get_experiment(experiment_id)
        if experiment is None:
            raise MlflowException(
                f"Could not create run under experiment with ID {experiment_id} - no such "
                "experiment exists.",
                databricks_pb2.RESOURCE_DOES_NOT_EXIST,
            )
        if experiment.lifecycle_stage != LifecycleStage.ACTIVE:
            raise MlflowException(
                f"Could not create run under non-active experiment with ID {experiment_id}.",
                databricks_pb2.INVALID_STATE,
            )
        tags = tags or []
        run_name_tag = _get_run_name_from_tags(tags)
        if run_name and run_name_tag and run_name != run_name_tag:
            raise MlflowException(
                "Both 'run_name' argument and 'mlflow.runName' tag are specified, but with "
                f"different values (run_name='{run_name}', mlflow.runName='{run_name_tag}').",
                INVALID_PARAMETER_VALUE,
            )
        run_name = run_name or run_name_tag or _generate_random_name()
        if not run_name_tag:
            tags.append(RunTag(key=MLFLOW_RUN_NAME, value=run_name))
        run_uuid = uuid.uuid4().hex
        artifact_uri = self._get_artifact_dir(experiment_id, run_uuid)
        run_info = RunInfo(
            run_id=run_uuid,
            run_name=run_name,
            experiment_id=experiment_id,
            artifact_uri=artifact_uri,
            user_id=user_id,
            status=RunStatus.to_string(RunStatus.RUNNING),
            start_time=start_time,
            end_time=None,
            lifecycle_stage=LifecycleStage.ACTIVE,
        )
        # Persist run metadata and create directories for logging metrics, parameters, artifacts
        run_dir = self._get_run_dir(run_info.experiment_id, run_info.run_id)
        mkdir(run_dir)
        run_info_dict = _make_persisted_run_info_dict(run_info)
        run_info_dict["deleted_time"] = None
        write_yaml(run_dir, FileStore.META_DATA_FILE_NAME, run_info_dict)
        mkdir(run_dir, FileStore.METRICS_FOLDER_NAME)
        mkdir(run_dir, FileStore.PARAMS_FOLDER_NAME)
        mkdir(run_dir, FileStore.ARTIFACTS_FOLDER_NAME)
        for tag in tags:
            self.set_tag(run_uuid, tag)
        return self.get_run(run_id=run_uuid)

    def get_run(self, run_id):
        """
        Note: Will get both active and deleted runs.
        """
        _validate_run_id(run_id)
        run_info = self._get_run_info(run_id)
        if run_info is None:
            raise MlflowException(
                f"Run '{run_id}' metadata is in invalid state.",
                databricks_pb2.INVALID_STATE,
            )
        return self._get_run_from_info(run_info)

    def _get_run_from_info(self, run_info):
        metrics = self._get_all_metrics(run_info)
        params = self._get_all_params(run_info)
        tags = self._get_all_tags(run_info)
        inputs: RunInputs = self._get_all_inputs(run_info)
        outputs: RunOutputs = self._get_all_outputs(run_info)
        if not run_info.run_name:
            run_name = _get_run_name_from_tags(tags)
            if run_name:
                run_info._set_run_name(run_name)
        return Run(run_info, RunData(metrics, params, tags), inputs, outputs)

    def _get_run_info(self, run_uuid):
        """
        Note: Will get both active and deleted runs.
        """
        exp_id, run_dir = self._find_run_root(run_uuid)
        if run_dir is None:
            raise MlflowException(
                f"Run '{run_uuid}' not found", databricks_pb2.RESOURCE_DOES_NOT_EXIST
            )
        run_info = self._get_run_info_from_dir(run_dir)
        if run_info.experiment_id != exp_id:
            raise MlflowException(
                f"Run '{run_uuid}' metadata is in invalid state.",
                databricks_pb2.INVALID_STATE,
            )
        return run_info

    def _get_run_info_from_dir(self, run_dir):
        meta = FileStore._read_yaml(run_dir, FileStore.META_DATA_FILE_NAME)
        return _read_persisted_run_info_dict(meta)

    def _get_run_files(self, run_info, resource_type):
        run_dir = self._get_run_dir(run_info.experiment_id, run_info.run_id)
        # run_dir exists since run validity has been confirmed above.
        if resource_type == "metric":
            subfolder_name = FileStore.METRICS_FOLDER_NAME
        elif resource_type == "param":
            subfolder_name = FileStore.PARAMS_FOLDER_NAME
        elif resource_type == "tag":
            subfolder_name = FileStore.TAGS_FOLDER_NAME
        else:
            raise Exception("Looking for unknown resource under run.")
        return self._get_resource_files(run_dir, subfolder_name)

    def _get_experiment_files(self, experiment_id):
        _validate_experiment_id(experiment_id)
        experiment_dir = self._get_experiment_path(experiment_id, assert_exists=True)
        return self._get_resource_files(experiment_dir, FileStore.EXPERIMENT_TAGS_FOLDER_NAME)

    def _get_resource_files(self, root_dir, subfolder_name):
        source_dirs = find(root_dir, subfolder_name, full_path=True)
        if len(source_dirs) == 0:
            return root_dir, []
        file_names = []
        for root, _, files in os.walk(source_dirs[0]):
            for name in files:
                abspath = os.path.join(root, name)
                file_names.append(os.path.relpath(abspath, source_dirs[0]))
        if sys.platform == "win32":
            # Turn metric relative path into metric name.
            # Metrics can have '/' in the name. On windows, '/' is interpreted as a separator.
            # When the metric is read back the path will use '\' for separator.
            # We need to translate the path into posix path.
            from mlflow.utils.file_utils import relative_path_to_artifact_path

            file_names = [relative_path_to_artifact_path(x) for x in file_names]
        return source_dirs[0], file_names

    @staticmethod
    def _get_metric_from_file(
        parent_path: str, metric_name: str, run_id: str, exp_id: str
    ) -> Metric:
        _validate_metric_name(metric_name)
        metric_objs = [
            FileStore._get_metric_from_line(run_id, metric_name, line, exp_id)
            for line in read_file_lines(parent_path, metric_name)
        ]
        if len(metric_objs) == 0:
            raise ValueError(f"Metric '{metric_name}' is malformed. No data found.")
        # Python performs element-wise comparison of equal-length tuples, ordering them
        # based on their first differing element. Therefore, we use max() operator to find the
        # largest value at the largest timestamp. For more information, see
        # https://docs.python.org/3/reference/expressions.html#value-comparisons
        return max(metric_objs, key=lambda m: (m.step, m.timestamp, m.value))

    def get_all_metrics(self, run_uuid):
        _validate_run_id(run_uuid)
        run_info = self._get_run_info(run_uuid)
        return self._get_all_metrics(run_info)

    def _get_all_metrics(self, run_info):
        parent_path, metric_files = self._get_run_files(run_info, "metric")
        metrics = []
        for metric_file in metric_files:
            metrics.append(
                self._get_metric_from_file(
                    parent_path, metric_file, run_info.run_id, run_info.experiment_id
                )
            )
        return metrics

    @staticmethod
    def _get_metric_from_line(
        run_id: str, metric_name: str, metric_line: str, exp_id: str
    ) -> Metric:
        metric_parts = metric_line.strip().split(" ")
        if len(metric_parts) != 2 and len(metric_parts) != 3 and len(metric_parts) != 5:
            raise MlflowException(
                f"Metric '{metric_name}' is malformed; persisted metric data contained "
                f"{len(metric_parts)} fields. Expected 2, 3, or 5 fields. "
                f"Experiment id: {exp_id}",
                databricks_pb2.INTERNAL_ERROR,
            )
        ts = int(metric_parts[0])
        val = float(metric_parts[1])
        step = int(metric_parts[2]) if len(metric_parts) == 3 else 0
        dataset_name = str(metric_parts[3]) if len(metric_parts) == 5 else None
        dataset_digest = str(metric_parts[4]) if len(metric_parts) == 5 else None
        return Metric(
            key=metric_name,
            value=val,
            timestamp=ts,
            step=step,
            dataset_name=dataset_name,
            dataset_digest=dataset_digest,
            run_id=run_id,
        )

    def get_metric_history(self, run_id, metric_key, max_results=None, page_token=None):
        """
        Return all logged values for a given metric.

        Args:
            run_id: Unique identifier for run.
            metric_key: Metric name within the run.
            max_results: An indicator for paginated results.
            page_token: Token indicating the page of metric history to fetch.

        Returns:
            A :py:class:`mlflow.store.entities.paged_list.PagedList` of
            :py:class:`mlflow.entities.Metric` entities if ``metric_key`` values
            have been logged to the ``run_id``, else an empty list.

        """
        _validate_run_id(run_id)
        _validate_metric_name(metric_key)
        run_info = self._get_run_info(run_id)

        parent_path, metric_files = self._get_run_files(run_info, "metric")
        if metric_key not in metric_files:
            return PagedList([], None)

        all_lines = read_file_lines(parent_path, metric_key)

        all_metrics = [
            FileStore._get_metric_from_line(run_id, metric_key, line, run_info.experiment_id)
            for line in all_lines
        ]

        if max_results is None:
            # If no max_results specified, return all metrics but handle page_token if provided
            offset = SearchUtils.parse_start_offset_from_page_token(page_token)
            metrics = all_metrics[offset:]
            next_page_token = None
        else:
            metrics, next_page_token = SearchUtils.paginate(all_metrics, page_token, max_results)

        return PagedList(metrics, next_page_token)

    @staticmethod
    def _get_param_from_file(parent_path, param_name):
        _validate_param_name(param_name)
        value = read_file(parent_path, param_name)
        return Param(param_name, value)

    def get_all_params(self, run_uuid):
        _validate_run_id(run_uuid)
        run_info = self._get_run_info(run_uuid)
        return self._get_all_params(run_info)

    def _get_all_params(self, run_info):
        parent_path, param_files = self._get_run_files(run_info, "param")
        params = []
        for param_file in param_files:
            params.append(self._get_param_from_file(parent_path, param_file))
        return params

    @staticmethod
    def _get_experiment_tag_from_file(parent_path, tag_name):
        _validate_tag_name(tag_name)
        tag_data = read_file(parent_path, tag_name)
        return ExperimentTag(tag_name, tag_data)

    def get_all_experiment_tags(self, exp_id):
        parent_path, tag_files = self._get_experiment_files(exp_id)
        tags = []
        for tag_file in tag_files:
            tags.append(self._get_experiment_tag_from_file(parent_path, tag_file))
        return tags

    @staticmethod
    def _get_tag_from_file(parent_path, tag_name):
        _validate_tag_name(tag_name)
        tag_data = read_file(parent_path, tag_name)
        return RunTag(tag_name, tag_data)

    def get_all_tags(self, run_uuid):
        _validate_run_id(run_uuid)
        run_info = self._get_run_info(run_uuid)
        return self._get_all_tags(run_info)

    def _get_all_tags(self, run_info):
        parent_path, tag_files = self._get_run_files(run_info, "tag")
        tags = []
        for tag_file in tag_files:
            tags.append(self._get_tag_from_file(parent_path, tag_file))
        return tags

    def _list_run_infos(self, experiment_id, view_type):
        self._check_root_dir()
        if not self._has_experiment(experiment_id):
            return []
        experiment_dir = self._get_experiment_path(experiment_id, assert_exists=True)
        run_dirs = list_all(
            experiment_dir,
            filter_func=lambda x: all(
                os.path.basename(os.path.normpath(x)) != reservedFolderName
                for reservedFolderName in FileStore.RESERVED_EXPERIMENT_FOLDERS
            )
            and os.path.isdir(x),
            full_path=True,
        )
        run_infos = []
        for r_dir in run_dirs:
            try:
                # trap and warn known issues, will raise unexpected exceptions to caller
                run_info = self._get_run_info_from_dir(r_dir)
                if run_info.experiment_id != experiment_id:
                    logging.warning(
                        "Wrong experiment ID (%s) recorded for run '%s'. "
                        "It should be %s. Run will be ignored.",
                        str(run_info.experiment_id),
                        str(run_info.run_id),
                        str(experiment_id),
                        exc_info=True,
                    )
                    continue
                if LifecycleStage.matches_view_type(view_type, run_info.lifecycle_stage):
                    run_infos.append(run_info)
            except MissingConfigException as rnfe:
                # trap malformed run exception and log
                # this is at debug level because if the same store is used for
                # artifact storage, it's common the folder is not a run folder
                r_id = os.path.basename(r_dir)
                logging.debug(
                    "Malformed run '%s'. Detailed error %s",
                    r_id,
                    str(rnfe),
                    exc_info=True,
                )
        return run_infos

    def _search_runs(
        self,
        experiment_ids,
        filter_string,
        run_view_type,
        max_results,
        order_by,
        page_token,
    ):
        if max_results > SEARCH_MAX_RESULTS_THRESHOLD:
            raise MlflowException(
                "Invalid value for request parameter max_results. It must be at "
                f"most {SEARCH_MAX_RESULTS_THRESHOLD}, but got value {max_results}",
                databricks_pb2.INVALID_PARAMETER_VALUE,
            )
        runs = []
        for experiment_id in experiment_ids:
            run_infos = self._list_run_infos(experiment_id, run_view_type)
            runs.extend(self._get_run_from_info(r) for r in run_infos)
        filtered = SearchUtils.filter(runs, filter_string)
        sorted_runs = SearchUtils.sort(filtered, order_by)
        runs, next_page_token = SearchUtils.paginate(sorted_runs, page_token, max_results)
        return runs, next_page_token

    def log_metric(self, run_id: str, metric: Metric):
        _validate_run_id(run_id)
        _validate_metric(metric.key, metric.value, metric.timestamp, metric.step)
        run_info = self._get_run_info(run_id)
        check_run_is_active(run_info)
        self._log_run_metric(run_info, metric)
        if metric.model_id is not None:
            self._log_model_metric(
                experiment_id=run_info.experiment_id,
                model_id=metric.model_id,
                run_id=run_id,
                metric=metric,
            )

    def _log_run_metric(self, run_info, metric):
        metric_path = self._get_metric_path(run_info.experiment_id, run_info.run_id, metric.key)
        make_containing_dirs(metric_path)
        if metric.dataset_name is not None and metric.dataset_digest is not None:
            append_to(
                metric_path,
                f"{metric.timestamp} {metric.value} {metric.step} {metric.dataset_name} "
                f"{metric.dataset_digest}\n",
            )
        else:
            append_to(metric_path, f"{metric.timestamp} {metric.value} {metric.step}\n")

    def _log_model_metric(self, experiment_id: str, model_id: str, run_id: str, metric: Metric):
        metric_path = self._get_model_metric_path(
            experiment_id=experiment_id, model_id=model_id, metric_key=metric.key
        )
        make_containing_dirs(metric_path)
        if metric.dataset_name is not None and metric.dataset_digest is not None:
            append_to(
                metric_path,
                f"{metric.timestamp} {metric.value} {metric.step} {run_id} {metric.dataset_name} "
                f"{metric.dataset_digest}\n",
            )
        else:
            append_to(metric_path, f"{metric.timestamp} {metric.value} {metric.step} {run_id}\n")

    def _writeable_value(self, tag_value):
        if tag_value is None:
            return ""
        elif is_string_type(tag_value):
            return tag_value
        else:
            return str(tag_value)

    def log_param(self, run_id, param):
        _validate_run_id(run_id)
        param = _validate_param(param.key, param.value)
        run_info = self._get_run_info(run_id)
        check_run_is_active(run_info)
        self._log_run_param(run_info, param)

    def _log_run_param(self, run_info, param):
        param_path = self._get_param_path(run_info.experiment_id, run_info.run_id, param.key)
        writeable_param_value = self._writeable_value(param.value)
        if os.path.exists(param_path):
            self._validate_new_param_value(
                param_path=param_path,
                param_key=param.key,
                run_id=run_info.run_id,
                new_value=writeable_param_value,
            )
        make_containing_dirs(param_path)
        write_to(param_path, writeable_param_value)

    def _validate_new_param_value(self, param_path, param_key, run_id, new_value):
        """
        When logging a parameter with a key that already exists, this function is used to
        enforce immutability by verifying that the specified parameter value matches the existing
        value.
        :raises: py:class:`mlflow.exceptions.MlflowException` if the specified new parameter value
                 does not match the existing parameter value.
        """
        with open(param_path) as param_file:
            current_value = param_file.read()
        if current_value != new_value:
            raise MlflowException(
                f"Changing param values is not allowed. Param with key='{param_key}' was already"
                f" logged with value='{current_value}' for run ID='{run_id}'. Attempted logging"
                f" new value '{new_value}'.",
                databricks_pb2.INVALID_PARAMETER_VALUE,
            )

    def set_experiment_tag(self, experiment_id, tag):
        """
        Set a tag for the specified experiment

        Args:
            experiment_id: String ID of the experiment
            tag: ExperimentRunTag instance to log
        """
        _validate_tag_name(tag.key)
        experiment = self.get_experiment(experiment_id)
        if experiment.lifecycle_stage != LifecycleStage.ACTIVE:
            raise MlflowException(
                f"The experiment {experiment.experiment_id} must be in the 'active' "
                "lifecycle_stage to set tags",
                error_code=databricks_pb2.INVALID_PARAMETER_VALUE,
            )
        tag_path = self._get_experiment_tag_path(experiment_id, tag.key)
        make_containing_dirs(tag_path)
        write_to(tag_path, self._writeable_value(tag.value))

    def delete_experiment_tag(self, experiment_id, key):
        """
        Delete a tag from the specified experiment

        Args:
            experiment_id: String ID of the experiment
            key: String name of the tag to be deleted
        """
        experiment = self.get_experiment(experiment_id)
        if experiment.lifecycle_stage != LifecycleStage.ACTIVE:
            raise MlflowException(
                f"The experiment {experiment.experiment_id} must be in the 'active' "
                "lifecycle_stage to delete tags",
                error_code=databricks_pb2.INVALID_PARAMETER_VALUE,
            )
        tag_path = self._get_experiment_tag_path(experiment_id, key)
        if not exists(tag_path):
            raise MlflowException(
                f"No tag with name: {key} in experiment with id {experiment_id}",
                error_code=RESOURCE_DOES_NOT_EXIST,
            )
        os.remove(tag_path)

    def set_tag(self, run_id, tag):
        _validate_run_id(run_id)
        _validate_tag_name(tag.key)
        run_info = self._get_run_info(run_id)
        check_run_is_active(run_info)
        self._set_run_tag(run_info, tag)
        if tag.key == MLFLOW_RUN_NAME:
            run_status = RunStatus.from_string(run_info.status)
            self.update_run_info(run_id, run_status, run_info.end_time, tag.value)

    def _set_run_tag(self, run_info, tag):
        tag_path = self._get_tag_path(run_info.experiment_id, run_info.run_id, tag.key)
        make_containing_dirs(tag_path)
        # Don't add trailing newline
        write_to(tag_path, self._writeable_value(tag.value))

    def delete_tag(self, run_id, key):
        """
        Delete a tag from a run. This is irreversible.

        Args:
            run_id: String ID of the run.
            key: Name of the tag.
        """
        _validate_run_id(run_id)
        run_info = self._get_run_info(run_id)
        check_run_is_active(run_info)
        tag_path = self._get_tag_path(run_info.experiment_id, run_id, key)
        if not exists(tag_path):
            raise MlflowException(
                f"No tag with name: {key} in run with id {run_id}",
                error_code=RESOURCE_DOES_NOT_EXIST,
            )
        os.remove(tag_path)

    def _overwrite_run_info(self, run_info, deleted_time=None):
        run_dir = self._get_run_dir(run_info.experiment_id, run_info.run_id)
        run_info_dict = _make_persisted_run_info_dict(run_info)
        if deleted_time is not None:
            run_info_dict["deleted_time"] = deleted_time
        write_yaml(run_dir, FileStore.META_DATA_FILE_NAME, run_info_dict, overwrite=True)

    def log_batch(self, run_id, metrics, params, tags):
        _validate_run_id(run_id)
        metrics, params, tags = _validate_batch_log_data(metrics, params, tags)
        _validate_batch_log_limits(metrics, params, tags)
        _validate_param_keys_unique(params)
        run_info = self._get_run_info(run_id)
        check_run_is_active(run_info)
        try:
            for param in params:
                self._log_run_param(run_info, param)
            for metric in metrics:
                self._log_run_metric(run_info, metric)
                if metric.model_id is not None:
                    self._log_model_metric(
                        experiment_id=run_info.experiment_id,
                        model_id=metric.model_id,
                        run_id=run_id,
                        metric=metric,
                    )
            for tag in tags:
                # NB: If the tag run name value is set, update the run info to assure
                # synchronization.
                if tag.key == MLFLOW_RUN_NAME:
                    run_status = RunStatus.from_string(run_info.status)
                    self.update_run_info(run_id, run_status, run_info.end_time, tag.value)
                self._set_run_tag(run_info, tag)
        except Exception as e:
            raise MlflowException(e, INTERNAL_ERROR)

    def record_logged_model(self, run_id, mlflow_model):
        from mlflow.models import Model

        if not isinstance(mlflow_model, Model):
            raise TypeError(
                f"Argument 'mlflow_model' should be mlflow.models.Model, got '{type(mlflow_model)}'"
            )
        _validate_run_id(run_id)
        run_info = self._get_run_info(run_id)
        check_run_is_active(run_info)
        model_dict = mlflow_model.get_tags_dict()
        run_info = self._get_run_info(run_id)
        path = self._get_tag_path(run_info.experiment_id, run_info.run_id, MLFLOW_LOGGED_MODELS)
        if os.path.exists(path):
            with open(path) as f:
                model_list = json.loads(f.read())
        else:
            model_list = []
        tag = RunTag(MLFLOW_LOGGED_MODELS, json.dumps(model_list + [model_dict]))

        try:
            self._set_run_tag(run_info, tag)
        except Exception as e:
            raise MlflowException(e, INTERNAL_ERROR)

    def log_inputs(
        self,
        run_id: str,
        datasets: Optional[list[DatasetInput]] = None,
        models: Optional[list[LoggedModelInput]] = None,
    ):
        """
        Log inputs, such as datasets and models, to the specified run.

        Args:
            run_id: String id for the run
            datasets: List of :py:class:`mlflow.entities.DatasetInput` instances to log
                as inputs to the run.
            models: List of :py:class:`mlflow.entities.LoggedModelInput` instances to log
                as inputs to the run.

        Returns:
            None.
        """
        _validate_run_id(run_id)
        run_info = self._get_run_info(run_id)
        check_run_is_active(run_info)

        if datasets is None and models is None:
            return

        experiment_dir = self._get_experiment_path(run_info.experiment_id, assert_exists=True)
        run_dir = self._get_run_dir(run_info.experiment_id, run_id)

        for dataset_input in datasets or []:
            dataset = dataset_input.dataset
            dataset_id = FileStore._get_dataset_id(
                dataset_name=dataset.name, dataset_digest=dataset.digest
            )
            dataset_dir = os.path.join(experiment_dir, FileStore.DATASETS_FOLDER_NAME, dataset_id)
            if not os.path.exists(dataset_dir):
                os.makedirs(dataset_dir, exist_ok=True)
                write_yaml(dataset_dir, FileStore.META_DATA_FILE_NAME, dict(dataset))

            input_id = FileStore._get_dataset_input_id(dataset_id=dataset_id, run_id=run_id)
            input_dir = os.path.join(run_dir, FileStore.INPUTS_FOLDER_NAME, input_id)
            if not os.path.exists(input_dir):
                os.makedirs(input_dir, exist_ok=True)
                fs_input = FileStore._FileStoreInput(
                    source_type=InputVertexType.DATASET,
                    source_id=dataset_id,
                    destination_type=InputVertexType.RUN,
                    destination_id=run_id,
                    tags={tag.key: tag.value for tag in dataset_input.tags},
                )
                fs_input.write_yaml(input_dir, FileStore.META_DATA_FILE_NAME)

        for model_input in models or []:
            model_id = model_input.model_id
            input_id = FileStore._get_model_input_id(model_id=model_id, run_id=run_id)
            input_dir = os.path.join(run_dir, FileStore.INPUTS_FOLDER_NAME, input_id)
            if not os.path.exists(input_dir):
                os.makedirs(input_dir, exist_ok=True)
                fs_input = FileStore._FileStoreInput(
                    source_type=InputVertexType.MODEL,
                    source_id=model_id,
                    destination_type=InputVertexType.RUN,
                    destination_id=run_id,
                    tags={},
                )
                fs_input.write_yaml(input_dir, FileStore.META_DATA_FILE_NAME)

    def log_outputs(self, run_id: str, models: list[LoggedModelOutput]):
        """
        Log outputs, such as models, to the specified run.

        Args:
            run_id: String id for the run
            models: List of :py:class:`mlflow.entities.LoggedModelOutput` instances to log
                as outputs of the run.

        Returns:
            None.
        """
        _validate_run_id(run_id)
        run_info = self._get_run_info(run_id)
        check_run_is_active(run_info)

        if models is None:
            return

        run_dir = self._get_run_dir(run_info.experiment_id, run_id)

        for model_output in models:
            model_id = model_output.model_id
            output_dir = os.path.join(run_dir, FileStore.OUTPUTS_FOLDER_NAME, model_id)
            if not os.path.exists(output_dir):
                os.makedirs(output_dir, exist_ok=True)
                fs_output = FileStore._FileStoreOutput(
                    source_type=OutputVertexType.RUN_OUTPUT,
                    source_id=model_id,
                    destination_type=OutputVertexType.MODEL_OUTPUT,
                    destination_id=run_id,
                    tags={},
                    step=model_output.step,
                )
                fs_output.write_yaml(output_dir, FileStore.META_DATA_FILE_NAME)

    @staticmethod
    def _get_dataset_id(dataset_name: str, dataset_digest: str) -> str:
        md5 = hashlib.md5(dataset_name.encode("utf-8"), usedforsecurity=False)
        md5.update(dataset_digest.encode("utf-8"))
        return md5.hexdigest()

    @staticmethod
    def _get_dataset_input_id(dataset_id: str, run_id: str) -> str:
        md5 = hashlib.md5(dataset_id.encode("utf-8"), usedforsecurity=False)
        md5.update(run_id.encode("utf-8"))
        return md5.hexdigest()

    @staticmethod
    def _get_model_input_id(model_id: str, run_id: str) -> str:
        md5 = hashlib.md5(model_id.encode("utf-8"), usedforsecurity=False)
        md5.update(run_id.encode("utf-8"))
        return md5.hexdigest()

    class _FileStoreInput(NamedTuple):
        source_type: int
        source_id: str
        destination_type: int
        destination_id: str
        tags: dict[str, str]

        def write_yaml(self, root: str, file_name: str):
            dict_for_yaml = {
                "source_type": InputVertexType.Name(self.source_type),
                "source_id": self.source_id,
                "destination_type": InputVertexType.Name(self.destination_type),
                "destination_id": self.source_id,
                "tags": self.tags,
            }
            write_yaml(root, file_name, dict_for_yaml)

        @classmethod
        def from_yaml(cls, root, file_name):
            dict_from_yaml = FileStore._read_yaml(root, file_name)
            return cls(
                source_type=InputVertexType.Value(dict_from_yaml["source_type"]),
                source_id=dict_from_yaml["source_id"],
                destination_type=InputVertexType.Value(dict_from_yaml["destination_type"]),
                destination_id=dict_from_yaml["destination_id"],
                tags=dict_from_yaml["tags"],
            )

    class _FileStoreOutput(NamedTuple):
        source_type: int
        source_id: str
        destination_type: int
        destination_id: str
        tags: dict[str, str]
        step: int

        def write_yaml(self, root: str, file_name: str):
            dict_for_yaml = {
                "source_type": OutputVertexType.Name(self.source_type),
                "source_id": self.source_id,
                "destination_type": OutputVertexType.Name(self.destination_type),
                "destination_id": self.source_id,
                "tags": self.tags,
                "step": self.step,
            }
            write_yaml(root, file_name, dict_for_yaml)

        @classmethod
        def from_yaml(cls, root, file_name):
            dict_from_yaml = FileStore._read_yaml(root, file_name)
            return cls(
                source_type=OutputVertexType.Value(dict_from_yaml["source_type"]),
                source_id=dict_from_yaml["source_id"],
                destination_type=OutputVertexType.Value(dict_from_yaml["destination_type"]),
                destination_id=dict_from_yaml["destination_id"],
                tags=dict_from_yaml["tags"],
                step=dict_from_yaml["step"],
            )

    def _get_all_inputs(self, run_info: RunInfo) -> RunInputs:
        run_dir = self._get_run_dir(run_info.experiment_id, run_info.run_id)
        inputs_parent_path = os.path.join(run_dir, FileStore.INPUTS_FOLDER_NAME)
        if not os.path.exists(inputs_parent_path):
            return RunInputs(dataset_inputs=[], model_inputs=[])

        experiment_dir = self._get_experiment_path(run_info.experiment_id, assert_exists=True)
        dataset_inputs = self._get_dataset_inputs(run_info, inputs_parent_path, experiment_dir)
        model_inputs = self._get_model_inputs(inputs_parent_path, experiment_dir)
        return RunInputs(dataset_inputs=dataset_inputs, model_inputs=model_inputs)

    def _get_dataset_inputs(
        self, run_info: RunInfo, inputs_parent_path: str, experiment_dir_path: str
    ) -> list[DatasetInput]:
        datasets_parent_path = os.path.join(experiment_dir_path, FileStore.DATASETS_FOLDER_NAME)
        if not os.path.exists(datasets_parent_path):
            return []

        dataset_dirs = os.listdir(datasets_parent_path)
        dataset_inputs = []
        for input_dir in os.listdir(inputs_parent_path):
            input_dir_full_path = os.path.join(inputs_parent_path, input_dir)
            fs_input = FileStore._FileStoreInput.from_yaml(
                input_dir_full_path, FileStore.META_DATA_FILE_NAME
            )
            if fs_input.source_type != InputVertexType.DATASET:
                continue

            matching_dataset_dirs = [d for d in dataset_dirs if d == fs_input.source_id]
            if not matching_dataset_dirs:
                logging.warning(
                    f"Failed to find dataset with ID '{fs_input.source_id}' referenced as an input"
                    f" of the run with ID '{run_info.run_id}'. Skipping."
                )
                continue
            elif len(matching_dataset_dirs) > 1:
                logging.warning(
                    f"Found multiple datasets with ID '{fs_input.source_id}'. Using the first one."
                )

            dataset_dir = matching_dataset_dirs[0]
            dataset = FileStore._get_dataset_from_dir(datasets_parent_path, dataset_dir)
            dataset_input = DatasetInput(
                dataset=dataset,
                tags=[InputTag(key=key, value=value) for key, value in fs_input.tags.items()],
            )
            dataset_inputs.append(dataset_input)

        return dataset_inputs

    def _get_model_inputs(
        self, inputs_parent_path: str, experiment_dir_path: str
    ) -> list[LoggedModelInput]:
        model_inputs = []
        for input_dir in os.listdir(inputs_parent_path):
            input_dir_full_path = os.path.join(inputs_parent_path, input_dir)
            fs_input = FileStore._FileStoreInput.from_yaml(
                input_dir_full_path, FileStore.META_DATA_FILE_NAME
            )
            if fs_input.source_type != InputVertexType.MODEL:
                continue

            model_input = LoggedModelInput(model_id=fs_input.source_id)
            model_inputs.append(model_input)

        return model_inputs

    def _get_all_outputs(self, run_info: RunInfo) -> RunOutputs:
        run_dir = self._get_run_dir(run_info.experiment_id, run_info.run_id)
        outputs_parent_path = os.path.join(run_dir, FileStore.OUTPUTS_FOLDER_NAME)
        if not os.path.exists(outputs_parent_path):
            return RunOutputs(model_outputs=[])

        experiment_dir = self._get_experiment_path(run_info.experiment_id, assert_exists=True)
        model_outputs = self._get_model_outputs(outputs_parent_path, experiment_dir)
        return RunOutputs(model_outputs=model_outputs)

    def _get_model_outputs(
        self, outputs_parent_path: str, experiment_dir: str
    ) -> list[LoggedModelOutput]:
        model_outputs = []
        for output_dir in os.listdir(outputs_parent_path):
            output_dir_full_path = os.path.join(outputs_parent_path, output_dir)
            fs_output = FileStore._FileStoreOutput.from_yaml(
                output_dir_full_path, FileStore.META_DATA_FILE_NAME
            )
            if fs_output.destination_type != OutputVertexType.MODEL_OUTPUT:
                continue

            model_output = LoggedModelOutput(model_id=fs_output.destination_id, step=fs_output.step)
            model_outputs.append(model_output)

        return model_outputs

    def _search_datasets(self, experiment_ids) -> list[_DatasetSummary]:
        """
        Return all dataset summaries associated to the given experiments.

        Args:
            experiment_ids: List of experiment ids to scope the search

        Returns:
            A List of :py:class:`mlflow.entities.DatasetSummary` entities.

        """

        @dataclass(frozen=True)
        class _SummaryTuple:
            experiment_id: str
            name: str
            digest: str
            context: str

        MAX_DATASET_SUMMARIES_RESULTS = 1000
        summaries = set()
        for experiment_id in experiment_ids:
            experiment_dir = self._get_experiment_path(experiment_id, assert_exists=True)
            run_dirs = list_all(
                experiment_dir,
                filter_func=lambda x: all(
                    os.path.basename(os.path.normpath(x)) != reservedFolderName
                    for reservedFolderName in FileStore.RESERVED_EXPERIMENT_FOLDERS
                )
                and os.path.isdir(x),
                full_path=True,
            )
            for run_dir in run_dirs:
                run_info = self._get_run_info_from_dir(run_dir)
                run_inputs = self._get_all_inputs(run_info)
                for dataset_input in run_inputs.dataset_inputs:
                    context = None
                    for input_tag in dataset_input.tags:
                        if input_tag.key == MLFLOW_DATASET_CONTEXT:
                            context = input_tag.value
                            break
                    dataset = dataset_input.dataset
                    summaries.add(
                        _SummaryTuple(experiment_id, dataset.name, dataset.digest, context)
                    )
                    # If we reached MAX_DATASET_SUMMARIES_RESULTS entries, then return right away.
                    if len(summaries) == MAX_DATASET_SUMMARIES_RESULTS:
                        return [
                            _DatasetSummary(
                                experiment_id=summary.experiment_id,
                                name=summary.name,
                                digest=summary.digest,
                                context=summary.context,
                            )
                            for summary in summaries
                        ]

        return [
            _DatasetSummary(
                experiment_id=summary.experiment_id,
                name=summary.name,
                digest=summary.digest,
                context=summary.context,
            )
            for summary in summaries
        ]

    @staticmethod
    def _get_dataset_from_dir(parent_path, dataset_dir) -> Dataset:
        dataset_dict = FileStore._read_yaml(
            os.path.join(parent_path, dataset_dir), FileStore.META_DATA_FILE_NAME
        )
        return Dataset.from_dictionary(dataset_dict)

    @staticmethod
    def _read_yaml(root, file_name, retries=2):
        """
        Read data from yaml file and return as dictionary, retrying up to
        a specified number of times if the file contents are unexpectedly
        empty due to a concurrent write.

        Args:
            root: Directory name.
            file_name: File name. Expects to have '.yaml' extension.
            retries: The number of times to retry for unexpected empty content.

        Returns:
            Data in yaml file as dictionary.
        """

        def _read_helper(root, file_name, attempts_remaining=2):
            result = read_yaml(root, file_name)
            if result is not None or attempts_remaining == 0:
                return result
            else:
                time.sleep(0.1 * (3 - attempts_remaining))
                return _read_helper(root, file_name, attempts_remaining - 1)

        return _read_helper(root, file_name, attempts_remaining=retries)

    def _get_traces_artifact_dir(self, experiment_id, trace_id):
        return append_to_uri_path(
            self.get_experiment(experiment_id).artifact_location,
            FileStore.TRACES_FOLDER_NAME,
            trace_id,
            FileStore.ARTIFACTS_FOLDER_NAME,
        )

    def _save_trace_info(self, trace_info: TraceInfo, trace_dir, overwrite=False):
        """
        TraceInfo is saved into `traces` folder under the experiment, each trace
        is saved in the folder named by its trace_id.
        `request_metadata` and `tags` folder store their key-value pairs such that each
        key is the file name, and value is written as the string value.
        Detailed directories structure is as below:
        | - experiment_id
        |   - traces
        |     - trace_id1
        |       - trace_info.yaml
        |       - request_metadata
        |         - key
        |       - tags
        |     - trace_id2
        |     - ...
        |   - run_id1 ...
        |   - run_id2 ...
        """
        # Save basic trace info to TRACE_INFO_FILE_NAME
        trace_info_dict = self._convert_trace_info_to_dict(trace_info)
        write_yaml(
            trace_dir,
            FileStore.TRACE_INFO_FILE_NAME,
            trace_info_dict,
            overwrite=overwrite,
        )
        # Save trace_metadata to its own folder
        self._write_dict_to_trace_sub_folder(
            trace_dir,
            FileStore.TRACE_TRACE_METADATA_FOLDER_NAME,
            trace_info.trace_metadata,
        )
        # Save tags to its own folder
        self._write_dict_to_trace_sub_folder(
            trace_dir, FileStore.TRACE_TAGS_FOLDER_NAME, trace_info.tags
        )
        # Save assessments to its own folder
        for assessment in trace_info.assessments:
            self.create_assessment(assessment)

    def _convert_trace_info_to_dict(self, trace_info: TraceInfo):
        """
        Convert trace info to a dictionary for persistence.
        Drop request_metadata and tags as they're saved into separate files.
        """
        trace_info_dict = trace_info.to_dict()
        trace_info_dict.pop("trace_metadata", None)
        trace_info_dict.pop("tags", None)
        return trace_info_dict

    def _write_dict_to_trace_sub_folder(self, trace_dir, sub_folder, dictionary):
        mkdir(trace_dir, sub_folder)
        for key, value in dictionary.items():
            # always validate as tag name to make sure the file name is valid
            _validate_tag_name(key)
            tag_path = os.path.join(trace_dir, sub_folder, key)
            # value are written as strings
            write_to(tag_path, self._writeable_value(value))

    def _get_dict_from_trace_sub_folder(self, trace_dir, sub_folder):
        parent_path, files = self._get_resource_files(trace_dir, sub_folder)
        dictionary = {}
        for file_name in files:
            _validate_tag_name(file_name)
            value = read_file(parent_path, file_name)
            dictionary[file_name] = value
        return dictionary

    def start_trace(self, trace_info: TraceInfo) -> TraceInfo:
        """
        Create a trace using the V3 API format with a complete Trace object.

        Args:
            trace_info: The TraceInfo object to create in the backend.

        Returns:
            The created TraceInfo object from the backend.
        """
        _validate_experiment_id(trace_info.experiment_id)
        experiment_dir = self._get_experiment_path(
            trace_info.experiment_id, view_type=ViewType.ACTIVE_ONLY, assert_exists=True
        )

        # Create traces directory structure
        mkdir(experiment_dir, FileStore.TRACES_FOLDER_NAME)
        traces_dir = os.path.join(experiment_dir, FileStore.TRACES_FOLDER_NAME)
        mkdir(traces_dir, trace_info.trace_id)
        trace_dir = os.path.join(traces_dir, trace_info.trace_id)

        # Add artifact location to tags
        artifact_uri = self._get_traces_artifact_dir(trace_info.experiment_id, trace_info.trace_id)
        tags = dict(trace_info.tags)
        tags[MLFLOW_ARTIFACT_LOCATION] = artifact_uri

        # Create updated TraceInfo with artifact location tag
        trace_info.tags.update(tags)
        self._save_trace_info(trace_info, trace_dir)
        return trace_info

    def get_trace_info(self, trace_id: str) -> TraceInfo:
        """
        Get the trace matching the `trace_id`.

        Args:
            trace_id: String id of the trace to fetch.

        Returns:
            The fetched Trace object, of type ``mlflow.entities.TraceInfo``.
        """
        return self._get_trace_info_and_dir(trace_id)[0]

    def _get_trace_info_and_dir(self, trace_id: str) -> tuple[TraceInfo, str]:
        trace_dir = self._find_trace_dir(trace_id, assert_exists=True)
        trace_info = self._get_trace_info_from_dir(trace_dir)
        if trace_info and trace_info.trace_id != trace_id:
            raise MlflowException(
                f"Trace with ID '{trace_id}' metadata is in invalid state.",
                databricks_pb2.INVALID_STATE,
            )
        return trace_info, trace_dir

    def _find_trace_dir(self, trace_id, assert_exists=False):
        self._check_root_dir()
        all_experiments = self._get_active_experiments(True) + self._get_deleted_experiments(True)
        for experiment_dir in all_experiments:
            traces_dir = os.path.join(experiment_dir, FileStore.TRACES_FOLDER_NAME)
            if exists(traces_dir):
                if traces := find(traces_dir, trace_id, full_path=True):
                    return traces[0]
        if assert_exists:
            raise MlflowException(
                f"Trace with ID '{trace_id}' not found",
                RESOURCE_DOES_NOT_EXIST,
            )

    def _get_trace_info_from_dir(self, trace_dir) -> Optional[TraceInfo]:
        if not os.path.exists(os.path.join(trace_dir, FileStore.TRACE_INFO_FILE_NAME)):
            return None
        trace_info_dict = FileStore._read_yaml(trace_dir, FileStore.TRACE_INFO_FILE_NAME)
        trace_info = TraceInfo.from_dict(trace_info_dict)
        trace_info.trace_metadata = self._get_dict_from_trace_sub_folder(
            trace_dir, FileStore.TRACE_TRACE_METADATA_FOLDER_NAME
        )
        trace_info.tags = self._get_dict_from_trace_sub_folder(
            trace_dir, FileStore.TRACE_TAGS_FOLDER_NAME
        )
        trace_info.assessments = self._load_assessments(trace_info.trace_id)
        return trace_info

    def set_trace_tag(self, trace_id: str, key: str, value: str):
        """
        Set a tag on the trace with the given trace_id.

        Args:
            trace_id: The ID of the trace.
            key: The string key of the tag.
            value: The string value of the tag.
        """
        trace_dir = self._find_trace_dir(trace_id, assert_exists=True)
        self._write_dict_to_trace_sub_folder(
            trace_dir, FileStore.TRACE_TAGS_FOLDER_NAME, {key: value}
        )

    def delete_trace_tag(self, trace_id: str, key: str):
        """
        Delete a tag on the trace with the given trace_id.

        Args:
            trace_id: The ID of the trace.
            key: The string key of the tag.
        """
        _validate_tag_name(key)
        trace_dir = self._find_trace_dir(trace_id, assert_exists=True)
        tag_path = os.path.join(trace_dir, FileStore.TRACE_TAGS_FOLDER_NAME, key)
        if not exists(tag_path):
            raise MlflowException(
                f"No tag with name: {key} in trace with ID {trace_id}.",
                RESOURCE_DOES_NOT_EXIST,
            )
        os.remove(tag_path)

    def _get_assessments_dir(self, trace_id: str) -> str:
        trace_dir = self._find_trace_dir(trace_id, assert_exists=True)
        return os.path.join(trace_dir, FileStore.ASSESSMENTS_FOLDER_NAME)

    def _get_assessment_path(self, trace_id: str, assessment_id: str) -> str:
        assessments_dir = self._get_assessments_dir(trace_id)
        return os.path.join(assessments_dir, f"{assessment_id}.yaml")

    def _save_assessment(self, assessment: Assessment) -> None:
        assessment_path = self._get_assessment_path(assessment.trace_id, assessment.assessment_id)
        make_containing_dirs(assessment_path)

        assessment_dict = assessment.to_dictionary()
        write_yaml(
            root=os.path.dirname(assessment_path),
            file_name=os.path.basename(assessment_path),
            data=assessment_dict,
            overwrite=True,
        )

    def _load_assessments(self, trace_id: str) -> list[Assessment]:
        assessments_dir = self._get_assessments_dir(trace_id)
        if not exists(assessments_dir):
            return []
        assessment_paths = os.listdir(assessments_dir)
        return [
            self._load_assessment(trace_id, assessment_path.split(".")[0])
            for assessment_path in assessment_paths
        ]

    def _load_assessment(self, trace_id: str, assessment_id: str) -> Assessment:
        assessment_path = self._get_assessment_path(trace_id, assessment_id)

        if not exists(assessment_path):
            raise MlflowException(
                f"Assessment with ID '{assessment_id}' not found for trace '{trace_id}'",
                RESOURCE_DOES_NOT_EXIST,
            )

        try:
            assessment_dict = FileStore._read_yaml(
                root=os.path.dirname(assessment_path), file_name=os.path.basename(assessment_path)
            )
            return Assessment.from_dictionary(assessment_dict)
        except Exception as e:
            raise MlflowException(
                f"Failed to load assessment with ID '{assessment_id}' for trace '{trace_id}': {e}",
                INTERNAL_ERROR,
            ) from e

    def get_assessment(self, trace_id: str, assessment_id: str) -> Assessment:
        """
        Retrieves a specific assessment associated with a trace from the file store.

        Args:
            trace_id: The unique identifier of the trace containing the assessment.
            assessment_id: The unique identifier of the assessment to retrieve.

        Returns:
            Assessment: The requested assessment object (either Expectation or Feedback).

        Raises:
            MlflowException: If the trace_id is not found, if no assessment with the
                specified assessment_id exists for the trace, or if the stored
                assessment data cannot be deserialized.
        """

        return self._load_assessment(trace_id, assessment_id)

    def create_assessment(self, assessment: Assessment) -> Assessment:
        """
        Creates a new assessment record associated with a specific trace.

        Args:
            assessment: The assessment object to create. The assessment will be modified
                    in-place to include the generated assessment_id and timestamps.

        Returns:
            Assessment: The input assessment object updated with backend-generated metadata.

        Raises:
            MlflowException: If the trace doesn't exist or there's an error saving the assessment.
        """

        assessment_id = generate_assessment_id()
        creation_timestamp = int(time.time() * 1000)

        assessment.assessment_id = assessment_id
        assessment.create_time_ms = creation_timestamp
        assessment.last_update_time_ms = creation_timestamp
        assessment.valid = True

        if assessment.overrides:
            original_assessment = self.get_assessment(assessment.trace_id, assessment.overrides)
            original_assessment.valid = False
            self._save_assessment(original_assessment)

        self._save_assessment(assessment)
        return assessment

    def update_assessment(
        self,
        trace_id: str,
        assessment_id: str,
        name: Optional[str] = None,
        expectation: Optional[Expectation] = None,
        feedback: Optional[Feedback] = None,
        rationale: Optional[str] = None,
        metadata: Optional[dict[str, str]] = None,
    ) -> Assessment:
        """
        Updates an existing assessment with new values while preserving immutable fields.

        `source` and `span_id` are immutable and cannot be changed.
        The last_update_time_ms will always be updated to the current timestamp.
        Metadata will be merged with the new metadata taking precedence.

        Args:
            trace_id: The unique identifier of the trace containing the assessment.
            assessment_id: The unique identifier of the assessment to update.
            name: The updated name of the assessment. If None, preserves existing name.
            expectation: Updated expectation value for expectation assessments.
            feedback: Updated feedback value for feedback assessments.
            rationale: Updated rationale text. If None, preserves existing rationale.
            metadata: Updated metadata dict. Will be merged with existing metadata.

        Returns:
            Assessment: The updated assessment object with new last_update_time_ms.

        Raises:
            MlflowException: If the assessment doesn't exist, if immutable fields have
                            changed, or if there's an error saving the assessment.
        """
        existing_assessment = self.get_assessment(trace_id, assessment_id)

        if expectation is not None and feedback is not None:
            raise MlflowException.invalid_parameter_value(
                "Cannot specify both `expectation` and `feedback` parameters."
            )

        if expectation is not None and not isinstance(existing_assessment, Expectation):
            raise MlflowException.invalid_parameter_value(
                "Cannot update expectation value on a Feedback assessment."
            )

        if feedback is not None and not isinstance(existing_assessment, Feedback):
            raise MlflowException.invalid_parameter_value(
                "Cannot update feedback value on an Expectation assessment."
            )

        merged_metadata = None
        if existing_assessment.metadata or metadata:
            merged_metadata = (existing_assessment.metadata or {}).copy()
            if metadata:
                merged_metadata.update(metadata)

        updated_timestamp = int(time.time() * 1000)

        if isinstance(existing_assessment, Expectation):
            new_value = expectation.value if expectation is not None else existing_assessment.value

            updated_assessment = Expectation(
                name=name if name is not None else existing_assessment.name,
                value=new_value,
                source=existing_assessment.source,
                trace_id=trace_id,
                metadata=merged_metadata,
                span_id=existing_assessment.span_id,
                create_time_ms=existing_assessment.create_time_ms,
                last_update_time_ms=updated_timestamp,
            )
        else:
            if feedback is not None:
                new_value = feedback.value
                new_error = feedback.error
            else:
                new_value = existing_assessment.value
                new_error = existing_assessment.error

            updated_assessment = Feedback(
                name=name if name is not None else existing_assessment.name,
                value=new_value,
                error=new_error,
                source=existing_assessment.source,
                trace_id=trace_id,
                metadata=merged_metadata,
                span_id=existing_assessment.span_id,
                create_time_ms=existing_assessment.create_time_ms,
                last_update_time_ms=updated_timestamp,
                rationale=rationale if rationale is not None else existing_assessment.rationale,
            )

        updated_assessment.assessment_id = existing_assessment.assessment_id
        updated_assessment.valid = existing_assessment.valid
        updated_assessment.overrides = existing_assessment.overrides

        if hasattr(existing_assessment, "run_id"):
            updated_assessment.run_id = existing_assessment.run_id

        self._save_assessment(updated_assessment)

        return updated_assessment

    def delete_assessment(self, trace_id: str, assessment_id: str) -> None:
        """
        Delete an assessment from a trace.

        If the deleted assessment was overriding another assessment, the overridden
        assessment will be restored to valid=True.

        Args:
            trace_id: The ID of the trace containing the assessment.
            assessment_id: The ID of the assessment to delete.

        Raises:
            MlflowException: If the trace_id is not found or deletion fails.
        """
        # First validate that the trace exists (this will raise if trace not found)
        self._find_trace_dir(trace_id, assert_exists=True)

        assessment_path = self._get_assessment_path(trace_id, assessment_id)

        # Early return if assessment doesn't exist (idempotent behavior)
        if not exists(assessment_path):
            return

        # Get override info before deletion
        overrides_assessment_id = None
        try:
            assessment_to_delete = self._load_assessment(trace_id, assessment_id)
            overrides_assessment_id = assessment_to_delete.overrides
        except Exception:
            pass

        assessment_path = self._get_assessment_path(trace_id, assessment_id)
        try:
            os.remove(assessment_path)

            # Clean up empty assessments directory if no more assessments exist
            assessments_dir = self._get_assessments_dir(trace_id)
            if exists(assessments_dir) and not os.listdir(assessments_dir):
                os.rmdir(assessments_dir)

        except OSError as e:
            raise MlflowException(
                f"Failed to delete assessment with ID '{assessment_id}' "
                f"for trace '{trace_id}': {e}",
                INTERNAL_ERROR,
            ) from e

        # If this assessment was overriding another assessment, restore the original
        if overrides_assessment_id:
            try:
                original_assessment = self.get_assessment(trace_id, overrides_assessment_id)
                original_assessment.valid = True
                original_assessment.last_update_time_ms = int(time.time() * 1000)
                self._save_assessment(original_assessment)
            except MlflowException:
                pass

    def _delete_traces(
        self,
        experiment_id: str,
        max_timestamp_millis: Optional[int] = None,
        max_traces: Optional[int] = None,
        trace_ids: Optional[list[str]] = None,
    ) -> int:
        """
        Delete traces based on the specified criteria.

        - Either `max_timestamp_millis` or `trace_ids` must be specified, but not both.
        - `max_traces` can't be specified if `trace_ids` is specified.

        Args:
            experiment_id: ID of the associated experiment.
            max_timestamp_millis: The maximum timestamp in milliseconds since the UNIX epoch for
                deleting traces. Traces older than or equal to this timestamp will be deleted.
            max_traces: The maximum number of traces to delete. If max_traces is specified, and
                it is less than the number of traces that would be deleted based on the
                max_timestamp_millis, the oldest traces will be deleted first.
            trace_ids: A set of trace IDs to delete.

        Returns:
            The number of traces deleted.
        """
        experiment_path = self._get_experiment_path(experiment_id, assert_exists=True)
        traces_path = os.path.join(experiment_path, FileStore.TRACES_FOLDER_NAME)
        deleted_traces = 0
        if max_timestamp_millis:
            trace_paths = list_all(traces_path, lambda x: os.path.isdir(x), full_path=True)
            trace_info_and_paths = []
            for trace_path in trace_paths:
                try:
                    trace_info = self._get_trace_info_from_dir(trace_path)
                    if trace_info and trace_info.timestamp_ms <= max_timestamp_millis:
                        trace_info_and_paths.append((trace_info, trace_path))
                except MissingConfigException as e:
                    # trap malformed trace exception and log warning
                    trace_id = os.path.basename(trace_path)
                    _logger.warning(
                        f"Malformed trace with ID '{trace_id}'. Detailed error {e}",
                        exc_info=_logger.isEnabledFor(logging.DEBUG),
                    )
            trace_info_and_paths.sort(key=lambda x: x[0].timestamp_ms)
            # if max_traces is not None then it must > 0
            deleted_traces = min(len(trace_info_and_paths), max_traces or len(trace_info_and_paths))
            trace_info_and_paths = trace_info_and_paths[:deleted_traces]
            for _, trace_path in trace_info_and_paths:
                shutil.rmtree(trace_path)
            return deleted_traces
        if trace_ids:
            for trace_id in trace_ids:
                trace_path = os.path.join(traces_path, trace_id)
                # Do not throw if the trace doesn't exist
                if exists(trace_path):
                    shutil.rmtree(trace_path)
                    deleted_traces += 1
            return deleted_traces

    def search_traces(
        self,
        experiment_ids: list[str],
        filter_string: Optional[str] = None,
        max_results: int = SEARCH_TRACES_DEFAULT_MAX_RESULTS,
        order_by: Optional[list[str]] = None,
        page_token: Optional[str] = None,
        model_id: Optional[str] = None,
        sql_warehouse_id: Optional[str] = None,
    ) -> tuple[list[TraceInfo], Optional[str]]:
        """
        Return traces that match the given list of search expressions within the experiments.

        Args:
            experiment_ids: List of experiment ids to scope the search.
            filter_string: A search filter string. Supported filter keys are `name`,
                           `status`, `timestamp_ms` and `tags`.
            max_results: Maximum number of traces desired.
            order_by: List of order_by clauses. Supported sort key is `timestamp_ms`. By default
                      we sort by timestamp_ms DESC.
            page_token: Token specifying the next page of results. It should be obtained from
                a ``search_traces`` call.
            model_id: If specified, return traces associated with the model ID.
            sql_warehouse_id: Only used in Databricks. The ID of the SQL warehouse to use for
                searching traces in inference tables.

        Returns:
            A tuple of a list of :py:class:`TraceInfo <mlflow.entities.TraceInfo>` objects that
            satisfy the search expressions and a pagination token for the next page of results.
            If the underlying tracking store supports pagination, the token for the
            next page may be obtained via the ``token`` attribute of the returned object; however,
            some store implementations may not support pagination and thus the returned token would
            not be meaningful in such cases.
        """
        if max_results > SEARCH_MAX_RESULTS_THRESHOLD:
            raise MlflowException(
                "Invalid value for request parameter max_results. It must be at "
                f"most {SEARCH_MAX_RESULTS_THRESHOLD}, but got value {max_results}",
                INVALID_PARAMETER_VALUE,
            )
        traces = []
        for experiment_id in experiment_ids:
            trace_infos = self._list_trace_infos(experiment_id)
            traces.extend(trace_infos)
        filtered = SearchTraceUtils.filter(traces, filter_string)
        sorted_traces = SearchTraceUtils.sort(filtered, order_by)
        traces, next_page_token = SearchTraceUtils.paginate(sorted_traces, page_token, max_results)
        return traces, next_page_token

    def _list_trace_infos(self, experiment_id):
        experiment_path = self._get_experiment_path(experiment_id, assert_exists=True)
        traces_path = os.path.join(experiment_path, FileStore.TRACES_FOLDER_NAME)
        if not os.path.exists(traces_path):
            return []
        trace_paths = list_all(traces_path, lambda x: os.path.isdir(x), full_path=True)
        trace_infos = []
        for trace_path in trace_paths:
            try:
                if trace_info := self._get_trace_info_from_dir(trace_path):
                    trace_infos.append(trace_info)
            except MissingConfigException as e:
                # trap malformed trace exception and log warning
                trace_id = os.path.basename(trace_path)
                logging.warning(
                    f"Malformed trace with ID '{trace_id}'. Detailed error {e}",
                    exc_info=_logger.isEnabledFor(logging.DEBUG),
                )
        return trace_infos

    def create_logged_model(
        self,
        experiment_id: str = DEFAULT_EXPERIMENT_ID,
        name: Optional[str] = None,
        source_run_id: Optional[str] = None,
        tags: Optional[list[LoggedModelTag]] = None,
        params: Optional[list[LoggedModelParameter]] = None,
        model_type: Optional[str] = None,
    ) -> LoggedModel:
        """
        Create a new logged model.

        Args:
            experiment_id: ID of the experiment to which the model belongs.
            name: Name of the model. If not specified, a random name will be generated.
            source_run_id: ID of the run that produced the model.
            tags: Tags to set on the model.
            params: Parameters to set on the model.
            model_type: Type of the model.

        Returns:
            The created model.
        """
        _validate_logged_model_name(name)
        experiment = self.get_experiment(experiment_id)
        if experiment is None:
            raise MlflowException(
                f"Could not create model under experiment with ID {experiment_id} - no such "
                "experiment exists." % experiment_id,
                databricks_pb2.RESOURCE_DOES_NOT_EXIST,
            )
        if experiment.lifecycle_stage != LifecycleStage.ACTIVE:
            raise MlflowException(
                f"Could not create model under non-active experiment with ID {experiment_id}.",
                databricks_pb2.INVALID_STATE,
            )
        for param in params or []:
            _validate_param(param.key, param.value)

        name = name or _generate_random_name()
        model_id = f"m-{str(uuid.uuid4()).replace('-', '')}"
        artifact_location = self._get_model_artifact_dir(experiment_id, model_id)
        creation_timestamp = int(time.time() * 1000)
        model = LoggedModel(
            experiment_id=experiment_id,
            model_id=model_id,
            name=name,
            artifact_location=artifact_location,
            creation_timestamp=creation_timestamp,
            last_updated_timestamp=creation_timestamp,
            source_run_id=source_run_id,
            status=LoggedModelStatus.PENDING,
            tags=tags,
            params=params,
            model_type=model_type,
        )

        # Persist model metadata and create directories for logging metrics, tags
        model_dir = self._get_model_dir(experiment_id, model_id)
        mkdir(model_dir)
        model_info_dict: dict[str, Any] = self._make_persisted_model_dict(model)
        model_info_dict["lifecycle_stage"] = LifecycleStage.ACTIVE
        write_yaml(model_dir, FileStore.META_DATA_FILE_NAME, model_info_dict)
        mkdir(model_dir, FileStore.METRICS_FOLDER_NAME)
        mkdir(model_dir, FileStore.PARAMS_FOLDER_NAME)
        self.log_logged_model_params(model_id=model_id, params=params or [])
        self.set_logged_model_tags(model_id=model_id, tags=tags or [])

        return self.get_logged_model(model_id=model_id)

    def log_logged_model_params(self, model_id: str, params: list[LoggedModelParameter]):
        """
        Set parameters on the specified logged model.

        Args:
            model_id: ID of the model.
            params: Parameters to set on the model.

        Returns:
            None
        """
        for param in params or []:
            _validate_param(param.key, param.value)

        model = self.get_logged_model(model_id)
        for param in params:
            param_path = os.path.join(
                self._get_model_dir(model.experiment_id, model.model_id),
                FileStore.PARAMS_FOLDER_NAME,
                param.key,
            )
            make_containing_dirs(param_path)
            # Don't add trailing newline
            write_to(param_path, self._writeable_value(param.value))

    def finalize_logged_model(self, model_id: str, status: LoggedModelStatus) -> LoggedModel:
        """
        Finalize a model by updating its status.

        Args:
            model_id: ID of the model to finalize.
            status: Final status to set on the model.

        Returns:
            The updated model.
        """
        model_dict = self._get_model_dict(model_id)
        model = LoggedModel.from_dictionary(model_dict)
        model.status = status
        model.last_updated_timestamp = int(time.time() * 1000)
        model_dir = self._get_model_dir(model.experiment_id, model.model_id)
        model_info_dict = self._make_persisted_model_dict(model)
        write_yaml(model_dir, FileStore.META_DATA_FILE_NAME, model_info_dict, overwrite=True)
        return self.get_logged_model(model_id)

    def set_logged_model_tags(self, model_id: str, tags: list[LoggedModelTag]) -> None:
        """
        Set tags on the specified logged model.

        Args:
            model_id: ID of the model.
            tags: Tags to set on the model.

        Returns:
            None
        """
        model = self.get_logged_model(model_id)
        for tag in tags:
            _validate_tag_name(tag.key)
            tag_path = os.path.join(
                self._get_model_dir(model.experiment_id, model.model_id),
                FileStore.TAGS_FOLDER_NAME,
                tag.key,
            )
            make_containing_dirs(tag_path)
            # Don't add trailing newline
            write_to(tag_path, self._writeable_value(tag.value))

    def delete_logged_model_tag(self, model_id: str, key: str) -> None:
        """
        Delete a tag on the specified logged model.

        Args:
            model_id: ID of the model.
            key: The string key of the tag.

        Returns:
            None
        """
        _validate_tag_name(key)
        model = self.get_logged_model(model_id)
        tag_path = os.path.join(
            self._get_model_dir(model.experiment_id, model.model_id),
            FileStore.TAGS_FOLDER_NAME,
            key,
        )
        if not exists(tag_path):
            raise MlflowException(
                f"No tag with key {key!r} found for model with ID {model_id!r}.",
                RESOURCE_DOES_NOT_EXIST,
            )
        os.remove(tag_path)

    def get_logged_model(self, model_id: str) -> LoggedModel:
        """
        Fetch the logged model with the specified ID.

        Args:
            model_id: ID of the model to fetch.

        Returns:
            The fetched model.
        """
        return LoggedModel.from_dictionary(self._get_model_dict(model_id))

    def delete_logged_model(self, model_id: str) -> None:
        model = self.get_logged_model(model_id)
        model_dict = self._make_persisted_model_dict(model)
        model_dict["lifecycle_stage"] = LifecycleStage.DELETED
        model_dir = self._get_model_dir(model.experiment_id, model.model_id)
        write_yaml(
            model_dir,
            FileStore.META_DATA_FILE_NAME,
            model_dict,
            overwrite=True,
        )

    def _get_model_artifact_dir(self, experiment_id: str, model_id: str) -> str:
        return append_to_uri_path(
            self.get_experiment(experiment_id).artifact_location,
            FileStore.MODELS_FOLDER_NAME,
            model_id,
            FileStore.ARTIFACTS_FOLDER_NAME,
        )

    def _make_persisted_model_dict(self, model: LoggedModel) -> dict[str, Any]:
        model_dict = model.to_dictionary()
        for field in ("tags", "params", "metrics"):
            model_dict.pop(field, None)
        return model_dict

    def _get_model_dict(self, model_id: str) -> dict[str, Any]:
        exp_id, model_dir = self._find_model_root(model_id)
        if model_dir is None:
            raise MlflowException(
                f"Model '{model_id}' not found", databricks_pb2.RESOURCE_DOES_NOT_EXIST
            )
        model_dict: dict[str, Any] = self._get_model_info_from_dir(model_dir)
        if model_dict.get("lifecycle_stage") == LifecycleStage.DELETED:
            raise MlflowException(
                f"Model '{model_id}' not found", databricks_pb2.RESOURCE_DOES_NOT_EXIST
            )

        if model_dict["experiment_id"] != exp_id:
            raise MlflowException(
                f"Model '{model_id}' metadata is in invalid state.", databricks_pb2.INVALID_STATE
            )
        return model_dict

    def _get_model_dir(self, experiment_id: str, model_id: str) -> str:
        if not self._has_experiment(experiment_id):
            return None
        return os.path.join(
            self._get_experiment_path(experiment_id, assert_exists=True),
            FileStore.MODELS_FOLDER_NAME,
            model_id,
        )

    def _find_model_root(self, model_id):
        self._check_root_dir()
        all_experiments = self._get_active_experiments(False) + self._get_deleted_experiments(False)
        for experiment_dir in all_experiments:
            models_dir_path = os.path.join(
                self.root_directory, experiment_dir, FileStore.MODELS_FOLDER_NAME
            )
            if not os.path.exists(models_dir_path):
                continue
            models = find(models_dir_path, model_id, full_path=True)
            if len(models) == 0:
                continue
            return os.path.basename(os.path.dirname(os.path.abspath(models_dir_path))), models[0]
        return None, None

    def _get_model_from_dir(self, model_dir: str) -> LoggedModel:
        return LoggedModel.from_dictionary(self._get_model_info_from_dir(model_dir))

    def _get_model_info_from_dir(self, model_dir: str) -> dict[str, Any]:
        model_dict = FileStore._read_yaml(model_dir, FileStore.META_DATA_FILE_NAME)
        model_dict["tags"] = self._get_all_model_tags(model_dir)
        model_dict["params"] = {p.key: p.value for p in self._get_all_model_params(model_dir)}
        model_dict["metrics"] = self._get_all_model_metrics(
            model_id=model_dict["model_id"], model_dir=model_dir
        )
        return model_dict

    def _get_all_model_tags(self, model_dir: str) -> list[LoggedModelTag]:
        parent_path, tag_files = self._get_resource_files(model_dir, FileStore.TAGS_FOLDER_NAME)
        tags = []
        for tag_file in tag_files:
            tags.append(self._get_tag_from_file(parent_path, tag_file))
        return tags

    def _get_all_model_params(self, model_dir: str) -> list[LoggedModelParameter]:
        parent_path, param_files = self._get_resource_files(model_dir, FileStore.PARAMS_FOLDER_NAME)
        return [self._get_param_from_file(parent_path, param_file) for param_file in param_files]

    def _get_all_model_metrics(self, model_id: str, model_dir: str) -> list[Metric]:
        parent_path, metric_files = self._get_resource_files(
            model_dir, FileStore.METRICS_FOLDER_NAME
        )
        metrics = []
        for metric_file in metric_files:
            metrics.extend(
                FileStore._get_model_metrics_from_file(
                    model_id=model_id, parent_path=parent_path, metric_name=metric_file
                )
            )
        return metrics

    @staticmethod
    def _get_model_metrics_from_file(
        model_id: str, parent_path: str, metric_name: str
    ) -> list[Metric]:
        _validate_metric_name(metric_name)
        metric_objs = [
            FileStore._get_model_metric_from_line(model_id, metric_name, line)
            for line in read_file_lines(parent_path, metric_name)
        ]
        if len(metric_objs) == 0:
            raise ValueError(f"Metric '{metric_name}' is malformed. No data found.")

        # Group metrics by (dataset_name, dataset_digest)
        grouped_metrics = defaultdict(list)
        for metric in metric_objs:
            key = (metric.dataset_name, metric.dataset_digest)
            grouped_metrics[key].append(metric)

        # Compute the max for each group
        return [
            max(group, key=lambda m: (m.step, m.timestamp, m.value))
            for group in grouped_metrics.values()
        ]

    @staticmethod
    def _get_model_metric_from_line(model_id: str, metric_name: str, metric_line: str) -> Metric:
        metric_parts = metric_line.strip().split(" ")
        if len(metric_parts) not in [4, 6]:
            raise MlflowException(
                f"Metric '{metric_name}' is malformed; persisted metric data contained "
                f"{len(metric_parts)} fields. Expected 4 or 6 fields.",
                databricks_pb2.INTERNAL_ERROR,
            )
        ts = int(metric_parts[0])
        val = float(metric_parts[1])
        step = int(metric_parts[2])
        run_id = str(metric_parts[3])
        dataset_name = str(metric_parts[4]) if len(metric_parts) == 6 else None
        dataset_digest = str(metric_parts[5]) if len(metric_parts) == 6 else None
        # TODO: Read run ID from the metric file and pass it to the Metric constructor
        return Metric(
            key=metric_name,
            value=val,
            timestamp=ts,
            step=step,
            model_id=model_id,
            dataset_name=dataset_name,
            dataset_digest=dataset_digest,
            run_id=run_id,
        )

    def search_logged_models(
        self,
        experiment_ids: list[str],
        filter_string: Optional[str] = None,
        datasets: Optional[list[DatasetFilter]] = None,
        max_results: Optional[int] = None,
        order_by: Optional[list[dict[str, Any]]] = None,
        page_token: Optional[str] = None,
    ) -> PagedList[LoggedModel]:
        """
        Search for logged models that match the specified search criteria.

        Args:
            experiment_ids: List of experiment ids to scope the search.
            filter_string: A search filter string.
            datasets: List of dictionaries to specify datasets on which to apply metrics filters.
                The following fields are supported:

                dataset_name (str): Required. Name of the dataset.
                dataset_digest (str): Optional. Digest of the dataset.
            max_results: Maximum number of logged models desired. Default is 100.
            order_by: List of dictionaries to specify the ordering of the search results.
                The following fields are supported:

                field_name (str): Required. Name of the field to order by, e.g. "metrics.accuracy".
                ascending: (bool): Optional. Whether the order is ascending or not.
                dataset_name: (str): Optional. If ``field_name`` refers to a metric, this field
                    specifies the name of the dataset associated with the metric. Only metrics
                    associated with the specified dataset name will be considered for ordering.
                    This field may only be set if ``field_name`` refers to a metric.
                dataset_digest (str): Optional. If ``field_name`` refers to a metric, this field
                    specifies the digest of the dataset associated with the metric. Only metrics
                    associated with the specified dataset name and digest will be considered for
                    ordering. This field may only be set if ``dataset_name`` is also set.
            page_token: Token specifying the next page of results.

        Returns:
            A :py:class:`PagedList <mlflow.store.entities.PagedList>` of
            :py:class:`LoggedModel <mlflow.entities.LoggedModel>` objects.
        """
        if datasets and not all(d.get("dataset_name") for d in datasets):
            raise MlflowException(
                "`dataset_name` in the `datasets` clause must be specified.",
                INVALID_PARAMETER_VALUE,
            )
        max_results = max_results or SEARCH_LOGGED_MODEL_MAX_RESULTS_DEFAULT
        all_models = []
        for experiment_id in experiment_ids:
            models = self._list_models(experiment_id)
            all_models.extend(models)
        filtered = SearchLoggedModelsUtils.filter_logged_models(all_models, filter_string, datasets)
        sorted_logged_models = SearchLoggedModelsUtils.sort(filtered, order_by)
        logged_models, next_page_token = SearchLoggedModelsUtils.paginate(
            sorted_logged_models, page_token, max_results
        )
        return PagedList(logged_models, next_page_token)

    def _list_models(self, experiment_id: str) -> list[LoggedModel]:
        self._check_root_dir()
        if not self._has_experiment(experiment_id):
            return []
        experiment_dir = self._get_experiment_path(experiment_id, assert_exists=True)
        models_folder = os.path.join(experiment_dir, FileStore.MODELS_FOLDER_NAME)
        if not exists(models_folder):
            return []
        model_dirs = list_all(
            models_folder,
            filter_func=lambda x: all(
                os.path.basename(os.path.normpath(x)) != reservedFolderName
                for reservedFolderName in FileStore.RESERVED_EXPERIMENT_FOLDERS
            )
            and os.path.isdir(x),
            full_path=True,
        )
        models = []
        for m_dir in model_dirs:
            try:
                # trap and warn known issues, will raise unexpected exceptions to caller
                m_dict = self._get_model_info_from_dir(m_dir)
                if m_dict.get("lifecycle_stage") == LifecycleStage.DELETED:
                    continue
                model = LoggedModel.from_dictionary(m_dict)
                if model.experiment_id != experiment_id:
                    logging.warning(
                        "Wrong experiment ID (%s) recorded for model '%s'. "
                        "It should be %s. Model will be ignored.",
                        str(model.experiment_id),
                        str(model.model_id),
                        str(experiment_id),
                        exc_info=True,
                    )
                    continue
                models.append(model)
            except MissingConfigException as exc:
                # trap malformed model exception and log
                # this is at debug level because if the same store is used for
                # artifact storage, it's common the folder is not a run folder
                m_id = os.path.basename(m_dir)
                logging.debug(
                    "Malformed model '%s'. Detailed error %s", m_id, str(exc), exc_info=True
                )
        return models

    #######################################################################################
    # Below are legacy V2 Tracing APIs. DO NOT USE. Use the V3 APIs instead.
    #######################################################################################
    def deprecated_start_trace_v2(
        self,
        experiment_id: str,
        timestamp_ms: int,
        request_metadata: dict[str, str],
        tags: dict[str, str],
    ) -> TraceInfoV2:
        """
        DEPRECATED. DO NOT USE.

        Start an initial TraceInfo object in the backend store.

        Args:
            experiment_id: String id of the experiment for this run.
            timestamp_ms: Start time of the trace, in milliseconds since the UNIX epoch.
            request_metadata: Metadata of the trace.
            tags: Tags of the trace.

        Returns:
            The created TraceInfo object.
        """
        request_id = generate_request_id_v2()
        _validate_experiment_id(experiment_id)
        experiment_dir = self._get_experiment_path(
            experiment_id, view_type=ViewType.ACTIVE_ONLY, assert_exists=True
        )
        mkdir(experiment_dir, FileStore.TRACES_FOLDER_NAME)
        traces_dir = os.path.join(experiment_dir, FileStore.TRACES_FOLDER_NAME)
        mkdir(traces_dir, request_id)
        trace_dir = os.path.join(traces_dir, request_id)
        artifact_uri = self._get_traces_artifact_dir(experiment_id, request_id)
        tags.update({MLFLOW_ARTIFACT_LOCATION: artifact_uri})
        trace_info = TraceInfoV2(
            request_id=request_id,
            experiment_id=experiment_id,
            timestamp_ms=timestamp_ms,
            execution_time_ms=None,
            status=TraceStatus.IN_PROGRESS,
            request_metadata=request_metadata,
            tags=tags,
        )
        self._save_trace_info(trace_info.to_v3(), trace_dir)
        return trace_info

    def deprecated_end_trace_v2(
        self,
        request_id: str,
        timestamp_ms: int,
        status: TraceStatus,
        request_metadata: dict[str, str],
        tags: dict[str, str],
    ) -> TraceInfoV2:
        """
        DEPRECATED. DO NOT USE.

        Update the TraceInfo object in the backend store with the completed trace info.

        Args:
            request_id : Unique string identifier of the trace.
            timestamp_ms: End time of the trace, in milliseconds. The execution time field
                in the TraceInfo will be calculated by subtracting the start time from this.
            status: Status of the trace.
            request_metadata: Metadata of the trace. This will be merged with the existing
                metadata logged during the start_trace call.
            tags: Tags of the trace. This will be merged with the existing tags logged
                during the start_trace or set_trace_tag calls.

        Returns:
            The updated TraceInfo object.
        """
        trace_info, trace_dir = self._get_trace_info_and_dir(request_id)
        trace_info.execution_duration = timestamp_ms - trace_info.request_time
        trace_info.state = status.to_state()
        trace_info.trace_metadata.update(request_metadata)
        trace_info.tags.update(tags)
        self._save_trace_info(trace_info, trace_dir, overwrite=True)
        return TraceInfoV2.from_v3(trace_info)
