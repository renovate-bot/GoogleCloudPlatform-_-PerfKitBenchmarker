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

"""Runs SPEC SFS 2014.

SPEC SFS 2014 homepage: http://www.spec.org/sfs2014/

In order to run this benchmark copy your 'SPECsfs2014_SP2.iso'
and 'netmist_license_key' files into the data/ directory.

TODO: This benchmark should be decoupled from Gluster and allow users
to run against any file server solution. In addition, Gluster should
eventually become a "disk type" so that any benchmark that runs
against a filesystem can run against Gluster.
"""


import posixpath
import xml.etree.ElementTree
from absl import flags
from perfkitbenchmarker import background_tasks
from perfkitbenchmarker import configs
from perfkitbenchmarker import data
from perfkitbenchmarker import flag_util
from perfkitbenchmarker import sample
from perfkitbenchmarker import vm_util
from perfkitbenchmarker.linux_packages import gluster

FLAGS = flags.FLAGS
BENCHMARKS = ['VDI', 'DATABASE', 'SWBUILD', 'VDA', 'EDA']

flags.DEFINE_string(
    'specsfs2014_config',
    None,
    'This flag can be used to specify an alternate SPEC config file to use. '
    'If this option is specified, none of the other benchmark specific flags '
    'which operate on the config file will be used (since the default config '
    'file will be replaced by this one).',
)
flags.DEFINE_list(
    'specsfs2014_benchmarks', BENCHMARKS, 'The SPEC SFS 2014 benchmarks to run.'
)
flags.register_validator(
    'specsfs2014_benchmarks',
    lambda benchmarks: benchmarks and set(benchmarks).issubset(BENCHMARKS),
    'Invalid benchmarks list. specsfs2014_benchmarks must be a subset of '
    + ', '.join(BENCHMARKS),
)
flag_util.DEFINE_integerlist(
    'specsfs2014_load',
    [1],
    'The starting load in units of SPEC "business metrics". The meaning of '
    'business metric varies depending on the SPEC benchmark (e.g. VDI has '
    'load measured in virtual desktops).',
    module_name=__name__,
)
flags.DEFINE_integer(
    'specsfs2014_incr_load',
    1,
    'The amount to increment "load" by for each run.',
    lower_bound=1,
)
flags.DEFINE_integer(
    'specsfs2014_num_runs',
    1,
    'The total number of SPEC runs. The load for the nth run is '
    '"load" + n * "specsfs_incr_load".',
    lower_bound=1,
)
flags.DEFINE_boolean(
    'specsfs2014_auto_mode',
    False,
    'If True, automatically find the max passing score for each benchmark. '
    'This ignores other flags such as specsfs2014_load, specsfs2014_incr_load, '
    'and specsfs2014_num_runs.',
)

BENCHMARK_NAME = 'specsfs2014'
BENCHMARK_CONFIG = """
specsfs2014:
  description: >
    Run SPEC SFS 2014. For a full explanation of all benchmark modes
    see http://www.spec.org/sfs2014/. In order to run this benchmark
    copy your 'SPECsfs2014_SP2.iso' and 'netmist_license_key' files
    into the data/ directory.
  vm_groups:
    gluster_servers:
      vm_spec: *default_dual_core
      disk_spec: *default_500_gb
      vm_count: 3
    clients:
      vm_spec: *default_dual_core
      vm_count: null
"""

_SPEC_SFS_2014_ISO = 'SPECsfs2014_SP2.iso'
_SPEC_SFS_2014_LICENSE = 'netmist_license_key'
_SPEC_DIR = 'spec'
_SPEC_CONFIG = 'sfs_rc'

_VOLUME_NAME = 'gv0'
_MOUNT_POINT = '/scratch'
_MOUNTPOINTS_FILE = 'mountpoints.txt'
_PUBLISHED_METRICS = frozenset([
    'achieved rate',
    'average latency',
    'overall throughput',
    'read throughput',
    'write throughput',
])
_METADATA_KEYS = frozenset([
    'op rate',
    'run time',
    'processes per client',
    'file size',
    'client data set size',
    'starting data set size',
    'initial file space',
    'maximum file space',
])

BENCHMARK_DATA = {
    _SPEC_SFS_2014_ISO: (
        '666d3f79e9184211736c32c825edb007c6a5ad88eeceb3c99aa01acf733c6fb3'
    )
}


def GetConfig(user_config):
  return configs.LoadConfig(BENCHMARK_CONFIG, user_config, BENCHMARK_NAME)


def CheckPrerequisites(unused_benchmark_config):
  """Verifies that the required resources are present.

  Raises:
    perfkitbenchmarker.data.ResourceNotFound: On missing resource.
  """
  data.ResourcePath(_SPEC_SFS_2014_LICENSE)
  if FLAGS.specsfs2014_config:
    data.ResourcePath(FLAGS.specsfs2014_config)


def _PrepareSpec(vm):
  """Prepares a SPEC client by copying SPEC to the VM."""
  mount_dir = 'spec_mnt'
  vm.RemoteCommand('mkdir %s' % mount_dir)
  vm.RemoteCommand('mkdir %s' % _SPEC_DIR)
  vm.InstallPreprovisionedBenchmarkData(
      'specsfs2014', [_SPEC_SFS_2014_ISO], '~/'
  )
  vm.PushFile(data.ResourcePath(_SPEC_SFS_2014_LICENSE), _SPEC_DIR)
  vm.RemoteCommand(
      'sudo mount -t iso9660 -o loop %s %s' % (_SPEC_SFS_2014_ISO, mount_dir)
  )
  vm.RemoteCommand('cp -r %s/* %s' % (mount_dir, _SPEC_DIR))
  vm.RemoteCommand('sudo umount {0} && sudo rm -rf {0}'.format(mount_dir))


def _ConfigureSpec(
    prime_client, clients, benchmark, load=None, num_runs=None, incr_load=None
):
  """Configures SPEC SFS 2014 on the prime client.

  This function modifies the default configuration file (sfs_rc) which
  can be found either in the SPEC SFS 2014 user guide or within the iso.
  It also creates a file containing the client mountpoints so that SPEC
  can run in a distributed manner.

  Args:
    prime_client: The VM from which SPEC will be controlled.
    clients: A list of SPEC client VMs (including the prime_client).
    benchmark: The sub-benchmark to run.
    load: List of ints. The LOAD parameter to SPECSFS.
    num_runs: The NUM_RUNS parameter to SPECSFS.
    incr_load: The INCR_LOAD parameter to SPECSFS.
  """
  config_path = posixpath.join(_SPEC_DIR, _SPEC_CONFIG)
  prime_client.RemoteCommand('sudo cp {0}.bak {0}'.format(config_path))

  stdout, _ = prime_client.RemoteCommand('pwd')
  exec_path = posixpath.join(
      stdout.strip(), _SPEC_DIR, 'binaries', 'linux', 'x86_64', 'netmist'
  )
  load = load or FLAGS.specsfs2014_load
  num_runs = num_runs or FLAGS.specsfs2014_num_runs
  incr_load = incr_load or FLAGS.specsfs2014_incr_load
  configuration_overrides = {
      'USER': prime_client.user_name,
      'EXEC_PATH': exec_path.replace('/', r'\/'),
      'CLIENT_MOUNTPOINTS': _MOUNTPOINTS_FILE,
      'BENCHMARK': benchmark,
      'LOAD': ' '.join([str(x) for x in load]),
      'NUM_RUNS': num_runs,
      'INCR_LOAD': incr_load,
      'WARMUP_TIME': 60,
  }
  # Any special characters in the overrides dictionary should be escaped so
  # that they don't interfere with sed.
  sed_expressions = ' '.join([
      '-e "s/{0}=.*/{0}={1}/"'.format(k, v)
      for k, v in configuration_overrides.items()
  ])
  sed_cmd = 'sudo sed -i {} {}'.format(sed_expressions, config_path)
  prime_client.RemoteCommand(sed_cmd)

  mount_points = [f'{client.internal_ip} {_MOUNT_POINT}' for client in clients]
  vm_util.CreateRemoteFile(
      prime_client,
      '\n'.join(mount_points),
      posixpath.join(_SPEC_DIR, _MOUNTPOINTS_FILE),
  )


def Prepare(benchmark_spec):
  """Install SPEC SFS 2014.

  Args:
    benchmark_spec: The benchmark specification. Contains all data that is
      required to run the benchmark.
  """
  gluster_servers = benchmark_spec.vm_groups['gluster_servers']
  clients = benchmark_spec.vm_groups['clients']
  prime_client = clients[0]

  # Set up Gluster
  if gluster_servers:
    gluster.ConfigureServers(gluster_servers, _VOLUME_NAME)

    args = [
        ((client, gluster_servers[0], _VOLUME_NAME, _MOUNT_POINT), {})
        for client in clients
    ]
    background_tasks.RunThreaded(gluster.MountGluster, args)

  # Set up SPEC
  background_tasks.RunThreaded(_PrepareSpec, clients)

  # Create a backup of the config file.
  prime_client.RemoteCommand(
      'cp {0} {0}.bak'.format(posixpath.join(_SPEC_DIR, _SPEC_CONFIG))
  )

  prime_client.AuthenticateVm()
  # Make sure any Static VMs are setup correctly.
  for client in clients:
    prime_client.TestAuthentication(client)


def _ParseSpecSfsOutput(output, extra_metadata=None):
  """Returns samples generated from the output of SPEC SFS 2014.

  Args:
    output: The stdout from running SPEC.
    extra_metadata: Dict of metadata to include with results.

  Returns:
    List of sample.Sample objects.

  This parses the contents of the results xml file and creates samples
  from the achieved operation rate, latency, and throughput metrics.
  The samples are annotated with metadata collected from the xml file
  including information about the benchmark name, the load, and data size.
  """
  root = xml.etree.ElementTree.fromstring(output)
  samples = []

  for run in root.findall('run'):
    metadata = {
        'benchmark': run.find('benchmark').attrib['name'],
        'business_metric': run.find('business_metric').text,
    }
    if extra_metadata:
      metadata.update(extra_metadata)

    for key in _METADATA_KEYS:
      element = run.find('metric[@name="%s"]' % key)
      units = element.attrib.get('units')
      label = '%s (%s)' % (key, units) if units else key
      metadata[label] = element.text

    if run.find('valid_run').text == 'INVALID_RUN':
      metadata['valid_run'] = False
    else:
      metadata['valid_run'] = True

    for metric in run.findall('metric'):
      name = metric.attrib['name']
      if name in _PUBLISHED_METRICS:
        samples.append(
            sample.Sample(
                name,
                float(metric.text),
                metric.attrib.get('units', ''),
                metadata,
            )
        )
  return samples


def _RunSpecSfs(benchmark_spec):
  """Run SPEC SFS 2014 once.

  Args:
    benchmark_spec: The benchmark specification. Contains all data that is
      required to run the benchmark.

  Returns:
    A list of sample.Sample objects.
  """
  prime_client = benchmark_spec.vm_groups['clients'][0]
  run_cmd = 'cd {} && python3 sfsmanager -r sfs_rc {}'.format(
      _SPEC_DIR, '-a' if FLAGS.specsfs2014_auto_mode else ''
  )
  prime_client.RobustRemoteCommand(run_cmd, ignore_failure=True)
  results_file = posixpath.join(_SPEC_DIR, 'results', 'sfssum_sfs2014_SP2.xml')
  output, _ = prime_client.RemoteCommand('cat %s' % results_file)

  if benchmark_spec.vm_groups['gluster_servers']:
    gluster_metadata = {
        'gluster_stripes': FLAGS.gluster_stripes,
        'gluster_replicas': FLAGS.gluster_replicas,
    }
  else:
    gluster_metadata = {}

  return _ParseSpecSfsOutput(output, extra_metadata=gluster_metadata)


def Run(benchmark_spec):
  """Run SPEC SFS 2014 for each configuration.

  Args:
    benchmark_spec: The benchmark specification. Contains all data that is
      required to run the benchmark.

  Returns:
    A list of sample.Sample objects.
  """
  clients = benchmark_spec.vm_groups['clients']
  prime_client = clients[0]
  results = []

  if FLAGS.specsfs2014_config:
    prime_client.PushFile(
        data.ResourcePath(FLAGS.specsfs2014_config),
        posixpath.join(_SPEC_DIR, _SPEC_CONFIG),
    )
    results += _RunSpecSfs(benchmark_spec)
  else:
    for benchmark in FLAGS.specsfs2014_benchmarks:
      _ConfigureSpec(prime_client, clients, benchmark)
      results += _RunSpecSfs(benchmark_spec)

  return results


def Cleanup(benchmark_spec):
  """Cleanup SPEC SFS 2014.

  Args:
    benchmark_spec: The benchmark specification. Contains all data that is
      required to run the benchmark.
  """
  clients = benchmark_spec.vm_groups['clients']
  gluster_servers = benchmark_spec.vm_groups['gluster_servers']

  for client in clients:
    client.RemoteCommand('sudo umount %s' % _MOUNT_POINT)
    client.RemoteCommand(
        'rm %s && sudo rm -rf %s' % (_SPEC_SFS_2014_ISO, _SPEC_DIR)
    )

  if gluster_servers:
    gluster.DeleteVolume(gluster_servers[0], _VOLUME_NAME)
