"""Implementation of a model & endpoint in Vertex AI.

Uses gcloud python libraries to manage those resources.

One time setup of service account:
- We assume the existence of a
"{PROJECT_NUMBER}-compute@developer.gserviceaccount.com" service account with
the required permissions.
- Follow instructions from
https://cloud.google.com/vertex-ai/docs/general/custom-service-account
to create it & give permissions if one doesn't exist.
"""

import json
import logging
import os
import re
import time
from typing import Any
from absl import flags
# pylint: disable=g-import-not-at-top, g-statement-before-imports
# External needs from google.cloud.
# pytype: disable=module-attr
try:
  from google.cloud.aiplatform import aiplatform
except ImportError:
  from google.cloud import aiplatform
from google.api_core import exceptions as google_exceptions
from perfkitbenchmarker import errors
from perfkitbenchmarker import resource
from perfkitbenchmarker import sample
from perfkitbenchmarker import virtual_machine
from perfkitbenchmarker import vm_util
from perfkitbenchmarker.providers.gcp import flags as gcp_flags
from perfkitbenchmarker.providers.gcp import gcs
from perfkitbenchmarker.providers.gcp import util
from perfkitbenchmarker.resources import managed_ai_model
from perfkitbenchmarker.resources import managed_ai_model_spec

FLAGS = flags.FLAGS


CLI = 'CLI'
MODEL_GARDEN_CLI = 'MODEL-GARDEN-CLI'
SDK = 'SDK'
SERVICE_ACCOUNT_BASE = '{}-compute@developer.gserviceaccount.com'


class VertexAiModelInRegistry(managed_ai_model.BaseManagedAiModel):
  """Represents a Vertex AI model in the model registry.

  Attributes:
    model_name: The official name of the model in Model Garden, e.g. Llama2.
    name: The name of the created model in private model registry.
    model_resource_name: The full resource name of the created model, e.g.
      projects/123/locations/us-east1/models/1234.
    region: The region, derived from the zone.
    project: The project.
    endpoint: The PKB resource endpoint the model is deployed to.
    gcloud_model: Representation of the model in gcloud python library.
    service_account: Name of the service account used by the model.
    model_deploy_time: Time it took to deploy the model.
    model_upload_time: Time it took to upload the model.
    vm: A way to run commands on the machine.
    json_write_times: List of times it took to write the json request to disk.
    json_cache: Cache from request JSON -> JSON request file.
    gcs_bucket_copy_time: Time it took to copy the model to the GCS bucket.
    gcs_client: The GCS client used to copy the model to the GCS bucket. Only
      instantiated if ai_create_bucket flag is True.
    bucket_uri: The GCS bucket where the model is stored.
    model_bucket_path: Where the model bucket is located.
    staging_bucket: The staging bucket used by the model.
  """

  CLOUD: str = 'GCP'
  INTERFACE: list[str] | str = [CLI, SDK, MODEL_GARDEN_CLI]

  endpoint: 'VertexAiEndpoint'
  model_spec: 'VertexAiModelSpec'
  model_name: str
  name: str
  region: str
  project: str
  gcloud_model: aiplatform.Model | None
  service_account: str
  model_resource_name: str | None
  model_deploy_time: float | None
  model_upload_time: float | None
  json_write_times: list[float]
  json_cache: dict[str, str]
  gcs_bucket_copy_time: float | None
  gcs_client: gcs.GoogleCloudStorageService | None
  bucket_uri: str
  model_bucket_path: str
  staging_bucket: str

  def __init__(
      self,
      vm: virtual_machine.BaseVirtualMachine,
      model_spec: managed_ai_model_spec.BaseManagedAiModelSpec,
      name: str | None = None,
      bucket_uri: str | None = None,
      **kwargs,
  ):
    super().__init__(model_spec, vm, **kwargs)
    if not isinstance(model_spec, VertexAiModelSpec):
      raise errors.Config.InvalidValue(
          f'Invalid model spec class: "{model_spec.__class__.__name__}". '
          'Must be a VertexAiModelSpec. It had config values of '
          f'{model_spec.model_name} & {model_spec.cloud}'
      )
    self.model_spec = model_spec
    self.model_name = model_spec.model_name
    self.model_resource_name = None
    if name:
      self.name = name
    else:
      self.name = 'pkb' + FLAGS.run_uri
    self.project = FLAGS.project
    self.endpoint = VertexAiEndpoint(
        name=self.name,
        region=self.region,
        project=self.project,
        vm=self.vm,
        interface=self.INTERFACE,
    )
    if not self.project:
      raise errors.Setup.InvalidConfigurationError(
          'Project is required for Vertex AI but was not set.'
      )
    self.gcloud_model = None
    self.metadata.update({
        'name': self.name,
        'model_name': self.model_name,
        'model_size': self.model_spec.model_size,
        'machine_type': self.model_spec.machine_type,
        'accelerator_type': self.model_spec.accelerator_type,
        'accelerator_count': self.model_spec.accelerator_count,
    })
    project_number = util.GetProjectNumber(self.project)
    self.service_account = SERVICE_ACCOUNT_BASE.format(project_number)
    self.model_upload_time = None
    self.model_deploy_time = None
    self.json_write_times = []
    self.json_cache = {}
    self.gcs_client = None
    if bucket_uri is not None:
      self.bucket_uri = bucket_uri
    elif gcp_flags.AI_BUCKET_URI.value is not None:
      self.bucket_uri = gcp_flags.AI_BUCKET_URI.value
    else:
      self.gcs_client = gcs.GoogleCloudStorageService()
      self.gcs_client.PrepareService(self.region)
      self.bucket_uri = f'{self.project}-{self.region}-tmp-{self.name}'
    self.model_bucket_path = 'gs://' + os.path.join(
        self.bucket_uri, self.model_spec.model_bucket_suffix
    )
    self.staging_bucket = 'gs://' + os.path.join(self.bucket_uri, 'temporal')
    self.gcs_bucket_copy_time = None

  def _InitializeNewModel(self) -> 'VertexAiModelInRegistry':
    """Returns a new instance of the same class."""
    return self.__class__(
        vm=self.vm,
        model_spec=self.model_spec,
        name=self.name + '2',
        # Reuse the same bucket for the next model.
        bucket_uri=self.bucket_uri,
    )

  def GetRegionFromZone(self, zone: str) -> str:
    return util.GetRegionFromZone(zone)

  def ListExistingEndpoints(self, region: str | None = None) -> list[str]:
    """Returns a list of existing model endpoint ids in the same region."""
    if region is None:
      region = self.region
    # Expected output example:
    # ENDPOINT_ID          DISPLAY_NAME
    # 12345                some_endpoint_name
    out, _, _ = self.vm.RunCommand(
        f'gcloud ai endpoints list --region={region} --project={self.project}'
    )
    lines = out.splitlines()
    if not lines:
      return []
    ids = [line.split()[0] for line in lines]
    ids.pop(0)  # Remove the first line which just has titles
    return ids

  def GetSamples(self) -> list[sample.Sample]:
    """Gets samples relating to the provisioning of the resource."""
    samples = super().GetSamples()
    metadata = self.GetResourceMetadata()
    if self.model_upload_time:
      samples.append(
          sample.Sample(
              'Model Upload Time',
              self.model_upload_time,
              'seconds',
              metadata,
          )
      )
    if self.model_deploy_time:
      samples.append(
          sample.Sample(
              'Model Deploy Time',
              self.model_deploy_time,
              'seconds',
              metadata,
          )
      )
    if self.json_write_times:
      samples.append(
          sample.Sample(
              'Max JSON Write Time',
              max(self.json_write_times),
              'seconds',
              metadata,
          )
      )
    if self.gcs_bucket_copy_time:
      samples.append(
          sample.Sample(
              'GCS Bucket Copy Time',
              self.gcs_bucket_copy_time,
              'seconds',
              metadata,
          )
      )
    return samples

  def _SendPrompt(
      self, prompt: str, max_tokens: int, temperature: float, **kwargs: Any
  ) -> list[str]:
    """Sends a prompt to the model and returns the response."""
    instances = self.model_spec.ConvertToInstances(
        prompt, max_tokens, temperature, **kwargs
    )
    if self.INTERFACE == SDK:
      assert self.endpoint.ai_endpoint
      response = self.endpoint.ai_endpoint.predict(instances=instances)
      str_responses = [str(response) for response in response.predictions]
      return str_responses
    out, _, _ = self.vm.RunCommand(
        self.GetPromptCommand(prompt, max_tokens, temperature, **kwargs),
    )
    responses = out.strip('[]').split(',')
    return responses

  def GetPromptCommand(
      self, prompt: str, max_tokens: int, temperature: float, **kwargs: Any
  ) -> str:
    """Returns the command to send a prompt to the model."""
    instances = self.model_spec.ConvertToInstances(
        prompt, max_tokens, temperature, **kwargs
    )
    instances_dict = {'instances': instances, 'parameters': {}}
    start_write_time = time.time()
    json_dump = json.dumps(instances_dict)
    if json_dump in self.json_cache:
      name = self.json_cache[json_dump]
    else:
      name = self.vm.WriteTemporaryFile(json_dump)
      self.json_cache[json_dump] = name
    end_write_time = time.time()
    write_time = end_write_time - start_write_time
    self.json_write_times.append(write_time)
    return (
        'gcloud ai endpoints predict'
        f' {self.endpoint.endpoint_name} --json-request={name}'
    )

  def _Create(self) -> None:
    """Creates the underlying resource."""
    if self.INTERFACE == MODEL_GARDEN_CLI:
      self._CreateModelGarden()
      return
    start_model_upload = time.time()
    if self.INTERFACE == SDK:
      env_vars = self.model_spec.GetEnvironmentVariables(
          model_bucket_path=self.model_bucket_path
      )
      logging.info('Uploading ai model %s', self.model_name)
      self.gcloud_model = aiplatform.Model.upload(
          display_name=self.name,
          serving_container_image_uri=self.model_spec.container_image_uri,
          serving_container_command=self.model_spec.serving_container_command,
          serving_container_args=self.model_spec.serving_container_args,
          serving_container_ports=self.model_spec.serving_container_ports,
          serving_container_predict_route=self.model_spec.serving_container_predict_route,
          serving_container_health_route=self.model_spec.serving_container_health_route,
          serving_container_environment_variables=env_vars,
          artifact_uri=self.model_bucket_path,
          labels=util.GetDefaultTags(),
      )
      self.model_resource_name = self.gcloud_model.resource_name
    else:
      self._UploadViaGcloudCommand()
    end_model_upload = time.time()
    self.model_upload_time = end_model_upload - start_model_upload
    logging.info(
        'Model resource uploaded with name: %s in %s seconds',
        self.model_resource_name,
        self.model_upload_time,
    )
    start_model_deploy = time.time()
    if self.INTERFACE == SDK:
      assert self.gcloud_model
      try:
        self.gcloud_model.deploy(
            endpoint=self.endpoint.ai_endpoint,
            machine_type=self.model_spec.machine_type,
            accelerator_type=self.model_spec.accelerator_type,
            accelerator_count=self.model_spec.accelerator_count,
            deploy_request_timeout=1800,
            max_replica_count=self.max_scaling,
        )
      except google_exceptions.ServiceUnavailable as ex:
        logging.info('Tried to deploy model but got unavailable error %s', ex)
        raise errors.Benchmarks.QuotaFailure(ex)
    else:
      accelerator_type = self.model_spec.accelerator_type.lower()
      accelerator_type = accelerator_type.replace('_', '-')
      _, err, code = self.vm.RunCommand(
          f'gcloud ai endpoints deploy-model {self.endpoint.endpoint_name}'
          f' --model={self.model_resource_name} --region={self.region}'
          f' --project={self.project} --display-name={self.name}'
          f' --machine-type={self.model_spec.machine_type}'
          f' --accelerator=type={accelerator_type},count={self.model_spec.accelerator_count}'
          f' --service-account={self.service_account}'
          f' --max-replica-count={self.max_scaling}',
          ignore_failure=True,
      )
      if code:
        if (
            'The operations may still be underway remotely and may still'
            ' succeed'
            in err
        ):

          @vm_util.Retry(
              poll_interval=self.POLL_INTERVAL,
              fuzz=0,
              timeout=self.READY_TIMEOUT,
              retryable_exceptions=(errors.Resource.RetryableCreationError,),
          )
          def WaitUntilReady():
            if not self._IsReady():
              raise errors.Resource.RetryableCreationError('Not yet ready')

          WaitUntilReady()
        elif 'Machine type temporarily unavailable' in err:
          raise errors.Benchmarks.QuotaFailure(err)
        else:
          raise errors.VmUtil.IssueCommandError(err)
    end_model_deploy = time.time()
    self.model_deploy_time = end_model_deploy - start_model_deploy
    logging.info(
        'Successfully deployed model in %s seconds', self.model_deploy_time
    )

  def _CreateModelGarden(self) -> None:
    """Deploys the model via model garden CLI."""
    deploy_start_time = time.time()
    deploy_cmd = (
        'gcloud beta ai model-garden models deploy'
        f' --model={self.model_spec.GetModelGardenName()}'
        f' --endpoint-display-name={self.name}'
        f' --project={self.project} --region={self.region}'
        f' --machine-type={self.model_spec.machine_type}'
    )
    _, err_out, _ = self.vm.RunCommand(deploy_cmd, timeout=60 * 60)
    deploy_end_time = time.time()
    self.model_deploy_time = deploy_end_time - deploy_start_time
    operation_id = _FindRegexInOutput(
        err_out,
        r'gcloud ai operations describe (.*) --region',
        errors.Resource.CreationError,
    )
    out, _, _ = self.vm.RunCommand(
        'gcloud ai operations describe'
        f' {operation_id} --project={self.project} --region={self.region}'
    )
    # Only get the model id, not the full resource name.
    self.model_resource_name = _FindRegexInOutput(
        out,
        r'model:' rf' projects/(.*)/locations/{self.region}/models/(.*)@',
        exception_type=errors.Resource.CreationError,
        group_index=2,
    )
    self.endpoint.endpoint_name = _FindRegexInOutput(
        out,
        r'endpoint: (.*)\n',
        exception_type=errors.Resource.CreationError,
    )
    logging.info(
        'Model resource with name %s deployed & found with model id %s &'
        ' endpoint id %s',
        self.name,
        self.model_resource_name,
        self.endpoint.endpoint_name,
    )

  def _PostCreate(self):
    super()._PostCreate()
    if self.INTERFACE == MODEL_GARDEN_CLI:
      self.endpoint.UpdateLabels()

  def _UploadViaGcloudCommand(self) -> None:
    """Uploads the model via gcloud command."""
    upload_cmd = (
        f'gcloud ai models upload --display-name={self.name}'
        f' --project={self.project} --region={self.region}'
        f' --artifact-uri={self.model_bucket_path}'
    )
    if util.GetDefaultTags():
      upload_cmd += f' --labels={util.MakeFormattedDefaultTags()}'
    upload_cmd += self.model_spec.GetModelUploadCliArgs(
        model_bucket_path=self.model_bucket_path
    )
    self.vm.RunCommand(upload_cmd)
    out, _, _ = self.vm.RunCommand(
        f'gcloud ai models list --project={self.project} --region={self.region}'
    )
    lines = out.splitlines()
    for line in lines:
      pieces = line.split()
      if len(pieces) != 2:
        continue
      if pieces[1] == self.name:
        self.model_resource_name = pieces[0]
        logging.info(
            'Model resource with name %s uploaded & found with model id %s',
            self.name,
            self.model_resource_name,
        )
        return

    if not self.model_resource_name:
      raise errors.Resource.CreationError(
          'Could not find model resource with name %s' % self.name
      )

  def _CreateDependencies(self):
    """Creates the endpoint & copies the model to a bucket."""
    if self.INTERFACE == SDK:
      aiplatform.init(
          project=self.project,
          location=self.region,
          staging_bucket=self.staging_bucket,
          service_account=self.service_account,
      )
    super()._CreateDependencies()
    if self.INTERFACE == MODEL_GARDEN_CLI:
      return
    if self.gcs_client:
      gcs_bucket_copy_start_time = time.time()
      self.gcs_client.MakeBucket(
          self.bucket_uri
      )  # pytype: disable=attribute-error
      self.gcs_client.Copy(
          self.model_spec.model_garden_bucket,
          self.model_bucket_path,
          recursive=True,
          timeout=60 * 40,
      )  # pytype: disable=attribute-error
      self.gcs_bucket_copy_time = time.time() - gcs_bucket_copy_start_time
    self.endpoint.Create()

  def Delete(self, freeze: bool = False) -> None:
    """Deletes the underlying resource & its dependencies."""
    # Normally _DeleteDependencies is called by parent after _Delete, but we
    # need to call it before.
    self._DeleteDependencies()
    super().Delete(freeze)

  def _Delete(self) -> None:
    """Deletes the underlying resource."""
    logging.info('Deleting the resource: %s.', self.model_name)
    if self.INTERFACE == SDK:
      assert self.gcloud_model
      self.gcloud_model.delete()
      return
    self.vm.RunCommand(
        'gcloud ai models delete'
        f' {self.model_resource_name} --region={self.region} --project={self.project}'
    )

  def _DeleteDependencies(self):
    super()._DeleteDependencies()
    self.endpoint.Delete()
    if self.gcs_client:
      self.gcs_client.DeleteBucket(
          self.bucket_uri
      )  # pytype: disable=attribute-error

  def __getstate__(self):
    """Override pickling as the AI platform objects are not picklable."""
    to_pickle_dict = {
        'name': self.name,
        'model_name': self.model_name,
        'model_bucket_path': self.model_bucket_path,
        'region': self.region,
        'project': self.project,
        'service_account': self.service_account,
        'model_upload_time': self.model_upload_time,
        'model_deploy_time': self.model_deploy_time,
        'model_spec': self.model_spec,
    }
    return to_pickle_dict

  def __setstate__(self, pickled_dict):
    """Override pickling as the AI platform objects are not picklable."""
    self.name = pickled_dict['name']
    self.model_name = pickled_dict['model_name']
    self.model_bucket_path = pickled_dict['model_bucket_path']
    self.region = pickled_dict['region']
    self.project = pickled_dict['project']
    self.service_account = pickled_dict['service_account']
    self.model_upload_time = pickled_dict['model_upload_time']
    self.model_deploy_time = pickled_dict['model_deploy_time']
    self.model_spec = pickled_dict['model_spec']


class VertexAiEndpoint(resource.BaseResource):
  """Represents a Vertex AI endpoint.

  Attributes:
    name: The name of the endpoint.
    interface: The interface for making changes to the endpoint.
    project: The project.
    region: The region, derived from the zone.
    endpoint_name: The full resource name of the created endpoint, e.g.
      projects/123/locations/us-east1/endpoints/1234.
    ai_endpoint: The AIPlatform object representing the endpoint.
  """

  def __init__(
      self,
      name: str,
      interface: str,
      project: str,
      region: str,
      vm: virtual_machine.BaseVirtualMachine,
      **kwargs,
  ):
    super().__init__(**kwargs)
    self.name = name
    self.ai_endpoint = None
    self.interface = (interface,)
    self.project = project
    self.region = region
    self.vm = vm
    self.endpoint_name = None

  def _Create(self) -> None:
    """Creates the underlying resource."""
    logging.info('Creating the endpoint: %s.', self.name)
    if self.interface == SDK:
      self.ai_endpoint = aiplatform.Endpoint.create(
          display_name=f'{self.name}-endpoint'
      )
      return

    _, err, _ = self.vm.RunCommand(
        f'gcloud ai endpoints create --display-name={self.name}-endpoint'
        f' --project={self.project} --region={self.region}'
        f' --labels={util.MakeFormattedDefaultTags()}',
        ignore_failure=True,
    )
    self.endpoint_name = _FindRegexInOutput(
        err, r'Created Vertex AI endpoint: (.+)\.'
    )
    if not self.endpoint_name:
      raise errors.VmUtil.IssueCommandError(
          f'Could not find endpoint name in output {err}.'
      )
    logging.info('Successfully created endpoint %s', self.endpoint_name)
    self.ai_endpoint = aiplatform.Endpoint(self.endpoint_name)

  def _Delete(self) -> None:
    """Deletes the underlying resource."""
    logging.info('Deleting the endpoint: %s.', self.name)
    if self.interface == SDK:
      assert self.ai_endpoint
      self.ai_endpoint.delete(force=True)
      self.ai_endpoint = None  # Object is not picklable - none it out
      return
    out, _, _ = self.vm.RunCommand(
        f'gcloud ai endpoints describe {self.endpoint_name}',
    )
    model_id = _FindRegexInOutput(out, r'  id: \'(.+)\'')
    if model_id:
      self.vm.RunCommand(
          'gcloud ai endpoints undeploy-model'
          f' {self.endpoint_name} --deployed-model-id={model_id} --quiet',
      )
    else:
      if 'deployedModels:' not in out:
        logging.info(
            'No deployed models found; perhaps they failed to deploy or were'
            ' already deleted?'
        )
      else:
        raise errors.VmUtil.IssueCommandError(
            'Found deployed models but Could not find model id in'
            f' output.\n{out}'
        )
    self.vm.RunCommand(
        f'gcloud ai endpoints delete {self.endpoint_name} --quiet'
    )
    # None it out here as well, until all commands are supported over gcloud.
    self.ai_endpoint = None

  def UpdateLabels(self) -> None:
    """Updates the labels of the endpoint."""
    if self.interface == SDK:
      return
    self.vm.RunCommand(
        f'gcloud ai endpoints update {self.endpoint_name} '
        f' --project={self.project} --region={self.region}'
        f' --update-labels={util.MakeFormattedDefaultTags()}',
    )


def _FindRegexInOutput(
    output: str,
    regex: str,
    exception_type: type[errors.Error] | None = None,
    group_index: int = 1,
) -> str | None:
  """Returns the 1st match of the regex in the output.

  Args:
    output: The output to search.
    regex: The regex to search for.
    exception_type: The exception type to raise if no match is found.
    group_index: If there are multiple groups in the regex, which one to return.
  """
  matches = re.search(regex, output)
  if not matches:
    if exception_type:
      raise exception_type(
          f'Could not find match for regex {regex} in output {output}.'
      )
    return None
  return matches.group(group_index)


class VertexAiModelSpec(managed_ai_model_spec.BaseManagedAiModelSpec):
  """Spec for a Vertex AI model.

  Attributes:
    env_vars: Environment variables set on the node.
    serving_container_command: Command run on container to start the model.
    serving_container_args: The arguments passed to container create.
    serving_container_ports: The ports to expose for the model.
    serving_container_predict_route: The route to use for prediction requests.
    serving_container_health_route: The route to use for health checks.
    machine_type: The machine type for model's cluster.
    accelerator_type: The type of the GPU/TPU.
    model_bucket_suffix: Suffix with the particular version of the model (eg 7b)
    model_garden_bucket: The bucket in Model Garden to copy from.
  """

  CLOUD = 'GCP'

  def __init__(self, component_full_name, flag_values=None, **kwargs):
    super().__init__(component_full_name, flag_values=flag_values, **kwargs)
    # The pre-built serving docker images.
    self.container_image_uri: str
    self.model_bucket_suffix: str
    self.model_garden_bucket: str
    self.serving_container_command: list[str]
    self.serving_container_args: list[str]
    self.serving_container_ports: list[int]
    self.serving_container_predict_route: str
    self.serving_container_health_route: str
    self.machine_type: str
    self.accelerator_count: int
    self.accelerator_type: str

  def GetModelUploadCliArgs(self, **input_args) -> str:
    """Returns the kwargs needed to upload the model."""
    env_vars = self.GetEnvironmentVariables(**input_args)
    env_vars_str = ','.join(f'{key}={value}' for key, value in env_vars.items())
    ports_str = ','.join(str(port) for port in self.serving_container_ports)
    return (
        f' --container-image-uri={self.container_image_uri}'
        f' --container-command={",".join(self.serving_container_command)}'
        f' --container-args={",".join(self.serving_container_args)}'
        f' --container-ports={ports_str}'
        f' --container-predict-route={self.serving_container_predict_route}'
        f' --container-health-route={self.serving_container_health_route}'
        f' --container-env-vars={env_vars_str}'
    )

  def GetModelDeployKwargs(self) -> dict[str, Any]:
    """Returns the kwargs needed to deploy the model."""
    return {
        'machine_type': self.machine_type,
        'accelerator_type': self.accelerator_type,
        'accelerator_count': self.accelerator_count,
    }

  def GetEnvironmentVariables(self, **kwargs) -> dict[str, str]:
    """Returns container's environment variables needed by Llama2."""
    return {
        'MODEL_ID': kwargs['model_bucket_path'],
        'DEPLOY_SOURCE': 'pkb',
    }

  def ConvertToInstances(
      self, prompt: str, max_tokens: int, temperature: float, **kwargs: Any
  ) -> list[dict[str, Any]]:
    """Converts input to the form expected by the model."""
    instances = {
        'prompt': prompt,
        'max_tokens': max_tokens,
        'temperature': temperature,
    }
    for params in ['top_p', 'top_k', 'raw_response']:
      if params in kwargs:
        instances[params] = kwargs[params]
    return [instances]

  def GetModelGardenName(self) -> str:
    """Returns the name of the model in Model Garden."""
    return ''


class VertexAiLlama2Spec(VertexAiModelSpec):
  """Spec for running the Llama2 7b & 70b models."""

  MODEL_NAME: str = 'llama2'
  MODEL_SIZE: list[str] = ['7b', '70b']
  VLLM_ARGS = [
      '--host=0.0.0.0',
      '--port=7080',
      '--swap-space=16',
      '--gpu-memory-utilization=0.9',
      '--max-model-len=1024',
      '--max-num-batched-tokens=4096',
  ]

  def __init__(self, component_full_name, flag_values=None, **kwargs):
    super().__init__(component_full_name, flag_values=flag_values, **kwargs)
    # The pre-built serving docker images.
    self.container_image_uri = 'us-docker.pkg.dev/vertex-ai/vertex-vision-model-garden-dockers/pytorch-vllm-serve:20240222_0916_RC00'
    self.serving_container_command = [
        'python',
        '-m',
        'vllm.entrypoints.api_server',
    ]
    size_suffix = os.path.join('llama2', f'llama2-{self.model_size}-hf')
    self.model_garden_bucket = os.path.join(
        'gs://vertex-model-garden-public-us-central1', size_suffix
    )
    self.model_bucket_suffix = size_suffix
    self.serving_container_ports = [7080]
    self.serving_container_predict_route = '/generate'
    self.serving_container_health_route = '/ping'
    # Machine type from deployment notebook:
    # https://pantheon.corp.google.com/vertex-ai/colab/notebooks?e=13802955
    if self.model_size == '7b':
      self.machine_type = 'g2-standard-12'
      self.accelerator_count = 1
    else:
      self.machine_type = 'g2-standard-96'
      self.accelerator_count = 8
    self.accelerator_type = 'NVIDIA_L4'

    self.serving_container_args = self.VLLM_ARGS.copy()
    self.serving_container_args.append(
        f'--tensor-parallel-size={self.accelerator_count}'
    )

  def GetModelGardenName(self) -> str:
    """Returns the name of the model in Model Garden."""
    return f'meta/llama2@llama-2-{self.model_size}'


class VertexAiLlama3Spec(VertexAiModelSpec):
  """Spec for running the Llama3 70b model."""

  MODEL_NAME: str = 'llama3'
  MODEL_SIZE: str = '8b'
  VLLM_ARGS = [
      '--host=0.0.0.0',
      '--port=8080',
      '--swap-space=16',
      '--gpu-memory-utilization=0.9',
      '--max-model-len=1024',
      '--dtype=auto',
      '--max-loras=1',
      '--max-cpu-loras=8',
      '--max-num-seqs=256',
      '--disable-log-stats',
  ]

  def __init__(self, component_full_name, flag_values=None, **kwargs):
    super().__init__(component_full_name, flag_values=flag_values, **kwargs)
    # The pre-built serving docker images.
    self.container_image_uri = 'us-docker.pkg.dev/vertex-ai/vertex-vision-model-garden-dockers/pytorch-vllm-serve:20241001_0916_RC00'
    self.serving_container_command = [
        'python',
        '-m',
        'vllm.entrypoints.api_server',
    ]
    size_suffix = os.path.join('llama3', 'llama3-8b-hf')
    self.model_garden_bucket = os.path.join(
        'gs://vertex-model-garden-public-us', size_suffix
    )
    self.model_bucket_suffix = size_suffix
    self.serving_container_ports = [7080]
    self.serving_container_predict_route = '/generate'
    self.serving_container_health_route = '/ping'
    # Machine type from deployment notebook:
    # https://pantheon.corp.google.com/vertex-ai/publishers/meta/model-garden/llama3
    self.machine_type = 'g2-standard-12'
    self.accelerator_count = 1
    self.accelerator_type = 'NVIDIA_L4'
    self.serving_container_args = self.VLLM_ARGS.copy()
    self.serving_container_args.append(
        f'--tensor-parallel-size={self.accelerator_count}'
    )

  def GetModelUploadCliArgs(self, **input_args) -> str:
    """Returns the kwargs needed to upload the model."""
    upload_args = super().GetModelUploadCliArgs(**input_args)
    upload_args += (
        f' --container-shared-memory-size-mb={16 * 1024}'  # 16GB
        f' --container-deployment-timeout-seconds={60 * 40}'
    )
    return upload_args

  def GetModelGardenName(self) -> str:
    """Returns the name of the model in Model Garden."""
    return f'meta/llama3@meta-llama-3-{self.model_size}'
