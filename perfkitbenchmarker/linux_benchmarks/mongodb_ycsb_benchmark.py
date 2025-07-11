# Copyright 2015 PerfKitBenchmarker Authors. All rights reserved.
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

"""Run YCSB against MongoDB.

YCSB is a load generator for many 'cloud' databases. MongoDB is a NoSQL
database.

MongoDB homepage: http://www.mongodb.org/
YCSB homepage: https://github.com/brianfrankcooper/YCSB/wiki
"""

from collections.abc import Sequence
import functools
import json
import posixpath
import re
from typing import Any, Optional
from absl import flags
from perfkitbenchmarker import background_tasks
from perfkitbenchmarker import benchmark_spec as bm_spec
from perfkitbenchmarker import configs
from perfkitbenchmarker import errors
from perfkitbenchmarker import linux_virtual_machine
from perfkitbenchmarker import sample
from perfkitbenchmarker.linux_packages import mongosh
from perfkitbenchmarker.linux_packages import ycsb

flags.DEFINE_integer(
    'mongodb_readahead_kb', None, 'Configure block device readahead settings.'
)
flags.DEFINE_bool(
    'mongodb_primary_only',
    False,
    'Run with a simple primary-only setup. Mutually exclusive with'
    ' --mongodb_pss. If both are False, the default PSA setup will be used.',
)
flags.DEFINE_bool(
    'mongodb_pss',
    False,
    'Run with one primary and two secondaries, no arbiter. Mutually exclusive'
    ' with --mongodb_primary_only. If both are False, the default PSA setup'
    ' will be used.',
)
flags.DEFINE_integer(
    'mongodb_batchsize',
    1,
    'Client request batch size. Applies to inserts only (YCSB limitation).',
)

_MONGODB_NVME_QUEUE_DEPTH = flags.DEFINE_integer(
    'mongodb_nvme_queue_depth',
    None,
    'NVMe data disk queue depth. If unspecified, default value will be used.',
)

FLAGS = flags.FLAGS

_VERSION_REGEX = r'\d+\.\d+\.\d+'

BENCHMARK_NAME = 'mongodb_ycsb'
BENCHMARK_CONFIG = """
mongodb_ycsb:
  description: Run YCSB against MongoDB.
  vm_groups:
    primary:
      vm_spec: *default_dual_core
      disk_spec:
        GCP:
          disk_size: 500
          disk_type: pd-balanced
          mount_point: /scratch
        AWS:
          disk_size: 500
          disk_type: gp3
          mount_point: /scratch
        Azure:
          disk_size: 500
          disk_type: Premium_LRS
          mount_point: /scratch
      vm_count: 1
    secondary:
      vm_spec: *default_dual_core
      disk_spec:
        GCP:
          disk_size: 500
          disk_type: pd-balanced
          mount_point: /scratch
        AWS:
          disk_size: 500
          disk_type: gp3
          mount_point: /scratch
        Azure:
          disk_size: 500
          disk_type: Premium_LRS
          mount_point: /scratch
      vm_count: 1
    secondary_2:
      vm_spec: *default_dual_core
      disk_spec:
        GCP:
          disk_size: 500
          disk_type: pd-balanced
          mount_point: /scratch
        AWS:
          disk_size: 500
          disk_type: gp3
          mount_point: /scratch
        Azure:
          disk_size: 500
          disk_type: Premium_LRS
          mount_point: /scratch
      vm_count: 0
    arbiter:
      vm_spec: *default_dual_core
      vm_count: 1
    clients:
      os_type: ubuntu2204  # Python 2
      vm_spec: *default_dual_core
      vm_count: 1
  flags:
    openjdk_version: 8
    disk_fs_type: xfs
    fstab_options: noatime
    enable_transparent_hugepages: false
    create_and_boot_post_task_delay: 5
"""

_LinuxVM = linux_virtual_machine.BaseLinuxVirtualMachine


def GetConfig(user_config: dict[str, Any]) -> dict[str, Any]:
  """Validates the user config dictionary."""
  # Default config has 1 client, 1 primary, 1 secondary, and 1 arbiter VM.
  config = configs.LoadConfig(BENCHMARK_CONFIG, user_config, BENCHMARK_NAME)

  if FLAGS['ycsb_client_vms'].present:
    config['vm_groups']['clients']['vm_count'] = FLAGS.ycsb_client_vms

  primary_count = config['vm_groups']['primary']['vm_count']
  if FLAGS.mongodb_primary_only:
    if primary_count != 1:
      raise errors.Config.InvalidValue(
          'Must have exactly one primary VM when using --mongodb_primary_only.'
      )
    # Must have exactly zero secondary and arbiter VMs when using
    # --mongodb_primary_only.
    config['vm_groups']['secondary']['vm_count'] = 0
    config['vm_groups']['arbiter']['vm_count'] = 0
  elif FLAGS.mongodb_pss:
    config['vm_groups']['secondary_2']['vm_count'] = 1
    config['vm_groups']['arbiter']['vm_count'] = 0
    secondary_count = config['vm_groups']['secondary']['vm_count']
    secondary_2_count = config['vm_groups']['secondary_2']['vm_count']
    arbiter_count = config['vm_groups']['arbiter']['vm_count']
    if (
        primary_count != 1
        or secondary_count + secondary_2_count != 2
        or arbiter_count != 0
    ):
      raise errors.Config.InvalidValue(
          'Must have exactly one primary, two secondaries, and no arbiter VM.'
      )
  else:
    secondary_count = config['vm_groups']['secondary']['vm_count']
    arbiter_count = config['vm_groups']['arbiter']['vm_count']
    if any([
        count != 1 for count in [primary_count, secondary_count, arbiter_count]
    ]):
      raise errors.Config.InvalidValue(
          'Must have exactly one primary, secondary, and arbiter VM.'
      )
  return config


def _GetDataDir(vm: _LinuxVM) -> str:
  return posixpath.join(vm.GetScratchDir(), 'mongodb-data')


def _PrepareServer(vm: _LinuxVM) -> None:
  """Installs MongoDB on the server."""
  vm.Install('mongodb_server')
  vm.Install('mongosh')

  data_dir = _GetDataDir(vm)
  vm.RemoteCommand(f'sudo rm -rf {data_dir}')
  vm.RemoteCommand(f'mkdir {data_dir} && chmod a+rwx {data_dir}')
  vm.RemoteCommand(
      f'sudo sed -i "s|dbPath:.*|dbPath: {data_dir}|"'
      f' {vm.GetPathToConfig("mongodb_server")}'
  )

  if FLAGS.mongodb_readahead_kb is not None:
    vm.SetReadAhead(
        FLAGS.mongodb_readahead_kb * 2,
        [d.GetDevicePath() for d in vm.scratch_disks],
    )

  # Settings taken from MongoDB operations checklist
  vm.ApplySysctlPersistent({
      'fs.file-max': 98000,
      'kernel.pid_max': 64000,
      'kernel.threads-max': 64000,
      'vm.max_map_count': 102400,
  })

  # Too many connections fails if we don't set file descriptor limit higher.
  vm.RemoteCommand('ulimit -n 64000 && sudo systemctl start mongod')


def _PrepareArbiter(vm: _LinuxVM) -> None:
  """Installs MongoDB on the arbiter."""
  vm.Install('mongodb_server')
  vm.Install('mongosh')
  vm.RemoteCommand('ulimit -n 64000 && sudo systemctl start mongod')


def _PrepareReplicaSet(
    server_vms: Sequence[_LinuxVM], arbiter_vm: Optional[_LinuxVM]
) -> None:
  """Prepares the replica set for the benchmark.

  This benchmark currently uses a primary-secondary-arbiter replica set
  configuration. The secondary keeps a full replica of the data while the
  arbiter does not. The arbiter is still able to vote. See
  https://www.mongodb.com/docs/manual/core/replica-set-architecture-three-members
  for more information.

  Args:
    server_vms: The primary (index 0) and secondary (index 1) server VMs to use.
    arbiter_vm: The arbiter VM to use.
  """
  if FLAGS.mongodb_pss and len(server_vms) == 3 and arbiter_vm is None:
    args = {
        '_id': '"rs0"',
        'members': [
            {
                '_id': 0,
                'host': f'"{server_vms[0].internal_ip}:27017"',
                'priority': 1,
            },
            {
                '_id': 1,
                'host': f'"{server_vms[1].internal_ip}:27017"',
                'priority': 1,
            },
            {
                '_id': 2,
                'host': f'"{server_vms[2].internal_ip}:27017"',
                'priority': 1,
            },
        ],
    }
  elif (
      not FLAGS.mongodb_pss
      and not FLAGS.mongodb_primary_only
      and len(server_vms) == 2
      and arbiter_vm is not None
  ):
    args = {
        '_id': '"rs0"',
        'members': [
            {
                '_id': 0,
                'host': f'"{server_vms[0].internal_ip}:27017"',
                'priority': 1,
            },
            {
                '_id': 1,
                'host': f'"{server_vms[1].internal_ip}:27017"',
                'priority': 0.5,
            },
            {
                '_id': 2,
                'host': f'"{arbiter_vm.internal_ip}:27017"',
                'arbiterOnly': True,
            },
        ],
    }
  else:
    raise errors.Config.InvalidValue(
        'Invalid number of server VMs and arbiter VM for replica set.'
    )
  mongosh.RunCommand(server_vms[0], f'rs.initiate({json.dumps(args)})')
  mongosh.RunCommand(server_vms[0], 'rs.conf()')
  mongosh.RunCommand(server_vms[0], 'rs.status()')


def _PrepareClient(vm: _LinuxVM) -> None:
  """Install YCSB on the client VM."""
  vm.Install('ycsb')
  vm.Install('mongosh')
  # Disable logging for MongoDB driver, which is otherwise quite verbose.
  log_config = """<configuration><root level="WARN"/></configuration>"""

  vm.RemoteCommand(
      "echo '{}' > {}/logback.xml".format(log_config, ycsb.YCSB_DIR)
  )


def _GetMongoDbURL(benchmark_spec: bm_spec.BenchmarkSpec) -> str:
  """Returns the connection string used to connect to the instance."""

  # all the connection strings here require committing to disk (journal)
  #  prior to client acknowledgement. See
  #  https://www.mongodb.com/docs/manual/reference/write-concern/#acknowledgment-behavior

  primary = benchmark_spec.vm_groups['primary'][0]

  if FLAGS.mongodb_primary_only:
    return (
        f'"mongodb://{primary.internal_ip}:27017/ycsb'
        '?w=1&j=true&compression=snappy&maxPoolSize=100000"'
    )

  if FLAGS.mongodb_pss:
    secondary = benchmark_spec.vm_groups['secondary'][0]
    secondary_2 = benchmark_spec.vm_groups['secondary_2'][0]
    return (
        f'"mongodb://{primary.internal_ip}:27017,'
        f'{secondary.internal_ip}:27017,'
        f'{secondary_2.internal_ip}:27017/ycsb'
        '?replicaSet=rs0&w=majority&compression=snappy&maxPoolSize=100000"'
    )

  secondary = benchmark_spec.vm_groups['secondary'][0]
  arbiter = benchmark_spec.vm_groups['arbiter'][0]
  return (
      f'"mongodb://{primary.internal_ip}:27017,'
      f'{secondary.internal_ip}:27017,'
      f'{arbiter.internal_ip}:27017/ycsb'
      '?replicaSet=rs0&w=majority&compression=snappy&maxPoolSize=100000"'
  )


def _GetNvmeDataDiskName(vm: _LinuxVM) -> str:
  """Returns the name of the NVMe data disk."""
  # --- Step 1: Find the name of the partition mounted on "/" ---
  # The 'df /' command reports on the filesystem for the root directory.
  # We get its source device and remove output headers.
  root_partition_device_command = (
      'ROOT_PARTITION_DEVICE=$(df / --output=source | tail -n 1)'
  )

  # --- Step 2: Find the parent disk for that root partition ---
  # 'lsblk -no pkname' asks for the "parent kernel name" of a given device.
  # This gives us the name of the boot disk (e.g., "nvme0n1").
  boot_disk_command = 'BOOT_DISK=$(lsblk -no pkname "$ROOT_PARTITION_DEVICE")'

  # --- Step 3: Find the data disk ---
  # We list all devices of type 'disk' and use 'grep -v' to exclude the
  # boot disk we just identified. The name that remains is the data disk.
  data_disk_command = (
      'lsblk -d -o NAME,TYPE | awk \'$2=="disk" {print $1}\' | grep -v'
      ' "^$BOOT_DISK$"'
  )

  data_disk_name, _ = vm.RemoteCommand(
      ';'.join(
          [root_partition_device_command, boot_disk_command, data_disk_command]
      )
  )
  return data_disk_name.strip()


def Prepare(benchmark_spec: bm_spec.BenchmarkSpec) -> None:
  """Install MongoDB on one VM and YCSB on another.

  Args:
    benchmark_spec: The benchmark specification. Contains all data that is
      required to run the benchmark.
  """
  server_vms = []
  primary = benchmark_spec.vm_groups['primary'][0]
  server_vms.append(primary)
  secondary = None
  secondary_2 = None
  arbiter = None
  clients = benchmark_spec.vm_groups['clients']

  server_partials = [functools.partial(_PrepareServer, primary)]
  arbiter_partial = []
  client_partials = [
      functools.partial(_PrepareClient, client) for client in clients
  ]

  if not FLAGS.mongodb_primary_only:
    if FLAGS.mongodb_pss:
      secondary = benchmark_spec.vm_groups['secondary'][0]
      server_vms.append(secondary)
      secondary_2 = benchmark_spec.vm_groups['secondary_2'][0]
      server_vms.append(secondary_2)
      server_partials += [functools.partial(_PrepareServer, secondary)]
      server_partials += [functools.partial(_PrepareServer, secondary_2)]
    else:
      secondary = benchmark_spec.vm_groups['secondary'][0]
      server_vms.append(secondary)
      arbiter = benchmark_spec.vm_groups['arbiter'][0]
      server_partials += [functools.partial(_PrepareServer, secondary)]
      arbiter_partial += [functools.partial(_PrepareArbiter, arbiter)]

  background_tasks.RunThreaded(
      (lambda f: f()), server_partials + arbiter_partial + client_partials
  )

  if not FLAGS.mongodb_primary_only:
    _PrepareReplicaSet(server_vms, arbiter)

  benchmark_spec.executor = ycsb.YCSBExecutor('mongodb', cp=ycsb.YCSB_DIR)
  benchmark_spec.mongodb_url = _GetMongoDbURL(benchmark_spec)
  benchmark_spec.mongodb_version = re.findall(
      _VERSION_REGEX,
      mongosh.RunCommand(primary, 'db.version()')[0],
  )[-1]
  load_kwargs = {
      'mongodb.url': benchmark_spec.mongodb_url,
      'mongodb.batchsize': 10,
      'mongodb.upsert': True,
      'core_workload_insertion_retry_limit': 10,
  }
  benchmark_spec.executor.Load(clients, load_kwargs=load_kwargs)

  # Print some useful loading stats
  mongosh.RunCommand(primary, 'db.stats()')
  if not FLAGS.mongodb_primary_only:
    mongosh.RunCommand(primary, 'rs.conf()')
    mongosh.RunCommand(primary, 'rs.status()')
  primary.RemoteCommand('df -h')

  # Set NVMe queue depth on server VM(s) if value is provided.
  if _MONGODB_NVME_QUEUE_DEPTH.value:
    for server_vm in server_vms:
      nvme_data_disk_name = _GetNvmeDataDiskName(server_vm)
      server_vm.RemoteCommand(
          f'cat /sys/block/{nvme_data_disk_name}/queue/nr_requests'
      )
      server_vm.RemoteCommand(
          f'echo {_MONGODB_NVME_QUEUE_DEPTH.value} | sudo tee'
          f' /sys/block/{nvme_data_disk_name}/queue/nr_requests'
      )
      server_vm.RemoteCommand(
          f'cat /sys/block/{nvme_data_disk_name}/queue/nr_requests'
      )


def Run(benchmark_spec: bm_spec.BenchmarkSpec) -> list[sample.Sample]:
  """Run YCSB against MongoDB.

  Args:
    benchmark_spec: The benchmark specification. Contains all data that is
      required to run the benchmark.

  Returns:
    A list of sample.Sample objects.
  """

  run_kwargs = {
      'mongodb.url': benchmark_spec.mongodb_url,
      'mongodb.batchsize': FLAGS.mongodb_batchsize,
      'mongodb.upsert': True,
  }
  samples = list(
      benchmark_spec.executor.Run(
          benchmark_spec.vm_groups['clients'],
          run_kwargs=run_kwargs,
      )
  )

  if FLAGS.mongodb_readahead_kb is not None:
    for s in samples:
      s.metadata['readahead_kb'] = FLAGS.mongodb_readahead_kb
      if hasattr(benchmark_spec, 'mongodb_version'):
        s.metadata['mongodb_version'] = benchmark_spec.mongodb_version

  mongosh.RunTwoCommands(
      benchmark_spec.vm_groups['primary'][0],
      'use ycsb',
      'db.usertable.stats()',
  )

  # Copy client and server logs to the scratch directory.
  for _, vms in benchmark_spec.vm_groups.items():
    for vm in vms:
      vm.CopyLogs('/var/log')
      vm.CopyLogs('/etc/mongod.conf')
  return samples


def Cleanup(benchmark_spec: bm_spec.BenchmarkSpec) -> None:
  """Remove MongoDB and YCSB.

  Args:
    benchmark_spec: The benchmark specification. Contains all data that is
      required to run the benchmark.
  """

  def CleanupServer(server: _LinuxVM) -> None:
    server.RemoteCommand(
        'sudo service %s stop' % server.GetServiceName('mongodb_server')
    )
    server.RemoteCommand('rm -rf %s' % _GetDataDir(server))

  CleanupServer(benchmark_spec.vm_groups['workers'][0])
