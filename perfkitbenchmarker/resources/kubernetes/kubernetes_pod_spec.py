# Copyright 2017 PerfKitBenchmarker Authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Contains code related to Kubernetes pod spec decoding."""

from typing import Optional, Union

from perfkitbenchmarker import provider_info
from perfkitbenchmarker import virtual_machine
from perfkitbenchmarker.configs import option_decoders
from perfkitbenchmarker.resources.kubernetes import kubernetes_resources_spec


class KubernetesPodSpec(virtual_machine.BaseVmSpec):
  """Object containing the information needed to create a Kubernetes Pod.

  Attributes:
    host_network: Whether to directly use the pod's host for networking.
    resource_limits: The max resource limits (cpu & memory).
    resource_requests: The requested resources (cpu & memory).
  """

  CLOUD: Union[list[str], str] = [
      provider_info.AWS,
      provider_info.AZURE,
      provider_info.GCP,
  ]
  PLATFORM: str = provider_info.KUBERNETES

  def __init__(self, *args, **kwargs):
    self.host_network: bool = False
    self.resource_limits: Optional[
        kubernetes_resources_spec.KubernetesResourcesSpec
    ] = None
    self.resource_requests: Optional[
        kubernetes_resources_spec.KubernetesResourcesSpec
    ] = None
    super().__init__(*args, **kwargs)

  @classmethod
  def _GetOptionDecoderConstructions(cls):
    """Gets decoder classes and constructor args for each configurable option.

    Returns:
      dict. Maps option name string to a (ConfigOptionDecoder class, dict) pair.
          The pair specifies a decoder class and its __init__() keyword
          arguments to construct in order to decode the named option.
    """
    result = super()._GetOptionDecoderConstructions()
    result.update({
        'host_network': (
            option_decoders.BooleanDecoder,
            {'default': False},
        ),
        'resource_limits': (
            kubernetes_resources_spec.KubernetesResourcesDecoder,
            {'default': None},
        ),
        'resource_requests': (
            kubernetes_resources_spec.KubernetesResourcesDecoder,
            {'default': None},
        ),
    })
    return result
