# Copyright 2019 PerfKitBenchmarker Authors. All rights reserved.
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
"""Module containing class for AWS' elasticache memcached clusters."""


import json
import logging

from absl import flags
from perfkitbenchmarker import errors
from perfkitbenchmarker import managed_memory_store
from perfkitbenchmarker import provider_info
from perfkitbenchmarker import vm_util
from perfkitbenchmarker.providers.aws import flags as aws_flags
from perfkitbenchmarker.providers.aws import util


MEMCACHED_VERSIONS = ['1.5.10', '1.5.16', '1.6.6']
_DEFAULT_ZONE = 'us-east-1a'
FLAGS = flags.FLAGS


class ElastiCacheMemcached(managed_memory_store.BaseManagedMemoryStore):
  """Object representing a AWS Elasticache memcached instance."""

  CLOUD = provider_info.AWS
  SERVICE_TYPE = 'elasticache'
  MEMORY_STORE = managed_memory_store.MEMCACHED

  def __init__(self, spec):
    super().__init__(spec)
    self.subnet_group_name = 'subnet-%s' % self.name
    self.zone = spec.zone or _DEFAULT_ZONE
    self.region = util.GetRegionFromZone(self.zone)
    self.node_type = aws_flags.ELASTICACHE_NODE_TYPE.value
    self.version = managed_memory_store.MANAGED_MEMORY_STORE_VERSION.value

  def CheckPrerequisites(self):
    if self.version and self.version not in MEMCACHED_VERSIONS:
      raise errors.Config.InvalidValue('Invalid Memcached version.')

  def GetResourceMetadata(self):
    """Returns a dict containing metadata about the cache cluster.

    Returns:
      dict mapping string property key to value.
    """
    self.metadata.update({
        'cloud_memcached_version': self.version,
        'cloud_memcached_node_type': self.node_type,
    })
    return self.metadata

  def _CreateDependencies(self):
    """Create the subnet dependencies."""
    subnet_id = self._GetClientVm().network.subnet.id
    cmd = [
        'aws',
        'elasticache',
        'create-cache-subnet-group',
        '--region',
        self.region,
        '--cache-subnet-group-name',
        self.subnet_group_name,
        '--cache-subnet-group-description',
        '"memcached benchmark subnet"',
        '--subnet-ids',
        subnet_id,
    ]

    vm_util.IssueCommand(cmd)

  def _DeleteDependencies(self):
    """Delete the subnet dependencies."""
    cmd = [
        'aws',
        'elasticache',
        'delete-cache-subnet-group',
        '--region',
        self.region,
        '--cache-subnet-group-name',
        self.subnet_group_name,
    ]
    vm_util.IssueCommand(cmd, raise_on_failure=False)

  def _Create(self):
    """Creates the cache cluster."""
    cmd = [
        'aws',
        'elasticache',
        'create-cache-cluster',
        '--engine',
        'memcached',
        '--region',
        self.region,
        '--cache-cluster-id',
        self.name,
        '--preferred-availability-zone',
        self.zone,
        '--num-cache-nodes',
        str(managed_memory_store.MEMCACHED_NODE_COUNT),
        '--cache-node-type',
        self.node_type,
        '--cache-subnet-group-name',
        self.subnet_group_name,
    ]

    if self.version:
      cmd += ['--engine-version', self.version]

    cmd += ['--tags']
    cmd += util.MakeFormattedDefaultTags()
    vm_util.IssueCommand(cmd)

  def _Delete(self):
    """Deletes the cache cluster."""
    cmd = [
        'aws',
        'elasticache',
        'delete-cache-cluster',
        '--region',
        self.region,
        '--cache-cluster-id',
        self.name,
    ]
    vm_util.IssueCommand(cmd, raise_on_failure=False)

  def _IsDeleting(self):
    """Returns True if cluster is being deleted and false otherwise."""
    cluster_info = self._DescribeInstance()
    return cluster_info.get('CacheClusterStatus', '') == 'deleting'

  def _IsReady(self):
    """Returns True if cluster is ready and false otherwise."""
    cluster_info = self._DescribeInstance()
    if cluster_info.get('CacheClusterStatus', '') == 'available':
      self.version = cluster_info.get('EngineVersion')
      return True
    return False

  def _Exists(self):
    """Returns true if the cluster exists and is not being deleted."""
    cluster_info = self._DescribeInstance()
    return cluster_info.get('CacheClusterStatus', '') not in [
        '',
        'deleting',
        'create-failed',
    ]

  def _DescribeInstance(self):
    """Calls describe on cluster.

    Returns:
      dict mapping string cluster_info property key to value.
    """
    cmd = [
        'aws',
        'elasticache',
        'describe-cache-clusters',
        '--region',
        self.region,
        '--cache-cluster-id',
        self.name,
    ]
    stdout, stderr, retcode = vm_util.IssueCommand(cmd, raise_on_failure=False)
    if retcode != 0:
      logging.info('Could not find cluster %s, %s', self.name, stderr)
      return {}
    for cluster_info in json.loads(stdout)['CacheClusters']:
      if cluster_info['CacheClusterId'] == self.name:
        return cluster_info
    return {}

  @vm_util.Retry(max_retries=5)
  def _PopulateEndpoint(self):
    """Populates address and port information from cluster_info.

    Raises:
      errors.Resource.RetryableGetError:
      Failed to retrieve information on cluster
    """
    cluster_info = self._DescribeInstance()
    if not cluster_info:
      raise errors.Resource.RetryableGetError(
          'Failed to retrieve information on {}.'.format(self.name)
      )

    endpoint = cluster_info['ConfigurationEndpoint']
    self._ip = endpoint['Address']
    self._port = endpoint['Port']
