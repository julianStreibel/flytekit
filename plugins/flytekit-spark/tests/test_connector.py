import http
import json
from datetime import timedelta
from unittest import mock

import pytest
from aioresponses import aioresponses
from flyteidl.core.execution_pb2 import TaskExecution

from flytekit.core.constants import FLYTE_FAIL_ON_ERROR
from flytekitplugins.spark.connector import DATABRICKS_API_ENDPOINT, DatabricksJobMetadata, get_header, \
    _get_databricks_job_spec, DEFAULT_DATABRICKS_INSTANCE_ENV_KEY

from flytekit.extend.backend.base_agent import AgentRegistry
from flytekit.interfaces.cli_identifiers import Identifier
from flytekit.models import literals, task
from flytekit.models.core.identifier import ResourceType
from flytekit.models.task import Container, Resources, TaskTemplate
import os


@pytest.fixture(scope="function")
def task_template() -> TaskTemplate:
    task_id = Identifier(
        resource_type=ResourceType.TASK, project="project", domain="domain", name="name", version="version"
    )
    task_metadata = task.TaskMetadata(
        True,
        task.RuntimeMetadata(task.RuntimeMetadata.RuntimeType.FLYTE_SDK, "1.0.0", "python"),
        timedelta(days=1),
        literals.RetryStrategy(3),
        True,
        "0.1.1b0",
        "This is deprecated!",
        True,
        "A",
        (),
    )
    task_config = {
        "sparkConf": {
            "spark.driver.memory": "1000M",
            "spark.executor.memory": "1000M",
            "spark.executor.cores": "1",
            "spark.executor.instances": "2",
            "spark.driver.cores": "1",
        },
        "mainApplicationFile": "dbfs:/entrypoint.py",
        "databricksConf": {
            "run_name": "flytekit databricks plugin example",
            "new_cluster": {
                "spark_version": "12.2.x-scala2.12",
                "node_type_id": "n2-highmem-4",
                "num_workers": 1,
            },
            "timeout_seconds": 3600,
            "max_retries": 1,
        }
    }
    container = Container(
        image="flyteorg/flytekit:databricks-0.18.0-py3.7",
        command=[],
        args=[
            "pyflyte-fast-execute",
            "--additional-distribution",
            "s3://my-s3-bucket/flytesnacks/development/24UYJEF2HDZQN3SG4VAZSM4PLI======/script_mode.tar.gz",
            "--dest-dir",
            "/root",
            "--",
            "pyflyte-execute",
            "--inputs",
            "s3://my-s3-bucket",
            "--output-prefix",
            "s3://my-s3-bucket",
            "--raw-output-data-prefix",
            "s3://my-s3-bucket",
            "--checkpoint-path",
            "s3://my-s3-bucket",
            "--prev-checkpoint",
            "s3://my-s3-bucket",
            "--resolver",
            "flytekit.core.python_auto_container.default_task_resolver",
            "--",
            "task-module",
            "spark_local_example",
            "task-name",
            "hello_spark",
        ],
        resources=Resources(
            requests=[],
            limits=[],
        ),
        env={"foo": "bar"},
        config={},
    )

    dummy_template = TaskTemplate(
        id=task_id,
        custom=task_config,
        metadata=task_metadata,
        container=container,
        interface=None,
        type="spark",
    )

    return dummy_template


@pytest.mark.asyncio
async def test_databricks_agent(task_template: TaskTemplate):
    agent = AgentRegistry.get_agent("spark")

    task_template.custom["databricksInstance"] = "test-account.cloud.databricks.com"

    mocked_token = "mocked_databricks_token"
    mocked_context = mock.patch("flytekit.current_context", autospec=True).start()
    mocked_context.return_value.secrets.get.return_value = mocked_token

    databricks_metadata = DatabricksJobMetadata(
        databricks_instance="test-account.cloud.databricks.com",
        run_id="123",
    )

    mock_create_response = {"run_id": "123"}
    mock_get_response = {
        "job_id": "1",
        "run_id": "123",
        "state": {"life_cycle_state": "TERMINATED", "result_state": "SUCCESS", "state_message": "OK"},
    }
    mock_delete_response = {}
    create_url = f"https://test-account.cloud.databricks.com{DATABRICKS_API_ENDPOINT}/runs/submit"
    get_url = f"https://test-account.cloud.databricks.com{DATABRICKS_API_ENDPOINT}/runs/get?run_id=123"
    delete_url = f"https://test-account.cloud.databricks.com{DATABRICKS_API_ENDPOINT}/runs/cancel"
    with aioresponses() as mocked:
        mocked.post(create_url, status=http.HTTPStatus.OK, payload=mock_create_response)
        res = await agent.create(task_template, None)
        spec = _get_databricks_job_spec(task_template)
        data = json.dumps(spec)
        mocked.assert_called_with(create_url, method="POST", data=data, headers=get_header())
        spark_envs = spec["new_cluster"]["spark_env_vars"]
        assert spark_envs["foo"] == "bar"
        assert spark_envs[FLYTE_FAIL_ON_ERROR] == "true"
        assert res == databricks_metadata

        mocked.get(get_url, status=http.HTTPStatus.OK, payload=mock_get_response)
        resource = await agent.get(databricks_metadata)
        assert resource.phase == TaskExecution.SUCCEEDED
        assert resource.outputs is None
        assert resource.message == "OK"
        assert resource.log_links[0].name == "Databricks Console"
        assert resource.log_links[0].uri == "https://test-account.cloud.databricks.com/#job/1/run/123"

        mocked.post(delete_url, status=http.HTTPStatus.OK, payload=mock_delete_response)
        await agent.delete(databricks_metadata)

    assert get_header() == {"Authorization": f"Bearer {mocked_token}", "content-type": "application/json"}

    mock.patch.stopall()


@pytest.mark.asyncio
async def test_agent_create_with_no_instance(task_template: TaskTemplate):
    agent = AgentRegistry.get_agent("spark")

    with pytest.raises(ValueError) as e:
        await agent.create(task_template, None)


@pytest.mark.asyncio
async def test_agent_create_with_default_instance(task_template: TaskTemplate):
    agent = AgentRegistry.get_agent("spark")

    mocked_token = "mocked_databricks_token"
    mocked_context = mock.patch("flytekit.current_context", autospec=True).start()
    mocked_context.return_value.secrets.get.return_value = mocked_token

    databricks_metadata = DatabricksJobMetadata(
        databricks_instance="test-account.cloud.databricks.com",
        run_id="123",
    )

    mock_create_response = {"run_id": "123"}

    os.environ[DEFAULT_DATABRICKS_INSTANCE_ENV_KEY] = "test-account.cloud.databricks.com"

    create_url = f"https://test-account.cloud.databricks.com{DATABRICKS_API_ENDPOINT}/runs/submit"
    with aioresponses() as mocked:
        mocked.post(create_url, status=http.HTTPStatus.OK, payload=mock_create_response)
        res = await agent.create(task_template, None)
        spec = _get_databricks_job_spec(task_template)
        data = json.dumps(spec)
        mocked.assert_called_with(create_url, method="POST", data=data, headers=get_header())
        assert res == databricks_metadata

    mock.patch.stopall()
