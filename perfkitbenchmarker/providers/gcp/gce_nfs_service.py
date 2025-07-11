"""Resource for GCE NFS service."""

import json
import logging

from absl import flags
from perfkitbenchmarker import disk
from perfkitbenchmarker import errors
from perfkitbenchmarker import nfs_service
from perfkitbenchmarker import provider_info
from perfkitbenchmarker import vm_util
from perfkitbenchmarker.providers.gcp import gce_network
from perfkitbenchmarker.providers.gcp import util

FLAGS = flags.FLAGS

STANDARD = 'STANDARD'
PREMIUM = 'PREMIUM'
ZONAL = 'ZONAL'
REGIONAL = 'REGIONAL'
HIGH_SCALE_SSD = 'high-scale-ssd'
ENTERPRISE = 'enterprise'


class GceNFSDiskSpec(disk.BaseNFSDiskSpec):
  CLOUD = provider_info.GCP


class GceNfsService(nfs_service.BaseNfsService):
  """Resource for GCE NFS service."""

  CLOUD = provider_info.GCP
  NFS_TIERS = (STANDARD, PREMIUM, ZONAL, REGIONAL, HIGH_SCALE_SSD, ENTERPRISE)
  DEFAULT_TIER = 'STANDARD'
  user_managed = False

  def __init__(self, disk_spec, zone):
    super().__init__(disk_spec, zone)
    self.name = 'nfs-%s' % FLAGS.run_uri
    self.server_directory = '/vol0'

  @property
  def network(self):
    spec = gce_network.GceNetworkSpec(project=FLAGS.project)
    network = gce_network.GceNetwork.GetNetworkFromNetworkSpec(spec)
    return network.network_resource.name

  def GetRemoteAddress(self):
    return self._Describe()['networks'][0]['ipAddresses'][0]

  def _Create(self):
    logging.info('Creating NFS server %s', self.name)
    volume_arg = 'name={},capacity={}'.format(
        self.server_directory.strip('/'), self.disk_spec.disk_size
    )
    network_arg = 'name={}'.format(self.network)
    args = [
        '--file-share',
        volume_arg,
        '--network',
        network_arg,
        '--labels',
        util.MakeFormattedDefaultTags(),
    ]
    if self.nfs_tier:
      args += ['--tier', self.nfs_tier]
    try:
      self._NfsCommand('create', *args)
    except errors.Error as ex:
      # if this NFS service already exists reuse it
      if self._Exists():
        logging.info('Reusing existing NFS server %s', self.name)
      else:
        raise errors.Resource.RetryableCreationError(
            'Error creating NFS service %s' % self.name, ex
        )

  def _Delete(self):
    logging.info('Deleting NFS server %s', self.name)
    try:
      self._NfsCommand('delete', '--async')
    except errors.Error as ex:
      if self._Exists():
        raise errors.Resource.RetryableDeletionError(ex)
      else:
        logging.info('NFS server %s already deleted', self.name)

  def _Exists(self):
    try:
      self._Describe()
      return True
    except errors.Error:
      return False

  def _IsReady(self):
    try:
      return self._Describe().get('state', None) == 'READY'
    except errors.Error:
      return False

  def _Describe(self):
    return self._NfsCommand('describe')

  def _GetLocation(self):
    if self.nfs_tier in [ENTERPRISE, REGIONAL]:
      return util.GetRegionFromZone(self.zone)
    return self.zone

  def _NfsCommand(self, verb, *args):
    cmd = [FLAGS.gcloud_path, '--quiet', '--format', 'json']
    if FLAGS.project:
      cmd += ['--project', FLAGS.project]
    cmd += ['filestore', 'instances', verb, self.name]
    cmd += [str(arg) for arg in args]
    cmd += ['--location', self._GetLocation()]
    stdout, stderr, retcode = vm_util.IssueCommand(
        cmd, raise_on_failure=False, timeout=1800
    )
    if retcode:
      raise errors.Error('Error running command %s : %s' % (verb, stderr))
    return json.loads(stdout)
