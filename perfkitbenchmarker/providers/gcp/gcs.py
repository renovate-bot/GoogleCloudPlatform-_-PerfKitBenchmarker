# Copyright 2016 PerfKitBenchmarker Authors. All rights reserved.
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

"""Contains classes/functions related to Google Cloud Storage."""

import logging
import ntpath
import os
import posixpath
import re
from typing import List as TList

from absl import flags
from perfkitbenchmarker import errors
from perfkitbenchmarker import linux_packages
from perfkitbenchmarker import object_storage_service
from perfkitbenchmarker import os_types
from perfkitbenchmarker import provider_info
from perfkitbenchmarker import temp_dir
from perfkitbenchmarker import vm_util
from perfkitbenchmarker.providers.gcp import util

_DEFAULT_GCP_SERVICE_KEY_FILE = 'gcp_credentials.json'
DEFAULT_GCP_REGION = 'us-central1'
GCLOUD_CONFIG_PATH = '.config/gcloud'
GCS_CLIENT_PYTHON = 'python'
GCS_CLIENT_BOTO = 'boto'
READER = 'objectViewer'
WRITER = 'objectCreator'

flags.DEFINE_string(
    'google_cloud_sdk_version',
    None,
    'Use a particular version of the Google Cloud SDK, e.g.: 103.0.0',
)
GCS_CLIENT = flags.DEFINE_enum(
    'gcs_client',
    GCS_CLIENT_PYTHON,
    [GCS_CLIENT_PYTHON, GCS_CLIENT_BOTO],
    'The GCS client library to use (default python).',
)

FLAGS = flags.FLAGS


class GoogleCloudStorageService(object_storage_service.ObjectStorageService):
  """Interface to Google Cloud Storage."""

  STORAGE_NAME = provider_info.GCP

  location: str

  def __init__(self):
    super().__init__()
    self.location = None
    self.hierarchical_name_space = False
    self.uniform_bucket_level_access = False

  def PrepareService(
      self,
      location,
      hierarchical_name_space=False,
      uniform_bucket_level_access=False,
  ):
    self.location = location or DEFAULT_GCP_REGION
    self.hierarchical_name_space = hierarchical_name_space
    self.uniform_bucket_level_access = uniform_bucket_level_access

  def MakeBucket(
      self, bucket_name, placement=None, raise_on_failure=True, tag_bucket=True
  ):
    """Creates a GCS bucket.

    Args:
      bucket_name: The name of the bucket to create, without gs:// prefix.
      placement: The placement of the bucket.
      raise_on_failure: If False, exceptions are swallowed.
      tag_bucket: If True, tag the bucket with default tags.
    """
    command = ['gcloud', 'storage', 'buckets', 'create']
    if self.location:
      command.extend(['--location', self.location])
    if object_storage_service.STORAGE_CLASS.value:
      command.extend([
          '--default-storage-class',
          object_storage_service.STORAGE_CLASS.value,
      ])
    elif self.location and '-' in self.location:
      # regional buckets
      command.extend(['--default-storage-class', 'regional'])
    if placement:
      command.extend(['--placement', placement])
    if self.hierarchical_name_space:
      command.extend(['--enable-hierarchical-namespace'])
    if self.uniform_bucket_level_access:
      command.extend(['--uniform-bucket-level-access'])
    if FLAGS.project:
      command.extend(['--project', FLAGS.project])
    if object_storage_service.OBJECT_TTL_DAYS.value:
      command.extend(
          ['--retention', f'{object_storage_service.OBJECT_TTL_DAYS.value}d']
      )
    command.extend([f'gs://{bucket_name}'])

    _, stderr, ret_code = vm_util.IssueCommand(command, raise_on_failure=False)
    if ret_code and raise_on_failure:
      raise errors.Benchmarks.BucketCreationError(stderr)

    if tag_bucket:
      command = ['gsutil', 'label', 'ch']
      for key, value in util.GetDefaultTags().items():
        command.extend(['-l', f'{key}:{value}'])
      command.extend([f'gs://{bucket_name}'])
      _, stderr, ret_code = vm_util.IssueCommand(
          command, raise_on_failure=False
      )
      if ret_code and raise_on_failure:
        raise errors.Benchmarks.BucketCreationError(stderr)

  def Copy(self, src_url, dst_url, recursive=False, timeout=None):
    """See base class.

    Args:
      src_url: The source url to copy from. Both url parameters need a schema
        prefix, e.g. gs://, local, or other.
      dst_url: The destination url to copy to.
      recursive: If True, copy the directory recursively.
      timeout: The timeout for the copy command.
    """
    cmd = ['gsutil', 'cp']
    if recursive or timeout is not None:
      # -m runs in parallel, which is faster.
      cmd = ['gsutil', '-m', 'cp']
    if recursive:
      cmd += ['-r']
    cmd += [src_url, dst_url]
    vm_util.IssueCommand(cmd, timeout=timeout)

  def CopyToBucket(self, src_path, bucket_name, object_path):
    """See base class.

    Args:
      src_path: The source path to copy from. Needs a schema prefix, e.g. gs://,
        local, or other.
      bucket_name: GCS bucket, without gs://.
      object_path: Path within the bucket to the object.
    """
    dst_url = self.MakeRemoteCliDownloadUrl(bucket_name, object_path)
    vm_util.IssueCommand(['gsutil', 'cp', src_path, dst_url])

  def MakeRemoteCliDownloadUrl(self, bucket_name, object_path):
    """See base class.

    Args:
      bucket_name: GCS bucket, without gs://.
      object_path: Path within the bucket to the object.

    Returns:
      The full gs:// URL.
    """
    path = posixpath.join(bucket_name, object_path)
    return 'gs://' + path

  def GenerateCliDownloadFileCommand(self, src_url, local_path):
    """See base class."""
    return f'gsutil cp "{src_url}" "{local_path}"'

  def List(self, bucket_name):
    """See base class."""
    # Full URI is required by gsutil.
    if not bucket_name.startswith('gs://'):
      bucket_name = 'gs://' + bucket_name
    stdout, _, _ = vm_util.IssueCommand(['gsutil', 'ls', bucket_name])
    return stdout

  def ListTopLevelSubfolders(self, bucket_name):
    """Lists the top level folders (not files) in a bucket_name.

    Each folder is returned as its full uri, eg. "gs://pkbtpch1/customer/", so
    just the folder name is extracted. When there's more than one, splitting
    on the newline returns a final blank row, so blank values are skipped.

    Args:
      bucket_name: Name of the bucket to list the top level subfolders of.

    Returns:
      A list of top level subfolder names. Can be empty if there are no folders.
    """
    return [
        obj.split('/')[-2].strip()
        for obj in self.List(bucket_name).split('\n')
        if obj and obj.endswith('/')
    ]

  @vm_util.Retry()
  def DeleteBucket(self, bucket_name):
    # We want to retry rm and rb together because it's possible that
    # we issue rm followed by rb, but then rb fails because the
    # metadata store isn't consistent and the server that handles the
    # rb thinks there are still objects in the bucket. It's also
    # possible for rm to fail because the metadata store is
    # inconsistent and rm doesn't find all objects, so can't delete
    # them all.
    self.EmptyBucket(bucket_name)

    def _bucket_not_found(stdout, stderr, retcode):
      del stdout  # unused

      return retcode and 'BucketNotFoundException' in stderr

    vm_util.IssueCommand(
        ['gsutil', 'rb', f'gs://{bucket_name}'],
        suppress_failure=_bucket_not_found,
    )

  def EmptyBucket(self, bucket_name):
    # Ignore failures here and retry in DeleteBucket.  See more comments there.
    vm_util.IssueCommand(
        ['gsutil', '-m', 'rm', '-r', f'gs://{bucket_name}/*'],
        raise_on_failure=False,
    )

  def AclBucket(self, entity: str, roles: TList[str], bucket_name: str):
    """Updates access control lists.

    Args:
      entity: the user or group to grant permission.
      roles: the IAM roles to be granted.
      bucket_name: the name of the bucket to change
    """
    vm_util.IssueCommand([
        'gsutil',
        'iam',
        'ch',
        f"{entity}:{','.join(roles)}",
        f'gs://{bucket_name}',
    ])

  def MakeBucketPubliclyReadable(self, bucket_name, also_make_writable=False):
    """See base class."""
    roles = [READER]
    logging.warning('Making bucket %s publicly readable!', bucket_name)
    if also_make_writable:
      roles.append(WRITER)
      logging.warning('Making bucket %s publicly writable!', bucket_name)
    self.AclBucket('allUsers', roles, bucket_name)

  # Use JSON API over XML for URLs
  def GetDownloadUrl(self, bucket_name, object_name, use_https=True):
    """See base class."""
    # https://cloud.google.com/storage/docs/downloading-objects
    scheme = 'https' if use_https else 'http'
    return (
        f'{scheme}://storage.googleapis.com/storage/v1/'
        f'b/{bucket_name}/o/{object_name}?alt=media'
    )

  def GetUploadUrl(self, bucket_name, object_name, use_https=True):
    """See base class."""
    # https://cloud.google.com/storage/docs/uploading-objects
    # Note I don't believe GCS supports upload via HTTP.
    scheme = 'https' if use_https else 'http'
    return (
        f'{scheme}://storage.googleapis.com/upload/storage/v1/'
        f'b/{bucket_name}/o?uploadType=media&name={object_name}'
    )

  UPLOAD_HTTP_METHOD = 'POST'

  @classmethod
  def AcquireWritePermissionsWindows(cls, vm):
    """Prepare boto file on a remote Windows instance.

    If the boto file specifies a service key file, copy that service key file to
    the VM and modify the .boto file on the VM to point to the copied file.

    Args:
      vm: gce virtual machine object.
    """
    if GCS_CLIENT.value == GCS_CLIENT_PYTHON:
      return

    boto_src = object_storage_service.FindBotoFile()
    boto_des = object_storage_service.DEFAULT_BOTO_LOCATION_USER
    stdout, _ = vm.RemoteCommand(f'Test-Path {boto_des}')
    if 'True' in stdout:
      return
    with open(boto_src) as f:
      boto_contents = f.read()
    match = re.search(r'gs_service_key_file\s*=\s*(.*)', boto_contents)
    if match:
      service_key_src = match.group(1)
      service_key_des = ntpath.join(
          vm.home_dir, posixpath.basename(service_key_src)
      )
      boto_src = cls._PrepareGcsServiceKey(
          vm, boto_src, service_key_src, service_key_des
      )
    vm.PushFile(boto_src, boto_des)

  @classmethod
  def AcquireWritePermissionsLinux(cls, vm):
    """Prepare boto file on a remote Linux instance.

    If the boto file specifies a service key file, copy that service key file to
    the VM and modify the .boto file on the VM to point to the copied file.

    Args:
      vm: gce virtual machine object.
    """
    if GCS_CLIENT.value == GCS_CLIENT_PYTHON:
      return

    vm_pwd, _ = vm.RemoteCommand('pwd')
    home_dir = vm_pwd.strip()
    boto_src = object_storage_service.FindBotoFile()
    boto_des = object_storage_service.DEFAULT_BOTO_LOCATION_USER
    if vm.TryRemoteCommand(f'test -f {boto_des}'):
      return
    with open(boto_src) as f:
      boto_contents = f.read()
    match = re.search(r'gs_service_key_file\s*=\s*(.*)', boto_contents)
    if match:
      service_key_src = match.group(1)
      service_key_des = posixpath.join(
          home_dir, posixpath.basename(service_key_src)
      )
      boto_src = cls._PrepareGcsServiceKey(
          vm, boto_src, service_key_src, service_key_des
      )
    vm.PushFile(boto_src, boto_des)

  @classmethod
  def _PrepareGcsServiceKey(
      cls, vm, boto_src, service_key_src, service_key_des
  ):
    """Copy GS service key file to remote VM and update key path in boto file.

    Args:
      vm: gce virtual machine object.
      boto_src: string, the boto file path in local machine.
      service_key_src: string, the gs service key file in local machine.
      service_key_des: string, the gs service key file in remote VM.

    Returns:
      The updated boto file path.
    """
    vm.PushFile(service_key_src, service_key_des)
    key = 'gs_service_key_file'
    with open(boto_src) as src_file:
      boto_path = os.path.join(
          temp_dir.GetRunDirPath(), posixpath.basename(boto_src)
      )
      with open(boto_path, 'w') as des_file:
        for line in src_file:
          if line.startswith(f'{key} = '):
            des_file.write(f'{key} = {service_key_des}\n')
          else:
            des_file.write(line)
    return boto_path

  def PrepareVM(self, vm):
    vm.Install('wget')
    # Unfortunately there isn't one URL scheme that works for both
    # versioned archives and "always get the latest version".
    if FLAGS.google_cloud_sdk_version is not None:
      sdk_file = (
          'google-cloud-sdk-%s-linux-x86_64.tar.gz'
          % FLAGS.google_cloud_sdk_version
      )
      sdk_url = 'https://storage.googleapis.com/cloud-sdk-release/' + sdk_file
    else:
      sdk_file = 'google-cloud-sdk.tar.gz'
      sdk_url = 'https://dl.google.com/dl/cloudsdk/release/' + sdk_file
    vm.RemoteCommand('wget ' + sdk_url)
    vm.RemoteCommand('tar xvf ' + sdk_file)
    # Versioned and unversioned archives both unzip to a folder called
    # 'google-cloud-sdk'.
    vm.RemoteCommand(
        'bash ./google-cloud-sdk/install.sh '
        '--disable-installation-options '
        '--usage-report=false '
        '--rc-path=.bash_profile '
        '--path-update=true '
        '--bash-completion=true'
    )
    vm.Install('google_cloud_storage')

    vm.RemoteCommand('mkdir -p .config')

    if GCS_CLIENT.value == GCS_CLIENT_BOTO:
      if vm.BASE_OS_TYPE == os_types.WINDOWS:
        self.AcquireWritePermissionsWindows(vm)
      else:
        self.AcquireWritePermissionsLinux(vm)
      vm.Install('gcs_boto_plugin')

    vm.gsutil_path, _ = vm.RemoteCommand('which gsutil', login_shell=True)
    vm.gsutil_path = vm.gsutil_path.split()[0]

    # Detect if we need to install crcmod for gcp.
    # See "gsutil help crc" for details.
    raw_result, _ = vm.RemoteCommand(f'{vm.gsutil_path} version -l')
    logging.info('gsutil version -l raw result is %s', raw_result)
    search_string = 'compiled crcmod: True'
    result_string = re.findall(search_string, raw_result)
    if not result_string:
      logging.info('compiled crcmod is not available, installing now...')
      try:
        # Try uninstall first just in case there is a pure python version of
        # crcmod on the system already, this is required by gsutil doc:
        # https://cloud.google.com/storage/docs/
        # gsutil/addlhelp/CRC32CandInstallingcrcmod
        vm.Uninstall('crcmod')
      except errors.VirtualMachine.RemoteCommandError:
        logging.info(
            'pip uninstall crcmod failed, could be normal if crcmod '
            'is not available at all.'
        )
      vm.Install('crcmod')
      vm.installed_crcmod = True
    else:
      logging.info('compiled crcmod is available, not installing again.')
      vm.installed_crcmod = False

  def CleanupVM(self, vm):
    vm.RemoveFile('google-cloud-sdk')
    vm.RemoveFile(GCLOUD_CONFIG_PATH)
    if GCS_CLIENT.value == GCS_CLIENT_BOTO:
      vm.RemoveFile(object_storage_service.DEFAULT_BOTO_LOCATION_USER)
      vm.Uninstall('gcs_boto_plugin')

  def CLIUploadDirectory(self, vm, directory, files, bucket_name):
    return vm.RemoteCommand(
        'time %s -m cp %s/* gs://%s/' % (vm.gsutil_path, directory, bucket_name)
    )

  def CLIDownloadBucket(self, vm, bucket_name, objects, dest):
    return vm.RemoteCommand(
        'time %s -m cp gs://%s/* %s' % (vm.gsutil_path, bucket_name, dest)
    )

  def Metadata(self, vm):
    metadata = {
        'pkb_installed_crcmod': vm.installed_crcmod,
        'gcs_client': str(GCS_CLIENT.value),
    }
    if GCS_CLIENT.value == GCS_CLIENT_BOTO:
      metadata.update({
          object_storage_service.BOTO_LIB_VERSION: (
              linux_packages.GetPipPackageVersion(vm, 'boto')
          )
      })
    return metadata

  def APIScriptArgs(self):
    return ['--gcs_client=' + str(GCS_CLIENT.value)]

  @classmethod
  def APIScriptFiles(cls):
    return ['gcs.py', 'gcs_boto.py']
