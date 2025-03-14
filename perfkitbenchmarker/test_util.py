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
"""Functions and classes to make testing easier."""


import os

from absl import flags
import mock
from perfkitbenchmarker import benchmark_spec
from perfkitbenchmarker import sample
from perfkitbenchmarker.configs import benchmark_config_spec


_BENCHMARK_NAME = 'test_benchmark'
_BENCHMARK_UID = 'uid'


class SamplesTestMixin:
  """A mixin for unittest.TestCase that adds a type-specific equality

  predicate for samples.
  """

  def __init__(self, *args, **kwargs):
    super().__init__(*args, **kwargs)

    self.addTypeEqualityFunc(
        sample.Sample, self.assertSamplesEqualUpToTimestamp
    )

  def assertSamplesEqualUpToTimestamp(self, a, b, msg=None):
    """Assert that two samples are equal, ignoring timestamp differences."""
    self.assertEqual(
        a.metric,
        b.metric,
        msg or 'Samples %s and %s have different metrics' % (a, b),
    )
    if isinstance(a.value, float) and isinstance(b.value, float):
      self.assertAlmostEqual(
          a.value,
          b.value,
          places=6,
          msg=msg or 'Samples %s and %s have different values' % (a, b),
      )
    else:
      self.assertEqual(
          a.value,
          b.value,
          msg or 'Samples %s and %s have different values' % (a, b),
      )
    self.assertEqual(
        a.unit, b.unit, msg or 'Samples %s and %s have different units' % (a, b)
    )
    self.assertDictEqual(
        a.metadata,
        b.metadata,
        msg or 'Samples %s and %s have different metadata' % (a, b),
    )
    # Deliberately don't compare the timestamp fields of the samples.

  def assertSampleListsEqualUpToTimestamp(self, a, b, msg=None):
    """Compare two lists of samples.

    Sadly, the builtin assertListsEqual will only use Python's
    built-in equality predicate for testing the equality of elements
    in a list. Since we compare lists of samples a lot, we need a
    custom test for that.
    """

    self.assertEqual(
        len(a),
        len(b),
        msg or 'Lists %s and %s are not the same length' % (a, b),
    )
    for i in range(len(a)):
      self.assertIsInstance(
          a[i],
          sample.Sample,
          msg
          or ('%s (item %s in list) is not a sample.Sample object' % (a[i], i)),
      )
      self.assertIsInstance(
          b[i],
          sample.Sample,
          msg
          or ('%s (item %s in list) is not a sample.Sample object' % (b[i], i)),
      )
      try:
        self.assertSamplesEqualUpToTimestamp(a[i], b[i], msg=msg)
      except self.failureException as ex:
        ex.message = str(ex) + ' (was item %s in list)' % i
        ex.args = (ex.message,)
        raise ex

  def assertSampleInList(self, a, b, msg=None):  # pylint:disable=invalid-name
    """Assert that sample a is in list b (up to timestamp)."""
    found = False
    for s in b:
      try:
        self.assertSamplesEqualUpToTimestamp(a, s, msg=msg)
      except self.failureException:
        continue
      found = True
    if not found:
      msg = msg or f'{a} not found in {b}.'
      raise AssertionError(msg)


def assertDiskMounts(benchmark_config, mount_point):
  """Test whether a disk mounts in a given configuration.

  Sets up a virtual machine following benchmark_config and then tests
  whether the path mount_point contains a working disk by trying to
  create a file there. Returns nothing if file creation works;
  otherwise raises an exception.

  Args:
    benchmark_config: a dict in the format of benchmark_spec.BenchmarkSpec. The
      config must specify exactly one virtual machine.
    mount_point: a path, represented as a string.

  Raises:
    RemoteCommandError if it cannot create a file at mount_point and
    verify that the file exists.

    AssertionError if benchmark_config does not specify exactly one
    virtual machine.
  """

  assert len(benchmark_config['vm_groups']) == 1
  vm_group = next(iter(benchmark_config['vm_groups'].values()))
  assert vm_group.get('num_vms', 1) == 1
  m = mock.MagicMock()
  m.BENCHMARK_NAME = _BENCHMARK_NAME
  config_spec = benchmark_config_spec.BenchmarkConfigSpec(
      _BENCHMARK_NAME, flag_values=flags.FLAGS, **benchmark_config
  )
  spec = benchmark_spec.BenchmarkSpec(m, config_spec, _BENCHMARK_UID)
  with spec.RedirectGlobalFlags():
    try:
      spec.ConstructVirtualMachines()
      spec.Provision()

      vm = spec.vms[0]

      test_file_path = os.path.join(mount_point, 'test_file')
      vm.RemoteCommand('touch %s' % test_file_path)

      # This will raise RemoteCommandError if the test file does not
      # exist.
      vm.RemoteCommand('test -e %s' % test_file_path)

    finally:
      spec.Delete()
